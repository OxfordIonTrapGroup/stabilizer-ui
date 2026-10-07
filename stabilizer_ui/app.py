"""Opens the UI for a Stabilizer, found by asking MQTT brokers which devices there are.

The application (the UI target) and the firmware version of a device are found out from
what it publishes (and, for firmware which does not retain its build metadata, answers),
so there is no need for a device database.
"""
import argparse
import asyncio
import importlib
import logging
import sys

from contextlib import suppress
from typing import Optional
from PyQt6 import QtCore, QtWidgets
from qasync import QEventLoop
from stabilizer.stream import get_local_ip

from .discovery import TARGETS, Device, discover, parse_broker
from .firmware import CURRENT, Firmware
from .mqtt import NetworkAddress
from .stream.thread import StreamThread
from .transfer_function.measurement import SweepRunner
from .utils import AsyncQueueThreadsafe, fmt_mac

logger = logging.getLogger(__name__)

#: The brokers to search for devices by default.
DEFAULT_BROKERS = ["10.255.6.4:1883"]


def create_application() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication(sys.argv)
    app.setOrganizationName("Oxford Ion Trap Quantum Computing group")
    app.setOrganizationDomain("photonic.link")
    app.setApplicationName("Stabilizer UI")
    return app


def target_module(target: str):
    """The `app` module of a target (in `stabilizer_ui.target`).

    Besides `UiWindow`, `StabilizerInterface`, `topics` and `TITLE`, it may define
    `add_arguments(parser)` for command line arguments of its own,
    `device_db_defaults(args, entry)` to fill them in from a `device_db` entry, and
    `start(ui, interface, loop, args)`, which `run_ui()` calls before running the event
    loop (for tasks and servers of the target), and which returns a function to call when
    the UI is closed (or `None`).
    """
    return importlib.import_module(f"stabilizer_ui.target.{target}.app")


def add_target_arguments(parser: argparse.ArgumentParser, targets: list[str]):
    """Add the command line arguments of the given targets (modules in
    `stabilizer_ui.target`) to `parser`, in a group each."""
    for target in targets:
        module = target_module(target)
        add_arguments = getattr(module, "add_arguments", None)
        if add_arguments is not None:
            add_arguments(parser.add_argument_group(f"{target} options"))


def run_ui(loop: QEventLoop,
           target: str,
           broker_address: NetworkAddress,
           device_id: str,
           name: str,
           stream_port: int = 0,
           firmware: Firmware = CURRENT,
           args: Optional[argparse.Namespace] = None):
    """Show the UI of `target` (a module in `stabilizer_ui.target`) for a device, and run
    until it is closed (exiting the process).

    :param name: The name of the device (empty if it has none). The window follows the
        name retained on the broker, and the user can change it.
    :param firmware: The firmware the device is expected to run (found out again anyway).
    :param args: The parsed command line arguments (including those of the target, see
        `target_module()`), if any.
    """
    module = target_module(target)
    module.topics.app_root.name = device_id

    # Find out which local IP address we are going to direct the stream to.
    # Assume the local IP address is the same for the broker and the stabilizer.
    local_ip = get_local_ip(broker_address.get_ip())
    requested_stream_target = NetworkAddress.from_str_ip(local_ip, stream_port)

    ui = module.UiWindow(module.TITLE)
    ui.set_device(device_id, name)
    ui.show()

    ui.update_comm_status(True,
                          f"Connecting to MQTT broker at {broker_address.get_ip()}…")
    stabilizer_interface = module.StabilizerInterface()

    stream_target_queue = AsyncQueueThreadsafe(loop, maxsize=1)
    stream_target_queue.put_nowait(requested_stream_target)

    stabilizer_task = loop.create_task(
        stabilizer_interface.update(ui, broker_address, stream_target_queue, firmware))

    stream_thread = StreamThread(
        ui.update_stream,
        ui.fftScopeWidget,
        stream_target_queue,
        broker_address,
        loop,
    )
    stream_thread.start()
    ui.set_stream_thread(stream_thread, ui.device_label)

    if hasattr(ui, "set_sweep_runner"):
        ui.set_sweep_runner(
            SweepRunner(stabilizer_interface, stream_thread, ui.device_label,
                        ui.afe_gains, ui.settings_snapshot))

    start = getattr(module, "start", None)
    cleanup = None
    if start is not None:
        cleanup = start(ui, stabilizer_interface, loop, args)

    try:
        sys.exit(loop.run_forever())
    finally:
        stream_thread.close()
        if cleanup is not None:
            cleanup()
        with suppress(asyncio.CancelledError):
            stabilizer_task.cancel()
            loop.run_until_complete(stabilizer_task)


