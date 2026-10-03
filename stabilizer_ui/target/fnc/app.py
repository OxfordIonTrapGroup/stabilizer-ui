from .ui import UiWindow
from .interface import StabilizerInterface
from . import topics

from ...app import run_from_device_db

__all__ = ["UiWindow", "StabilizerInterface", "topics", "TITLE", "main"]

#: The name of the application in window titles.
TITLE = "FNC"


def main():
    run_from_device_db("fnc", "Interface for the FNC Stabilizer.")


if __name__ == "__main__":
    main()
