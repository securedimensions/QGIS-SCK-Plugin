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

"""QGIS memory layers and click-to-select for nearby public OSM places (FoI)."""

import json
import logging

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsProject,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QEvent, QObject, Qt, QThread, QVariant, pyqtSignal
from qgis.PyQt.QtGui import QColor

from .maptool import canvas_point_to_wgs84
from .overpass import lookup_nearby_public_places

_logger = logging.getLogger("sck.places")

PLACES_PROPERTY = "sck_nearby_places"
PLACES_GROUP_NAME = "SCK nearby places"
_LAYER_SPECS = (
    ("Polygon", "SCK nearby places (polygons)", "polygon"),
    ("MultiPolygon", "SCK nearby places (multipolygons)", "polygon"),
    ("LineString", "SCK nearby places (lines)", "line"),
    ("Point", "SCK nearby places (points)", "point"),
)


def _string_field(name):
    try:
        from qgis.PyQt.QtCore import QMetaType
        return QgsField(name, QMetaType.Type.QString)
    except Exception:
        return QgsField(name, QVariant.String)


def _int_field(name):
    try:
        from qgis.PyQt.QtCore import QMetaType
        return QgsField(name, QMetaType.Type.Int)
    except Exception:
        return QgsField(name, QVariant.Int)


def _qgs_geometry(geom_dict):
    """Build a QgsGeometry from a GeoJSON geometry dict."""
    if not isinstance(geom_dict, dict):
        return None
    text = json.dumps(geom_dict)
    geom = None
    if hasattr(QgsGeometry, "fromJson"):
        try:
            geom = QgsGeometry.fromJson(text)
        except Exception:
            geom = None
    if geom is None or geom.isEmpty():
        try:
            from qgis.core import QgsJsonUtils
            geom = QgsJsonUtils.geometryFromGeoJson(text)
        except Exception:
            return None
    if geom is None or geom.isEmpty():
        return None
    return geom


def _left_button():
    return Qt.MouseButton.LeftButton


def _style_layer(layer, kind):
    if kind == "polygon":
        symbol = QgsFillSymbol.createSimple({
            "color": "51,136,255,70",
            "outline_color": "30,90,200,230",
            "outline_width": "0.8",
        })
    elif kind == "line":
        symbol = QgsLineSymbol.createSimple({
            "line_color": "30,90,200,230",
            "line_width": "1.2",
        })
    else:
        symbol = QgsMarkerSymbol.createSimple({
            "name": "circle",
            "color": "51,136,255,180",
            "outline_color": "30,90,200,230",
            "size": "4",
        })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    try:
        layer.setSelectionColor(QColor(255, 190, 0, 180))
    except Exception as err:
        _logger.debug("Could not set place-layer selection color: %s", err)


def _feature_attr(qgs_feature, name):
    fields = qgs_feature.fields()
    idx = -1
    if hasattr(fields, "indexOf"):
        idx = fields.indexOf(name)
    if idx < 0 and hasattr(fields, "indexFromName"):
        idx = fields.indexFromName(name)
    if idx < 0 and hasattr(fields, "lookupField"):
        idx = fields.lookupField(name)
    if idx < 0:
        return None
    return qgs_feature.attribute(idx)


def _feature_from_qgs(qgs_feature):
    """Rebuild a GeoJSON Feature from a memory-layer feature."""
    osm_id = str(_feature_attr(qgs_feature, "osm_id") or "").strip()
    name = str(_feature_attr(qgs_feature, "name") or "").strip()
    kind = str(_feature_attr(qgs_feature, "kind") or "").strip()
    try:
        distance_m = int(_feature_attr(qgs_feature, "distance_m") or 0)
    except (TypeError, ValueError):
        distance_m = 0
    geom = qgs_feature.geometry()
    if geom is None or geom.isEmpty():
        return None
    try:
        geom_dict = json.loads(geom.asJson())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not name or not osm_id or not geom_dict:
        return None
    return {
        "type": "Feature",
        "geometry": geom_dict,
        "properties": {
            "osm_id": osm_id,
            "name": name,
            "kind": kind,
            "distance_m": distance_m,
        },
    }


