# Number of IO channels on stabilizer
NUM_CHANNELS = 2
# Number of IIR filters per channel
NUM_IIR_FILTERS_PER_CHANNEL = 2
# Number of harmonics (fundamental included) in the feedforward waveform
NUM_HARMONICS = 5
# Sample period in seconds: 128 ticks of the 100 MHz timer clock, as for `dual-iir`
# (not in the `stabilizer` package)
SAMPLE_PERIOD = 10e-9 * 128
