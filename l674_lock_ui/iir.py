from enum import Enum
import stabilizer.iir_coefficients as iir
from stabilizer import SAMPLE_PERIOD
from typing import namedtuple
from math import inf

__all__ = [LowpassFilter, HighpassFilter, AllpassFilter, NotchFilter, PIDFilter, XPassArgs, NotchArgs, PidArgs]


class _AbstractArgs:
    def __init__(self, *kwargs):
        for key, value in kwargs:
            if key not in self.parameters:
                raise ValueError()
            setattr(self, key, value)


class XPassArgs(_AbstractArgs):
    parameters = ["f0", "K"]


class NotchArgs(_AbstractArgs):
    parameters = ["f0", "Q", "K"]


class PidArgs(_AbstractArgs):
    parameters = [
        "Kp", "Ki", "Ki_limit", "Kii", "Kii_limit", "Kd", "Kd_limit", "Kdd", "Kdd_limit"
    ]


class LowpassFilter:
    def get_ba(self, *args, **kwargs):
        args = XPassArgs(*args, **kwargs)
        args.sample_period = SAMPLE_PERIOD
        return iir.lowpass_coefficients(args)


class HighpassFilter:
    def get_ba(self, *args, **kwargs):
        args = XPassArgs(*args, **kwargs)
        args.sample_period = SAMPLE_PERIOD
        return iir.highpass_coefficients(args)


class AllpassFilter:
    def get_ba(self, *args, **kwargs):
        args = XPassArgs(*args, **kwargs)
        args.sample_period = SAMPLE_PERIOD
        return iir.allpass_coefficients(args)


class NotchFilter:
    def get_ba(self, *args, **kwargs):
        args = XPassArgs(*args, **kwargs)
        args.sample_period = SAMPLE_PERIOD
        return iir.notch_coefficients(args)


class PIDFilter:
    def get_ba(self, *args, **kwargs):
        args = XPassArgs(*args, **kwargs)
        args.sample_period = SAMPLE_PERIOD
        return iir.pid_coefficients(args)