class PlacesWorker(QThread):
    """Look up nearby public OSM places off the GUI thread."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, lat, lon, parent=None):
        super().__init__(parent)
        self.lat = lat
        self.lon = lon

    def run(self):
        try:
            collection = lookup_nearby_public_places(self.lat, self.lon)
            self.succeeded.emit(collection)
        except Exception as err:
            self.failed.emit(str(err) or "Overpass lookup failed")


class PlacesLayerStore(QObject):
    """Owns QGIS memory layers that show nearby public places."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layers = []

    def clear(self):
        """Remove plugin-owned nearby-place layers from the project."""
        project = QgsProject.instance()
        ids = []
        for layer in list(self.layers):
            if layer is None:
                continue
            try:
                ids.append(layer.id())
            except RuntimeError:
                pass
        self.layers = []
        leftover = []
        for lid, layer in project.mapLayers().items():
            try:
                if layer.customProperty(PLACES_PROPERTY):
                    leftover.append(lid)
            except RuntimeError:
                continue
        for lid in ids + leftover:
            try:
                project.removeMapLayer(lid)
            except RuntimeError:
                pass
        self._remove_group()

    def has_layers(self):
        """True when nearby-place memory layers exist in the project."""
        return bool(self.layers)

    def is_visible(self):
        """True when the SCK nearby places group is checked on in the Layers panel."""
        group = self._group()
        if group is None:
            return False
        try:
            return bool(group.itemVisibilityChecked())
        except Exception:
            return bool(group.isVisible()) if hasattr(group, "isVisible") else False

    def set_visible(self, visible):
        """Show or hide the nearby places group (Layers panel checkbox)."""
        group = self._group()
        if group is None:
            return
        try:
            group.setItemVisibilityChecked(bool(visible))
        except Exception as err:
            _logger.debug("Could not set nearby-places group visibility: %s", err)

    def _root(self):
        return QgsProject.instance().layerTreeRoot()

    def _group(self):
        root = self._root()
        if root is None:
            return None
        if hasattr(root, "findGroup"):
            return root.findGroup(PLACES_GROUP_NAME)
        for child in root.children():
            if getattr(child, "name", lambda: "")() == PLACES_GROUP_NAME:
                return child
        return None

    def _ensure_group(self):
        group = self._group()
        if group is not None:
            return group
        root = self._root()
        if root is None:
            return None
        return root.insertGroup(0, PLACES_GROUP_NAME)

    def _remove_group(self):
        group = self._group()
        root = self._root()
        if group is None or root is None:
            return
        try:
            root.removeChildNode(group)
        except Exception as err:
            _logger.debug("Could not remove nearby-places group: %s", err)

    def set_collection(self, collection):
        """Replace place layers with features from a GeoJSON FeatureCollection."""
        self.clear()
        buckets = {key: [] for key, _name, _kind in _LAYER_SPECS}
        for item in (collection or {}).get("features") or []:
            geom = item.get("geometry") if isinstance(item, dict) else None
            if not isinstance(geom, dict):
                continue
            gtype = geom.get("type")
            if gtype in buckets:
                buckets[gtype].append(item)
        project = QgsProject.instance()
        group = self._ensure_group()
        created = []
        for gtype, name, kind in _LAYER_SPECS:
            features = buckets.get(gtype) or []
            if not features:
                continue
            layer = self._make_layer(gtype, name, kind, features)
            if layer is None:
                continue
            if group is not None:
                project.addMapLayer(layer, False)
                group.addLayer(layer)
            else:
                project.addMapLayer(layer)
            created.append(layer)
        self.layers = created
        self.set_visible(True)
        _logger.info("Nearby places layers: %s", [layer.name() for layer in created])
        return created

    def highlight(self, osm_id):
        """Select the feature with this osm_id on the place layers."""
        wanted = str(osm_id or "").strip()
        for layer in self.layers:
            try:
                layer.removeSelection()
            except RuntimeError:
                continue
            if not wanted:
                continue
            ids = []
            for feat in layer.getFeatures():
                value = _feature_attr(feat, "osm_id")
                if str(value or "").strip() == wanted:
                    ids.append(feat.id())
            if ids:
                layer.selectByIds(ids)

    def _make_layer(self, gtype, name, kind, features):
        uri = "%s?crs=EPSG:4326" % gtype
        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            _logger.warning("Could not create memory layer %s", name)
            return None
        provider = layer.dataProvider()
        provider.addAttributes([
            _string_field("osm_id"),
            _string_field("name"),
            _string_field("kind"),
            _int_field("distance_m"),
        ])
        layer.updateFields()
        qgs_features = []
        for item in features:
            geom = _qgs_geometry(item.get("geometry"))
            if geom is None:
                continue
            props = item.get("properties") or {}
            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttributes([
                str(props.get("osm_id") or ""),
                str(props.get("name") or ""),
                str(props.get("kind") or ""),
                int(props.get("distance_m") or 0),
            ])
            qgs_features.append(feat)
        if not qgs_features:
            return None
        provider.addFeatures(qgs_features)
        layer.updateExtents()
        layer.setCustomProperty(PLACES_PROPERTY, True)
        _style_layer(layer, kind)
        return layer


