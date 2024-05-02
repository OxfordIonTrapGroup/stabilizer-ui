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
from ...ui_utils import fmt_mac

logger = logging.getLogger(__name__)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps

DEFAULT_WINDOW_SIZE = (1200, 600)


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Interface for the Dual-IIR Stabilizer.")
    parser.add_argument("-b", "--broker-host", default="10.255.6.4")
    parser.add_argument("--broker-port", default=1883, type=int)
    parser.add_argument("--stabilizer-mac", default="80-34-28-5f-4f-5d", help="MAC address of the stabilizer")
    parser.add_argument("--stream-port", default=9293, type=int)
    parser.add_argument("--name", default="FNC", help="Application name")
    parser.add_argument(
        "--log",
        default="INFO",
        choices=["DEBUG, INFO, WARNING, ERROR, CRITICAL"],
        help="Logging level")
    args = parser.parse_args()

    logger.setLevel(logging.getLevelName(args.log))

    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("FNC UI")

    topics.app_root.name = f"{fmt_mac(args.stabilizer_mac)}"

    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)

        ui = UiWindow()
        ui.setWindowTitle(args.name + f" [{fmt_mac(args.stabilizer_mac)}]")
        ui.resize(*DEFAULT_WINDOW_SIZE)
        ui.show()

        ui.set_comm_status(f"Connecting to MQTT broker at {args.broker_host}…")

        stabilizer_interface = StabilizerInterface()

        # Find out which local IP address we are going to direct the stream to.
        # Assume the local IP address is the same for the broker and the stabilizer.
        local_ip = get_local_ip(args.broker_host)

        stream_target = NetworkAddress(local_ip, args.stream_port)
        broker_address = NetworkAddress.from_str_ip(args.broker_host, args.broker_port)

        stabilizer_task = loop.create_task(
            stabilizer_interface.update(ui, broker_address,
                                        ui.set_mqtt_configs(stream_target)))

        stream_thread = StreamThread(
            ui.update_stream,
            ui.fftScopeWidget.precondition_data(),
            SCOPE_UPDATE_PERIOD,
            stream_target,
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
