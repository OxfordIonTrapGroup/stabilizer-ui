"""The 674 nm laser lock UI: `l674_ui DEVICE_NAME` (from the device database), or through
`stabilizer_ui`.

Besides the window, the target runs the lock monitor (which relocks the laser, see
`lock.py`) and a sipyco RPC server publishing the lock state for ARTIQ experiments
(`l674_lock_ui.get_lock_state()`).
"""
import argparse
import asyncio
import logging
from typing import Callable, Optional

from .ui import UiWindow
from .interface import StabilizerInterface
from . import topics

from ...app import run_from_device_db

__all__ = [
    "UiWindow", "StabilizerInterface", "topics", "TITLE", "main", "add_arguments",
    "device_db_defaults", "start"
]

logger = logging.getLogger(__name__)

#: The name of the application in window titles.
TITLE = "674 lock"

#: Default port of the lock state RPC server.
DEFAULT_RPC_PORT = 4110


class UIStatePublisher:
    """The RPC target (`l674_lock_ui`) for ARTIQ experiments."""

    def __init__(self, ui: UiWindow):
        self.ui = ui

    def get_lock_state(self) -> str:
        """The lock state: `locked`, `out_of_lock`, `relocking` or `uninitialised`."""
        return self.ui.lock_state.name

    def get_transmission(self) -> Optional[float]:
        """The last transmission reading, in volts at the ADC1 input (`None` if there is
        none)."""
        return self.ui.transmission

    def ping(self) -> bool:
        return True


def add_arguments(parser):
    """The command line arguments of this target (see `stabilizer_ui.app`)."""
    parser.add_argument(
        "--wand-server",
        metavar="HOST:PORT",
        help="WAnD wavemeter server for relocking (default: as retained on the broker, "
        "or from the device database)")
    parser.add_argument("--wand-channel",
                        metavar="NAME",
                        help="Laser (channel) of the wavemeter server")
    parser.add_argument("--solstis-host",
                        metavar="HOST[:PORT]",
                        help="SolsTiS ICE-Bloc controller")
    # As `sipyco.common_args.simple_network_args()`, for the lock state RPC server.
    parser.add_argument("--bind",
                        default=[],
                        action="append",
                        help="additional hostname or IP address to bind the lock state "
                        "RPC server to; use '*' to bind to all interfaces (default: "
                        "localhost only)")
    parser.add_argument("--no-localhost-bind",
                        default=False,
                        action="store_true",
                        help="do not implicitly also bind the RPC server to localhost")
    parser.add_argument("-p",
                        "--port",
                        default=DEFAULT_RPC_PORT,
                        type=int,
                        help=f"TCP port of the lock state RPC server (default: "
                        f"{DEFAULT_RPC_PORT}; 0 disables it)")


def device_db_defaults(args: argparse.Namespace, entry: dict):
    """Fill in the relocking parameters from the device database entry."""
    address = entry.get("wand-address")
    if args.wand_server is None and address is not None:
        args.wand_server = f"{address.get_ip()}:{address.port}"
    if args.wand_channel is None:
        args.wand_channel = entry.get("wand-channel")
    if args.solstis_host is None:
        args.solstis_host = entry.get("solstis-host")


def start(ui: UiWindow, interface: StabilizerInterface, loop: asyncio.AbstractEventLoop,
          args: Optional[argparse.Namespace]) -> Callable[[], None]:
    """Start the lock monitor and the RPC server; returns the function to stop them."""
    if args is not None:
        ui.set_relock_defaults(args.wand_server, args.wand_channel, args.solstis_host)
    ui.start_lock_monitor(interface)

    server = None
    port = DEFAULT_RPC_PORT if args is None else args.port
    if port:
        try:
            from sipyco import common_args, pc_rpc
        except ImportError:
            logger.error("sipyco is not installed; no lock state RPC server")
        else:
            if args is None:
                bind = ["127.0.0.1", "::1"]
            else:
                bind = common_args.bind_address_from_args(args)
            server = pc_rpc.Server({"l674_lock_ui": UIStatePublisher(ui)},
                                   "Publishes the state of the 674 nm laser lock UI")
            try:
                loop.run_until_complete(server.start(bind, port))
                logger.info("Lock state RPC server on port %d", port)
            except OSError as e:
                logger.error("Failed to start the lock state RPC server: %s", e)
                server = None

    def cleanup():
        ui.stop_lock_monitor()
        if server is not None:
            loop.run_until_complete(server.stop())

    return cleanup


def main():
    run_from_device_db("l674",
                       "Interface for the 674 nm laser lock (Vescent + Stabilizer).")


if __name__ == "__main__":
    main()
