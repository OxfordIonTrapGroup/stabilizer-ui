"""A log view in the window, fed from the `logging` module."""
import html
import logging

from PyQt6 import QtCore, QtGui, QtWidgets

#: Number of lines kept.
MAX_LINES = 5000

#: Colours of the levels (above INFO).
LEVEL_COLOURS = {
    logging.WARNING: "darkorange",
    logging.ERROR: "red",
    logging.CRITICAL: "red",
}


class _Emitter(QtCore.QObject):
    message = QtCore.pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """Forwards log records to the GUI thread (as the `message` signal), where they are
    shown. (Not a `QObject` itself, so that `logging.shutdown()` can still reach it after
    Qt has gone.)"""

    def __init__(self, level=logging.INFO):
        super().__init__(level=level)
        self._emitter = _Emitter()
        self._time_format = "%Y-%m-%d %H:%M:%S"

    @property
    def message(self):
        return self._emitter.message

    def emit(self, record: logging.LogRecord):
        try:
            text = html.escape(record.getMessage())
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                text += "<br/>" + html.escape(formatter.formatException(
                    record.exc_info)).replace("\n", "<br/>")
            colour = LEVEL_COLOURS.get(record.levelno)
            if colour is not None:
                text = f"<span style='color: {colour}'>{text}</span>"
            timestamp = logging.Formatter().formatTime(record, self._time_format)
            self._emitter.message.emit(
                f"<span style='color: gray'>{timestamp}</span>&nbsp; {text}")
        except RuntimeError:
            # The emitter has been deleted with the window.
            pass
        except Exception:
            self.handleError(record)


class _ExcludePrefix(logging.Filter):
    """Drops the records of the loggers below a name (which another handler shows)."""

    def __init__(self, prefix: str):
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.name == self._prefix
                    or record.name.startswith(self._prefix + "."))


class LogView(QtWidgets.QPlainTextEdit):
    """Shows the log of the given loggers: all messages (from `level`) of `logger_name`,
    and warnings and errors of `other_logger_name` (the rest of the application)."""

    def __init__(self,
                 logger_name: str,
                 other_logger_name: str = "stabilizer_ui",
                 level=logging.INFO,
                 parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(MAX_LINES)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.document().setDefaultStyleSheet("")

        self._handlers = []
        handler = QtLogHandler(level)
        handler.message.connect(self.append)
        main_logger = logging.getLogger(logger_name)
        if main_logger.getEffectiveLevel() > level:
            # The messages have to reach the handler regardless of the root logger.
            main_logger.setLevel(level)
        main_logger.addHandler(handler)
        self._handlers.append((main_logger, handler))

        if other_logger_name and other_logger_name != logger_name:
            other = QtLogHandler(logging.WARNING)
            other.addFilter(_ExcludePrefix(logger_name))
            other.message.connect(self.append)
            logging.getLogger(other_logger_name).addHandler(other)
            self._handlers.append((logging.getLogger(other_logger_name), other))

    def append(self, line: str):
        self.appendHtml(line)
        self.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def close_handlers(self):
        for logger, handler in self._handlers:
            logger.removeHandler(handler)
        self._handlers.clear()
