"""Long-term estimates of the power spectral density of the stream data."""

import logging

from PyQt6.QtWidgets import QMessageBox

logger = logging.getLogger(__name__)


class SpectralDensityMixin:
    """Adds the spectral density window to the Tools menu of a main window."""

    def _add_spectral_density_action(self):
        """Add the (initially disabled) action to the Tools menu."""
        self._stream_message = None
        self._spectral_density_window = None
        self.spectralDensityAction = self.tools_menu().addAction("&Spectral density…")
        self.spectralDensityAction.setShortcut("Ctrl+D")
        self.spectralDensityAction.setEnabled(False)
        self.spectralDensityAction.triggered.connect(self.show_spectral_density)

    def set_stream_thread(self, stream_thread, device: str):
        """Also enable the spectral density window."""
        super().set_stream_thread(stream_thread, device)
        self.spectralDensityAction.setEnabled(True)

    def show_spectral_density(self):
        if self._spectral_density_window is None:
            try:
                # Needs the `stabilizer_psd` extension (`psd/`), which is optional.
                from .dialog import SpectralDensityWindow
            except ImportError as e:
                logger.warning("Spectral density window not available: %s", e)
                QMessageBox.warning(
                    self, "Spectral density not available",
                    "The spectral density window needs the stabilizer-psd package (in "
                    "psd/), which is built with a Rust toolchain. Install Rust, and run "
                    "`uv sync` (with the `psd` dependency group, as by default).\n\n"
                    f"{e}")
                return
            self._spectral_density_window = SpectralDensityWindow(
                self._stream_thread, self._stream_device, self)
            self._spectral_density_window.set_stream_status(self._stream_message)
        self._spectral_density_window.show()
        self._spectral_density_window.raise_()
        self._spectral_density_window.activateWindow()

    def update_stream_status(self, message):
        super().update_stream_status(message)
        self._stream_message = message
        if self._spectral_density_window is not None:
            self._spectral_density_window.set_stream_status(message)
