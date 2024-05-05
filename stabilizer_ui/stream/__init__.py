#!/usr/bin/python3

#: Duration of a single scope trace of streamed data, in seconds.
#: PyQt's drawing speed and the FFT processing step limit this value.
MAX_BUFFER_PERIOD = 0.02

#: Time scale of quantities reported by the stream thread, in seconds.
SCOPE_TIME_SCALE = 1e-3  # Use ms and kHz as units.
