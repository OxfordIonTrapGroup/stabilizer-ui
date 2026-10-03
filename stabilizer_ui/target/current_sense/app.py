from .ui import UiWindow
from .interface import StabilizerInterface
from . import topics

from ...app import run_from_device_db

__all__ = ["UiWindow", "StabilizerInterface", "topics", "TITLE", "main"]

#: The name of the application in window titles.
TITLE = "Current sense"


def main():
    run_from_device_db("current_sense", "Interface for the Current sense Stabilizer.")


if __name__ == "__main__":
    main()
