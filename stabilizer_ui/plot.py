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


def _is_one_two_five(x: float) -> bool:
    """Whether `x` is 1, 2 or 5 times a power of ten."""
    mantissa = x / 10**math.floor(math.log10(x) + 1e-9)
    return round(mantissa, 6) in (1, 2, 5)


class LogAxis(pg.AxisItem):
    """Logarithmic axis labelling only 1, 2 and 5 times powers of ten (unless there are
    fewer than two of them), as the default labels for the minor ticks overlap."""

    def logTickStrings(self, values, scale, spacing):
        strings = super().logTickStrings(values, scale, spacing)
        labelled = [_is_one_two_five(10**value * scale) for value in values]
        if sum(labelled) < 2:
            return strings
        return [string if keep else "" for string, keep in zip(strings, labelled)]


class FrequencyAxis(LogAxis):
    """`LogAxis` for frequencies, with SI prefixes."""

    def logTickStrings(self, values, scale, spacing):
        return [
            format_frequency(10**value * scale, unit=False) if string else ""
            for value, string in zip(values,
                                     super().logTickStrings(values, scale, spacing))
        ]
