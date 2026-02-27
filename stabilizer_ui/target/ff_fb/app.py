import argparse
import asyncio
import logging
import sys

from contextlib import suppress
from PyQt6 import QtWidgets
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .ui import UiWindow
from .interface import StabilizerInterface
from . import topics

from ...stream.thread import StreamThread
from ...mqtt import NetworkAddress
from ...utils import fmt_mac, AsyncQueueThreadsafe
from ...device_db import stabilizer_devices

logger = logging.getLogger(__name__)

# On Windows, explicitly use the selector-based event loop
# to ensure compatibility with qasync + PyQt.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def main():
    """
    Entry point for the FF_FB Stabilizer UI.

    Responsibilities:
    - Parse CLI arguments
    - Validate device configuration
    - Initialize Qt + asyncio event loop
    - Start MQTT interface task
    - Start UDP streaming thread
    - Run UI event loop
    """
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Interface for the FF_FB Stabilizer.")
    parser.add_argument("stabilizer_name",
                        metavar="DEVICE_NAME",
                        type=str,
                        help="Stabilizer name as entered in the device database")
    parser.add_argument("--stream-port", default=0, type=int)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        
    # Retrieve device configuration from local device database.
    try:
        stabilizer = stabilizer_devices[args.stabilizer_name]
    except KeyError:
        logger.error(f"Device '{args.stabilizer_name}' not found in device database.")
        sys.exit(1)
        
    # Ensure this device is configured to run the ff_fb firmware
    if stabilizer["application"] != "ff_fb":
        logger.error(
            f"Device '{args.stabilizer_name}' is not listed as running ff_fb.")
        sys.exit(1)
        
    # Configure MQTT topic root based on device network ID or MAC.
    topics.app_root.name = stabilizer.get("net_id", fmt_mac(stabilizer["mac-address"]))
    broker_address = stabilizer["broker"]

    # Find out which local IP address we are going to direct the stream to.
    # Assume the local IP address is the same for the broker and the stabilizer.
    local_ip = get_local_ip(broker_address.get_ip())
    requested_stream_target = NetworkAddress(local_ip, args.stream_port)
    
    # Initialize Qt application.
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("Stabilizer UI")
    
    # Use qasync to integrate Qt event loop with asyncio.
    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)
        # Create main UI window.
        ui = UiWindow(f"Dual_iir_w_ff_fb [{args.stabilizer_name}]")
        ui.show()
        ui.update_comm_status(True,
                              f"Connecting to MQTT broker at {broker_address.get_ip()}…")
         # Create high-level stabilizer interface (MQTT control layer).
        stabilizer_interface = StabilizerInterface()
        # Queue used to communicate stream target changes between
        # asyncio task and streaming thread.
        stream_target_queue = AsyncQueueThreadsafe(maxsize=1)
        stream_target_queue.put_nowait(requested_stream_target)
        
        # Start asynchronous MQTT update task.
        stabilizer_task = loop.create_task(
            stabilizer_interface.update(ui, broker_address, stream_target_queue))
        
        # Start background UDP stream receiver thread.
        #
        # This receives ADC/DAC data and forwards it to:
        # - UI plotting callbacks
        # - FFT widget
        stream_thread = StreamThread(
            ui.update_stream,
            ui.fftScopeWidget,
            stream_target_queue,
            broker_address,
            loop,
        )
        stream_thread.start()

        try:
            # Start Qt/async event loop.
            sys.exit(loop.run_forever())
        finally:
             # Ensure stream thread is closed cleanly.
            stream_thread.close()
            # Cancel MQTT task
            with suppress(asyncio.CancelledError):
                stabilizer_task.cancel()
                loop.run_until_complete(stabilizer_task)


if __name__ == "__main__":
    main()
