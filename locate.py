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

"""One-shot device location, same idea as STAplus-SCK-App “Use my location”."""

import logging
import os
import sys

from qgis.PyQt.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal

_logger = logging.getLogger("sck.locate")

LOCATE_TIMEOUT_MS = 22000
POSITION_TIMEOUT_MS = 20000
CORELOCATION_POLL_MS = 400

_MACOS_DENIED = (
    "QGIS is not allowed to use Location Services. Open System Settings → "
    "Privacy & Security → Location Services, turn Location Services on, enable "
    "QGIS, then try Locate me again."
)


def _position_source_class():
    """QGeoPositionInfoSource from QGIS PyQt, or None if Qt Positioning is missing."""
    try:
        from qgis.PyQt.QtPositioning import QGeoPositionInfoSource
        return QGeoPositionInfoSource
    except ImportError:
        try:
            from PyQt6.QtPositioning import QGeoPositionInfoSource
            return QGeoPositionInfoSource
        except ImportError:
            return None


def _enum_value(source_cls, name, default=None):
    """Read QGeoPositionInfoSource.Error.<name> across Qt5/Qt6 bindings."""
    if source_cls is None:
        return default
    error = getattr(source_cls, "Error", None)
    if error is not None and hasattr(error, name):
        return getattr(error, name)
    return getattr(source_cls, name, default)


def _add_qt_plugin_paths():
    """Make Qt Positioning plugins inside QGIS.app visible to createDefaultSource()."""
    try:
        from qgis.core import QgsApplication
    except Exception:
        return
    prefix = QgsApplication.prefixPath() or ""
    plugin_path = ""
    if hasattr(QgsApplication, "pluginPath"):
        try:
            plugin_path = QgsApplication.pluginPath() or ""
        except Exception:
            plugin_path = ""
    contents = os.path.abspath(os.path.join(prefix, "..", "..")) if prefix else ""
    roots = [
        os.path.join(prefix, "plugins"),
        os.path.join(prefix, "PlugIns"),
        plugin_path,
        os.path.join(contents, "PlugIns"),
        os.path.join(contents, "PlugIns", "positioning"),
        os.path.join(contents, "Resources", "plugins"),
        os.path.join(contents, "Resources", "qtplugins"),
    ]
    seen = set()
    for root in roots:
        if not root or root in seen or not os.path.isdir(root):
            continue
        seen.add(root)
        QCoreApplication.addLibraryPath(root)
        _logger.info("Added Qt plugin path %s", root)


def _coord_wgs84(info_or_coord):
    """Return (lat, lon) from a QGeoPositionInfo or QGeoCoordinate, or None."""
    coord = info_or_coord
    if hasattr(info_or_coord, "coordinate"):
        try:
            coord = info_or_coord.coordinate()
        except Exception:
            return None
    if coord is None:
        return None
    try:
        if hasattr(coord, "isValid") and not coord.isValid():
            return None
        lat = float(coord.latitude())
        lon = float(coord.longitude())
    except Exception:
        return None
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon


