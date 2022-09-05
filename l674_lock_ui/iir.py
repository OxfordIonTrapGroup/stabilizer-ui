from enum import Enum
import stabilizer.iir_coefficients as iir
from stabilizer import SAMPLE_PERIOD
from collections import namedtuple
from math import inf


class _AbstractArgs:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if key not in self.parameters:
                raise ValueError(f"Key: {key} not found.")
            setattr(self, key, value)


class XPassArgs(_AbstractArgs):
    parameters = ["f0", "K"]


class NotchArgs(_AbstractArgs):
    parameters = ["f0", "Q", "K"]


class PidArgs(_AbstractArgs):
    parameters = [
        "Kp", "Ki", "Ki_limit", "Kii", "Kii_limit", "Kd", "Kd_limit", "Kdd", "Kdd_limit"
    ]


def get_lowpass_coefficients(*args, **kwargs):
    args = XPassArgs(*args, **kwargs)
    args.sample_period = SAMPLE_PERIOD
    return iir.lowpass_coefficients(args)


def get_highpass_coefficients(*args, **kwargs):
    args = XPassArgs(*args, **kwargs)
    args.sample_period = SAMPLE_PERIOD
    return iir.highpass_coefficients(args)


def get_allpass_coefficients(*args, **kwargs):
    args = XPassArgs(*args, **kwargs)
    args.sample_period = SAMPLE_PERIOD
    return iir.allpass_coefficients(args)


def get_notch_coefficients(*args, **kwargs):
    args = NotchArgs(*args, **kwargs)
    args.sample_period = SAMPLE_PERIOD
    return iir.notch_coefficients(args)


def get_pid_coefficients(*args, **kwargs):
    args = PidArgs(*args, **kwargs)
    args.sample_period = SAMPLE_PERIOD
    return iir.pid_coefficients(args)


__all__ = [
    "get_lowpass_coefficients", "get_highpass_coefficients", "get_allpass_coefficients",
    "get_notch_coefficients", "get_pid_coefficients", "XPassArgs", "NotchArgs",
    "PidArgs"
]
