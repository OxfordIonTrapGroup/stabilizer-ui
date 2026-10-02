# stabilizer-ui
A UI for communicating with and visualising live data streamed from the ARTIQ Stabilizer board.

## Applications:
* `dual_iir`: For Stabilizers running the `dual_iir` binary. 
* `fnc`: For the binary on fibre noise cancellation
* `current_sense`: For the `current_sense` binary (`dual-iir` with a mains-synchronised harmonic
  feedforward on channel 0, for the current sense board)

All require the v0.11 firmware (OxfordIonTrapGroup/stabilizer after the merge of upstream
quartiq/stabilizer, which uses the miniconf-mqtt v0.20 settings protocol).

## Getting started
1. Clone this repository and `cd` into it in the terminal. Install [uv](https://docs.astral.sh/uv/).
2. Run `uv sync` to create the Python environment.
3. Add the `stabilizer` device you wish to connect to in `device_db.py` similar to the existing entries, specifying the MQTT topic, broker address, and firmware application that device is running. 
4. The app can now be launched using `uv run <target>_ui <device_name>`, where `target` is one of the application names listed above and with the `device_name` as entered in the `device_db`. 
   
   For example,
   ```
    uv run fnc_ui lab1_729
   ```

    This should launch the application. On the right hand side should be a live stream of the IO data of the stabilizer -- you may need to disable some firewall restrictions to get this to work properly.

## Scope

The scope shows the last *Duration* of the stream (up to 10 s; the arrows step through 1,
2 and 5 times powers of ten), or with *Enable FFT*, its amplitude spectral density (one
segment of that duration, Hamming window). Long traces are redrawn less often, as the FFT
and drawing take longer.

## Several clients

Several UIs, and other MQTT clients such as scripts, can control the same device at the
same time. This needs firmware built with the minimq fix of the OxfordIonTrapGroup fork
(minimq branch `v0.10-in-flight-limit`). Earlier firmware can leave requests unanswered
when several clients make them at once, which the UI reports as a connection error.

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

## Transfer function measurements (`dual_iir`, `current_sense`)

*Tools > Transfer function…* (Ctrl+T) opens a window for measuring transfer functions with
exponential sine sweeps. The sweep is generated by the signal source of the firmware and
added to the DAC output of the selected channel; the UI captures the full-rate stream
during the sweep and deconvolves the responses of all channels with the (exactly known)
stimulus.

From the ADC and DAC responses of the excited channel, it shows the plant (ADC/DAC), the
loop gain (with crossover frequency and phase margin), the controller (compared to the
designed filter), and the sensitivity functions. With the channel set to *Hold*, this
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
