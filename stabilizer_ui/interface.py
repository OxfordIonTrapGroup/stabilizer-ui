import stabilizer
import asyncio
import logging
import json

from typing import Any, Optional
from gmqtt import Message as MqttMessage
from PyQt6.QtWidgets import QWidget

from .ui import AbstractUiWindow
from .firmware import CURRENT, Firmware
from .mqtt import (DEVICE_NAME_KEY, MiniconfError, MqttInterface, NetworkAddress,
                   UiMqttBridge, UiMqttConfig, set_will, values_match)
from .iir.filters import settings_coefficients
from .topic_tree import TopicTree

logger = logging.getLogger(__name__)

Y_MAX = stabilizer.voltage_to_machine_units(stabilizer.DAC_FULL_SCALE)

#: Time to wait for a device which has just connected to the broker to start publishing
#: its settings, in seconds. (It does so 2 s after having subscribed to them.)
SETTINGS_DUMP_TIMEOUT = 5.0

#: Time after the last setting published by a device which has just connected until we
#: start making requests, in seconds.
SETTINGS_DUMP_QUIET = 1.0

#: Time to wait for the device to stop the stream when closing, in seconds.
CLOSE_TIMEOUT = 1.0


class FirmwareChanged(ConnectionError):
    """The device runs another firmware than the will message is for."""


