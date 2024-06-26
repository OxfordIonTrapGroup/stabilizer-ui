import argparse
import asyncio
import logging
import sys

from contextlib import suppress
from PyQt5 import QtWidgets
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .interface import StabilizerInterface
from .ui import UiWindow
from . import topics

from ...stream.thread import StreamThread
from ...mqtt import NetworkAddress
from ...utils import fmt_mac, AsyncQueueThreadsafe

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Interface for the Dual-IIR Stabilizer.")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac",
                        default="80-34-28-5f-4f-5d",
                        help="MAC address of the stabilizer")
    parser.add_argument("--stream-port", default=0, type=int)
    parser.add_argument("--name", default="FNC", help="Application name")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("FNC UI")

    topics.app_root.name = f"{fmt_mac(args.stabilizer_mac)}"

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UiWindow()
        ui.setWindowTitle(args.name + f" [{fmt_mac(args.stabilizer_mac)}]")
        ui.show()

        ui.set_comm_status(f"Connecting to MQTT broker at {args.broker_host}…")

        stabilizer_interface = StabilizerInterface()

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)

        requested_stream_target = NetworkAddress(local_ip, args.stream_port)
        broker_address = NetworkAddress.from_str_ip(args.broker_host, args.broker_port)

        stream_target_queue = AsyncQueueThreadsafe(loop, maxsize=1)
        stream_target_queue.put_nowait(requested_stream_target)

        stabilizer_task = loop.create_task(
            stabilizer_interface.update(ui, broker_address, stream_target_queue))

        stream_thread = StreamThread(
            ui.update_stream,
            ui.fftScopeWidget,
            stream_target_queue,
            broker_address,
            loop,
        )
        stream_thread.start()

        try:
            sys.exit(loop.run_forever())
        finally:
            stream_thread.close()
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
