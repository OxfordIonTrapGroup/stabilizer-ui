"""The relocking state machine and the lock monitor of `l674`, against an in-process model
of the laser (no Qt, no network)."""
import asyncio
import contextlib
import logging
import math

import numpy as np
import pytest

from stabilizer_ui.target.l674 import lock
from stabilizer_ui.target.l674.lock import LockMonitor, LockState, relock_laser

#: Lock detect threshold, in volts.
THRESHOLD = 0.5


class Laser:
    """The laser and the cavity: the free-running detuning follows the SolsTiS tunes and
    the kicks; the lock captures (zero detuning) with the AOM lock released, the PZT lock
    on and the detuning within the capture range. (A smaller copy of the model of
    `tools/fake_stabilizer.py`.)"""

    HWHM = 2e6
    CAPTURE_RANGE = 3e6
    PZT_RANGE = 100e6
    LOCKED_READING = 150e6
    #: The etalon selects modes: its effect on the frequency comes in steps of this many
    #: tune percent.
    ETALON_STEP = 0.1

    def __init__(self, seed=0):
        self.rng = np.random.default_rng(seed)
        self.etalon_tune = 50.0
        self.resonator_tune = 50.0
        self.etalon_locked = True
        self.drift = 0.0
        self.wavemeter_bias = 0.0
        self.wavemeter_noise = 0.3e6
        self.aom = False
        self.pzt = False
        self.locked = False

    def free_detuning(self):
        etalon = round((self.etalon_tune - 50) / self.ETALON_STEP) * self.ETALON_STEP
        return (lock.ETALON_TUNE_SLOPE * etalon + lock.RESONATOR_TUNE_SLOPE *
                (self.resonator_tune - 50) + self.drift)

    def update(self):
        free = self.free_detuning()
        limit = self.PZT_RANGE if self.locked else self.CAPTURE_RANGE
        self.locked = self.aom and self.pzt and abs(free) < limit

    def detuning(self):
        self.update()
        return 0.0 if self.locked else self.free_detuning()

    def transmission(self):
        x = self.detuning() / self.HWHM
        return 1.0 / (1 + x * x)

    def reading(self):
        return (self.LOCKED_READING + self.detuning() + self.wavemeter_bias +
                self.wavemeter_noise * self.rng.standard_normal())


class Control:
    """`lock.LockControl` on the laser model, recording what the relocking does."""

    def __init__(self, laser: Laser):
        self.laser = laser
        self.online = True
        self.states = []
        self.actions = []
        self.reads = 0

    def threshold(self):
        return THRESHOLD

    async def read_transmission(self):
        if not self.online:
            raise ConnectionError("offline")
        self.reads += 1
        return self.laser.transmission()

    async def set_aom_lock(self, enabled):
        self.actions.append(("aom", enabled))
        self.laser.aom = enabled

    async def set_pzt_lock(self, enabled):
        self.actions.append(("pzt", enabled))
        self.laser.pzt = enabled

    def show_state(self, state, transmission):
        self.states.append(state)


class Solstis:
    """A connected fake ICE-Bloc on the laser model."""

    def __init__(self, laser: Laser, log: list):
        self.laser = laser
        self.log = log
        self._initialised = True

    @property
    def etalon_tune(self):
        return self.laser.etalon_tune

    @property
    def resonator_tune(self):
        return self.laser.resonator_tune

    @property
    def etalon_locked(self):
        return self.laser.etalon_locked

    async def set_etalon_tune(self, tune):
        if not 0 <= tune <= 100:
            raise ValueError("Invalid etalon tuning value")
        self.log.append(("etalon", tune))
        self.laser.etalon_tune = tune

    async def set_resonator_tune(self, tune, blind=False):
        if not 0 <= tune <= 100:
            raise ValueError("Invalid resonator tuning value")
        self.log.append(("resonator", tune))
        self.laser.resonator_tune = tune

    async def set_etalon_locked(self, locked):
        self.log.append(("etalon_locked", locked))
        self.laser.etalon_locked = locked

    async def close(self):
        self._initialised = False


class EnsureSolstis:
    """Replaces `lock.EnsureSolstis`: connects the fake on entering."""

    laser = None
    log = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.solstis = None

    async def __aenter__(self):
        self.solstis = Solstis(self.laser, self.log)
        return self.solstis

    async def __aexit__(self, *exc_info):
        if exc_info[0] and exc_info[0] is not asyncio.CancelledError:
            logging.getLogger(__name__).exception("SolsTiS failed", exc_info=exc_info)
            return True

    async def close(self):
        pass


