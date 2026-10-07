import logging
import textwrap
import asyncio

from math import inf
from . import mqtt

logger = logging.getLogger(__name__)

# Unit conversions
kilo = (
    lambda w: mqtt.read(w) * 1e3,
    lambda w, v: mqtt.write(w, v / 1e3),
)

# TODO: check if this is correct, this is what the code previously had
kilo2 = (
    lambda w: mqtt.read(w) * 1e3,
    lambda w, v: mqtt.write(w, v / 1e3),
)

mega = (
    lambda widgets: mqtt.read(widgets) * 1e6,
    lambda widgets, value: mqtt.write(widgets, value / 1e6),
)

milli = (
    lambda widgets: mqtt.read(widgets) * 1e-3,
    lambda widgets, value: mqtt.write(widgets, value * 1e3),
)

# A check box bound to the negation of a setting.
invert = (
    lambda widgets: not mqtt.read(widgets),
    lambda widgets, value: mqtt.write(widgets, not value),
)


def radio_group(choices: list[str]):
    """Read and write handlers binding a group of radio buttons (one per choice, in the
    order of `choices`) to a setting with one of `choices` as its value."""

    def read(widgets):
        for i in range(1, len(widgets)):
            if widgets[i].isChecked():
                return choices[i]
        # Default to first value, as Qt can sometimes transiently have all buttons
        # of a radio group disabled (apparently so when clicking an already-selected
        # button).
        return choices[0]

    def write(widgets, value):
        one_checked = False
        for widget, choice in zip(widgets, choices):
            checked = choice == value
            widget.setChecked(checked)
            one_checked |= checked
        if not one_checked:
            logger.warning("Unexpected value: '%s' (choices: '%s')", value, choices)

    return read, write


def link_spinbox_to_is_inf_checkbox():

    def read(widgets):
        """Expects widgets in the form [spinbox, checkbox]."""
        if widgets[1].isChecked():
            return inf
        else:
            return widgets[0].value()

    def write(widgets, value):
        """Expects widgets in the form [spinbox, checkbox]."""
        is_inf = value == inf
        widgets[1].setChecked(is_inf)
        # Clicking the checkbox disables the spinbox (see the `.ui` files).
        widgets[0].setDisabled(is_inf)
        if not is_inf:
            widgets[0].setValue(value)

    return read, write


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def format_size(size: float) -> str:
    """A size in bytes, in B, kB, MB or GB (decimal)."""
    if size < 1e3:
        return f"{size:.0f} B"
    if size < 1e6:
        return f"{size / 1e3:.1f} kB"
    if size < 1e9:
        return f"{size / 1e6:.1f} MB"
    return f"{size / 1e9:.2f} GB"


def format_frequency(frequency: float) -> str:
    """A frequency in Hz or kHz, to three significant digits."""
    if frequency < 1e3:
        return f"{frequency:.3g} Hz"
    return f"{frequency / 1e3:.3g} kHz"


def fmt_mac(mac: str) -> str:
    mac_nosep = "".join(c for c in mac if c.isalnum()).lower()
    if len(mac_nosep) != 12 or any(char not in "0123456789abcdef" for char in mac_nosep):
        raise ValueError(f"Invalid MAC address: {mac}")
    return "-".join(textwrap.wrap(mac_nosep, 2))


class AsyncQueueThreadsafe(asyncio.Queue):

    def __init__(self, loop=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._loop = loop or asyncio.get_event_loop()

    async def get_threadsafe(self, timeout=None):
        '''Get an item from the queue in a threadsafe manner.

        This is equivalent to asyncio.Queue.get(), but can be called from a different thread.
        '''
        future = asyncio.run_coroutine_threadsafe(self.get(), self._loop)
        return future.result(timeout)

    async def put_threadsafe(self, item, timeout=None):
        '''Put an item into the queue in a threadsafe manner.

        This is equivalent to asyncio.Queue.put(), but can be called from a different thread.
        '''
        future = asyncio.run_coroutine_threadsafe(self.put(item), self._loop)
        return future.result(timeout)

    async def join_threadsafe(self, timeout=None):
        '''Block until all items in the queue have been gotten and processed in a threadsafe manner.

        This is equivalent to asyncio.Queue.join(), but can be called from a different thread.
        '''
        future = asyncio.run_coroutine_threadsafe(self.join(), self._loop)
        return future.result(timeout)
