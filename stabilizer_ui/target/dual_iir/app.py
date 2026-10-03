from .ui import UiWindow
from .interface import StabilizerInterface
from . import topics

from ...app import run_from_device_db

__all__ = ["UiWindow", "StabilizerInterface", "topics", "TITLE", "main"]

#: The name of the application in window titles.
TITLE = "Dual_IIR"


def main():
    run_from_device_db("dual_iir", "Interface for the Dual-IIR Stabilizer.")


if __name__ == "__main__":
    main()
