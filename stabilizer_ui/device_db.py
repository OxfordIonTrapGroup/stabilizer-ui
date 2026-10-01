#!/usr/bin/env python3
"""
A database of stabilizer devices in the group.
Identifies the device parameters by a logical name to avoid having to manually pass them
for every run

Accepted format:

<logical_name>: {
    "mac-address": str              # The MAC address of the device
    "application": str,             # The application the device is running; in ["fnc", "dual_iir", "current_sense"]
    "broker": NetworkAddress,       # The IP address and connection port of the MQTT broker
    "net_id": str, optional         # The MQTT topic of the stabilizer, if different from the MAC address. Needs to match flash settings on the device.
}

Apart from these base parameters, each application may define additional parameters linked to a setup as needed.
"""

from .mqtt import NetworkAddress

broker_255_6_4 = NetworkAddress.from_str_ip("10.255.6.4", 1883)

stabilizer_devices = {}
stabilizer_devices["lab1_729"] = {
    "mac-address": "68-27-19-80-72-9e",
    "application": "fnc",
    "broker": broker_255_6_4,
}

stabilizer_devices["lab1_raman_phaselock"] = {
    "mac-address": "44-b7-d0-c7-7d-24",
    "application": "dual_iir",
    "broker": broker_255_6_4,
}

# Stabilizer v1.2 with the current sense board (dungeon)
stabilizer_devices["dungeon_109_current_sense"] = {
    "mac-address": "68-27-19-80-3d-4d",
    "application": "current_sense",
    "broker": broker_255_6_4,
}
