import stabilizer.iir_coefficients as iir
from collections import OrderedDict


class _AbstractArgs:

    def __init__(self, sample_period, **kwargs):
        self.sample_period = sample_period
        for key, value in kwargs.items():
            if key not in self.parameters:
                raise ValueError(f"Key: {key} not found.")
            setattr(self, key, value)

    @classmethod
    def get_coefficients(cls, sample_period, **kwargs):
        args = cls(sample_period, **kwargs)
        return cls.coefficients_func(args)


class LowpassArgs(_AbstractArgs):
    filter_type = "lowpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.lowpass_coefficients


class HighpassArgs(_AbstractArgs):
    filter_type = "highpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.highpass_coefficients


class AllpassArgs(_AbstractArgs):
    filter_type = "allpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.allpass_coefficients


class NotchArgs(_AbstractArgs):
    filter_type = "notch"
    parameters = ["f0", "Q", "K"]
    coefficients_func = iir.notch_coefficients


class PidArgs(_AbstractArgs):
    filter_type = "pid"
    parameters = [
        "Kp",
        "Ki",
        "Ki_limit",
        "Kii",
        "Kii_limit",
        "Kd",
        "Kd_limit",
        "Kdd",
        "Kdd_limit",
    ]
    coefficients_func = iir.pid_coefficients


class ThroughArgs(_AbstractArgs):
    filter_type = "through"
    parameters = []

    @staticmethod
    def coefficients_func():
        return [1, 0, 0, 0, 0]

    @classmethod
    def get_coefficients(cls, *_args):
        return cls.coefficients_func()


class BlockAllArgs(_AbstractArgs):
    filter_type = "block"
    parameters = []

    @staticmethod
    def coefficients_func():
        return [0, 0, 0, 0, 0]

    @classmethod
    def get_coefficients(cls, *_args):
        return cls.coefficients_func()


FILTERS = [
    ThroughArgs, BlockAllArgs, PidArgs, NotchArgs, LowpassArgs, HighpassArgs, AllpassArgs
]


def filters():
    # Use an OrderedDict to control order of widgets
    return OrderedDict([(filter.filter_type, filter) for filter in FILTERS])


def get_filter(filter_type):
    return next(filter for filter in FILTERS if filter.filter_type == filter_type)
