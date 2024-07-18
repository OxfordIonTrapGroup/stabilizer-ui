# Type annotation for UiWindow leads to circular dependencies
from __future__ import annotations

import logging
import numpy as np
import asyncio
import time

from enum import Enum, unique
from typing import Callable, Awaitable

from .interface import StabilizerInterface
from .solstis import EnsureSolstis
from ...interface import WavemeterInterface

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
#: aliasing between etalon and resonator modes?), this is .
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

#: Timeout for wavemeter requests. Should be at least a few seconds at least to
#: accommodate long exposure times on other channels, but short enough to recover from
#: lost connections while auto-relocking in a timely fashion.
WAVEMETER_TIMEOUT = 10.0

#: Number of attempts to lock at target wavemeter reading before engaging resonance
#: search.
LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH = 5

logger = logging.getLogger(__name__)


@unique
class LockState(Enum):
    out_of_lock = "Out of lock"
    relocking = "Relocking"
    locked = "Locked"
    uninitialised = "Uninitialised"


@unique
class RelockStep(Enum):
    reset_lock = "reset_lock"
    decide_next = "decide_next"
    tune_etalon = "tune_etalon"
    tune_resonator = "tune_resonator"
    determine_resonance = "determine_resonance"
    try_lock = "try_lock"


async def relock_laser(ui: UiWindow, stabilizer_interface: StabilizerInterface,
                       get_freq: Callable[[], Awaitable[float]],
                       approximate_target_freq: float, solstis_host: str):
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
        voltage = await stabilizer_interface.read_adc()
        ui.update_lock_state(LockState.relocking, voltage)
        return voltage

    async def finite_state_machine(ensure_solstis):
        # Finite state machine, exiting only through try_lock success, or cancellation.
        step = RelockStep.reset_lock

        lock_attempts_left = LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH
        while True:
            logger.info("Current step: %s", step)
            if step == RelockStep.reset_lock:
                ui.enableAOMLockBox.setChecked(False)
                ui.disablePztButton.setChecked(True)
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
                    # Currently don't have resonator tune read-back after lock is engaged, so
                    # reopen connection completely.
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
                        except ValueError as e:
                            logger.info(
                                "Reached invalid resonator tune; restarting from etalon search"
                            )
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
                # Enable fast lock, and see if we got some transmission (allow considerably
                # lower transmission than fully locked threshold, though).
                ui.enableAOMLockBox.setChecked(True)
                await asyncio.sleep(0.2)
                transmission = await read_adc()
                if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                    logger.info(
                        "Transmission immediately low (%s); aborting lock attempt",
                        f"{transmission * 1e3:0.1f} mV")
                    step = RelockStep.reset_lock
                    continue

                # Enable PZT lock, monitor for a second during gain ramping to see if lock
                # keeps.
                ui.enablePztButton.setChecked(True)
                start = time.monotonic()
                reset = False
                while True:
                    dt = time.monotonic() - start
                    if dt > 1.0:
                        break
                    transmission = await read_adc()
                    if transmission < 0.2 * ui.lockDetectThresholdBox.value():
                        logger.info(
                            "Transmission low (%s) %s after enabling PZT lock; aborting lock attempt",
                            f"{transmission * 1e3:0.1f} mV", f"{dt * 1e3:0.0f} ms")
                        reset = True
                        break
                if reset:
                    step = RelockStep.reset_lock
                    continue

                # Finally, check against original criterion.
                transmission = await read_adc()
                if transmission < ui.lockDetectThresholdBox.value():
                    logger.info("Transmission not above threshold in the end")
                    step = RelockStep.reset_lock
                    continue

                logger.info("Laser relocked")
                return
            assert False, f"Unhandled step {step}"

    ensure_solstis = EnsureSolstis(solstis_host)
    try:
        await finite_state_machine(ensure_solstis)
    finally:
        await ensure_solstis.close()


