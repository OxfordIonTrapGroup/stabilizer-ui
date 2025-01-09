# stabilizer-ui
A UI for communicating with and visualising live data streamed from the ARTIQ Stabilizer board.

## Applications:
* `dual_iir`: For Stabilizers running the `dual_iir` binary. 
* `l674_lock`: For the `l674` binary available [here](https://github.com/OxfordIonTrapGroup/stabilizer/tree/l674)
* `fnc`: For the binary on fibre noise cancellation

## Getting started
1. Clone this repository and `cd` into it in the terminal. Install [Poetry](https://python-poetry.org/docs/). 
2. Run `poetry install` and `poetry shell` to activate the python environment.
3. Add the `stabilizer` device you wish to connect to in `device_db.py` similar to the existing entries, specifying the MQTT topic, broker address, and firmware application that device is running. 
4. The app can now be launched using `<target>_ui <device_name>`, where `target` is one of the application names listed above and with the `device_name` as entered in the `device_db`. 
   
   For example,
   ```
    fnc_ui lab1_729
   ```

    This should launch the application. On the right hand side should be a live stream of the IO data of the stabilizer -- you may need to disable some firewall restrictions to get this to work properly.