@pytest.fixture
def setup(monkeypatch):
    """A laser, its control, the fake SolsTiS, and short waiting times."""
    laser = Laser()
    control = Control(laser)
    EnsureSolstis.laser = laser
    EnsureSolstis.log = []
    monkeypatch.setattr(lock, "EnsureSolstis", EnsureSolstis)
    monkeypatch.setattr(lock, "PZT_LOCK_SETTLE_TIME", 0.05)
    monkeypatch.setattr(lock, "IDLE_ADC1_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(lock, "IDLE_WAVEMETER_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(lock, "OFFLINE_POLL_INTERVAL", 0.05)
    return laser, control


async def get_freq(laser):
    return (0, laser.reading(), None)


def relock(laser, control, target=Laser.LOCKED_READING):
    return asyncio.run(
        relock_laser(control, lambda: get_freq(laser), target, "solstis", 8088))


def test_relock_by_resonator(setup):
    """A lock lost by a 200 MHz jump is regained by tuning the resonator."""
    laser, control = setup
    laser.aom = laser.pzt = True
    laser.update()
    assert laser.locked
    laser.drift = 200e6
    assert not laser.transmission() > THRESHOLD

    relock(laser, control)

    assert laser.locked and laser.transmission() > THRESHOLD
    # The lock was reset first, then engaged AOM first, PZT second.
    assert control.actions[:2] == [("aom", False), ("pzt", False)]
    assert control.actions[-2:] == [("aom", True), ("pzt", True)]
    tunes = [t for kind, t in EnsureSolstis.log if kind == "resonator"]
    assert tunes, "resonator tuned"
    assert abs(tunes[-1] - (50 - 200e6 / lock.RESONATOR_TUNE_SLOPE)) < 0.02
    assert not any(kind == "etalon" for kind, _ in EnsureSolstis.log)
    assert all(state == LockState.relocking for state in control.states)


def test_relock_by_resonance_search(setup):
    """With the wavemeter off by more than the capture range, the lock attempts fail and
    the resonance is found by scanning the resonator for the transmission peak."""
    laser, control = setup
    laser.aom = laser.pzt = True
    laser.update()
    laser.drift = 100e6
    laser.wavemeter_bias = 8e6

    relock(laser, control)

    assert laser.locked
    # Each failed attempt releases the AOM lock, finds the transmission low, and resets
    # (without enabling the PZT lock); only the attempt after the search succeeds.
    attempts = control.actions.count(("aom", True))
    assert attempts == lock.LOCK_ATTEMPTS_BEFORE_RESONANCE_SEARCH + 1
    assert control.actions.count(("pzt", True)) == 1
    tunes = [t for kind, t in EnsureSolstis.log if kind == "resonator"]
    # The scan points, then the peak.
    assert len(tunes) > lock.RESONANCE_SEARCH_NUM_POINTS
    assert abs(laser.free_detuning()) < Laser.CAPTURE_RANGE


def test_relock_by_etalon(setup):
    """A jump of several GHz needs the etalon: it is unlocked, tuned, and locked again,
    before the resonator takes over."""
    laser, control = setup
    laser.aom = laser.pzt = True
    laser.update()
    laser.drift = 3e9

    relock(laser, control)

    assert laser.locked
    log = EnsureSolstis.log
    assert log[0] == ("etalon_locked", False)
    assert ("etalon_locked", True) in log
    etalon = [t for kind, t in log if kind == "etalon"]
    assert etalon and abs(etalon[-1] - (50 - 3e9 / lock.ETALON_TUNE_SLOPE)) < 0.05
    assert log.index(
        ("etalon_locked",
         True)) < log.index(next(entry for entry in log if entry[0] == "resonator"))


def test_relock_connection_lost(setup):
    """The relocking gives up (raising `ConnectionError`) when the device goes away."""
    laser, control = setup
    laser.drift = 100e6
    control.online = False
    with pytest.raises(ConnectionError):
        relock(laser, control)


def test_monitor(setup):
    """The monitor shows the lock state, relocks when the lock is lost (if enabled), and
    cancels a relock when relocking is disabled."""
    laser, control = setup
    laser.aom = laser.pzt = True
    laser.update()
    enabled = [True]
    available = []

    class Wavemeter:

        def __init__(self):
            self.connected = False

        async def try_connect(self):
            self.connected = True

        def is_connected(self):
            return self.connected

        async def close(self):
            self.connected = False

        async def get_freq_offset(self, age=0):
            return await get_freq(laser)

    async def run():
        wavemeter = Wavemeter()
        monitor = LockMonitor(control, wavemeter, ("solstis", 8088), lambda: enabled[0],
                              available.append)
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.sleep(0.3)
            assert available == [True]
            assert monitor.state == LockState.locked
            assert monitor.last_locked_freq_reading is not None
            assert abs(monitor.last_locked_freq_reading - Laser.LOCKED_READING) < 2e6

            # The device goes away and comes back.
            control.online = False
            await asyncio.sleep(0.2)
            assert monitor.state == LockState.uninitialised
            control.online = True
            await asyncio.sleep(0.2)
            assert monitor.state == LockState.locked

            # Lose the lock: relocked.
            laser.drift = 150e6
            for _ in range(100):
                await asyncio.sleep(0.05)
                if monitor.relocks:
                    break
            assert monitor.relocks == 1
            assert laser.locked
            await asyncio.sleep(0.1)
            assert monitor.state == LockState.locked

            # Relocking disabled while a relock is running: cancelled, out of lock.
            laser.drift = 300e6
            for _ in range(50):
                await asyncio.sleep(0.02)
                if monitor.relocking:
                    break
            assert monitor.relocking
            enabled[0] = False
            monitor.set_relock_enabled(False)
            await asyncio.sleep(0.2)
            assert not monitor.relocking
            assert monitor.state == LockState.out_of_lock
            assert monitor.relocks == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_monitor_without_wavemeter(setup):
    """Without a wavemeter, the state is shown but nothing is relocked."""
    laser, control = setup
    laser.aom = laser.pzt = True
    laser.update()

    async def run():
        monitor = LockMonitor(control, None, None, lambda: True)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.2)
        assert monitor.state == LockState.locked
        laser.drift = 100e6
        await asyncio.sleep(0.2)
        assert monitor.state == LockState.out_of_lock
        assert not monitor.relocking and not control.actions
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_constants_consistent():
    """The search radius covers the lock attempt range with margin."""
    assert lock.RESONANCE_SEARCH_RADIUS > lock.MAX_RELOCK_ATTEMPT_DELTA
    assert math.isfinite(lock.ETALON_TUNE_SLOPE) and lock.ETALON_TUNE_SLOPE != 0