def _map_point_from_pixel(canvas, pos):
    """Convert a canvas/viewport pixel to a point in the canvas CRS (QGIS 4: no toMapCoordinates)."""
    x = int(pos.x())
    y = int(pos.y())
    try:
        return canvas.mapSettings().mapToPixel().toMapCoordinates(x, y)
    except Exception as err:
        _logger.debug("mapToPixel toMapCoordinates failed: %s", err)
    if hasattr(canvas, "xyCoordinates"):
        try:
            return canvas.xyCoordinates(pos)
        except TypeError:
            return canvas.xyCoordinates(x, y)
    if hasattr(canvas, "getCoordinateTransform"):
        return canvas.getCoordinateTransform().toMapCoordinates(x, y)
    raise AttributeError("Could not convert canvas pixel to map coordinates")


def pick_place_at(canvas, store, pos):
    """Return the GeoJSON Feature under a canvas pixel, or None."""
    if store is None or not store.is_visible():
        return None
    layers = [layer for layer in store.layers if layer is not None]
    if not layers:
        return None
    map_point = _map_point_from_pixel(canvas, pos)
    try:
        pixels = float(canvas.mapUnitsPerPixel()) * 10.0
    except Exception:
        pixels = 10.0
    half = max(pixels, 1e-8)
    search = QgsRectangle(
        map_point.x() - half,
        map_point.y() - half,
        map_point.x() + half,
        map_point.y() + half,
    )
    canvas_crs = canvas.mapSettings().destinationCrs()
    project = QgsProject.instance()
    for layer in layers:
        try:
            if not layer.isValid():
                continue
            layer_crs = layer.crs()
        except RuntimeError:
            continue
        rect = search
        if layer_crs.isValid() and canvas_crs.isValid() and layer_crs != canvas_crs:
            try:
                transform = QgsCoordinateTransform(canvas_crs, layer_crs, project)
                rect = transform.transform(search)
            except Exception:
                rect = search
        request = QgsFeatureRequest().setFilterRect(rect)
        hit = None
        for feat in layer.getFeatures(request):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            try:
                if geom.intersects(QgsGeometry.fromRect(rect)):
                    hit = feat
                    break
            except Exception as err:
                _logger.debug("Could not test place-feature intersection: %s", err)
                continue
        if hit is None:
            continue
        feature = _feature_from_qgs(hit)
        if feature is None:
            continue
        osm_id = (feature.get("properties") or {}).get("osm_id")
        store.highlight(osm_id)
        return feature
    return None


class PlacesClickFilter(QObject):
    """Click (not drag) a visible place for FoI; if places are hidden, the same click moves the Thing marker."""

    foiPicked = pyqtSignal(object)
    mapClicked = pyqtSignal(float, float)

    def __init__(self, canvas, store, is_placing, hint=None, near_marker=None, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.store = store
        self.is_placing = is_placing
        self.hint = hint
        self.near_marker = near_marker
        self._press = None
        self._host = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        self._had_tracking = self._host.hasMouseTracking() if self._host is not None else False
        if self._host is not None:
            self._host.setMouseTracking(True)
            self._host.installEventFilter(self)

    def close(self):
        host = self._host
        self._host = None
        if host is not None:
            try:
                host.removeEventFilter(self)
            except RuntimeError:
                pass
            try:
                host.setMouseTracking(self._had_tracking)
            except RuntimeError:
                pass
        if self.hint is not None:
            self.hint.leave()

    def eventFilter(self, obj, event):
        if obj is not self._host:
            return False
        press_type = QEvent.Type.MouseButtonPress
        release_type = QEvent.Type.MouseButtonRelease
        move_type = QEvent.Type.MouseMove
        leave_type = QEvent.Type.Leave
        etype = event.type()
        if etype == move_type:
            if self.hint is not None:
                self.hint.follow(event.pos())
            return False
        if etype == leave_type:
            if self.hint is not None:
                self.hint.leave()
            return False
        if etype == press_type and event.button() == _left_button():
            self._press = event.pos()
            return False
        if etype == release_type and event.button() == _left_button():
            press = self._press
            self._press = None
            if press is None:
                return False
            try:
                dragged = (event.pos() - press).manhattanLength() > 8
            except Exception:
                dragged = False
            if dragged or self.is_placing():
                return False
            if self.near_marker is not None and self.near_marker(event.pos()):
                return True
            if self.store is not None and self.store.is_visible():
                feature = pick_place_at(self.canvas, self.store, event.pos())
                if feature is not None:
                    self.foiPicked.emit(feature)
                return False
            try:
                map_point = _map_point_from_pixel(self.canvas, event.pos())
                lat, lon = canvas_point_to_wgs84(self.canvas, map_point)
            except Exception:
                return False
            if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                return False
            self.mapClicked.emit(lat, lon)
        return False
