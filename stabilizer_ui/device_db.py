#!/usr/bin/env python3
"""
A database of stabilizer devices in the group.
Identifies the device parameters by a logical name to avoid having to manually pass them
for every run

Accepted format:

<logical_name>: {
    "mac-address": str              # The MAC address of the device
    "application": str,             # The application the device is running; in ["fnc", "dual_iir", "l674"]
    "broker": NetworkAddress,       # The IP address and connection port of the MQTT broker
    "net_id": str, optional         # The MQTT topic of the stabilizer, if different from the MAC address. Needs to match flash settings on the device.
}

Apart from these base parameters, each application may define additional parameters linked to a setup as needed.
"""

from .mqtt import NetworkAddress

broker_255_6_4 = NetworkAddress.from_str_ip("10.255.6.4", 1883)
wand_lab1 = NetworkAddress.from_str_ip("10.255.6.61", 3251)

stabilizer_devices = {}
stabilizer_devices["lab1_729"] = {
    "mac-address": "68-27-19-80-72-9e",
    "application": "fnc",
    "broker": broker_255_6_4,
}

stabilizer_devices["lab1_674"] = {
    "mac-address": "80-1f-12-5d-47-df",
    "application": "l674",
    "broker": broker_255_6_4,
    "wand-address": wand_lab1,
    "wand-channel": "lab1_674",
    "solstis-host": "10.179.22.23",
}

stabilizer_devices["lab1_raman_phaselock"] = {
    "mac-address": "80-34-28-5f-59-0b",
    "application": "dual_iir",
    "broker": broker_255_6_4,
}
