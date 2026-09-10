# Copyright (C) 2026 Secure Dimensions GmbH, Munich, Germany.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.

"""Write plugin messages to the QGIS Log Messages panel (tab "STAplus SCK")."""

import logging

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import QCoreApplication, QThread

LOG_TAG = "STAplus SCK"


def _level(record_levelno):
    """Map a Python logging level to a QGIS message level."""
    if record_levelno >= logging.ERROR:
        return Qgis.MessageLevel.Critical
    if record_levelno >= logging.WARNING:
        return Qgis.MessageLevel.Warning
    return Qgis.MessageLevel.Info


def _on_gui_thread():
    """True when the caller is the Qt GUI thread. QgsMessageLog must not be used off-thread."""
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def log(message, level=None):
    """Write one line to Log Messages → STAplus SCK. No-op if called off the GUI thread."""
    if level is None:
        level = Qgis.MessageLevel.Info
    if not _on_gui_thread():
        return
    QgsMessageLog.logMessage(str(message), LOG_TAG, level)


def log_info(message):
    """Info-level message in the QGIS log panel."""
    log(message, Qgis.MessageLevel.Info)


def log_warning(message):
    """Warning-level message in the QGIS log panel."""
    log(message, Qgis.MessageLevel.Warning)


def log_error(message):
    """Critical-level message in the QGIS log panel."""
    log(message, Qgis.MessageLevel.Critical)


def log_success(message):
    """Success-level message in the QGIS log panel."""
    log(message, Qgis.MessageLevel.Success)


class QgsLogHandler(logging.Handler):
    """Forward Python logging to the QGIS Log Messages panel."""

    def emit(self, record):
        """Forward one Python log record to QgsMessageLog."""
        if not _on_gui_thread():
            return
        try:
            QgsMessageLog.logMessage(self.format(record), LOG_TAG, _level(record.levelno))
        except Exception:
            pass


def install_python_logging():
    """Attach a QgsLogHandler to the "sck" logger once, so authenix/sta/mqtt logs appear in QGIS."""
    logger = logging.getLogger("sck")
    logger.setLevel(logging.DEBUG)
    if any(isinstance(handler, QgsLogHandler) for handler in logger.handlers):
        return logger
    handler = QgsLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
