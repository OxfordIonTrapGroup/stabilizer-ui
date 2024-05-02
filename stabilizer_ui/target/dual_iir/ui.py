from PyQt5 import QtWidgets
from stabilizer.stream import Parser, AdcDecoder, DacDecoder

from ...widgets import AbstractUiWindow
from ...iir.channel_settings import ChannelSettings
from ...stream.fft_scope import FftScope
from ...iir.filters import FILTERS


class UiWindow(AbstractUiWindow):

    def __init__(self, title: str = "Dual IIR"):
        super().__init__()

        self.setWindowTitle(title)

        wid = QtWidgets.QWidget(self)
        self.setCentralWidget(wid)
        layout = QtWidgets.QHBoxLayout()

        # Create UI for channel settings.
        self.channel_settings = [ChannelSettings(), ChannelSettings()]

        self.tab_channel_settings = QtWidgets.QTabWidget()
        for i, channel in enumerate(self.channel_settings):
            self.tab_channel_settings.addTab(channel, f"Channel {i}")
        layout.addWidget(self.tab_channel_settings)

        # Create UI for FFT scope.
        streamParser = Parser([AdcDecoder(), DacDecoder()])
        self.fft_scope = FftScope(streamParser)
        layout.addWidget(self.fft_scope)

        # Set main window layout
        wid.setLayout(layout)

    def update_stream(self, payload):
        # if self.tab_channel_settings.currentIndex() != 1:
        #    return
        self.fft_scope.update(payload)

    async def update_transfer_function(self, setting, all_values):
        if setting.split("/")[0] == "ui":
            channel, iir = setting.split("/")[1:3]
            path_root = f"ui/{channel}/{iir}/"
            filter_type = all_values[path_root + "filter"]
            filter_idx = [f.filter_type for f in FILTERS].index(filter_type)
            kwargs = {
                param: all_values[path_root + f"{filter_type}/{param}"]
                for param in FILTERS[filter_idx].parameters
            }
            ba = FILTERS[filter_idx].get_coefficients(**kwargs)
            _iir_widget = self.channel_settings[int(channel)].iir_widgets[int(iir)]
            _iir_widget.update_transfer_function(ba)
