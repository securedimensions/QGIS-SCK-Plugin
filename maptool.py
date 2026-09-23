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

"""QGIS map tool: click the canvas to set or move the STAplus Thing-location marker."""

import logging

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
)
from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor
from qgis.PyQt.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from .config import (
    DEFAULT_MAP_LAT,
    DEFAULT_MAP_LON,
    DEFAULT_MAP_SCALE,
    MARKER_MAP_SCALE,
    OSM_LAYER_NAME,
    OSM_XYZ_URI,
)

_logger = logging.getLogger("sck.maptool")


def _wgs84():
    """Geographic CRS used by STAplus Location GeoJSON (lon, lat)."""
    return QgsCoordinateReferenceSystem("EPSG:4326")


def canvas_point_to_wgs84(canvas, map_point):
    """Transform a point in the canvas CRS to (lat, lon) in EPSG:4326."""
    source = canvas.mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(source, _wgs84(), QgsProject.instance())
    wgs = transform.transform(QgsPointXY(map_point))
    return float(wgs.y()), float(wgs.x())


def wgs84_to_canvas_point(canvas, lat, lon):
    """Transform WGS84 (lat, lon) to a point in the canvas CRS."""
    dest = canvas.mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(_wgs84(), dest, QgsProject.instance())
    return transform.transform(QgsPointXY(float(lon), float(lat)))


def map_point_to_pixel(canvas, map_point):
    """Convert a point in the canvas CRS to viewport pixels (QGIS 4-safe)."""
    try:
        pixel = canvas.mapSettings().mapToPixel().transform(QgsPointXY(map_point))
        return float(pixel.x()), float(pixel.y())
    except Exception as err:
        _logger.debug("mapToPixel transform failed: %s", err)
    if hasattr(canvas, "getCoordinateTransform"):
        pixel = canvas.getCoordinateTransform().transform(QgsPointXY(map_point))
        return float(pixel.x()), float(pixel.y())
    raise AttributeError("Could not convert map point to canvas pixels")


def _icon_cross():
    return QgsVertexMarker.IconType.ICON_CROSS


def _cross_cursor():
    return QCursor(Qt.CursorShape.CrossCursor)


def _left_button():
    return Qt.MouseButton.LeftButton


CURSOR_HINT_FOI = "Select FoI"
CURSOR_HINT_THING = "Select Thing location"


class CanvasCursorHint(QLabel):
    """Small label that follows the map cursor and names the active click mode."""

    def __init__(self, canvas, parent=None):
        host = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        super().__init__(host)
        self._host = host
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "QLabel { background-color: rgba(32, 32, 32, 220); color: #fff; "
            "padding: 2px 8px; border-radius: 3px; font-size: 12px; }"
        )
        self.hide()

    def set_mode(self, text):
        """Set the caption; shown on the next mouse move over the canvas."""
        text = (text or "").strip()
        if self.text() == text:
            return
        self.setText(text)
        self.adjustSize()
        if not text:
            self.hide()

    def follow(self, pos):
        """Move the caption next to the cursor hotspot."""
        if not self.text():
            self.hide()
            return
        self.move(int(pos.x()) + 18, int(pos.y()) + 16)
        self.show()
        self.raise_()

    def leave(self):
        """Hide when the pointer leaves the canvas."""
        self.hide()

    def close_hint(self):
        """Remove the overlay (plugin unload)."""
        self.hide()
        self.setParent(None)
        self.deleteLater()


