from PyQt6 import QtWidgets, QtCore
from stabilizer import DEFAULT_DUAL_IIR_SAMPLE_PERIOD
from stabilizer.stream import Parser, AdcDecoder, DacDecoder

from . import *
from .topics import StabilizerSettings, UiSettings

from ...ui import AbstractUiWindow
from ...mqtt import NetworkAddress, UiMqttConfig
from ...ff_fb_settings.channel_settings import ChannelSettings, OffsetSettings
from ...stream.fft_scope import FftScope


# UI Configuration Parameters

DEFAULT_WINDOW_SIZE = (1200, 600)
# Default Y-axis ranges for scope plots
DEFAULT_DAC_PLOT_YRANGE = (-1, 1)
DEFAULT_ADC_PLOT_YRANGE = (-1, 1)

#: Interval between scope plot updates, in seconds.
#: PyQt's drawing speed limits value.
SCOPE_UPDATE_PERIOD = 0.05  # 20 fps


class UiWindow(AbstractUiWindow):
    """
    Main application window.

    Layout:
        ┌───────────────────────────────┬───────────────────────┐
        │ Offset + Channel Settings     │   FFT / Scope View    │
        └───────────────────────────────┴───────────────────────┘

    Left side:
        - DC offset controls
        - Per-channel configuration:
            - AFE gain
            - Cascaded IIR filters
            - Harmonic feedforward parameters

    Right side:
        - Live ADC/DAC streaming visualization
    """
    
    def __init__(self, title: str = "Dual IIR with FF"):
        super().__init__()
        self.setWindowTitle(title)

        # Main horizontal splitter:
        #   Left: configuration controls
        #   Right: streaming / FFT visualization
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)



        # Left-side container: offset + per-channel settings
        left_container = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_container)
        left_layout.setContentsMargins(0,0,0,0)

        # DC offset + PLL gain controls
        self.offset_widget = OffsetSettings()
        left_layout.addWidget(self.offset_widget)

        # Create one ChannelSettings widget per hardware channel
        # Each channel contains:
        #   - AFE gain
        #   - Two IIR stages
        #   - Harmonic feedforward parameters
        self.channels = [
            ChannelSettings(DEFAULT_DUAL_IIR_SAMPLE_PERIOD) for _ in range(NUM_CHANNELS)
        ]
        
        self.channelTabWidget = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channels):
            self.channelTabWidget.addTab(channel, f"Channel {i}")
        
        left_layout.addWidget(self.channelTabWidget)
        
        left_layout.setStretch(0, 1)  # Offset smaller
        left_layout.setStretch(1, 3)  # Channels larger
        splitter.addWidget(left_container)


        # Streaming scope:
        # Parses UDP stream (ADC + DAC samples)
        # Displays time-domain + FFT visualization
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fftScopeWidget = FftScope(streamParser, DEFAULT_DUAL_IIR_SAMPLE_PERIOD)
        splitter.addWidget(self.fftScopeWidget)
        # Set default plot ranges for each channel
        for i in range(NUM_CHANNELS):
            self.fftScopeWidget.graphics_view.getItem(
                0, i).setYRange(*DEFAULT_ADC_PLOT_YRANGE)
            self.fftScopeWidget.graphics_view.getItem(
                1, i).setYRange(*DEFAULT_DAC_PLOT_YRANGE)

        # Disable mouse wheel scrolling on spinboxes to prevent accidental changes
        spinboxes = self.channelTabWidget.findChildren(QtWidgets.QDoubleSpinBox)
        for box in spinboxes:
            box.wheelEvent = lambda *event: None

        self.resize(*DEFAULT_WINDOW_SIZE)

    def update_stream(self, payload):
        """
        Receive streamed ADC/DAC payload and update scope visualization.
        Called from streaming thread.
        """
        self.fftScopeWidget.update(payload)

    def set_mqtt_configs(self, stream_target: NetworkAddress):
        """
        Bind UI widgets to MQTT topic tree.

        Returns:
            settings_map: dict mapping topic path → UiMqttConfig

        This map is consumed by the interface layer to:
            - Push UI changes to firmware
            - Reflect firmware changes back into the UI
        """

        # `ui/#` are only used by the UI, the others by both UI and stabilizer
        settings_map = {
            # Stream target is firmware-controlled but configured from UI.
            StabilizerSettings.stream_target.path():
            UiMqttConfig(
                [],
                lambda _: stream_target._asdict(),
                lambda _w, _v: stream_target._asdict(),
            )
        }
        # Bind DC offset and PLL gains directly to firmware settings topics.
        settings_map[StabilizerSettings.v_offset.path()] = UiMqttConfig([self.offset_widget.offsetBox])
        settings_map[StabilizerSettings.ki_ff.path()] = UiMqttConfig([self.offset_widget.kiffBox])
        settings_map[StabilizerSettings.kp_ff.path()] = UiMqttConfig([self.offset_widget.kpffBox])
        
        # Bind per-channel settings:
        #   - AFE gain
        #   - Harmonic parameters
        #   - IIR filters
        for ch in range(NUM_CHANNELS):
            settings_map[StabilizerSettings.afes[ch].path()] = UiMqttConfig(
                [self.channels[ch].afeGainBox])
    

            # Harmonic feedforward parameters (amp + phase per order)
            hparamWidget = self.channels[ch].h_param_widgets      
            hparam_topic = UiSettings.h_params[ch]
            hparamWidget.set_mqtt_configs(settings_map, hparam_topic)
            

            # IIR filter configuration (filter type + parameters + plot settings)
            for iir in range(NUM_IIR_FILTERS_PER_CHANNEL):
                iirWidget = self.channels[ch].iir_widgets[iir]
                iir_topic = UiSettings.iirs[ch][iir]

                iirWidget.set_mqtt_configs(settings_map, iir_topic)
                
        return settings_map