def run_from_device_db(target: str, description: str):
    """The entry point of a target: run its UI for a device of `device_db`."""
    from .device_db import stabilizer_devices

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("stabilizer_name",
                        metavar="DEVICE_NAME",
                        type=str,
                        help="Stabilizer name as entered in the device database")
    parser.add_argument("--stream-port", default=0, type=int)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    add_target_arguments(parser, [target])
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("stabilizer_ui").setLevel(logging.DEBUG)

    try:
        stabilizer = stabilizer_devices[args.stabilizer_name]
    except KeyError:
        logger.error(f"Device '{args.stabilizer_name}' not found in device database.")
        sys.exit(1)

    if stabilizer["application"] != target:
        logger.error(
            f"Device '{args.stabilizer_name}' is not listed as running {target}.")
        sys.exit(1)

    device_id = stabilizer.get("net_id", fmt_mac(stabilizer["mac-address"]))
    broker_address = stabilizer["broker"]
    defaults = getattr(target_module(target), "device_db_defaults", None)
    if defaults is not None:
        defaults(args, stabilizer)

    app = create_application()
    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)
        firmware = loop.run_until_complete(
            _expected_firmware(broker_address, target, device_id))
        run_ui(loop, target, broker_address, device_id, args.stabilizer_name,
               args.stream_port, firmware, args)


async def _expected_firmware(broker_address: NetworkAddress, target: str,
                             device_id: str) -> Firmware:
    """The firmware of the device, if it can be found out now (otherwise the current
    one)."""
    app = next(app for app, module in TARGETS.items() if module == target)
    devices = await discover([broker_address],
                             lambda d: d.app == app and d.id == device_id)
    if devices and devices[0].firmware is not None:
        return devices[0].firmware
    return CURRENT


