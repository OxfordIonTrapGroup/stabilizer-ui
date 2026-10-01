import numpy as np
from stabilizer.stream_parser import DacDecoder as _DacDecoder


class DacDecoder(_DacDecoder):
    """`stabilizer.stream_parser.DacDecoder` fixed for NumPy 2.

    The upstream implementation XORs with `np.int16(0x8000)`, which raises an
    `OverflowError` with NumPy 2, as the value is out of range for `int16`.
    """

    def to_mu(self, data, start=0, stop=-1):
        # Convert the DAC offset binary to two's complement.
        data[start:stop] ^= np.int16(-0x8000)