async def monitor_lock_state(ui: UiWindow, stabilizer_interface: StabilizerInterface,
                             wand_host: NetworkAddress, wand_channel: str,
                             solstis_host: str):
    # While not relocking, we regularly poll the wavemeter and save the last reading,
    # to be "graduated" to last_locked_freq_reading once we know that the laser was
    # in lock for it.
    last_freq_reading = None
    last_locked_freq_reading = None

    # Connect to wavemeter server (but gracefully fail to allow using the UI for manual
    # operation even if the wavemeter is offline).
    wand = WavemeterInterface(wand_host.get_ip(), wand_host.port, wand_channel, WAVEMETER_TIMEOUT)
    await wand.try_connect()
    wavemeter_task = None
    if not wand.is_connected():
        logger.warning("Wavemeter connection not established; "
                       "automatic relocking not available")
        ui.enableRelockingBox.setChecked(False)
        ui.enableRelockingBox.setEnabled(False)
    else:
        ui.enableRelockingBox.setChecked(True)
        ui.enableRelockingBox.setEnabled(True)

        async def wavemeter_loop():
            nonlocal last_freq_reading
            while True:
                await asyncio.sleep(IDLE_WAVEMETER_POLL_INTERVAL)
                status, freq, _osa = await wand.get_freq_offset(age=0)
                if status == 0:
                    last_freq_reading = freq

        wavemeter_task = asyncio.create_task(wavemeter_loop())

    relock_task = None

    # KLUDGE: To cleanly shutdown the program, we want to be able to cancel() this task,
    # which might result in a CancelledError while we are waiting for the relock task
    # to finish – which is also what happens if the user unticks the relock enable check
    # box. To be able to disambiguate between these cases, record whether the
    # cancellation was triggered from the UI signal handler.
    we_cancelled_relock_task = False

    def relock_enable_changed(enabled):
        if not enabled and relock_task is not None:
            logger.info("Automatic relocking disabled; cancelling active relock task")
            nonlocal we_cancelled_relock_task
            we_cancelled_relock_task = True
            relock_task.cancel()

    ui.enableRelockingBox.toggled.connect(relock_enable_changed)

    try:
        while True:
            await asyncio.sleep(IDLE_ADC1_POLL_INTERVAL)
            reading = await stabilizer_interface.read_adc()
            is_locked = reading >= ui.lockDetectThresholdBox.value()
            if is_locked:
                if last_freq_reading is not None:
                    last_locked_freq_reading = last_freq_reading
            else:
                # Make sure a new frequency reading is fetched later, not one from
                # whatever might have been going on during relocking.
                last_freq_reading = None

                if ui.enableRelockingBox.isChecked():
                    assert relock_task is None
                    logger.info(
                        "Cavity transmission low (%s mV), starting relocking task.",
                        reading * 1e3)
                    if last_locked_freq_reading is None:
                        last_locked_freq_reading = DEFAULT_FREQ_TARGET
                        logger.warning(
                            "No good frequency reference reading available, "
                            "defaulting to %s MHz", last_locked_freq_reading / 1e6)
                    relock_task = asyncio.create_task(
                        relock_laser(ui, stabilizer_interface, wand.get_freq_offset,
                                     last_locked_freq_reading, solstis_host))
                    ui.update_lock_state(LockState.relocking, reading)
                    try:
                        await relock_task
                    except asyncio.CancelledError:
                        if not we_cancelled_relock_task:
                            raise
                        we_cancelled_relock_task = False
                    relock_task = None
                    continue

            ui.update_lock_state(LockState.locked if is_locked else LockState.out_of_lock,
                                 reading)
    except Exception:
        logger.exception("Unexpected relocking failure")
        ui.update_lock_state(LockState.uninitialised, None)
    finally:
        if relock_task:
            relock_task.cancel()
            await relock_task
        if wavemeter_task:
            wavemeter_task.cancel()
            await wavemeter_task
