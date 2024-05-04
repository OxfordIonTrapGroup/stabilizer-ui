from PyQt5 import QtWidgets
import textwrap
import asyncio


def lerp(start, stop, fractional_position):
    return start + (stop - start) * fractional_position


def inv_lerp(start, stop, position):
    return (position - start) / (stop - start)


def link_slider_to_spinbox(slider: QtWidgets.QSlider,
                           spinbox: QtWidgets.QDoubleSpinBox) -> None:
    """Links the given slider and spinbox, so that changes to one are reflected in the
    other.

    This is a bit painful due to the fact that sliders only support quantized integer
    positions, plus we are working without a data model that would allow us to
    distinguish user edits from sync updates to avoid loops.
    """

    def val_to_slider_pos(val):
        frac_pos = inv_lerp(spinbox.minimum(), spinbox.maximum(), val)
        return round(lerp(slider.minimum(), slider.maximum(), frac_pos))

    def update_box(pos):
        if val_to_slider_pos(spinbox.value()) == pos:
            # Already within rounding distance; don't forcibly quantise values.
            return
        frac_pos = inv_lerp(slider.minimum(), slider.maximum(), pos)
        spinbox.setValue(lerp(spinbox.minimum(), spinbox.maximum(), frac_pos))

    def update_slider(val):
        new_pos = val_to_slider_pos(val)
        if new_pos != slider.value():
            slider.setValue(new_pos)

    slider.valueChanged.connect(update_box)
    spinbox.valueChanged.connect(update_slider)

    # Only tangentially related: Don't update gains/... while user is typing.
    spinbox.setKeyboardTracking(False)


def fmt_mac(mac: str) -> str:
    mac_nosep = "".join(c for c in mac if c.isalnum()).lower()
    if len(mac_nosep) != 12 or any(char not in "0123456789abcdef" for char in mac_nosep):
        raise ValueError(f"Invalid MAC address: {mac}")
    return "-".join(textwrap.wrap(mac_nosep, 2))


class AsyncThreadsafeQueue(asyncio.Queue):
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
