from setuptools import find_packages, setup

setup(
    name="l674-lock-ui",
    version="0.1.0",
    url="https://github.com/dnadlinger/l674-lock-ui",
    description="l674 lock Stabilizer GUI (via MQTT), including automatic relocking",
    author="David Nadlinger",
    packages=find_packages(),
    dependencies=["gmqtt", "PyQt5", "qasync", "sipyco", "websockets", "pyqtgraph"],
    entry_points={"gui_scripts": ["l674_lock_ui = l674_lock_ui.app:main"]},
)
