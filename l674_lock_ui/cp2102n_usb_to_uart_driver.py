"""
Driver to toggle individual GPIO output on CP210x USB to UART Bridge.

Grandfathered in from 674-lock-gui (R. Srinivas).
"""

import usb.core
from usb.util import CTRL_IN, CTRL_OUT, CTRL_TYPE_VENDOR

MAX_GPIO_INDEX = 8

CP210X_VENDOR_ID = 0x10c4
CP210X_PRODUCT_ID = 0xea60

CP210X_REQUEST_TYPE_READ = CTRL_IN | CTRL_TYPE_VENDOR
CP210X_REQUEST_TYPE_WRITE = CTRL_OUT | CTRL_TYPE_VENDOR

CP210X_REQUEST_VENDOR = 0xFF

CP210X_VALUE_READ_LATCH = 0x00C2
CP210X_VALUE_WRITE_LATCH = 0x37E1


class CP2102N:
    def __init__(self):
        self.dev = usb.core.find(idVendor=CP210X_VENDOR_ID, idProduct=CP210X_PRODUCT_ID)
        if self.dev is None:
            raise Exception("Could not find USB device CP2102N.")

    def query(self, request, value, index, length):
        return self.dev.ctrl_transfer(CP210X_REQUEST_TYPE_READ, request, value, index,
                                      length)

    def get_gpio_states(self):
        results = []
        response = self.query(CP210X_REQUEST_VENDOR, CP210X_VALUE_READ_LATCH, 0, 1)
        if len(response) > 0:
            response = response[0]
        for idx in range(MAX_GPIO_INDEX):
            results.append((response & (1 << idx)) == 0)
        return results

    def write(self, request, value, index, data):
        return self.dev.ctrl_transfer(CP210X_REQUEST_TYPE_WRITE, request, value, index,
                                      data)

    def set_gpio(self, index, value: bool):
        """Set the latch value of a GPIO"""
        assert index >= 0 and index < MAX_GPIO_INDEX
        mask = 1 << index
        values = (0 if value else 1) << index
        msg = (values << 8) | mask
        return self.write(CP210X_REQUEST_VENDOR, CP210X_VALUE_WRITE_LATCH, msg, 0)

    def reset(self):
        self.dev.reset()
