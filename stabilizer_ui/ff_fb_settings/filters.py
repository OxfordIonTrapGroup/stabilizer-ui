import stabilizer.iir_coefficients as iir
from collections import OrderedDict


class _AbstractArgs:
    """
    Base class for all filter argument containers.

    Each concrete filter class:
    - Defines `filter_type` (string identifier used by UI/MQTT).
    - Declares required `parameters`.
    - Provides `coefficients_func`, which maps arguments → IIR coefficients.

    This class:
    - Stores the sampling period.
    - Validates provided keyword arguments.
    - Exposes a helper to compute coefficients in one call.
    """
    def __init__(self, sample_period, **kwargs):
        self.sample_period = sample_period
        for key, value in kwargs.items():
            if key not in self.parameters:
                raise ValueError(f"Key: {key} not found.")
            setattr(self, key, value)

    @classmethod
    def get_coefficients(cls, sample_period, **kwargs):
        """
        Convenience method:
        Create argument object and compute IIR coefficients.
        """
        args = cls(sample_period, **kwargs)
        return cls.coefficients_func(args)


class LowpassArgs(_AbstractArgs):
    """Second-order low-pass filter."""
    filter_type = "lowpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.lowpass_coefficients


class HighpassArgs(_AbstractArgs):
    """Second-order high-pass filter."""
    filter_type = "highpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.highpass_coefficients


class AllpassArgs(_AbstractArgs):
    """Second-order all-pass filter (phase shaping)."""
    filter_type = "allpass"
    parameters = ["f0", "K"]
    coefficients_func = iir.allpass_coefficients


class NotchArgs(_AbstractArgs):
    """Second-order notch filter."""
    filter_type = "notch"
    parameters = ["f0", "Q", "K"]
    coefficients_func = iir.notch_coefficients


class PidArgs(_AbstractArgs):
    """
    Digital PID controller represented as an IIR filter.

    Includes:
    - Proportional term (Kp)
    - Integral term(s) with optional limits
    - Derivative term(s) with optional limits

    The coefficient generator maps these gains into
    a biquad representation for embedded execution.
    """
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
    """
    Unity (pass-through) filter.

    Equivalent to:
        y[n] = x[n]

    Used to disable filtering without changing structure.
    """
    filter_type = "through"
    parameters = []

    @staticmethod
    def coefficients_func():
        return [1, 0, 0, 0, 0]

    @classmethod
    def get_coefficients(cls, *_args):
        return cls.coefficients_func()


class BlockAllArgs(_AbstractArgs):
    """
    Zero-output filter.

    Equivalent to:
        y[n] = 0

    Used to disable a channel entirely.
    """
    filter_type = "block"
    parameters = []

    @staticmethod
    def coefficients_func():
        return [0, 0, 0, 0, 0]

    @classmethod
    def get_coefficients(cls, *_args):
        return cls.coefficients_func()

# Ordered list of available filters.
# The order here determines the order of filter options shown in the UI.
FILTERS = [
    ThroughArgs, BlockAllArgs, PidArgs, NotchArgs, LowpassArgs, HighpassArgs, AllpassArgs
]


def filters():
    """
    Return filters as an OrderedDict:
        {filter_type_string: filter_class}

    OrderedDict ensures consistent UI ordering.
    """
    # Use an OrderedDict to control order of widgets
    return OrderedDict([(filter.filter_type, filter) for filter in FILTERS])


def get_filter(filter_type):
    """
    Return the filter class matching `filter_type`.

    Raises StopIteration if not found.
    """
    return next(filter for filter in FILTERS if filter.filter_type == filter_type)
