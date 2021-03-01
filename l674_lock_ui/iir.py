"""
Derive Stabilizer IIR config dicts from filter definitions in physical units.

(PID math based on the stabilizer.py script from the Stabilizer repository.)
"""

from typing import List
from scipy.signal.filter_design import iirnotch
import numpy as np

T_CYCLE = 128 / 100e6
FULL_SCALE = float((1 << 15) - 1)


def iir_config(biquad_coeffs: List[float], x_offset: float = 0.0):
    ba = np.array(biquad_coeffs, dtype=np.float)
    dc_gain = ba[:3].sum()
    return {
        "ba": ba.tolist(),
        "y_min": -FULL_SCALE - 1,
        "y_max": FULL_SCALE,
        "y_offset": dc_gain * FULL_SCALE * x_offset
    }


def notch_coeffs(freq: float, q: float) -> List[float]:
    b, a = iirnotch(freq, q, fs=(np.pi / T_CYCLE / 2))
    return np.concatenate((b, -a[1:]))


def pi_coeffs(kp: float, ki: float, gain_limit: float = 0.0) -> List[float]:
    ki = np.copysign(ki, kp) * T_CYCLE * 2
    eps = np.finfo(np.float32).eps
    if abs(ki) < eps:
        return [kp, 0.0, 0.0, 0.0, 0.0]

    gain_limit = np.copysign(gain_limit, kp)
    if abs(gain_limit) < eps:
        c = 1.0
    else:
        c = 1.0 / (1.0 + ki / gain_limit)
    a1 = 2 * c - 1.0
    b0 = ki * c + kp
    b1 = ki * c - a1 * kp
    if abs(b0 + b1) < eps:
        raise ValueError("low integrator gain and/or gain limit")

    return [b0, b1, 0.0, a1, 0.0]
