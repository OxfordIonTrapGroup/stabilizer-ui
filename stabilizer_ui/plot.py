import pyqtgraph as pg
from PyQt6.QtCore import QEvent

#: Background of disabled plots, instead of the usual black.
DISABLED_BACKGROUND = (64, 64, 64)
#: Opacity of the contents (curves, axes, labels) of disabled plots.
DISABLED_OPACITY = 0.5


class GraphicsLayoutWidget(pg.GraphicsLayoutWidget):
    """`pg.GraphicsLayoutWidget` which is greyed out while disabled, like other widgets.

    Use this in place of the pyqtgraph class (also as the header of the promoted widget
    in `.ui` files, `stabilizer_ui.plot`).
    """

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            enabled = self.isEnabled()
            self.setBackground(
                pg.getConfigOption("background") if enabled else DISABLED_BACKGROUND)
            self.ci.setOpacity(1.0 if enabled else DISABLED_OPACITY)