class DeviceLocator(QObject):
    """Ask the OS for the current WGS84 position (CoreLocation on macOS, else Qt)."""

    located = pyqtSignal(float, float)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = None
        self._cl = None
        self._busy = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._poll = QTimer(self)
        self._poll.setInterval(CORELOCATION_POLL_MS)
        self._poll.timeout.connect(self._on_corelocation_poll)

    def is_busy(self):
        """True while a locate request is in flight."""
        return self._busy

    def start(self):
        """Request one position update. No-op if a request is already running."""
        if self._busy:
            return True
        if sys.platform == "darwin":
            try:
                from .locate_macos import corelocation_available
            except Exception as err:
                _logger.warning("Could not import CoreLocation helper: %s", err)
            else:
                if corelocation_available():
                    return self._start_corelocation()
                _logger.warning("CoreLocation runtime is not available; trying Qt Positioning")
        return self._start_qt()

    def _start_corelocation(self):
        """macOS: CLLocationManager in this process (QGIS's Location Services grant)."""
        from .locate_macos import CoreLocationSession

        try:
            if self._cl is None:
                self._cl = CoreLocationSession(
                    on_fix=self._on_corelocation_fix,
                    on_denied=self._on_corelocation_denied,
                    on_fail=self._on_corelocation_fail,
                )
        except Exception as err:
            _logger.warning("CoreLocation session failed (%s); trying Qt Positioning", err)
            return self._start_qt()
        if self._cl.is_denied():
            self.failed.emit(_MACOS_DENIED)
            return False
        self._busy = True
        self._timeout.start(LOCATE_TIMEOUT_MS)
        try:
            self._cl.start()
        except Exception as err:
            self._busy = False
            self._timeout.stop()
            _logger.warning("CoreLocation start failed (%s); trying Qt Positioning", err)
            return self._start_qt()
        self._poll.start()
        _logger.info("Requested current device location via CoreLocation")
        return True

    def _start_qt(self):
        """Non-macOS (or CoreLocation unavailable): Qt Positioning."""
        source_cls = _position_source_class()
        if source_cls is None:
            self.failed.emit(
                "Qt Positioning is not available in this QGIS build, so Locate me cannot run."
            )
            return False
        _add_qt_plugin_paths()
        if self._source is None:
            self._source = source_cls.createDefaultSource(self)
            if self._source is None:
                self.failed.emit(
                    "No location provider is available. Enable Location Services for QGIS, then try again."
                )
                return False
            self._source.positionUpdated.connect(self._on_position)
            if hasattr(self._source, "errorOccurred"):
                self._source.errorOccurred.connect(self._on_source_error)
            _logger.info(
                "Location source %s methods=%s",
                self._source.sourceName() if hasattr(self._source, "sourceName") else self._source,
                self._source.supportedPositioningMethods()
                if hasattr(self._source, "supportedPositioningMethods")
                else "?",
            )
        cached = None
        if hasattr(self._source, "lastKnownPosition"):
            try:
                cached = _coord_wgs84(self._source.lastKnownPosition())
            except Exception:
                cached = None
        if cached:
            _logger.info("Using last known device location %.6f, %.6f", cached[0], cached[1])
            self.located.emit(cached[0], cached[1])
            return True
        self._busy = True
        self._timeout.start(LOCATE_TIMEOUT_MS)
        started = False
        try:
            self._source.requestUpdate(POSITION_TIMEOUT_MS)
            started = True
        except Exception as err:
            _logger.info("requestUpdate failed (%s); trying startUpdates", err)
        if hasattr(self._source, "startUpdates"):
            try:
                self._source.startUpdates()
                started = True
            except Exception as err:
                _logger.warning("startUpdates failed: %s", err)
        if not started:
            self._busy = False
            self._timeout.stop()
            self.failed.emit("Could not request the current location from Qt Positioning.")
            return False
        _logger.info("Requested current device location")
        return True

    def stop(self):
        """Cancel an in-flight request (plugin unload or a new locate)."""
        self._busy = False
        self._timeout.stop()
        self._poll.stop()
        if self._cl is not None:
            try:
                self._cl.stop()
            except Exception:
                pass
        if self._source is None:
            return
        try:
            self._source.stopUpdates()
        except Exception:
            pass

    def close(self):
        """Release OS location resources (plugin unload)."""
        self.stop()
        if self._cl is not None:
            try:
                self._cl.close()
            except Exception:
                pass
            self._cl = None

    def _finish_ok(self, lat, lon):
        self.stop()
        _logger.info("Device location %.6f, %.6f", lat, lon)
        self.located.emit(lat, lon)

    def _on_corelocation_fix(self, lat, lon):
        QTimer.singleShot(0, lambda: self._finish_ok_if_busy(lat, lon))

    def _on_corelocation_denied(self):
        QTimer.singleShot(0, lambda: self._fail_if_busy(_MACOS_DENIED))

    def _on_corelocation_fail(self, message):
        QTimer.singleShot(0, lambda: self._fail_if_busy(message))

    def _finish_ok_if_busy(self, lat, lon):
        if not self._busy:
            return
        self._finish_ok(lat, lon)

    def _fail_if_busy(self, message):
        if not self._busy:
            return
        self.stop()
        self.failed.emit(str(message))

    def _on_corelocation_poll(self):
        if not self._busy or self._cl is None:
            return
        parsed = None
        try:
            parsed = self._cl.read_wgs84()
        except Exception as err:
            _logger.info("CoreLocation poll failed: %s", err)
            return
        if parsed:
            self._finish_ok(parsed[0], parsed[1])

    def _on_position(self, info):
        if not self._busy:
            return
        parsed = _coord_wgs84(info)
        if parsed is None:
            self.stop()
            self.failed.emit("The location provider returned an invalid position.")
            return
        self._finish_ok(parsed[0], parsed[1])

    def _on_source_error(self, error):
        if not self._busy:
            return
        source_cls = _position_source_class()
        no_error = _enum_value(source_cls, "NoError")
        access = _enum_value(source_cls, "AccessError")
        timeout = _enum_value(source_cls, "UpdateTimeoutError")
        _logger.warning("Location source error %s (AccessError=%s NoError=%s)", error, access, no_error)
        if no_error is not None and error == no_error:
            return
        if sys.platform == "darwin":
            return
        if access is not None and error == access:
            self.stop()
            self.failed.emit(
                "Location permission denied. Enable Location Services for QGIS, then try again."
            )
            return
        if timeout is not None and error == timeout:
            return

    def _on_timeout(self):
        if not self._busy:
            return
        parsed = None
        if self._cl is not None:
            try:
                parsed = self._cl.read_wgs84()
            except Exception:
                parsed = None
        if parsed is None and self._source is not None and hasattr(self._source, "lastKnownPosition"):
            try:
                parsed = _coord_wgs84(self._source.lastKnownPosition())
            except Exception:
                parsed = None
        if parsed:
            self._finish_ok(parsed[0], parsed[1])
            return
        self.stop()
        if sys.platform == "darwin":
            self.failed.emit(
                "Could not get the current location in time. Check that Wi-Fi or GPS is available, then try Locate me again."
            )
            return
        self.failed.emit(
            "Could not get the current location in time. If macOS asked, allow QGIS, then try Locate me again."
        )
