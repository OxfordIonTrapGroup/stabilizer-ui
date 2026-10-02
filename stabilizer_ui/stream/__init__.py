#!/usr/bin/python3

#: Default duration of the scope traces of streamed data, in seconds.
DEFAULT_SCOPE_DURATION = 0.02

#: Longest duration of the scope traces, in seconds. The scope keeps this much data
#: (4 bytes per sample and source), and redraws less often for long durations, as the
#: FFT and drawing take longer.
MAX_SCOPE_DURATION = 10.0

#: Time scale of quantities reported by the stream thread, in seconds.
SCOPE_TIME_SCALE = 1e-3  # Use ms and kHz as units.
