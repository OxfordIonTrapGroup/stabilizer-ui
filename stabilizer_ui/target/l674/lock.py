"""Monitoring the lock, and automatically relocking the SolsTiS onto the cavity.

The relocking state machine (`relock_laser()`) and the monitor running it
(`LockMonitor`) work against a `LockControl` (implemented by the main window on top of the
Stabilizer interface), the WAnD wavemeter (`WavemeterInterface`) and the SolsTiS ICE-Bloc
(`solstis`), so that they can be run without the UI.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum, unique
from typing import Awaitable, Callable, Optional, Protocol

import numpy as np

from .solstis import EnsureSolstis
from .wavemeter import WavemeterInterface

logger = logging.getLogger(__name__)

#
# Parameters for auto-relocking algorithm.
#

#: When waiting for wavemeter readings to stabilise, wait until two consecutive readings
#: within the given number of Hz.
STABLE_READING_TOLERANCE = 5e6

#: Coarser version of STABLE_READING_TOLERANCE to use for etalon tune search, where
#: anything <100 MHz or so is completely irrelevant anyway.
ETALON_TUNE_READING_TOLERANCE = 20e6

#: Maximum laser offset from last known-good value for which to attempt reestablishing
#: the lock (rather than tuning the laser closer to the value).
MAX_RELOCK_ATTEMPT_DELTA = 3e6

#: Maximum laser offset from last known-good value for which to tune using the resonator
#: tuning control, rather than disabling the etalon lock and selecting a mode closer to
#: the target. (Must be larger than etalon step size, plus some "hysteresis".)
MAX_RESONATOR_TUNE_DELTA = 1e9

#: Response of the laser frequency to changes in the etalon tuning parameter (as per the
#: ICE-Bloc web UI), in Hz per "tune percent". Since the frequency is not continuous in
#: the parameter but rather displays steps (note aside: what are those actually – some
#: aliasing between etalon and resonator modes?), this is approximate.
ETALON_TUNE_SLOPE = -4.68e9  # Hz per "tune percent"
ETALON_TUNE_DAMPING = 1.0

#: Response of the laser frequency to changes in the resonator tuning (as per the
#: ICE-Bloc web UI), in Hz per "tune percent".
RESONATOR_TUNE_SLOPE = 269e6

#: An extra damping factor to apply to each resonator tuning step for stability (can be
#: decreased from 1.0 if the resonator tuning search oscillating becomes an issue).
RESONATOR_TUNE_DAMPING = 1.0

#: Approximate resonator scan range (in frequency, converted using RESONATOR_TUNE_SLOPE)
#: to use for determining point at which to attempt locking. Depends on the wavemeter
#: accuracy, plus some extra slack to deal with resonator drifts during the currently
#: trivial scan algorithm.
RESONANCE_SEARCH_RADIUS = 25e6

#: Number of scan points to use when determining resonator lock point. Values too small
#: would risk missing the peak, values too large would make laser drifts affect the scan
#: too much.
RESONANCE_SEARCH_NUM_POINTS = 100

#: Fallback frequency target when a wavemeter reading with the laser in lock has not
#: been observed yet.
DEFAULT_FREQ_TARGET = 150e6

#: Interval to request wavemeter frequency readings with while not relocking.
#: Should be small enough to track (thermal) drifts in the wavemeter calibration.
IDLE_WAVEMETER_POLL_INTERVAL = 5.0

#: Interval between ADC1 reading requests while the relock task is not running.
#: Effectively lower-bounds the average re-lock response time, so should be set fairly
#: low (but not too low, as each request generates two MQTT messages).
IDLE_ADC1_POLL_INTERVAL = 0.2

#: Interval between attempts to read the transmission while the device is not connected.
OFFLINE_POLL_INTERVAL = 1.0

#: Timeout for wavemeter requests. Should be at least a few seconds at least to
#: accommodate long exposure times on other channels, but short enough to recover from
#: lost connections while auto-relocking in a timely fashion.
WAVEMETER_TIMEOUT = 10.0

#: Number of attempts to lock at target wavemeter reading before engaging resonance
#: search.
LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH = 5

#: Fraction of the lock detect threshold the transmission has to reach right after
#: enabling the AOM lock (and while the PZT lock engages) for the attempt to go on.
EARLY_TRANSMISSION_FRACTION = 0.2

#: Time to watch the transmission after enabling the PZT lock (during the gain ramp), in
#: seconds.
PZT_LOCK_SETTLE_TIME = 1.0


@unique
class LockState(Enum):
    out_of_lock = "Out of lock"
    relocking = "Relocking"
    locked = "Locked"
    uninitialised = "Uninitialised"


class LockControl(Protocol):
    """What the relocking needs from the lock hardware (the Stabilizer, via the UI)."""

    def threshold(self) -> float:
        """The lock detect threshold, in volts at the ADC1 input."""

    async def read_transmission(self) -> float:
        """The filtered cavity transmission, in volts at the ADC1 input. Raises
        `ConnectionError` if the device is not available."""

    async def set_aom_lock(self, enabled: bool) -> None:
        """Release (or apply) the hold of the AOM lock, and wait for the device to have
        done it."""

    async def set_pzt_lock(self, enabled: bool) -> None:
        """Enable (or disable) the PZT lock, and wait for the device to have done it."""

    def show_state(self, state: LockState, transmission: Optional[float]) -> None:
        """Show the lock state (and the transmission it was derived from)."""


@unique
class RelockStep(Enum):
    reset_lock = "reset_lock"
    decide_next = "decide_next"
    tune_etalon = "tune_etalon"
    tune_resonator = "tune_resonator"
    determine_resonance = "determine_resonance"
    try_lock = "try_lock"


async def relock_laser(control: LockControl, get_freq: Callable[[], Awaitable[tuple]],
                       approximate_target_freq: float, solstis_host: str,
                       solstis_port: int):
    """Relock the laser: tune it to the target frequency (relative to the wavemeter
    reference) with the SolsTiS controls, and engage the AOM and PZT locks until the
    transmission is above the threshold. Returns once the laser is locked (or raises
    `asyncio.CancelledError`, or `ConnectionError` if the device goes away)."""
    logger.info(
        f"Relocking; target frequency: {(approximate_target_freq / 1e6):0.1f} MHz")

    async def get_freq_delta():
        while True:
            status, freq, _osa = await get_freq()
            if status != 0:
                # TODO: Probably downgrade to debug, as Low will be common following
                # lock loss.
                logger.info("Wavemeter returned non-zero channel status: %s", status)
                continue
            return freq - approximate_target_freq

    async def get_stable_freq_delta(tolerance=STABLE_READING_TOLERANCE):
        previous_delta = None
        while True:
            delta = await get_freq_delta()
            if previous_delta is not None:
                if abs(delta - previous_delta) < tolerance:
                    return delta
            previous_delta = delta

    async def read_adc():
        voltage = await control.read_transmission()
        control.show_state(LockState.relocking, voltage)
        return voltage

    async def finite_state_machine(ensure_solstis):
        # Finite state machine, exiting only through try_lock success, or cancellation.
        step = RelockStep.reset_lock

        lock_attempts_left = LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH
        while True:
            logger.info("Current step: %s", step.value)
            if step == RelockStep.reset_lock:
                await control.set_aom_lock(False)
                await control.set_pzt_lock(False)
                await asyncio.sleep(0.1)
                step = RelockStep.decide_next
                continue
            if step == RelockStep.decide_next:
                delta = await get_stable_freq_delta()
                logger.info(f"Laser frequency delta to target: {delta / 1e6:0.1f} MHz")
                if abs(delta) < MAX_RELOCK_ATTEMPT_DELTA:
                    if lock_attempts_left > 0:
                        lock_attempts_left -= 1
                        step = RelockStep.try_lock
                    else:
                        step = RelockStep.determine_resonance
                    continue
                if abs(delta) < MAX_RESONATOR_TUNE_DELTA:
                    step = RelockStep.tune_resonator
                    continue
                step = RelockStep.tune_etalon
                continue
            if step == RelockStep.tune_etalon:
                async with ensure_solstis as solstis:
                    if solstis.etalon_locked:
                        await solstis.set_etalon_locked(False)
                    while True:
                        delta = await get_stable_freq_delta(ETALON_TUNE_READING_TOLERANCE)
                        if abs(delta) < MAX_RESONATOR_TUNE_DELTA:
                            break
                        diff = -ETALON_TUNE_DAMPING * delta / ETALON_TUNE_SLOPE
                        new_tune = solstis.etalon_tune + diff
                        logger.info("Setting etalon tune to %s", f"{new_tune:0.3f}%")
                        await solstis.set_etalon_tune(new_tune)
                    await solstis.set_etalon_locked(True)
                    step = RelockStep.decide_next
                    # Currently don't have resonator tune read-back after lock is engaged,
                    # so reopen connection completely.
                    await solstis.close()
                continue
            if step == RelockStep.tune_resonator:
                async with ensure_solstis as solstis:
                    if not solstis.etalon_locked:
                        # User unlocked etalon, frequency just happened to be right.
                        step = RelockStep.tune_etalon
                        continue
                    while True:
                        delta = await get_stable_freq_delta()
                        if abs(delta) < MAX_RELOCK_ATTEMPT_DELTA:
                            step = RelockStep.decide_next
                            break
                        if abs(delta) > MAX_RESONATOR_TUNE_DELTA:
                            # Etalon lock jumped/…
                            logger.info(
                                "Large frequency delta encountered during "
                                "resonator tuning: %s MHz", delta / 1e6)
                            step = RelockStep.tune_etalon
                            break
                        tune_step = -RESONATOR_TUNE_DAMPING * delta / RESONATOR_TUNE_SLOPE
                        new_tune = solstis.resonator_tune + tune_step
                        logger.info("Setting resonator tune to %s", f"{new_tune:0.3f}%")
                        try:
                            await solstis.set_resonator_tune(new_tune)
                        except ValueError:
                            logger.info("Reached invalid resonator tune; restarting from "
                                        "etalon search")
                            step = RelockStep.tune_etalon
                            break
                continue
            if step == RelockStep.determine_resonance:
                async with ensure_solstis as solstis:
                    if not solstis.etalon_locked:
                        # User unlocked etalon, frequency just happened to be right.
                        step = RelockStep.tune_etalon
                        continue

                    tune_centre = solstis.resonator_tune
                    radius = RESONANCE_SEARCH_RADIUS / RESONATOR_TUNE_SLOPE
                    tunes = np.linspace(tune_centre - radius, tune_centre + radius,
                                        RESONANCE_SEARCH_NUM_POINTS)[::-1]
                    adc1s = []
                    for tune in tunes:
                        await solstis.set_resonator_tune(tune, blind=True)
                        await asyncio.sleep(0.005)
                        adc1s.append(await read_adc())
                    await solstis.set_resonator_tune(tunes[np.argmax(adc1s)])
                    lock_attempts_left = LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH
                    step = RelockStep.try_lock
                continue
            if step == RelockStep.try_lock:
                # Enable fast lock, and see if we got some transmission (allow
                # considerably lower transmission than fully locked threshold, though).
                await control.set_aom_lock(True)
                await asyncio.sleep(0.2)
                transmission = await read_adc()
                threshold = control.threshold()
                if transmission < EARLY_TRANSMISSION_FRACTION * threshold:
                    logger.info(
                        "Transmission immediately low (%s); aborting lock attempt",
                        f"{transmission * 1e3:0.1f} mV")
                    step = RelockStep.reset_lock
                    continue

                # Enable PZT lock, monitor for a second during gain ramping to see if lock
                # keeps.
                await control.set_pzt_lock(True)
                start = time.monotonic()
                reset = False
                while True:
                    dt = time.monotonic() - start
                    if dt > PZT_LOCK_SETTLE_TIME:
                        break
                    transmission = await read_adc()
                    if transmission < EARLY_TRANSMISSION_FRACTION * threshold:
                        logger.info(
                            "Transmission low (%s) %s after enabling PZT lock; aborting "
                            "lock attempt", f"{transmission * 1e3:0.1f} mV",
                            f"{dt * 1e3:0.0f} ms")
                        reset = True
                        break
                if reset:
                    step = RelockStep.reset_lock
                    continue

                # Finally, check against original criterion.
                transmission = await read_adc()
                if transmission < threshold:
                    logger.info("Transmission not above threshold in the end")
                    step = RelockStep.reset_lock
                    continue

                logger.info("Laser relocked")
                return
            assert False, f"Unhandled step {step}"

    ensure_solstis = EnsureSolstis(solstis_host, solstis_port)
    try:
        await finite_state_machine(ensure_solstis)
    finally:
        await ensure_solstis.close()


class LockMonitor:
    """Polls the transmission to show the lock state, and relocks the laser when the lock
    is lost (if enabled, and a wavemeter is available).

    While not relocking, the wavemeter is polled regularly, and the last reading
    while the laser was in lock is the target for relocking.

    `run()` is the task; `set_relock_enabled()` cancels a relock in progress when
    relocking is disabled.
    """

    def __init__(self,
                 control: LockControl,
                 wavemeter: Optional[WavemeterInterface],
                 solstis: Optional[tuple[str, int]],
                 relock_enabled: Callable[[], bool],
                 on_relock_available: Callable[[bool], None] = lambda available: None):
        """
        :param wavemeter: The wavemeter to use for relocking (`None` for no relocking).
        :param solstis: Host and port of the SolsTiS ICE-Bloc (`None` for no relocking).
        :param relock_enabled: Whether to relock when the lock is lost.
        :param on_relock_available: Called with whether relocking is possible (the
            wavemeter and the SolsTiS are configured, and the wavemeter answers).
        """
        self._control = control
        self._wavemeter = wavemeter
        self._solstis = solstis
        self._relock_enabled = relock_enabled
        self._on_relock_available = on_relock_available
        self.state = LockState.uninitialised
        #: The last transmission reading, in volts.
        self.transmission: Optional[float] = None
        #: The last wavemeter reading, and the last one while the laser was in lock.
        self.last_freq_reading: Optional[float] = None
        self.last_locked_freq_reading: Optional[float] = None
        self._relock_task: Optional[asyncio.Task] = None
        self._relock_cancelled = False
        #: Number of relocks completed (for tests and the RPC).
        self.relocks = 0

    @property
    def relocking(self) -> bool:
        return self._relock_task is not None

    def set_relock_enabled(self, enabled: bool):
        """Called when relocking is enabled or disabled by the user: a relock in progress
        is cancelled when disabling."""
        if not enabled and self._relock_task is not None:
            logger.info("Automatic relocking disabled; cancelling active relock task")
            self._relock_cancelled = True
            self._relock_task.cancel()

    def _show(self, state: LockState, transmission: Optional[float]):
        self.state = state
        self.transmission = transmission
        self._control.show_state(state, transmission)

    async def run(self):
        wavemeter_task = None
        # Connect to wavemeter server (but gracefully fail to allow using the UI for
        # manual operation even if the wavemeter is offline).
        if self._wavemeter is not None and self._solstis is not None:
            await self._wavemeter.try_connect()
            available = self._wavemeter.is_connected()
            if available:
                wavemeter_task = asyncio.create_task(self._poll_wavemeter())
            else:
                logger.warning("Wavemeter connection not established; "
                               "automatic relocking not available")
        else:
            logger.warning("Wavemeter or SolsTiS not configured; "
                           "automatic relocking not available")
            available = False
        self._on_relock_available(available)

        try:
            while True:
                await self._monitor_step(available)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected relocking failure")
            self._show(LockState.uninitialised, None)
            raise
        finally:
            if self._relock_task is not None:
                self._relock_task.cancel()
                try:
                    await self._relock_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._relock_task = None
            if wavemeter_task is not None:
                wavemeter_task.cancel()
                try:
                    await wavemeter_task
                except asyncio.CancelledError:
                    pass
            if self._wavemeter is not None:
                await self._wavemeter.close()

    async def _poll_wavemeter(self):
        while True:
            await asyncio.sleep(IDLE_WAVEMETER_POLL_INTERVAL)
            status, freq, _osa = await self._wavemeter.get_freq_offset(age=0)
            if status == 0:
                self.last_freq_reading = freq

    async def _monitor_step(self, relock_available: bool):
        await asyncio.sleep(IDLE_ADC1_POLL_INTERVAL)
        try:
            reading = await self._control.read_transmission()
        except ConnectionError:
            if self.state != LockState.uninitialised:
                logger.info("Stabilizer not available; waiting for it")
            self._show(LockState.uninitialised, None)
            await asyncio.sleep(OFFLINE_POLL_INTERVAL)
            return
        is_locked = reading >= self._control.threshold()
        if is_locked:
            if self.last_freq_reading is not None:
                self.last_locked_freq_reading = self.last_freq_reading
        else:
            # Make sure a new frequency reading is fetched later, not one from
            # whatever might have been going on during relocking.
            self.last_freq_reading = None

            if relock_available and self._relock_enabled():
                await self._relock(reading)
                return

        self._show(LockState.locked if is_locked else LockState.out_of_lock, reading)

    async def _relock(self, reading: float):
        assert self._relock_task is None
        logger.info("Cavity transmission low (%.1f mV), starting relocking task.",
                    reading * 1e3)
        if self.last_locked_freq_reading is None:
            self.last_locked_freq_reading = DEFAULT_FREQ_TARGET
            logger.warning(
                "No good frequency reference reading available, "
                "defaulting to %s MHz", self.last_locked_freq_reading / 1e6)
        host, port = self._solstis
        self._relock_cancelled = False
        self._relock_task = asyncio.create_task(
            relock_laser(self._control, self._wavemeter.get_freq_offset,
                         self.last_locked_freq_reading, host, port))
        self._show(LockState.relocking, reading)
        try:
            await self._relock_task
            self.relocks += 1
        except asyncio.CancelledError:
            if not self._relock_cancelled:
                raise
            logger.info("Relocking cancelled")
        except ConnectionError as e:
            logger.warning("Relocking abandoned, Stabilizer not available: %s", e)
            self._show(LockState.uninitialised, None)
        finally:
            self._relock_task = None