class AbstractStabilizerInterface:
    """
    Shim for controlling stabilizer over MQTT

    Several clients can control the same device, so what the UI shows follows the device
    and the broker, rather than the other way round:

    * Only what the device reports is shown for the device settings (`settings/...`),
      as it can refuse or modify a request. They are read when connecting (and after the
      device has reconnected) instead of being written, and read back after each
      request, by us or by another client. If the device has rejected or modified one
      of our (retained) requests, we retain its value instead, which is what it is to
      have after restarting.
    * The UI state (`ui/...`) is retained on the broker, and the changes other clients
      publish are shown (see `UiMqttBridge`). Only the client which made a change writes
      the biquad coefficients following from it. All clients compare the coefficients
      on the device to those their UI state gives, and show a warning if they differ.
    * The stream can only go to one client. The last client to start takes it, and the
      others take it back once nobody receives it.

    The firmware of the device is detected each time it connects (see
    `MqttInterface.detect_firmware()`). The widgets of the settings it does not have
    are disabled.
    """

    def __init__(self, sample_period: float, app_root: TopicTree):
        self._interface: Optional[MqttInterface] = None
        self.sample_period = sample_period
        self.app_root = app_root

        self._bridge: Optional[UiMqttBridge] = None
        self._ui: Optional[AbstractUiWindow] = None
        self._stream_key = "settings/stream"
        #: The address we receive the stream at, as in the stream target setting.
        self._stream_target = ""
        #: Whether the stream of the device was directed here when last read.
        self._owns_stream = False
        #: Whether we have requested the stream since last reading its target.
        self._stream_requested = False

        #: Whether the device is connected to the broker, and ready for requests.
        self._device_ready = False
        self._syncing = True
        self._connect_timer: Optional[asyncio.TimerHandle] = None
        #: Device settings to read.
        self._keys_to_read = set()
        #: The values of our retained requests which are still to be read back.
        self._retained = dict[str, Any]()

        #: The UI state of each biquad (`ui/chN/iirM`), by the key of its coefficients
        #: on the device.
        self._iirs = dict[str, TopicTree]()
        #: The value of each biquad on the device (or the error from reading it).
        self._device_biquads = dict[str, Any]()

        #: The firmware of the device, once detected after it has connected.
        self._firmware: Optional[Firmware] = None
        #: The firmware the will message is for.
        self._will_firmware: Firmware = CURRENT
        #: The widgets disabled because the firmware does not have their setting, with
        #: their tooltips.
        self._unavailable = dict[QWidget, str]()

    async def change(self, setting):
        """Write a bound setting, compiling filter recipes to raw coefficients."""
        if setting.app_root().name == "settings":
            await self.request_settings_change(setting.path(), setting.value)
        else:
            self._interface.publish(setting.path(), setting.value, retain=True)
            if iir := setting.get_parent_until(lambda node: node.name.startswith("iir")):
                await self._change_filter_setting(iir)

    async def update(self,
                     ui: AbstractUiWindow,
                     broker_address: NetworkAddress,
                     stream_target_queue: asyncio.Queue,
                     firmware: Firmware = CURRENT):
        """Run the session with the device.

        `firmware` is what the device is expected to run (e.g. as found by
        `discovery`). It is only used until the device has been asked, but the session
        then has to reconnect to the broker if it is wrong.
        """
        self._ui = ui
        self._will_firmware = firmware
        ui.set_settings_enabled(False)
        # Wait for the stream thread to read the initial port.
        # A bit hacky, would ideally use a join but that seems to lead to a deadlock.
        # TODO: Get rid of this hack.
        await asyncio.sleep(1)

        # Wait for stream target to be set
        stream_target = await stream_target_queue.get()
        stream_target_queue.task_done()
        logger.debug("Got stream target from stream thread.")

        settings_map = ui.set_mqtt_configs(stream_target)
        # Written when the user renames the device (`deviceRenamed`).
        settings_map[DEVICE_NAME_KEY] = UiMqttConfig(
            [], lambda _: ui.device_name,
            lambda _, name: ui.set_device_name(name if isinstance(name, str) else ""))
        self.app_root.get_or_create_child(DEVICE_NAME_KEY)
        self._stream_target = str(stream_target)

        try:
            bridge = await UiMqttBridge.new(broker_address,
                                            settings_map,
                                            will_message=self._will_message(firmware))
            ui.update_comm_status(
                True, f"Connected to MQTT broker at {broker_address.get_ip()}.")

            self._bridge = bridge
            self._connect_ui(ui, bridge)

            interface = MqttInterface(bridge.client,
                                      self.app_root.path(),
                                      timeout=10.0,
                                      fallback_handler=bridge.handle_message,
                                      on_error=bridge.interrupt)
            interface.firmware = firmware
            self._interface = interface
            take_stream = True
            while True:
                await bridge.connected.wait()
                try:
                    bridge.keys_to_write.clear()
                    self._retained.clear()
                    await bridge.load_ui(self.app_root.path(), ui, interface)
                    self._syncing = True
                    self._keys_to_read.update(key for key in bridge.configs
                                              if key.startswith("settings/"))
                    self._keys_to_read.update(self._iirs)
                    if take_stream:
                        bridge.queue_write(self._stream_key)
                        take_stream = False
                    bridge.updated.set()
                    while True:
                        await bridge.updated.wait()
                        bridge.updated.clear()
                        interface.check_connection()
                        while await self._step():
                            interface.check_connection()
                except FirmwareChanged:
                    # For the new will message.
                    await bridge.client.reconnect()
                except (ConnectionError, TimeoutError):
                    # gmqtt also calls reconnect on transport loss, and prevents
                    # overlapping reconnects. Request failures use exactly this path.
                    await bridge.client.reconnect(delay=True)

        except asyncio.CancelledError:
            pass
        except Exception as error:
            ui.update_comm_status(False, f"Stabilizer connection error: {error!r}")
            ui.set_settings_enabled(False)
            logger.exception("Stabilizer communication failure")
        finally:
            await self._close()

    def _disconnected(self, error):
        """Forget pending edits and disable controls until a full resynchronisation."""
        self._device_ready = False
        self._syncing = True
        # The device might have been updated when it connects again.
        self._firmware = None
        if self._connect_timer is not None:
            self._connect_timer.cancel()
            self._connect_timer = None
        self._keys_to_read.clear()
        self._retained.clear()
        self._device_biquads.clear()
        self._stream_requested = False
        self._ui.set_settings_enabled(False)
        self._ui.update_comm_status(False, f"{error}. Reconnecting…")
        self._ui.update_stream_status("Reconnecting…")

    def _connect_ui(self, ui: AbstractUiWindow, bridge: UiMqttBridge):
        bridge.on_disconnect = self._disconnected
        bridge.on_device_value = self._device_value_published
        bridge.on_settings_request = self._settings_request_seen
        bridge.on_ui_value = self._ui_value_received
        bridge.on_alive = self._alive_changed
        bridge.connect_ui()

        ui.streamTakeOverButton.clicked.connect(
            lambda: bridge.queue_write(self._stream_key))
        ui.deviceRenamed.connect(lambda _name: bridge.queue_write(DEVICE_NAME_KEY))

        for ui_channel in self.app_root.child("ui").children():
            for iir in ui_channel.children():
                if not (ui_channel.name.startswith("ch") and iir.name.startswith("iir")):
                    continue
                ch, idx = int(ui_channel.name[2:]), int(iir.name[3:])
                raw_key = f"settings/ch/{ch}/biquad/{idx}/repr/Raw"
                self._iirs[raw_key] = iir

                # Capture loop variable.
                def write(_checked=False, key=iir.path()):
                    bridge.queue_write(key)

                ui.channels[ch].iir_widgets[idx].writeToDeviceButton.clicked.connect(
                    write)

        # Show the transfer functions of the initial UI state.
        for iir in self._iirs.values():
            self._ui_value_received(iir.path())

    def _stream_topic(self, firmware: Firmware) -> str:
        return f"{self.app_root.path()}/{firmware.topic(self._stream_key)}"

    def _will_message(self, firmware: Firmware) -> MqttMessage:
        """Resets the stream target if we disconnect without doing so (also for devices
        connecting later, as it is retained)."""
        value = firmware.to_device(self._stream_key, str(NetworkAddress.UNSPECIFIED))
        # gmqtt sends `str` payloads as they are, so explicitly JSON-encode the value.
        return MqttMessage(self._stream_topic(firmware),
                           json.dumps(value),
                           retain=True,
                           will_delay_interval=3)

    async def _detect_firmware(self):
        firmware = await self._interface.detect_firmware()
        if firmware is not self._will_firmware:
            # Clear the stream target retained in the layout of the earlier firmware:
            # it might be ours, which the will no longer resets.
            self._bridge.client.publish(self._stream_topic(self._will_firmware),
                                        b"",
                                        retain=True)
            # The will message can only be changed when connecting to the broker.
            self._will_firmware = firmware
            set_will(self._bridge.client, self._will_message(firmware))
            error = FirmwareChanged(f"Stabilizer runs firmware {firmware}")
            self._bridge.interrupt(error)
            raise error
        self._firmware = firmware
        self._keys_to_read = {key for key in self._keys_to_read if firmware.has(key)}
        self._show_available_settings(firmware)
        self._ui.set_firmware(firmware)

    def _show_available_settings(self, firmware: Firmware):
        """Disable the widgets of the settings `firmware` does not have."""
        for widget, tooltip in self._unavailable.items():
            widget.setEnabled(True)
            widget.setToolTip(tooltip)
        self._unavailable.clear()
        for key, cfg in self._bridge.configs.items():
            if firmware.has(key):
                continue
            for widget in cfg.widgets:
                if widget is not None and widget not in self._unavailable:
                    self._unavailable[widget] = widget.toolTip()
                    widget.setEnabled(False)
                    widget.setToolTip(f"Not available in firmware {firmware}")

    def _update_all_topics(self):
        for key, cfg in self._bridge.configs.items():
            self.app_root.child(key).value = cfg.read_handler(cfg.widgets)

    async def _step(self) -> bool:
        """Do the most urgent outstanding piece of work. Returns whether there was any."""
        bridge = self._bridge
        if not self._device_ready:
            return False
        elif self._firmware is None:
            await self._detect_firmware()
        elif self._syncing and self._keys_to_read:
            await self._read(self._keys_to_read.pop())
        elif bridge.keys_to_write:
            # Changes in the UI come first: they supersede what was requested before.
            key = bridge.keys_to_write.pop()
            if not self._firmware.has(key):
                return True
            setting = self.app_root.child(key)
            self._update_all_topics()
            self._stream_requested |= key == self._stream_key
            await self.change(setting)
            self._ui_value_received(key)
        elif self._keys_to_read:
            await self._read(self._keys_to_read.pop())
        elif self._syncing:
            # Everything shown is now what the device has.
            self._syncing = False
            self._ui.set_settings_enabled(True)
            self._ui.update_comm_status(
                True, f"Connected to Stabilizer (firmware {self._firmware})")
        else:
            return False
        return True

    #
    # Following the device.
    #

    def _alive_changed(self, is_alive: bool, retained: bool):
        if is_alive and not retained and self._device_ready:
            self._bridge.interrupt(ConnectionError("Stabilizer reconnected"))
            return
        if is_alive and retained:
            self._device_connected()
            return
        self._device_ready = False
        self._ui.set_settings_enabled(False)
        if is_alive:
            # The device has just connected to the broker. It only subscribes to the
            # settings after publishing `alive` (requests before that are lost), then
            # applies the ones retained on the broker, and finally publishes all its
            # settings. So wait until it is done (the settings are shown as they
            # arrive).
            self._await_device(SETTINGS_DUMP_TIMEOUT)
        else:
            self._bridge.interrupt(ConnectionError("Stabilizer offline"))

    def _await_device(self, delay: float):
        if self._connect_timer is not None:
            self._connect_timer.cancel()
        self._connect_timer = asyncio.get_running_loop().call_later(
            delay, self._device_connected)

    def _device_connected(self):
        if self._connect_timer is not None:
            self._connect_timer.cancel()
            self._connect_timer = None
        self._device_ready = True
        self._bridge.updated.set()

    def _watched_key(self, key: str) -> Optional[str]:
        """The key of the device setting we follow which a request for `key` affects."""
        for raw_key in self._iirs:
            # Writing the type or another representation of a biquad replaces it.
            if key.startswith(raw_key[:-len("repr/Raw")]):
                return raw_key
        return key if key in self._bridge.configs else None

    def _settings_request_seen(self, key: str):
        """Another client has requested a setting to be changed. Only that client learns
        whether the device has accepted the request, so read the setting back."""
        key = self._watched_key(key)
        if key is None:
            return
        if key == self._stream_key:
            # If the stream then turns out to be off, it is not because the device did
            # not accept our request.
            self._stream_requested = False
        # The setting is now that client's to keep in step with the broker.
        self._retained.pop(key, None)
        self._keys_to_read.add(key)
        self._bridge.updated.set()

    def _read_back(self, key: str, value: Any, retain: bool):
        """Read back a setting we have requested to be changed to `value`."""
        watched = self._watched_key(key)
        if watched is None:
            return
        if retain and watched == key:
            self._retained[key] = value
        self._keys_to_read.add(watched)
        self._bridge.updated.set()

    async def _read(self, key: str):
        """Read a setting from the device, and show it.

        If the device has something else than what we have requested last (retained),
        retain its value instead. Otherwise, the device would have the value it has
        rejected (or modified) after restarting, and lose the one it has now.
        """
        try:
            value = await self._interface.get(key)
        except MiniconfError as e:
            self._retained.pop(key, None)
            if key not in self._iirs:
                error = ConnectionError(f"Failed to read {key}: {e}")
                self._bridge.interrupt(error)
                raise error from e
            value = e
        self._device_value_read(key, value)

        requested = self._retained.pop(key, None)
        if (requested is None or isinstance(value, MiniconfError)
                or key in self._bridge.keys_to_write or values_match(value, requested)):
            return
        logger.warning("Stabilizer has '%s' = %s instead of %s, retaining that", key,
                       value, requested)
        try:
            await self._interface.request(key, value, retain=True)
        except MiniconfError as e:
            logger.warning("Stabilizer reported failure to write setting: '%s'", e)

    def _device_value_read(self, key: str, value: Any):
        if key in self._bridge.keys_to_write:
            # Changed in the UI in the meantime, which is written and read back next.
            return
        self._show_device_value(key, value)

    def _device_value_published(self, key: str, value: Any):
        if self._connect_timer is not None:
            # The device is publishing its settings after connecting.
            self._await_device(SETTINGS_DUMP_QUIET)
        self._device_value_read(key, value)

    def _show_device_value(self, key: str, value: Any):
        if key == self._stream_key:
            self._stream_target_read(value)
        elif key in self._iirs:
            self._device_biquads[key] = value
            self._check_biquad(key)
        elif key in self._bridge.configs:
            self._bridge.show(key, value)

    def _stream_target_read(self, target: str):
        requested, self._stream_requested = self._stream_requested, False
        self._owns_stream = target == self._stream_target
        if self._owns_stream:
            self._ui.update_stream_status(None)
        elif not str(target).startswith("0.0.0.0:"):
            logger.info("Stream directed to another client (%s)", target)
            self._ui.update_stream_status(f"Stream is going to another client ({target})")
        elif requested:
            logger.warning("Stabilizer did not accept the stream target")
            self._ui.update_stream_status("Stream is off")
        else:
            # Nobody receives the stream (the client which did has closed, or the
            # device has restarted), so take it.
            self._bridge.queue_write(self._stream_key)

    def _ui_value_received(self, key: str):
        for raw_key, iir in self._iirs.items():
            if iir.path() == key:
                self._update_all_topics()
                self._update_transfer_function(iir)
                self._check_biquad(raw_key)

    def _update_transfer_function(self, setting: TopicTree):
        try:
            self._ui.update_transfer_function(setting)
        except Exception as e:
            # The coefficient calculation fails for some combinations of parameters.
            logger.warning("Failed to update transfer function: %s", e)

    def _check_biquad(self, raw_key: str):
        """Compare the biquad on the device to the one the UI state gives."""
        device = self._device_biquads.get(raw_key)
        if device is None:
            # Not read yet.
            return
        iir_setting = self._iirs[raw_key]
        ch, idx = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])
        channel = self._ui.channels[ch]
        is_error = isinstance(device, MiniconfError)
        # `Variant absent` means that the biquad is in another representation.
        available = not is_error or "Variant absent" in device.message
        channel.set_iir_available(idx, available)
        if not available:
            return

        widget = channel.iir_widgets[idx]
        self._update_all_topics()
        try:
            expected = self._biquad_value(iir_setting)
        except Exception as e:
            widget.set_device_mismatch(
                f"Not on the device, as the settings are invalid: {e}", can_write=False)
            return
        if not isinstance(device, dict) or not all(
                values_match(device.get(k), expected[k]) for k in expected):
            widget.set_device_mismatch(
                "The filter on the device differs from these settings.")
        else:
            widget.set_device_mismatch(None)

    async def _close(self):
        """Release our stream and always disconnect, including after a failed request."""
        if self._bridge is None:
            return
        try:
            if self._owns_stream and self._bridge.connected.is_set():
                await self._interface.request(self._stream_key,
                                              str(NetworkAddress.UNSPECIFIED),
                                              retain=True,
                                              timeout=CLOSE_TIMEOUT)
        except Exception as error:
            logger.warning("Failed to release stream: %r", error)
        finally:
            if self._connect_timer is not None:
                self._connect_timer.cancel()
            self._bridge.client.on_disconnect = lambda *_: None
            await self._bridge.client.disconnect()

    #
    # Writing settings.
    #

    def _biquad_value(self, iir_setting: TopicTree) -> dict:
        """The biquad (as the `Raw` representation of the device) for the UI state."""
        settings = iir_setting.value
        ba = settings_coefficients(self.sample_period, settings)

        x_offset = settings["x_offset"]
        forward_gain = sum(ba[:3])
        if forward_gain == 0 and x_offset != 0:
            logger.warning("Filter has no DC gain but x_offset is non-zero")
        y_offset = settings["y_offset"]
        return {
            "coeff": {
                "ba": list(ba)
            },
            "u": stabilizer.voltage_to_machine_units(y_offset + forward_gain * x_offset),
            "min": stabilizer.voltage_to_machine_units(settings["y_min"]),
            "max": stabilizer.voltage_to_machine_units(settings["y_max"]),
        }

    async def set_setting(self, key: str, value: Any, retain: bool = False):
        """Set a device setting, raising `MiniconfError` if the device reports an error.

        Unlike `request_settings_change()`, this is not retained by default.
        """
        if self._interface is None or not self._device_ready or self._syncing:
            raise ConnectionError("Not connected to Stabilizer")
        await self._interface.request(key, value, retain=retain)

    async def get_setting(self, key: str) -> Any:
        """Get the value of a device setting."""
        if self._interface is None or not self._device_ready or self._syncing:
            raise ConnectionError("Not connected to Stabilizer")
        return await self._interface.get(key)

    async def request_settings_change(self,
                                      key: str,
                                      value: Any,
                                      retain: bool = True) -> bool:
        """
        Write to the miniconf-provided topics, logging any error reported by the device.

        Returns whether the device reported success. Either way, the setting is read
        back afterwards if it is shown in the UI, as the device might have modified it.
        """
        try:
            await self._interface.request(key, value, retain=retain)
            return True
        except MiniconfError as e:
            logger.warning("Stabilizer reported failure to write setting: '%s'", e)
            return False
        finally:
            self._read_back(key, value, retain)

    async def _change_filter_setting(self, iir_setting):
        (_ch,
         _iir_idx) = int(iir_setting.get_parent().name[2:]), int(iir_setting.name[3:])
        biquad = f"settings/ch/{_ch}/biquad/{_iir_idx}"
        raw_key = f"{biquad}/repr/Raw"

        try:
            value = self._biquad_value(iir_setting)
        except Exception as e:
            # The coefficient calculation fails for some combinations of parameters.
            logger.warning("Invalid settings for %s, not written: %s", biquad, e)
            self._check_biquad(raw_key)
            return

        try:
            await self._interface.request(raw_key, value, retain=True)
        except MiniconfError as e:
            if "Variant absent" not in e.message:
                logger.warning("Stabilizer reported failure to write setting: '%s'", e)
                return
            # The biquad is configured using a different representation (e.g. `Pid`,
            # through another client). Only switch it in this case, as writing `typ`
            # resets the biquad to the representation's default. For the same reason,
            # `typ` is not retained, as the broker does not guarantee the order in which
            # the device receives the retained messages.
            logger.info("Switching %s to the Raw representation", biquad)
            if await self.request_settings_change(f"{biquad}/typ", "Raw", retain=False):
                await self.request_settings_change(raw_key, value)
        finally:
            self._read_back(raw_key, value, retain=True)
