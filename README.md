# stabilizer-ui
A UI for communicating with and visualising live data streamed from the ARTIQ Stabilizer board.

## Applications:
* `dual_iir`: For Stabilizers running the `dual_iir` binary. 
* `fnc`: For the binary on fibre noise cancellation
* `current_sense`: For the `current_sense` binary (`dual-iir` with a mains-synchronised harmonic
  feedforward on channel 0, for the current sense board)
* `l674`: For the `l674` binary (the 674 nm SolsTiS lock onto its cavity: cascaded fast
  and slow PZT controllers with lock detection), with automatic relocking

They are made for the v0.11 firmware (OxfordIonTrapGroup/stabilizer after the merge of
upstream quartiq/stabilizer, which uses the miniconf-mqtt v0.20 settings protocol).
`dual_iir` and `fnc` also work with the earlier firmware (OxfordIonTrapGroup/stabilizer
`master` before the merge, v0.9 with miniconf 0.9), except for what it does not have: the
run mode (which it only has for both channels together) and the signal source needed for
transfer function measurements. These are greyed out. The UI finds out which version a
device runs when it connects: from the build metadata the firmware keeps on the broker
(firmware built since October 2026), or else by asking the device.

## Getting started
1. Clone this repository and `cd` into it in the terminal. Install [uv](https://docs.astral.sh/uv/).
2. Run `uv sync` to create the Python environment. This also builds the extension for the
   spectral density window (`psd/`, see below), which needs a Rust toolchain (≥ 1.88, e.g.
   from [rustup](https://rustup.rs/); if there is none, maturin downloads one for the build)
   and, on Windows, the MSVC build tools. Where it cannot be built, leave it out with
   `uv sync --no-group psd`, or set `UV_NO_GROUP=psd` in the environment for all uv
   commands. Everything but the spectral density window works without it.
3. Run `uv run stabilizer_ui`. It lists the devices connected to the MQTT broker
   (`10.255.6.4:1883` by default; give others with `--broker HOST[:PORT]`, as often as
   needed) with their name, application and firmware version, and opens the UI for the
   one chosen. *Show disconnected devices* adds those which have a name, and those with
   the v0.9 firmware which have been connected before (the v0.11 firmware leaves no trace
   on the broker when it disconnects). To open a device directly, give its name or MQTT
   ID (its MAC address, unless configured otherwise); a unique part of either is enough:
   ```
   uv run stabilizer_ui lab1_729
   ```
   `--list` prints all the devices found instead. To open a device with the v0.11 firmware
   which is not connected and has no name, give it as `<application>/<ID>` (e.g.
   `dual-iir/44-b7-d0-c7-7d-24`); the UI then waits for it.

   Name a device with *Device → Rename…* in its window. The name is stored on the broker
   (retained, as `ui/name` below the device's topic), so everybody sees it in the window
   title and the list of devices, and recordings and measurements are named after it.

   On the right hand side should be a live stream of the IO data of the stabilizer -- you
   may need to disable some firewall restrictions to get this to work properly.

   The UI of each application can also be launched with `uv run <target>_ui <device_name>`
   for a device entered in `device_db.py`, where `target` is one of the application names
   listed above (e.g. `uv run fnc_ui lab1_729`).

## Scope

The scope shows the last *Duration* of the stream (up to 10 s; the arrows step through 1,
2 and 5 times powers of ten), or with *Enable FFT*, its amplitude spectral density (one
segment of that duration, Hamming window). Long traces are redrawn less often, as the FFT
and drawing take longer. For long-term averaged spectra, use the spectral density window.

## Recording the stream

The bar below the scope records the checked channels of the stream at the full sample rate
to an HDF5 file (for all four channels of `dual_iir`, 6.25 MB/s or 22.5 GB per hour; the bar
shows the rate of the current selection). *Record…* asks for the file, and *Stop* ends the
recording, as does closing the window.

* Each channel is stored as `channels/<name>`, in machine units (`int16`); multiply by its
  `scale` attribute for its `unit`. For the ADCs, this does not include the AFE gain, which
  is in the snapshot of the settings at the start (`settings`, as JSON). The file also has
  the sample period, and the time of the computer when the first data arrived.
* Stream data lost in transmission is zero, and listed in `lost` as (first sample, number
  of samples). If the stream stops for more than 10 s (e.g. while another client has it),
  or the device restarts, the recording stops, so that each file is one continuous time
  series.
* The file is written in SWMR mode, so that it stays readable if the UI quits
  unexpectedly, and can be read while recording:

  ```python
  import h5py
  with h5py.File("stream_lab1_729_2026-10-02T16_24_00.h5", "r", swmr=True) as f:
      adc0 = f["channels/ADC0"]
      adc0.refresh()  # For data written since opening the file.
      volts = adc0[-100_000:] * adc0.attrs["scale"]
  ```

Like the scope, the bar is disabled while the device is disconnected, but a recording
continues as long as the stream does.

## Spectral density

*Tools > Spectral density…* (Ctrl+D) opens a window which estimates the power spectral
density of each signal of the stream while it is open (shown as amplitude spectral
density). It uses the online estimation of
[stabilizer-stream](https://github.com/quartiq/stabilizer-stream) (through the Python
bindings in `psd/`): each stage of a cascade averages the spectra of 512-sample segments,
and decimates the signal by 8 for the next stage, so that the spectrum extends to lower
frequencies the longer it runs, with roughly constant relative resolution.

* Each stage averages up to *Max. averages* spectra, and then continues with an
  exponentially weighted average with that time constant. Lower frequencies are only shown
  once their stage has *Min. averages*. The status says how far the estimate has got.
* *Pause* stops averaging (as does closing the window), *Reset* starts again.
* *Cumulative RMS* shows the RMS above each frequency (the PSD integrated from the
  highest frequency down).
* *Store* keeps the current estimates for comparison. Traces can be renamed, saved as CSV
  and loaded again.

Lost stream data is left out of the estimate (and reported). The window needs the `psd`
dependency group (see *Getting started*).

## Several clients

Several UIs, and other MQTT clients such as scripts, can control the same device at the
same time. This needs firmware built with the minimq fix of the OxfordIonTrapGroup fork
(minimq branch `v0.10-in-flight-limit`). Earlier firmware (including v0.9) can leave
requests unanswered when several clients make them at once, which the UI reports as a
connection error.

The UI always shows the settings the device has:

* The settings are read from the device when the UI starts, and again when the device has
  restarted. They cannot be edited before that, or while the device is offline. Starting a
  UI does not change any settings (apart from directing the stream to it).
* Changes made by another client appear right away. Every setting is read back from the
  device after a change, so a request which the device rejects or modifies shows as what
  the device actually has.
* Rapid edits are coalesced: when a setting is ready to be sent, the UI sends its latest
  value. It does not replay every intermediate widget value.
* A lost connection or unanswered request disables the controls and marks the UI offline.
  Both use the MQTT reconnect path: subscriptions are restored, retained UI state is
  reloaded, and device settings are reread before editing is enabled. Pending edits are
  discarded; commands and measurements are not automatically retried.
* The device gets the settings retained on the broker when it restarts. The UI retains
  every change it makes, and if the device rejects or modifies one, it retains the value
  the device has instead. Other clients have to retain their changes themselves
  (`retain=True` with the miniconf Python client), or a restart undoes them.
* The filter settings (type, gains, …) are stored on the broker, as one topic for each
  filter (`ui/chN/iirM`), and the UI in which they are changed computes the filter
  coefficients for the device from them. (Filter settings stored by earlier versions, as
  a topic for each parameter, are still used for a filter until it is changed.) If the
  coefficients on the device are not those which the settings shown give (for instance
  because a script has written them), a warning appears below the filter settings, with a
  button to write the filter to the device.
  Simultaneous edits can also leave a recipe/coefficients mismatch; clients show the
  warning instead of automatically rewriting coefficients to choose a winner.
* A filter which the firmware does not have (currently, the second one of each channel) is
  greyed out.
* The device streams to one client only: the UI which was started last. The others say so
  in the status bar, where the stream can be taken over, and take it back by themselves
  once the UI receiving it has been closed.

## Feedforward (`current_sense`)

Channel 1 is a regular `dual_iir` channel. Channel 0, which the current sense board is
connected to, additionally has:

* *Current Sense Board*: the frontend offset voltage (offset DAC of the board) and the
  feedback offset current (from auxiliary DAC output 0).
* *Feedforward*: the amplitude and phase of the first five mains harmonics. The firmware
  adds the sum of `amplitude × sin(order × θ + phase)` to the ADC0 samples, where θ is the
  mains phase tracked by the PLL (zero at the rising edge of the reference on DI0). The
  amplitudes are those at the ADC, i.e. after the AFE gain, as shown on the scope. The
  settling times of the PLL are given as powers of two of 10 ns.
* No *External* run mode, as DI0 is the mains reference input.

These are stored as device settings only (there is no separate UI state).

## Laser lock (`l674`)

The window has the lock controls and the filters on the left, and the scope and a log of
the lock monitor (and of any warnings) in tabs on the right.

* *AOM lock*: releases the hold of the Vescent AOM lock (the auxiliary TTL output LVDS7 of
  the EEM connector, low when enabled). F5.
* *SolsTiS PZT lock*: the mode decides what the first filter of each channel is: zero
  (*Disable*, F6), a gain of 5 from ADC0 to the fast PZT with the slow PZT off (*Pass
  through*, F7, for finding the resonance with the Vescent ramp), or the filters designed
  below (*Enable*, F8). The mode is UI state (`ui/lock_mode`, retained on the broker); the
  designed filters stay as they are, so they can be edited while the lock is off. The
  second filter of each channel (e.g. a notch on the fast PZT) is always as designed. The
  *gain ramp time* ramps the error signal gain from zero when the lock is enabled.
  Channel 0 (*Fast PZT*) filters the error signal on ADC0; channel 1 (*Slow PZT*) filters
  the *output* of channel 0, not ADC1.
* *ADC1 input*: ADC1 is the cavity transmission. The lock is detected (LVDS6 high) once
  the transmission has stayed above the *threshold* (in volts at the input, before the
  *AFE gain* of ADC1) for the *holdoff*. The *reading* is the filtered transmission polled
  from the device (`settings/lock_detect/adc1_filtered`, read-only), coloured green when
  locked, red when out of lock, yellow while relocking and grey while unknown.
* *Automatic relocking*: when the transmission drops below the threshold, the laser is
  tuned back to the frequency the WAnD wavemeter read while it was locked (with the
  resonator of the SolsTiS, or its etalon for jumps of more than 1 GHz, through the
  ICE-Bloc web interface; if the lock does not catch after a few attempts, the resonator
  is scanned for the transmission peak), and the AOM and PZT locks are enabled again. The
  WAnD server (`host:port`), its channel and the SolsTiS host are UI state (`ui/relock`,
  retained on the broker, so they are shared by everybody opening the device); the
  command line (`--wand-server`, `--wand-channel`, `--solstis-host`) and the `device_db`
  entry only fill them in while nothing is retained. Relocking is enabled once the
  wavemeter answers, and can be switched off (which cancels a relock in progress). The
  lock state is published for ARTIQ experiments as a sipyco RPC (`l674_lock_ui`:
  `get_lock_state()` is `locked`, `out_of_lock`, `relocking` or `uninitialised`;
  `get_transmission()`), on port 4110 by default (`--port`, `--bind`, as for ARTIQ
  controllers; `--port 0` disables it).
* The filter settings of the earlier `l674` UI (fast/slow P and I gains, the notch) which
  are still retained on the broker are shown until the filters are changed (the fast
  gains were negated in the old UI, so the `pid` filter shows the negated values).

The transfer function and spectral density windows, and recording, work as for
`dual_iir`. For measuring the plant of the fast PZT, excite channel 0; channel 1 does not
see ADC1 (its input is the channel 0 output), so a sweep on it only shows its own filter.

## Transfer function measurements (`dual_iir`, `current_sense`, `l674`)

*Tools > Transfer function…* (Ctrl+T) opens a window for measuring transfer functions with
exponential sine sweeps. The sweep is generated by the signal source of the firmware and
added to the DAC output of the selected channel; the UI captures the full-rate stream
during the sweep and deconvolves the responses of all channels with the (exactly known)
stimulus.

From the ADC and DAC responses of the excited channel, it shows the plant (ADC/DAC), the
loop gain (with crossover frequency and phase margin), the controller (compared to the
designed filters), and the sensitivity functions. With the channel set to *Hold*, this
measures the plant open-loop; with the loop closed, everything is measured in situ. The
dashed lines show the estimated noise of each measurement.

* The sweep runs from the start to the stop frequency in the given duration. Longer sweeps
  give less noise and allow lower start frequencies. Several sweeps can be averaged.
* The *IR window* sets the frequency resolution, and thus the lowest usable frequency (the
  default covers ten periods of the start frequency; results within about an octave of the
  start frequency are less accurate). *Points/decade* sets the smoothing.
* The phase includes the latency from DAC to ADC (about 20 µs); use *Remove delay* to take
  it out.
* Measurements can be overlaid, renamed, saved as HDF5 (raw data, parameters, settings
  snapshot, and results), loaded and re-analysed, and exported as CSV or PNG/SVG plots.
* To estimate the linearity, the Bode plot can also show the 2nd to 4th harmonics (set the
  number under *Harmonics*). They are plotted against the fundamental frequency (the
  harmonic itself is at k times that), and normalised like the fundamental (e.g. for the
  plant, the ADC harmonic relative to the DAC fundamental), so that their distance to it
  is the harmonic distortion; they are only shown where they exceed three times their
  estimated noise. The distortion tab shows the harmonics of each channel in dBc, with
  their noise.
* The impulse response tab shows the windows used for the linear response, the noise
  estimate, and the harmonics.

For `current_sense`, which samples at half the rate, sweeps can go up to 191 kHz. The feedforward
is a disturbance at the mains harmonics for measurements on channel 0, so set its
amplitudes to zero for measurements at low frequencies.

The stream has to reach the UI without significant loss (lost batches are interpolated, and
reported in the status). The signal source settings are not retained on the broker, and the
source is disabled again after each measurement.

## Development
* `uv run poe fmt` formats the code, `uv run poe lint` runs flake8, and `uv run poe test`
  runs the tests.
* `psd/` is a separate package (`stabilizer-psd`, built with maturin) in the `psd`
  dependency group, which `uv sync` installs by default and rebuilds when its sources
  change. It depends on the `portable-lib` branch of the
  OxfordIonTrapGroup fork of stabilizer-stream, which fixes the build of the library on
  macOS and Windows, and makes the dependencies of its GUI optional.