class CanvasZoomControl(QWidget):
    """Standard + / − zoom buttons over the QGIS map canvas."""

    def __init__(self, canvas, parent=None):
        host = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        super().__init__(host)
        self.canvas = canvas
        self.setObjectName("sckCanvasZoomControl")
        self.setStyleSheet(
            "QWidget#sckCanvasZoomControl { background: transparent; }"
            "QToolButton { background-color: rgba(255, 255, 255, 235); color: #222; "
            "border: 1px solid #888; font-size: 16px; font-weight: bold; padding: 0; }"
            "QToolButton:hover { background-color: #f3f3f3; }"
            "QToolButton:pressed { background-color: #ddd; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        plus = QToolButton(self)
        plus.setText("+")
        plus.setToolTip("Zoom in")
        plus.setFixedSize(28, 28)
        plus.clicked.connect(self._zoom_in)
        minus = QToolButton(self)
        minus.setText("−")
        minus.setToolTip("Zoom out")
        minus.setFixedSize(28, 28)
        minus.clicked.connect(self._zoom_out)
        layout.addWidget(plus)
        layout.addWidget(minus)
        self.adjustSize()
        self.move(10, 10)
        self.show()
        self.raise_()

    def _zoom_in(self):
        canvas = self.canvas
        if hasattr(canvas, "zoomIn"):
            canvas.zoomIn()
            return
        if hasattr(canvas, "zoomByFactor"):
            canvas.zoomByFactor(0.5)

    def _zoom_out(self):
        canvas = self.canvas
        if hasattr(canvas, "zoomOut"):
            canvas.zoomOut()
            return
        if hasattr(canvas, "zoomByFactor"):
            canvas.zoomByFactor(2.0)

    def close_control(self):
        """Remove the overlay (plugin unload)."""
        self.hide()
        self.setParent(None)
        self.deleteLater()


def project_has_map_layers(project=None):
    """True when the current QGIS project already has at least one map layer."""
    project = project or QgsProject.instance()
    return bool(project.mapLayers())


def ensure_openstreetmap_basemap():
    """Add an OSM XYZ layer if the project is empty so the canvas is not a white void.

    Returns True when a layer was added.
    """
    project = QgsProject.instance()
    if project_has_map_layers(project):
        return False
    layer = QgsRasterLayer(OSM_XYZ_URI, OSM_LAYER_NAME, "wms")
    if not layer.isValid():
        layer = QgsRasterLayer(OSM_XYZ_URI, OSM_LAYER_NAME, "xyz")
    if not layer.isValid():
        _logger.warning("Could not create the OpenStreetMap XYZ layer")
        return False
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    if mercator.isValid():
        project.setCrs(mercator)
    project.addMapLayer(layer)
    return True


def zoom_canvas_to_wgs84(canvas, lat, lon, scale):
    """Center the canvas on a WGS84 point at the given map scale."""
    map_point = wgs84_to_canvas_point(canvas, lat, lon)
    canvas.setCenter(map_point)
    canvas.zoomScale(float(scale))
    canvas.refresh()


def prepare_canvas_for_marker(canvas, lat=None, lon=None):
    """Ensure a visible map. Zoom only when OSM was just added (empty project).

    Does not recenter after a marker click — the view stays where the user panned.
    """
    added = ensure_openstreetmap_basemap()
    dest = QgsCoordinateReferenceSystem("EPSG:3857")
    if added and dest.isValid():
        canvas.setDestinationCrs(dest)
    if added:
        if lat is not None and lon is not None:
            zoom_canvas_to_wgs84(canvas, lat, lon, MARKER_MAP_SCALE)
        else:
            zoom_canvas_to_wgs84(canvas, DEFAULT_MAP_LAT, DEFAULT_MAP_LON, DEFAULT_MAP_SCALE)
    else:
        canvas.refresh()
    return added


class PlaceMarkerMapTool(QgsMapTool):
    """Click the map to place or move a vertex marker. Emits WGS84 lat/lon."""

    locationPicked = pyqtSignal(float, float)

    def __init__(self, canvas, parent=None):
        """canvas is iface.mapCanvas(). parent is unused; the canvas owns the tool."""
        super().__init__(canvas)
        self._marker = None
        self.setCursor(_cross_cursor())

    def canvasReleaseEvent(self, event):
        """Left click: transform the map point to WGS84 and move the marker there."""
        if event.button() != _left_button():
            return
        map_point = self.toMapCoordinates(event.pos())
        try:
            lat, lon = canvas_point_to_wgs84(self.canvas(), map_point)
        except Exception as err:
            _logger.warning("Could not convert the map click to WGS84: %s", err)
            return
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            _logger.warning("Map click is outside WGS84 bounds: lat=%s lon=%s", lat, lon)
            return
        self.set_marker_map_point(map_point)
        self.locationPicked.emit(lat, lon)

    def set_wgs84(self, lat, lon):
        """Move the vertex marker to a WGS84 position (e.g. a restored or confirmed point)."""
        if lat is None or lon is None:
            return
        try:
            map_point = wgs84_to_canvas_point(self.canvas(), lat, lon)
        except Exception:
            return
        self.set_marker_map_point(map_point)

    def set_marker_map_point(self, map_point):
        """Create or move the vertex marker in canvas coordinates."""
        marker = self._ensure_marker()
        marker.setCenter(QgsPointXY(map_point))
        marker.show()

    def _ensure_marker(self):
        if self._marker is not None:
            return self._marker
        marker = QgsVertexMarker(self.canvas())
        marker.setColor(QColor(220, 50, 47))
        if hasattr(marker, "setFillColor"):
            marker.setFillColor(QColor(220, 50, 47, 200))
        marker.setIconType(_icon_cross())
        marker.setIconSize(24)
        marker.setPenWidth(3)
        if hasattr(marker, "setZValue"):
            marker.setZValue(1000)
        self._marker = marker
        return marker

    def refresh_from_wgs84(self, lat, lon):
        """Reproject the marker after the canvas CRS changes."""
        if lat is None or lon is None:
            return
        self.set_wgs84(lat, lon)

    def remove_marker(self):
        """Hide and destroy the vertex marker (plugin unload)."""
        marker = self._marker
        self._marker = None
        if marker is None:
            return
        canvas = self.canvas()
        try:
            scene = canvas.scene() if canvas is not None else None
            if scene is not None:
                scene.removeItem(marker)
        except RuntimeError:
            pass
