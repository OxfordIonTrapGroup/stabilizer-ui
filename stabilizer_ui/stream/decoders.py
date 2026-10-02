import numpy as np
from stabilizer.stream_parser import DacDecoder as _DacDecoder, Parser


class DacDecoder(_DacDecoder):
    """`stabilizer.stream_parser.DacDecoder` fixed for NumPy 2.

    The upstream implementation XORs with `np.int16(0x8000)`, which raises an
    `OverflowError` with NumPy 2, as the value is out of range for `int16`.
    """

    def to_mu(self, data, start=0, stop=-1):
        # Convert the DAC offset binary to two's complement.
        data[start:stop] ^= np.int16(-0x8000)


def source_scales(parser: Parser) -> np.ndarray:
    """SI units per machine unit for each stream source (see `Parser.units()`)."""
    scales = np.ones((parser.n_sources, 1))
    for i, decoder in enumerate(parser.decoders):
        decoder.to_si(scales, parser.decoder_endpoints[i],
                      parser.decoder_endpoints[i + 1])
    return scales[:, 0]


def to_machine_units(parser: Parser, data: np.ndarray):
    """Convert raw stream data, as (source, sample) array, to machine units (in place)."""
    for i, decoder in enumerate(parser.decoders):
        decoder.to_mu(data, parser.decoder_endpoints[i], parser.decoder_endpoints[i + 1])
