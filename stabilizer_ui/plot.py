import math

import pyqtgraph as pg
from PyQt6.QtCore import QEvent

#: Colours of the curves of different measurements or signals.
COLOURS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2",
    "#7f7f7f", "#bcbd22", "#17becf"
]

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


def format_frequency(f: float, unit: bool = True) -> str:
    suffix = "Hz" if unit else ""
    for scale, prefix in [(1e6, "M"), (1e3, "k")]:
        if f >= scale:
            return f"{f / scale:.4g} {prefix}{suffix}".strip()
    return f"{f:.4g} {suffix}".strip()


class FrequencyAxis(pg.AxisItem):
    """Logarithmic frequency axis labelling only 1, 2 and 5 times powers of ten (with SI
    prefixes), as the default labels for the minor ticks overlap."""

    def logTickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            f = 10**value * scale
            mantissa = f / 10**math.floor(math.log10(f) + 1e-9)
            labels.append(
                format_frequency(f, unit=False) if round(mantissa, 6) in (1, 2,
                                                                          5) else "")
        return labels