class DeviceDialog(QtWidgets.QDialog):
    """Lists the devices on the brokers, to choose the one to open."""

    COLUMNS = ["Name", "ID", "Application", "Firmware", "Status", "Broker"]

    DISCONNECTED_TOOLTIP = (
        "<p>Also list devices which are not connected to the broker right now.</p>"
        "<p>The broker (the server through which the devices and UIs exchange messages) "
        "keeps the last status message each device has sent, and hands it to anyone who "
        "connects later. A device announces \"connected\" when it connects, and leaves "
        "another message with the broker, to be sent on its behalf if the connection is "
        "lost. With the earlier firmware (v0.9), that message says \"disconnected\", so "
        "the device stays in the list after it has been switched off or unplugged, "
        "possibly for good. The current firmware (v0.11) erases the status instead, so "
        "its devices drop off the list, unless they have been given a name (which the "
        "broker keeps as well; see Device → Rename… in the window of a device).</p>"
        "<p>To open a disconnected device which is not listed (the UI then waits for it "
        "to connect), give it as &lt;application&gt;/&lt;ID&gt; on the command line, "
        "e.g. <code>stabilizer_ui dual-iir/44-b7-d0-c7-7d-24</code>.</p>")

    def __init__(self, brokers: list[NetworkAddress]):
        super().__init__()
        self.setWindowTitle("Stabilizer UI")
        self.resize(640, 320)
        self._brokers = brokers
        self._brokers_text = ", ".join(f"{b.get_ip()}:{b.port}" for b in brokers)
        #: The devices found, and those of them listed.
        self._devices: list[Device] = []
        self._shown: list[Device] = []

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._open())

        self.disconnected_box = QtWidgets.QCheckBox("Show disconnected devices")
        self.disconnected_box.setToolTip(self.DISCONNECTED_TOOLTIP)
        self.disconnected_box.toggled.connect(self._show_devices)

        self.status_label = QtWidgets.QLabel()

        buttons = QtWidgets.QDialogButtonBox()
        self.open_button = buttons.addButton(
            "Open", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.open_button.setEnabled(False)
        self.refresh_button = buttons.addButton(
            "Refresh", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.open_button.clicked.connect(self._open)
        self.refresh_button.clicked.connect(self.refresh)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.disconnected_box)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

        self.refresh()

    def refresh(self):
        self.status_label.setText(f"Searching {self._brokers_text}…")
        self.refresh_button.setEnabled(False)
        asyncio.ensure_future(self._refresh())

    async def _refresh(self):
        try:
            self._devices = await discover(self._brokers)
        finally:
            self.refresh_button.setEnabled(True)
        self._show_devices()

    def _show_devices(self):
        show_disconnected = self.disconnected_box.isChecked()
        self._shown = [
            device for device in self._devices
            if show_disconnected or device.alive is not False
        ]
        self.table.clearSelection()
        self.table.setRowCount(len(self._shown))
        for row, device in enumerate(self._shown):
            status = {True: "connected", False: "disconnected", None: "?"}[device.alive]
            for column, text in enumerate([
                    device.name, device.id,
                    device.app if device.target else f"{device.app} (no UI)",
                    device.firmware_label, status,
                    f"{device.broker.get_ip()}:{device.broker.port}"
            ]):
                item = QtWidgets.QTableWidgetItem(text)
                if column == self.COLUMNS.index("Firmware"):
                    item.setToolTip(device.firmware_details)
                if device.target is None:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

        disconnected = sum(device.alive is False for device in self._devices)
        if show_disconnected:
            text = f"{len(self._shown)} devices on {self._brokers_text}"
            if disconnected:
                text += f" ({disconnected} of them disconnected)"
        else:
            text = f"{len(self._shown)} connected devices on {self._brokers_text}"
            if disconnected:
                text += f" ({disconnected} disconnected not shown)"
        self.status_label.setText(text + ".")
        self._selection_changed()

    def selected(self) -> Optional[Device]:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        device = self._shown[rows.pop()]
        return device if device.target is not None else None

    def _selection_changed(self):
        self.open_button.setEnabled(self.selected() is not None)

    def _open(self):
        if self.selected() is not None:
            self.accept()


async def choose_device(brokers: list[NetworkAddress]) -> Optional[Device]:
    """Let the user choose a device in a `DeviceDialog`."""
    dialog = DeviceDialog(brokers)
    result = asyncio.get_running_loop().create_future()

    def finished(code):
        accepted = code == QtWidgets.QDialog.DialogCode.Accepted
        result.set_result(dialog.selected() if accepted else None)

    dialog.finished.connect(finished)
    dialog.show()
    return await result


async def find_device(brokers: list[NetworkAddress], spec: str) -> Device:
    """The device given as `[<app>/]<name or ID>`. A part of the name (in any case) or of
    the ID is enough if it is unique; a whole name or ID takes precedence over parts.
    Raises `LookupError` if there is no (or more than one) such device."""
    app, _, device_id = spec.rpartition("/")

    def matches(device: Device):
        return app in ("", device.app) and (device_id in device.id
                                            or device_id.lower() in device.name.lower())

    devices = await discover(brokers, matches)
    exact = [
        device for device in devices
        if device_id == device.id or device_id.lower() == device.name.lower()
    ]
    devices = exact or devices
    if len(devices) > 1:
        raise LookupError(f"Several devices match '{spec}': " +
                          ", ".join(f"{d} on {d.broker.get_ip()}" for d in devices))
    if devices:
        return devices[0]
    if app and len(brokers) == 1:
        # Disconnected devices of firmware v0.11 leave no trace.
        logger.warning("'%s' is not connected to %s, waiting for it", spec,
                       brokers[0].get_ip())
        return Device(brokers[0], app, device_id)
    raise LookupError(
        f"No device matching '{spec}' on {', '.join(b.get_ip() for b in brokers)}. "
        "(Give a device which is not connected as <app>/<ID>, with one broker.)")


def main():
    parser = argparse.ArgumentParser(
        prog="stabilizer_ui",
        description="Opens the UI for a Stabilizer, found on the MQTT brokers.")
    parser.add_argument(
        "device",
        metavar="DEVICE",
        nargs="?",
        help="The name of the device or its MQTT ID (its MAC address, unless "
        "configured otherwise; a unique part of either is enough), optionally "
        "as <app>/<ID> (e.g. dual-iir/fc-0f-e7-23-d5-6e). Without, the "
        "devices found are listed to choose from.")
    parser.add_argument(
        "--broker",
        action="append",
        metavar="HOST[:PORT]",
        help="MQTT broker to search (can be given several times; default: "
        f"{', '.join(DEFAULT_BROKERS)})")
    parser.add_argument("--list",
                        action="store_true",
                        help="List the devices found and exit")
    parser.add_argument("--stream-port", default=0, type=int)
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    add_target_arguments(parser, sorted(set(TARGETS.values())))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.debug:
        logging.getLogger("stabilizer_ui").setLevel(logging.DEBUG)
    brokers = [parse_broker(broker) for broker in args.broker or DEFAULT_BROKERS]

    if args.list:
        for device in asyncio.run(discover(brokers)):
            status = {True: "connected", False: "disconnected", None: "?"}[device.alive]
            print(f"{device.name or '-'}\t{device.id}\t{device.app}\t"
                  f"{device.firmware_label}\t{status}\t"
                  f"{device.broker.get_ip()}:{device.broker.port}")
        return

    app = create_application()
    with QEventLoop(app) as loop:
        asyncio.set_event_loop(loop)
        if args.device is None:
            device = loop.run_until_complete(choose_device(brokers))
            if device is None:
                return
        else:
            try:
                device = loop.run_until_complete(find_device(brokers, args.device))
            except LookupError as e:
                logger.error("%s", e)
                sys.exit(1)
            if device.target is None:
                logger.error("There is no UI for %s", device.app)
                sys.exit(1)
        run_ui(loop, device.target, device.broker, device.id, device.name,
               args.stream_port, device.firmware or CURRENT, args)


if __name__ == "__main__":
    main()
