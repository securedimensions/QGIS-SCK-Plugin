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

"""QGIS UI for STAplus SCK: dock panel, AUTHENIX sign-in, kit serial, and MQTT publish."""

import json
import logging
import os
import time
import traceback

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qgis.core import Qgis, QgsSettings

from . import authenix
from .config import (
    DEFAULT_FOI_LABEL,
    DEFAULT_LOCATION_NAME,
    FOI_JSON_KEY,
    MARKER_CONFIRMED_KEY,
    MARKER_COORD_EPS,
    MARKER_LAT_KEY,
    MARKER_LON_KEY,
    MARKER_MAP_SCALE,
    MARKER_NAME_KEY,
    NEARBY_PLACES_RADIUS_M,
    OAUTH_REDIRECT_HOST,
    OAUTH_REDIRECT_PATH,
    OAUTH_REDIRECT_PORT,
    SCK_ID_KEY,
    SCK_PORT_KEY,
    SETTINGS_GROUP,
    STA_URL,
    CHART_WINDOW_S,
    DISPLAY_NAME_KEY,
)
from .locate import DeviceLocator
from .log import install_python_logging, log_error, log_info, log_success, log_warning
from .maptool import (
    CURSOR_HINT_FOI,
    CURSOR_HINT_THING,
    CanvasCursorHint,
    CanvasZoomControl,
    PlaceMarkerMapTool,
    map_point_to_pixel,
    prepare_canvas_for_marker,
    wgs84_to_canvas_point,
    zoom_canvas_to_wgs84,
)
from .charts import MarkerChartOverlay
from .kit import READING_ROWS, SerialWorker, list_serial_ports, sample_chart_point, serial_backend
from .mqtt import MqttError, MqttPublisher
from .publish import SetupWorker, apply_sample, observation_group_payload
from .publish_dialog import PublishConsentDialog
from .places import PlacesClickFilter, PlacesLayerStore, PlacesWorker
from .sta import StaClient, StaError, grid_system_from_landing

_logger = logging.getLogger("sck.plugin")


def _dock_area():
    """Dock the panel on the right. Qt6 uses DockWidgetArea; older bindings used RightDockWidgetArea."""
    try:
        return Qt.DockWidgetArea.RightDockWidgetArea
    except AttributeError:
        return Qt.RightDockWidgetArea


class SckDock(QDockWidget):
    """Side panel: sign-in, marker, FoI places, kit serial, and MQTT publish."""

    def __init__(self, plugin, parent=None):
        """Build the dock widgets and wire buttons to SckPlugin actions."""
        super().__init__("STAplus SCK", parent)
        self.plugin = plugin
        self.setObjectName("STAplusSckDock")

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)

        self.sign_in_btn = QPushButton("Sign in")
        self.sign_out_btn = QPushButton("Log out")
        self.start_btn = QPushButton("Start publishing")
        self.stop_btn = QPushButton("Stop")
        self.publish_hint = QLabel("")
        self.publish_hint.setWordWrap(True)
        self.coord_label = QLabel()
        self.coord_label.setWordWrap(True)
        try:
            selectable = Qt.TextInteractionFlag.TextSelectableByMouse
        except AttributeError:
            selectable = Qt.TextSelectableByMouse
        self.coord_label.setTextInteractionFlags(selectable)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(DEFAULT_LOCATION_NAME)
        self.place_btn = QPushButton("Place marker on map")
        self.place_btn.setCheckable(True)
        self.locate_btn = QPushButton("Locate me")
        self.confirm_btn = QPushButton("Use this marker")
        self.foi_label = QLabel(DEFAULT_FOI_LABEL)
        self.foi_label.setWordWrap(True)
        self.find_places_btn = QPushButton("Find nearby places")
        self.show_places_cb = QCheckBox("Show nearby places")
        self.clear_foi_btn = QPushButton("Clear FoI")

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(160)
        self.refresh_ports_btn = QPushButton("Refresh")
        self.connect_kit_btn = QPushButton("Connect kit")
        self.serial_status = QLabel("Kit not connected")
        self.serial_status.setWordWrap(True)
        self.chart_hint = QLabel(
            "After the kit is connected, use Show charts or click the Thing marker on the map."
        )
        self.chart_hint.setWordWrap(True)
        self.show_charts_btn = QPushButton("Show charts")
        self.readings = QTableWidget(len(READING_ROWS), 2)
        self.readings.setHorizontalHeaderLabels(["Quantity", "Value"])
        self.readings.verticalHeader().setVisible(False)
        try:
            no_edit = QTableWidget.EditTrigger.NoEditTriggers
        except AttributeError:
            no_edit = QTableWidget.NoEditTriggers
        self.readings.setEditTriggers(no_edit)
        try:
            self.readings.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        except AttributeError:
            self.readings.setSelectionMode(QTableWidget.NoSelection)
        for row, (_key, label) in enumerate(READING_ROWS):
            self.readings.setItem(row, 0, QTableWidgetItem(label))
            self.readings.setItem(row, 1, QTableWidgetItem("—"))
        header = self.readings.horizontalHeader()
        try:
            stretch = QHeaderView.ResizeMode.Stretch
            contents = QHeaderView.ResizeMode.ResizeToContents
        except AttributeError:
            stretch = QHeaderView.Stretch
            contents = QHeaderView.ResizeToContents
        header.setSectionResizeMode(0, contents)
        header.setSectionResizeMode(1, stretch)
        self.readings.setMaximumHeight(220)

        auth_row = QHBoxLayout()
        auth_row.addWidget(self.sign_in_btn)
        auth_row.addWidget(self.sign_out_btn)
        action_row = QHBoxLayout()
        self.start_host = QWidget()
        start_host_layout = QHBoxLayout(self.start_host)
        start_host_layout.setContentsMargins(0, 0, 0, 0)
        start_host_layout.addWidget(self.start_btn)
        action_row.addWidget(self.start_host)
        action_row.addWidget(self.stop_btn)
        name_row = QFormLayout()
        name_row.addRow("Name", self.name_edit)
        marker_row = QHBoxLayout()
        marker_row.addWidget(self.place_btn)
        marker_row.addWidget(self.locate_btn)
        marker_row.addWidget(self.confirm_btn)
        places_row = QHBoxLayout()
        places_row.addWidget(self.find_places_btn)
        places_row.addWidget(self.show_places_cb)
        places_row.addWidget(self.clear_foi_btn)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_ports_btn)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.status)
        layout.addLayout(auth_row)
        layout.addLayout(action_row)
        layout.addWidget(self.publish_hint)
        layout.addLayout(name_row)
        layout.addWidget(self.coord_label)
        layout.addLayout(marker_row)
        layout.addWidget(self.foi_label)
        layout.addLayout(places_row)
        layout.addWidget(QLabel("Smart Citizen Kit"))
        layout.addLayout(port_row)
        layout.addWidget(self.connect_kit_btn)
        layout.addWidget(self.show_charts_btn)
        layout.addWidget(self.serial_status)
        layout.addWidget(self.chart_hint)
        layout.addWidget(self.readings)
        layout.addWidget(QLabel("Plugin log (also written to Log Messages → STAplus SCK)"))
        layout.addWidget(self.log_view)
        self.setWidget(container)

        self.sign_in_btn.clicked.connect(self.plugin.sign_in)
        self.sign_out_btn.clicked.connect(self.plugin.sign_out)
        self.start_btn.clicked.connect(self.plugin.start_publishing)
        self.stop_btn.clicked.connect(self.plugin.stop_publishing)
        self.place_btn.toggled.connect(self.plugin.set_placing_marker)
        self.locate_btn.clicked.connect(self.plugin.locate_me)
        self.confirm_btn.clicked.connect(self.plugin.confirm_marker)
        self.find_places_btn.clicked.connect(self.plugin.find_nearby_places)
        self.show_places_cb.toggled.connect(self.plugin.set_places_visible)
        self.clear_foi_btn.clicked.connect(self.plugin.clear_selected_foi)
        self.refresh_ports_btn.clicked.connect(self.plugin.refresh_serial_ports)
        self.connect_kit_btn.clicked.connect(self.plugin.toggle_serial)
        self.show_charts_btn.clicked.connect(self.plugin.toggle_kit_charts)
        self.fill_ports()
        self.refresh()

    def append_log(self, message):
        """Append one line to the dock's log view (QGIS Log Messages is updated separately)."""
        self.log_view.append(str(message))

    def refresh(self):
        """Update status text and enable/disable buttons from the stored AUTHENIX session."""
        session = authenix.load_session() or {}
        token = session.get("access_token") or ""
        signed_in = bool(token)
        usable = authenix.token_usable(token, session.get("expires_at"))
        user = session.get("user") or {}
        name = user.get("preferred_username") or user.get("sub") or ""
        if usable:
            self.status.setText("Signed in as %s" % (name or "AUTHENIX user"))
        elif signed_in:
            self.status.setText(
                "Session expired%s. Sign in again."
                % (" for %s" % name if name else "")
            )
        else:
            self.status.setText("Not signed in. Sign in with AUTHENIX (QGIS OAuth2).")
        self.sign_in_btn.setEnabled(not usable)
        self.sign_out_btn.setEnabled(signed_in)
        ready = (
            signed_in
            and self.plugin.kit_connected
            and self.plugin.marker_confirmed
            and bool(self.plugin.kit_mac)
            and not self.plugin.publishing
            and not self.plugin.setup_busy()
        )
        self.start_btn.setEnabled(ready)
        self.start_btn.setToolTip(self._start_publishing_tooltip(signed_in, ready))
        self.start_host.setToolTip(self.start_btn.toolTip())
        self.stop_btn.setEnabled(self.plugin.publishing)
        if self.plugin.publishing and self.plugin.setup_busy():
            self.publish_hint.setText("Updating Location and PartyLocation for the marker.")
        elif self.plugin.publishing:
            self.publish_hint.setText("Publishing ObservationGroups to STAplus.")
        elif not signed_in:
            self.publish_hint.setText("Sign in to publish.")
        elif not self.plugin.kit_connected or not self.plugin.kit_mac:
            self.publish_hint.setText("Connect the kit (MAC becomes sck_id) before publishing.")
        elif not self.plugin.marker_confirmed:
            self.publish_hint.setText("Confirm the Thing marker before publishing.")
        else:
            self.publish_hint.setText("")
        self.confirm_btn.setEnabled(self.plugin.lat is not None and self.plugin.lon is not None)
        self.locate_btn.setEnabled(not self.plugin.locator_busy())
        has_coords = self.plugin.lat is not None and self.plugin.lon is not None
        self.find_places_btn.setEnabled(has_coords and not self.plugin.places_busy())
        has_layers = bool(
            self.plugin.places_store is not None and self.plugin.places_store.has_layers()
        )
        self.show_places_cb.setEnabled(has_layers)
        self.show_places_cb.blockSignals(True)
        self.show_places_cb.setChecked(
            has_layers and self.plugin.places_store is not None and self.plugin.places_store.is_visible()
        )
        self.show_places_cb.blockSignals(False)
        self.clear_foi_btn.setEnabled(self.plugin.selected_foi is not None)
        self._sync_serial_controls()
        self._sync_coord_label()
        self._sync_foi_label()

    def _start_publishing_tooltip(self, signed_in, ready):
        """Hover text for Start publishing. Disabled Qt buttons pass hover to start_host."""
        if self.plugin.publishing:
            return "Publishing is already running. Click Stop to finish."
        if self.plugin.setup_busy():
            return "Wait until STAplus Datastreams are created."
        if ready:
            return "Create STAplus Datastreams and start MQTT publishing."
        missing = []
        if not signed_in:
            missing.append("sign in with AUTHENIX")
        if not self.plugin.kit_connected or not self.plugin.kit_mac:
            missing.append("connect the Smart Citizen Kit")
        if not self.plugin.marker_confirmed:
            if self.plugin.lat is None or self.plugin.lon is None:
                missing.append("place a Thing marker on the map and click Use this marker")
            else:
                missing.append("click Use this marker to confirm the Thing location")
        if not missing:
            return "Cannot start publishing yet."
        if len(missing) == 1:
            return "Cannot start publishing yet: %s." % missing[0]
        return "Cannot start publishing yet: %s; and %s." % (
            "; ".join(missing[:-1]),
            missing[-1],
        )

    def fill_ports(self):
        """Reload USB serial devices into the port combo, keeping the last used port if still present."""
        preferred = self.port_combo.currentData() or self.plugin.last_sck_port
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        ports = list_serial_ports()
        for device, label in ports:
            self.port_combo.addItem(label, device)
        if preferred:
            index = self.port_combo.findData(preferred)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.port_combo.blockSignals(False)
        backend = serial_backend()
        if not ports:
            if backend is None:
                self.serial_status.setText(
                    "No serial library in this QGIS Python. Install pyserial, or use a QGIS build with QtSerialPort."
                )
            elif "fail" not in (self.serial_status.text() or "").lower() and not self.plugin.kit_connected:
                self.serial_status.setText("No serial ports. Plug in the kit and click Refresh.")

    def show_readings(self, sample):
        """Update the compact readings table from one parsed SCK sample."""
        sample = sample or {}
        for row, (key, _label) in enumerate(READING_ROWS):
            value = sample.get(key, "—")
            self.readings.setItem(row, 1, QTableWidgetItem(str(value)))

    def clear_readings(self):
        """Reset live values after the kit disconnects."""
        for row in range(len(READING_ROWS)):
            self.readings.setItem(row, 1, QTableWidgetItem("—"))

    def _sync_serial_controls(self):
        """Enable Connect/Disconnect and the port list from the serial worker state."""
        running = self.plugin.serial_busy()
        connected = self.plugin.kit_connected
        stopping = self.plugin.serial_stopping
        self.port_combo.setEnabled(not running)
        self.refresh_ports_btn.setEnabled(not running)
        self.connect_kit_btn.setEnabled((connected and not stopping) or not running)
        if connected or stopping:
            self.connect_kit_btn.setText("Disconnect kit")
        else:
            self.connect_kit_btn.setText("Connect kit")
        has_marker = self.plugin.lat is not None and self.plugin.lon is not None
        charts_open = bool(
            self.plugin.chart_overlay is not None and self.plugin.chart_overlay.isVisible()
        )
        self.show_charts_btn.setEnabled(connected and has_marker)
        self.show_charts_btn.setText("Hide charts" if charts_open else "Show charts")

    def set_placing(self, placing):
        """Keep the Place marker button in sync when QGIS switches map tools."""
        self.place_btn.blockSignals(True)
        self.place_btn.setChecked(bool(placing))
        self.place_btn.blockSignals(False)
        self.place_btn.setText("Click the QGIS map…" if placing else "Place marker on map")

    def _sync_coord_label(self):
        """Show placed or confirmed WGS84 coordinates."""
        lat, lon = self.plugin.lat, self.plugin.lon
        if lat is None or lon is None:
            self.coord_label.setText("No marker yet. Place a marker on the QGIS map.")
            return
        coords = "%.6f, %.6f" % (lat, lon)
        if self.plugin.marker_confirmed:
            self.coord_label.setText("Using marker %s (lat, lon)." % coords)
        else:
            self.coord_label.setText("Marker at %s (lat, lon). Click Use this marker to confirm." % coords)

    def _sync_foi_label(self):
        """Show the selected public place, or The World when none is selected."""
        feature = self.plugin.selected_foi
        if not isinstance(feature, dict):
            self.foi_label.setText(DEFAULT_FOI_LABEL)
            return
        props = feature.get("properties") or {}
        name = str(props.get("name") or "").strip()
        kind = str(props.get("kind") or "").strip()
        if not name:
            self.foi_label.setText(DEFAULT_FOI_LABEL)
            return
        self.foi_label.setText("%s (%s)" % (name, kind) if kind else name)


class SckPlugin:
    """QGIS plugin controller: registers the menu/toolbar action and owns the dock."""

    def __init__(self, iface):
        """iface is the QGIS QgisInterface passed in by classFactory."""
        self.iface = iface
        self.action = None
        self.dock = None
        self.map_tool = None
        self.lat = None
        self.lon = None
        self.location_name = None
        self.marker_confirmed = False
        self.selected_foi = None
        self.locator = None
        self.places_store = None
        self.places_worker = None
        self.places_click_filter = None
        self.cursor_hint = None
        self.zoom_control = None
        self.chart_overlay = None
        self.serial_worker = None
        self.kit_connected = False
        self.serial_stopping = False
        self.last_sck_port = None
        self.kit_mac = None
        self.publishing = False
        self.publish_config = None
        self.mqtt_publisher = None
        self.setup_worker = None
        self._chart_history = []
        self._open_charts_on_sample = False
        install_python_logging()
        self.last_sck_port = self._read_sck_port()
        self.kit_mac = self._read_sck_id()

    def initGui(self):
        """Called by QGIS after load: add menu, toolbar icon, and dock (hidden until opened)."""
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "STAplus SCK", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dock)
        self.iface.addPluginToWebMenu("&STAplus SCK", self.action)
        self.iface.addWebToolBarIcon(self.action)

        self.dock = SckDock(self, self.iface.mainWindow())
        self.iface.addDockWidget(_dock_area(), self.dock)
        self.dock.hide()

        canvas = self.iface.mapCanvas()
        self.map_tool = PlaceMarkerMapTool(canvas, self)
        self.map_tool.locationPicked.connect(self.on_marker_moved)
        canvas.mapToolSet.connect(self._on_map_tool_set)
        if hasattr(canvas, "destinationCrsChanged"):
            canvas.destinationCrsChanged.connect(self._on_canvas_crs_changed)
        self.locator = DeviceLocator(self.iface.mainWindow())
        self.locator.located.connect(self._on_located)
        self.locator.failed.connect(self._on_locate_failed)
        self.places_store = PlacesLayerStore(self.iface.mainWindow())
        self.places_store.clear()
        self.cursor_hint = CanvasCursorHint(canvas)
        self.zoom_control = CanvasZoomControl(canvas)
        self.zoom_control.raise_()
        self.chart_overlay = MarkerChartOverlay(canvas)
        self.chart_overlay.visibilityChanged.connect(self._on_charts_visibility)
        self.places_click_filter = PlacesClickFilter(
            canvas,
            self.places_store,
            self._skip_select_click,
            hint=self.cursor_hint,
            near_marker=self._on_click_near_marker,
        )
        self.places_click_filter.foiPicked.connect(self.on_place_selected)
        self.places_click_filter.mapClicked.connect(self._on_map_click_place_marker)
        self._sync_cursor_hint()

        log_info("STAplus SCK plugin loaded")
        log_info("STA endpoint: %s" % STA_URL)
        try:
            client = StaClient()
            landing = client.landing_page()
            broker = client.mqtt_broker(landing)
            log_info("MQTT broker from STA landing page: %s:%s (%s)" % (
                broker["host"], broker["port"], broker["uri"]
            ))
            grid_system = grid_system_from_landing(landing)
            if grid_system:
                log_info("DGGS gridSystem from STA landing page: %s" % grid_system)
        except Exception as err:
            log_warning("Could not read MQTT endpoint from STA landing page: %s" % err)
        session = authenix.load_session() or {}
        if session.get("access_token"):
            user = session.get("user") or {}
            log_info(
                "Restored AUTHENIX session for %s"
                % (user.get("preferred_username") or user.get("sub") or "stored user")
            )
        else:
            log_info(
                "No stored AUTHENIX session. Use Sign in to log in with QGIS OAuth2 "
                "(http://%s:%s/%s)."
                % (OAUTH_REDIRECT_HOST, OAUTH_REDIRECT_PORT, OAUTH_REDIRECT_PATH)
            )
        self._load_marker()
        self._load_foi()
        if self.chart_overlay is not None and self.lat is not None and self.lon is not None:
            self.chart_overlay.set_anchor(self.lat, self.lon)
        self.dock.refresh()

    def unload(self):
        """Called by QGIS when the plugin is disabled: remove dock, menu, toolbar icon, and map tool."""
        log_info("STAplus SCK plugin unloaded")
        self.stop_publishing(quiet=True)
        self._stop_setup_worker()
        self._stop_serial_worker()
        if self.chart_overlay is not None:
            try:
                self.chart_overlay.visibilityChanged.disconnect(self._on_charts_visibility)
            except (TypeError, RuntimeError):
                pass
            self.chart_overlay.close_overlay()
            self.chart_overlay = None
        if self.locator is not None:
            if hasattr(self.locator, "close"):
                self.locator.close()
            else:
                self.locator.stop()
            self.locator = None
        self._stop_places_worker()
        if self.places_click_filter is not None:
            try:
                self.places_click_filter.foiPicked.disconnect(self.on_place_selected)
            except (TypeError, RuntimeError):
                pass
            try:
                self.places_click_filter.mapClicked.disconnect(self._on_map_click_place_marker)
            except (TypeError, RuntimeError):
                pass
            self.places_click_filter.close()
            self.places_click_filter = None
        if self.cursor_hint is not None:
            self.cursor_hint.close_hint()
            self.cursor_hint = None
        if self.zoom_control is not None:
            self.zoom_control.close_control()
            self.zoom_control = None
        try:
            self.iface.mapCanvas().setToolTip("")
        except Exception as err:
            _logger.debug("Could not clear map canvas tooltip: %s", err)
        self._teardown_map_tool()
        if self.places_store is not None:
            self.places_store.clear()
            self.places_store = None
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginWebMenu("&STAplus SCK", self.action)
            self.iface.removeWebToolBarIcon(self.action)
            self.action = None

    def show_dock(self):
        """Show the STAplus SCK panel and refresh sign-in status."""
        if self.dock is None:
            return
        self.dock.show()
        self.dock.raise_()
        self.dock.refresh()

    def _note(self, message, level=Qgis.MessageLevel.Info):
        """Show a short status in the dock, QGIS message bar, and Log Messages panel."""
        if level == Qgis.MessageLevel.Critical:
            log_error(message)
        elif level == Qgis.MessageLevel.Warning:
            log_warning(message)
        elif level == Qgis.MessageLevel.Success:
            log_success(message)
        else:
            log_info(message)
        if self.dock is not None:
            self.dock.append_log(message)
        self.iface.messageBar().pushMessage("STAplus SCK", message, level, 8)

    def sign_in(self):
        """Open the system browser for AUTHENIX login and store the session."""
        self.show_dock()
        self._note(
            "Starting AUTHENIX sign-in. Complete login in the browser; QGIS listens on http://%s:%s/%s."
            % (OAUTH_REDIRECT_HOST, OAUTH_REDIRECT_PORT, OAUTH_REDIRECT_PATH)
        )
        try:
            session = authenix.acquire_access_token(force_reauth=True)
        except authenix.AuthError as err:
            self._fail("AUTHENIX sign-in", err)
            return
        except Exception as err:
            self._fail("AUTHENIX sign-in", err)
            return
        authenix.save_session(session)
        user = session.get("user") or {}
        name = user.get("preferred_username") or user.get("sub") or "AUTHENIX user"
        log_success("Signed in as %s" % name)
        self._note("Signed in as %s" % name, Qgis.MessageLevel.Success)
        if self.dock is not None:
            self.dock.refresh()

    def sign_out(self):
        """Clear QGIS OAuth2 tokens and open AUTHENIX logout (needs id_token_hint)."""
        session = authenix.load_session() or {}
        try:
            id_token = authenix.id_token_for_logout(session)
            url = authenix.logout_url(id_token)
        except authenix.AuthError as err:
            authenix.clear_session()
            if self.dock is not None:
                self.dock.refresh()
            self._fail("AUTHENIX log out", err)
            return
        log_info("Opening AUTHENIX logout: %s" % url.split("?")[0])
        QDesktopServices.openUrl(QUrl(url))
        authenix.clear_session()
        self.stop_publishing(quiet=True)
        self._note("Logged out of AUTHENIX. QGIS OAuth2 tokens cleared.")
        if self.dock is not None:
            self.dock.refresh()

    def start_publishing(self):
        """Ask for Party name, license, and consent, then create Datastreams and start MQTT."""
        self.show_dock()
        if self.publishing or self.setup_busy():
            return
        if not self.kit_connected or not self.kit_mac:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No kit",
                "Connect the Smart Citizen Kit first. Its MAC address is stored as Thing properties.sck_id.",
            )
            return
        if not self.marker_confirmed or self.lat is None or self.lon is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No location",
                "Place a marker and click Use this marker before publishing.",
            )
            return
        try:
            session = authenix.ensure_fresh_session()
            client = StaClient(session)
            licenses = client.list_template_licenses()
            party = client.find_party()
        except (authenix.AuthError, StaError) as err:
            self._fail("Could not start publishing", err, sign_out=isinstance(err, authenix.AuthError))
            return
        except Exception as err:
            self._fail("Could not start publishing", err)
            return
        if not licenses:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No licenses",
                "The STAplus service did not return any License templates.",
            )
            return
        user = session.get("user") or {}
        default_name = (
            self._read_display_name()
            or (party or {}).get("displayName")
            or user.get("preferred_username")
            or user.get("name")
        )
        dialog = PublishConsentDialog(licenses, default_name, self.iface.mainWindow())
        try:
            accepted = QDialog.DialogCode.Accepted
        except AttributeError:
            accepted = QDialog.Accepted
        if dialog.exec() != accepted:
            return
        choices = dialog.values()
        self._save_display_name(choices["display_name"])
        try:
            authenix.ensure_fresh_session()
        except (authenix.AuthError, StaError) as err:
            self._fail("Could not start publishing", err, sign_out=isinstance(err, authenix.AuthError))
            return
        except Exception as err:
            self._fail("Could not start publishing", err)
            return
        params = {
            "display_name": choices["display_name"],
            "sck_id": self.kit_mac,
            "lat": self.lat,
            "lon": self.lon,
            "location_name": self.sta_location_name(),
            "foi_spec": self.selected_foi,
            "license_id": choices["license_id"],
            "attribution_text": choices["attribution_text"] if choices["needs_attribution"] else "",
        }
        self._stop_setup_worker()
        self.setup_worker = SetupWorker(params, self.iface.mainWindow())
        self.setup_worker.succeeded.connect(self.on_setup_ready)
        self.setup_worker.failed.connect(self.on_setup_failed)
        self._note("Creating STAplus Datastreams for kit %s…" % self.kit_mac)
        self.setup_worker.start()
        if self.dock is not None:
            self.dock.refresh()

    def setup_busy(self):
        """True while Party / Thing / Datastreams are being created for publishing."""
        return bool(self.setup_worker is not None and self.setup_worker.isRunning())

    def on_setup_ready(self, config):
        """Connect MQTT after STAplus entities exist, then publish each kit sample."""
        self.setup_worker = None
        relocating = bool(
            self.publishing
            and self.mqtt_publisher is not None
            and self.mqtt_publisher.is_connected()
        )
        self.publish_config = dict(config or {})
        pretty = json.dumps(self.publish_config, indent=2, default=str)
        log_success("Publishing setup:\n%s" % pretty)
        if relocating:
            if self.dock is not None:
                self.dock.append_log(pretty)
                self.dock.refresh()
            self._note(
                "Publishing to Thing %s at “%s” (PartyLocation %s)."
                % (
                    self.publish_config.get("thing_id"),
                    self.publish_config.get("location_name"),
                    self.publish_config.get("party_location_id"),
                ),
                Qgis.MessageLevel.Success,
            )
            self._maybe_relocate_publishing()
            return
        try:
            session = authenix.load_session() or {}
            host = self.publish_config.get("mqtt_host")
            port = self.publish_config.get("mqtt_port")
            if not host or not port:
                broker = StaClient(session).mqtt_broker()
                host, port = broker["host"], broker["port"]
                self.publish_config["mqtt_host"] = host
                self.publish_config["mqtt_port"] = port
                self.publish_config["mqtt_uri"] = broker.get("uri")
            publisher = MqttPublisher(host, port)
            publisher.connect(session.get("access_token") or "")
        except (authenix.AuthError, MqttError) as err:
            self.publish_config = None
            self._fail("MQTT connect failed", err, sign_out=isinstance(err, authenix.AuthError))
            if self.dock is not None:
                self.dock.refresh()
            return
        except Exception as err:
            self.publish_config = None
            self._fail("MQTT connect failed", err)
            if self.dock is not None:
                self.dock.refresh()
            return
        self.mqtt_publisher = publisher
        self.publishing = True
        if self.dock is not None:
            self.dock.append_log(pretty)
            self.dock.refresh()
        self._note(
            "Publishing to Thing %s (sck_id %s)."
            % (self.publish_config.get("thing_id"), self.publish_config.get("sck_id")),
            Qgis.MessageLevel.Success,
        )
        self._maybe_relocate_publishing()

    def on_setup_failed(self, error):
        self.setup_worker = None
        if self.publishing and self.mqtt_publisher is not None:
            if self.dock is not None:
                self.dock.refresh()
            QMessageBox.critical(
                self.iface.mainWindow(),
                "STAplus setup failed",
                "Could not switch to the new marker. Publishing continues at the previous site.\n\n%s"
                % error,
            )
            log_error("STAplus relocate failed:\n%s" % error)
            return
        self.publishing = False
        self.publish_config = None
        if self.dock is not None:
            self.dock.refresh()
        QMessageBox.critical(self.iface.mainWindow(), "STAplus setup failed", str(error))
        log_error("STAplus setup failed:\n%s" % error)

    def stop_publishing(self, quiet=False):
        """Stop MQTT publishing. The kit serial connection stays open."""
        was = self.publishing or self.mqtt_publisher is not None
        self.publishing = False
        self.publish_config = None
        publisher = self.mqtt_publisher
        self.mqtt_publisher = None
        if publisher is not None:
            try:
                publisher.disconnect()
            except Exception as err:
                _logger.debug("Could not disconnect MQTT publisher: %s", err)
        if was and not quiet:
            self._note("Publishing stopped. The kit can stay connected.")
        if self.dock is not None:
            self.dock.refresh()

    def _stop_setup_worker(self):
        worker = self.setup_worker
        self.setup_worker = None
        if worker is None:
            return
        try:
            worker.succeeded.disconnect(self.on_setup_ready)
            worker.failed.disconnect(self.on_setup_failed)
        except (TypeError, RuntimeError):
            pass
        if worker.isRunning():
            worker.wait(100)

    def _read_display_name(self):
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            return str(settings.value(DISPLAY_NAME_KEY) or "").strip() or None
        finally:
            settings.endGroup()

    def _save_display_name(self, name):
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            if name:
                settings.setValue(DISPLAY_NAME_KEY, str(name))
            else:
                settings.remove(DISPLAY_NAME_KEY)
        finally:
            settings.endGroup()

    def _fail(self, title, err, sign_out=False):
        """Show a critical error. If sign_out is True, drop the expired session so Sign in works again."""
        detail = str(err)
        log_error("%s: %s" % (title, detail))
        log_error(traceback.format_exc())
        if sign_out:
            authenix.clear_session()
            log_warning("Cleared the expired AUTHENIX session. Use Sign in to authenticate again.")
        if self.dock is not None:
            self.dock.append_log("%s: %s" % (title, detail))
            self.dock.refresh()
        QMessageBox.critical(self.iface.mainWindow(), title, detail)
        self.iface.messageBar().pushMessage("STAplus SCK", title, Qgis.MessageLevel.Critical, 8)

    def set_placing_marker(self, placing):
        """Activate or release the canvas click tool that sets the Thing-location marker."""
        canvas = self.iface.mapCanvas()
        if placing:
            self.show_dock()
            added = prepare_canvas_for_marker(canvas, self.lat, self.lon)
            if added:
                self._note(
                    "The QGIS project had no map layers, so OpenStreetMap was added. "
                    "Click the map (the main QGIS canvas, not this panel) to place the marker."
                )
            else:
                self._note("Click the QGIS map canvas to place or move the marker.")
            if self.lat is not None and self.lon is not None and self.map_tool is not None:
                self.map_tool.set_wgs84(self.lat, self.lon)
            canvas.setMapTool(self.map_tool)
            canvas.refresh()
            canvas.setFocus()
        elif canvas.mapTool() is self.map_tool:
            canvas.unsetMapTool(self.map_tool)
            self._restore_pan()
        if self.dock is not None:
            self.dock.set_placing(placing and canvas.mapTool() is self.map_tool)
        self._sync_cursor_hint()

    def _restore_pan(self):
        """Put the QGIS pan tool back so the canvas can be moved after placing a marker."""
        pan = getattr(self.iface, "actionPan", None)
        action = pan() if callable(pan) else None
        if action is not None:
            action.trigger()
            return
        try:
            from qgis.gui import QgsMapToolPan
        except ImportError:
            return
        canvas = self.iface.mapCanvas()
        if getattr(self, "_pan_tool", None) is None:
            self._pan_tool = QgsMapToolPan(canvas)
        canvas.setMapTool(self._pan_tool)

    def locator_busy(self):
        """True while Locate me is waiting for the OS position."""
        return bool(self.locator is not None and self.locator.is_busy())

    def serial_busy(self):
        """True while the serial worker thread is running (connecting, reading, or stopping)."""
        return bool(self.serial_worker is not None and self.serial_worker.isRunning())

    def refresh_serial_ports(self):
        """Reload the USB serial port list in the dock."""
        if self.dock is not None:
            self.dock.fill_ports()
            self.dock.refresh()

    def toggle_serial(self):
        """Connect to the Smart Citizen Kit, or stop the serial worker if it is running."""
        self.show_dock()
        if self.serial_busy():
            self.serial_stopping = True
            self.serial_worker.stop()
            if self.dock is not None:
                self.dock.serial_status.setText("Disconnecting…")
                self.dock.refresh()
            return
        if serial_backend() is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No serial library",
                "This QGIS Python has neither pyserial nor QtSerialPort. "
                "Install pyserial in the QGIS Python environment, or use a QGIS build that includes QtSerialPort.",
            )
            if self.dock is not None:
                self.dock.refresh()
            return
        port = self.dock.port_combo.currentData() if self.dock is not None else None
        if not port:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No serial port",
                "Plug in the Smart Citizen Kit and click Refresh.",
            )
            if self.dock is not None:
                self.dock.fill_ports()
                self.dock.refresh()
            return
        self.serial_stopping = False
        if self.dock is not None:
            self.dock.serial_status.setText("Opening %s…" % port)
            self.dock.refresh()
        self.serial_worker = SerialWorker(port, self.iface.mainWindow())
        self.serial_worker.connected.connect(self.on_serial_connected)
        self.serial_worker.sample.connect(self.on_kit_sample)
        self.serial_worker.failed.connect(self.on_serial_failed)
        self.serial_worker.finished_ok.connect(self.on_serial_finished)
        self.serial_worker.start()

    def on_serial_connected(self, port, mac=None):
        """Serial port opened, kit MAC read, and SCK monitor started."""
        self.kit_connected = True
        self.serial_stopping = False
        self.last_sck_port = port
        self.kit_mac = (mac or "").strip() or None
        self._save_sck_port(port)
        if self.kit_mac:
            self._save_sck_id(self.kit_mac)
        self._chart_history = []
        self._open_charts_on_sample = True
        if self.chart_overlay is not None:
            self.chart_overlay.set_history([])
            if self.lat is not None and self.lon is not None:
                self.chart_overlay.set_anchor(self.lat, self.lon)
        if self.dock is not None:
            if self.kit_mac:
                self.dock.serial_status.setText("Connected: %s · MAC %s" % (port, self.kit_mac))
            else:
                self.dock.serial_status.setText("Connected: %s · MAC unknown" % port)
            self.dock.clear_readings()
            self.dock.refresh()
        if self.kit_mac:
            self._note(
                "Smart Citizen Kit connected on %s (sck_id %s)." % (port, self.kit_mac),
                Qgis.MessageLevel.Success,
            )
        else:
            self._note(
                "Smart Citizen Kit connected on %s. No MAC in the serial config output." % port,
                Qgis.MessageLevel.Warning,
            )

    def on_serial_failed(self, error):
        """Show why the kit could not be opened or read."""
        self.kit_connected = False
        self._reset_kit_charts()
        if self.dock is not None:
            self.dock.serial_status.setText("Connection failed")
            self.dock.refresh()
        QMessageBox.critical(self.iface.mainWindow(), "Serial error", str(error))
        log_error("SCK serial error:\n%s" % error)

    def on_serial_finished(self):
        """Worker thread ended (disconnect, failure, or missing backend)."""
        self.kit_connected = False
        self.serial_stopping = False
        self.serial_worker = None
        self._reset_kit_charts()
        if self.publishing:
            self.stop_publishing(quiet=True)
            self._note("Kit disconnected; publishing stopped.")
        if self.dock is not None:
            text = self.dock.serial_status.text() or ""
            if "fail" not in text.lower():
                self.dock.serial_status.setText("Kit not connected")
            self.dock.refresh()

    def on_kit_sample(self, sample):
        """Update the dock table and charts; publish an ObservationGroup when publishing."""
        if not self.kit_connected or not isinstance(sample, dict):
            return
        values = sample
        if self.publishing and self.publish_config is not None:
            try:
                values = self._publish_sample(sample)
            except Exception as err:
                self._note("Publish failed: %s" % err, Qgis.MessageLevel.Warning)
                log_error(traceback.format_exc())
        if self.dock is not None:
            self.dock.show_readings(values)
        point = sample_chart_point(values)
        self._chart_history.append(point)
        cutoff = time.time() - CHART_WINDOW_S
        while self._chart_history and float(self._chart_history[0].get("ts") or 0) < cutoff:
            del self._chart_history[0]
        if self.chart_overlay is None:
            return
        self.chart_overlay.set_history(self._chart_history)
        if self._open_charts_on_sample and self.lat is not None and self.lon is not None:
            self._open_charts_on_sample = False
            self.chart_overlay.set_anchor(self.lat, self.lon)
            self.chart_overlay.show_at_marker()

    def _publish_sample(self, sample):
        """Normalize the sample, PUBLISH one ObservationGroup, return values for the table."""
        values = apply_sample(self.publish_config, sample)
        payload = json.dumps(observation_group_payload(self.publish_config, values))
        if self.mqtt_publisher is None or not self.mqtt_publisher.is_connected():
            raise MqttError("MQTT publisher is not connected")
        self.mqtt_publisher.publish(payload)
        log_info(
            "MQTT PUBACK for ObservationGroup at %s (sck_id %s)"
            % (values.get("phenomenon_time"), self.publish_config.get("sck_id"))
        )
        return values

    def _reset_kit_charts(self):
        """Clear live history and hide the canvas overlay when the kit is not connected."""
        self._open_charts_on_sample = False
        self._chart_history = []
        if self.chart_overlay is not None:
            self.chart_overlay.set_history([])
            self.chart_overlay.hide()
        if self.dock is not None:
            self.dock.clear_readings()

    def _on_click_near_marker(self, pos):
        """Toggle kit charts when the user clicks the Thing marker (kit must be connected)."""
        if self._skip_select_click():
            return False
        if not self.kit_connected or self.chart_overlay is None:
            return False
        if not self._pixel_near_thing_marker(pos):
            return False
        self.chart_overlay.set_anchor(self.lat, self.lon)
        self.chart_overlay.set_history(self._chart_history)
        self.chart_overlay.toggle()
        if self.dock is not None:
            self.dock.refresh()
        return True

    def toggle_kit_charts(self):
        """Open or close the map-canvas chart overlay from the dock button."""
        self.show_dock()
        if not self.kit_connected:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No kit",
                "Connect the Smart Citizen Kit first.",
            )
            return
        if self.lat is None or self.lon is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No location",
                "Place a marker on the QGIS map first.",
            )
            return
        if self.chart_overlay is None:
            return
        self.chart_overlay.set_anchor(self.lat, self.lon)
        self.chart_overlay.set_history(self._chart_history)
        self.chart_overlay.toggle()
        if self.dock is not None:
            self.dock.refresh()

    def _on_charts_visibility(self, _visible):
        """Keep Show charts / Hide charts in sync when the overlay is closed with ×."""
        if self.dock is not None:
            self.dock.refresh()

    def _pixel_near_thing_marker(self, pos, radius=24):
        """True if canvas pixel pos is within radius of the Thing marker."""
        if self.lat is None or self.lon is None or pos is None:
            return False
        canvas = self.iface.mapCanvas()
        try:
            map_point = wgs84_to_canvas_point(canvas, self.lat, self.lon)
            px, py = map_point_to_pixel(canvas, map_point)
        except Exception:
            return False
        dx = pos.x() - px
        dy = pos.y() - py
        return (dx * dx + dy * dy) <= (radius * radius)

    def _stop_serial_worker(self):
        """Stop the SCK serial thread on plugin unload."""
        worker = self.serial_worker
        self.serial_worker = None
        self.kit_connected = False
        self.serial_stopping = False
        if worker is None:
            return
        try:
            worker.connected.disconnect()
            worker.sample.disconnect()
            worker.failed.disconnect()
            worker.finished_ok.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            worker.stop()
        except Exception as err:
            _logger.debug("Could not stop serial worker: %s", err)
        if worker.isRunning():
            worker.wait(3000)

    def _read_sck_port(self):
        """Last serial device path stored under SETTINGS_GROUP."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            return str(settings.value(SCK_PORT_KEY) or "") or None
        finally:
            settings.endGroup()

    def _save_sck_port(self, port):
        """Remember the last successful SCK serial device."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            if port:
                settings.setValue(SCK_PORT_KEY, str(port))
            else:
                settings.remove(SCK_PORT_KEY)
        finally:
            settings.endGroup()

    def _read_sck_id(self):
        """Last kit MAC (Thing properties.sck_id) stored under SETTINGS_GROUP."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            return str(settings.value(SCK_ID_KEY) or "").strip() or None
        finally:
            settings.endGroup()

    def _save_sck_id(self, sck_id):
        """Remember the last kit MAC used as sck_id."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            if sck_id:
                settings.setValue(SCK_ID_KEY, str(sck_id))
            else:
                settings.remove(SCK_ID_KEY)
        finally:
            settings.endGroup()

    def locate_me(self):
        """Move the marker to the computer's current location (same as SCK-App Use my location)."""
        self.show_dock()
        canvas = self.iface.mapCanvas()
        added = prepare_canvas_for_marker(canvas, self.lat, self.lon)
        if added:
            self._note(
                "The QGIS project had no map layers, so OpenStreetMap was added."
            )
        if self.locator is None:
            self.locator = DeviceLocator(self.iface.mainWindow())
            self.locator.located.connect(self._on_located)
            self.locator.failed.connect(self._on_locate_failed)
        self._note("Finding current location…")
        if self.dock is not None:
            self.dock.refresh()
        if not self.locator.start():
            if self.dock is not None:
                self.dock.refresh()

    def _on_located(self, lat, lon):
        """Place the unconfirmed marker at the OS position and zoom the canvas there."""
        canvas = self.iface.mapCanvas()
        prepare_canvas_for_marker(canvas, lat, lon)
        if self.map_tool is not None:
            self.map_tool.set_wgs84(lat, lon)
        zoom_canvas_to_wgs84(canvas, lat, lon, MARKER_MAP_SCALE)
        self.on_marker_moved(lat, lon)
        self._note(
            "Marker moved to your location %.6f, %.6f. Click Use this marker to confirm."
            % (lat, lon),
            Qgis.MessageLevel.Success,
        )
        if self.dock is not None:
            self.dock.refresh()

    def _on_locate_failed(self, message):
        """Show why Locate me failed (usually Location Services / permission)."""
        if self.dock is not None:
            self.dock.refresh()
        QMessageBox.warning(self.iface.mainWindow(), "Locate me", str(message))
        self.iface.messageBar().pushMessage(
            "STAplus SCK", str(message), Qgis.MessageLevel.Warning, 8
        )
        log_warning(str(message))

    def on_marker_moved(self, lat, lon):
        """Map click (not yet confirmed). A later click moves the same marker."""
        self.lat = float(lat)
        self.lon = float(lon)
        self.marker_confirmed = False
        self._forget_nearby_places(announce=True)
        if self.chart_overlay is not None:
            self.chart_overlay.set_anchor(self.lat, self.lon)
            if self.kit_connected and self._chart_history and self._open_charts_on_sample:
                self._open_charts_on_sample = False
                self.chart_overlay.set_history(self._chart_history)
                self.chart_overlay.show_at_marker()
        if self.dock is not None:
            self.dock.refresh()
        log_info("Marker placed at %.6f, %.6f (lat, lon). Confirm with Use this marker." % (self.lat, self.lon))

    def _name_from_dock(self):
        """Current Location name from the dock, stripped; empty if none."""
        if self.dock is None:
            return (self.location_name or "").strip() or None
        return (self.dock.name_edit.text() or "").strip() or None

    def sta_location_name(self):
        """STAplus Location.name: dock/saved text, or DEFAULT_LOCATION_NAME if empty."""
        return self._name_from_dock() or DEFAULT_LOCATION_NAME

    def confirm_marker(self):
        """Confirm the current marker. STAplus Location is written when publishing starts."""
        self.show_dock()
        if self.lat is None or self.lon is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No location",
                "Place a marker on the QGIS map first.",
            )
            return
        self.location_name = self._name_from_dock()
        self.marker_confirmed = True
        self._save_marker()
        self._note(
            "Using marker %.6f, %.6f (lat, lon) as “%s”."
            % (self.lat, self.lon, self.sta_location_name()),
            Qgis.MessageLevel.Success,
        )
        if self.dock is not None:
            self.dock.refresh()
        self._maybe_relocate_publishing()

    def _publish_site_matches_marker(self):
        """True when the live marker name and position match the publishing Thing/PartyLocation."""
        cfg = self.publish_config or {}
        try:
            plat = float(cfg["lat"])
            plon = float(cfg["lon"])
        except (KeyError, TypeError, ValueError):
            return False
        if self.lat is None or self.lon is None:
            return False
        if abs(plat - float(self.lat)) > MARKER_COORD_EPS or abs(plon - float(self.lon)) > MARKER_COORD_EPS:
            return False
        return (cfg.get("location_name") or "") == self.sta_location_name()

    def _maybe_relocate_publishing(self):
        """If publishing and the confirmed marker changed, add Location and/or PartyLocation."""
        if not self.publishing or self.publish_config is None or self.setup_busy():
            return
        if not self.marker_confirmed or self.lat is None or self.lon is None:
            return
        if self._publish_site_matches_marker():
            return
        self._relocate_publishing()

    def _relocate_publishing(self):
        """Keep MQTT and the Thing. Add a Location and PartyLocation for the new marker."""
        try:
            authenix.ensure_fresh_session()
        except (authenix.AuthError, StaError) as err:
            self._fail(
                "Could not switch to the new marker",
                err,
                sign_out=isinstance(err, authenix.AuthError),
            )
            return
        except Exception as err:
            self._fail("Could not switch to the new marker", err)
            return
        params = {
            "location_only": True,
            "config": dict(self.publish_config or {}),
            "lat": self.lat,
            "lon": self.lon,
            "location_name": self.sta_location_name(),
        }
        self._stop_setup_worker()
        self.setup_worker = SetupWorker(params, self.iface.mainWindow())
        self.setup_worker.succeeded.connect(self.on_setup_ready)
        self.setup_worker.failed.connect(self.on_setup_failed)
        self._note(
            "Updating STAplus Location and PartyLocation for “%s”…"
            % self.sta_location_name()
        )
        self.setup_worker.start()
        if self.dock is not None:
            self.dock.refresh()

    def places_busy(self):
        """True while Overpass lookup is running."""
        worker = self.places_worker
        return bool(worker is not None and worker.isRunning())

    def find_nearby_places(self):
        """Query Overpass for named public places around the Thing marker."""
        self.show_dock()
        if self.lat is None or self.lon is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "No location",
                "Place a marker on the QGIS map first, then find nearby places.",
            )
            return
        if self.places_busy():
            return
        if self.dock is not None and self.dock.place_btn.isChecked():
            self.set_placing_marker(False)
        if self.places_store is not None and self.places_store.has_layers():
            self.places_store.set_visible(True)
        self._note("Looking up nearby public places…")
        worker = PlacesWorker(self.lat, self.lon, self.iface.mainWindow())
        self.places_worker = worker
        worker.succeeded.connect(self._on_places_ready)
        worker.failed.connect(self._on_places_failed)
        worker.finished.connect(self._on_places_worker_finished)
        worker.start()
        if self.dock is not None:
            self.dock.refresh()

    def _on_places_worker_finished(self):
        if self.dock is not None:
            self.dock.refresh()

    def _on_places_ready(self, collection):
        worker = self.places_worker
        origin_lat = getattr(worker, "lat", None)
        origin_lon = getattr(worker, "lon", None)
        if (
            self.lat is None
            or self.lon is None
            or origin_lat is None
            or origin_lon is None
            or abs(self.lat - origin_lat) > 1e-6
            or abs(self.lon - origin_lon) > 1e-6
        ):
            return
        features = (collection or {}).get("features") or []
        if self.places_store is not None:
            self.places_store.set_collection(collection)
        selected_id = None
        if self.selected_foi:
            selected_id = (self.selected_foi.get("properties") or {}).get("osm_id")
            ids = {(item.get("properties") or {}).get("osm_id") for item in features}
            if selected_id not in ids:
                self.selected_foi = None
                self._save_foi()
                selected_id = None
        if self.places_store is not None:
            self.places_store.highlight(selected_id)
            self.places_store.set_visible(True)
        self._sync_cursor_hint()
        if not features:
            self._note("No named public places within %d m." % NEARBY_PLACES_RADIUS_M)
            if self.dock is not None:
                self.dock.refresh()
            return
        count = len(features)
        self._note(
            "Found %d public place%s. Pan the map as usual; click a place to use as Feature of Interest."
            % (count, "" if count == 1 else "s"),
            Qgis.MessageLevel.Success,
        )
        if self.dock is not None:
            self.dock.refresh()

    def _on_places_failed(self, error):
        worker = self.places_worker
        origin_lat = getattr(worker, "lat", None)
        origin_lon = getattr(worker, "lon", None)
        if (
            origin_lat is not None
            and origin_lon is not None
            and self.lat is not None
            and self.lon is not None
            and (
                abs(self.lat - origin_lat) > 1e-6
                or abs(self.lon - origin_lon) > 1e-6
            )
        ):
            return
        QMessageBox.warning(
            self.iface.mainWindow(),
            "Nearby places",
            "Could not look up nearby public places.\n\n%s" % error,
        )
        self._note("Could not look up nearby places: %s" % error, Qgis.MessageLevel.Warning)
        if self.dock is not None:
            self.dock.refresh()

    def _placing_marker(self):
        """True while the Place marker map tool is the active canvas tool."""
        canvas = self.iface.mapCanvas()
        return self.map_tool is not None and canvas.mapTool() is self.map_tool

    def _is_zoom_tool(self):
        """True when QGIS Zoom In / Zoom Out is the active map tool."""
        tool = self.iface.mapCanvas().mapTool()
        if tool is None:
            return False
        try:
            from qgis.gui import QgsMapToolZoom
            if isinstance(tool, QgsMapToolZoom):
                return True
        except Exception as err:
            _logger.debug("QgsMapToolZoom check failed: %s", err)
        return "Zoom" in type(tool).__name__

    def _skip_select_click(self):
        """Do not treat this click as FoI/Thing select (place-marker or zoom tool is active)."""
        return self._placing_marker() or self._is_zoom_tool()

    def _cursor_mode_text(self):
        """Caption for the cursor overlay: FoI pick vs Thing location."""
        if self._is_zoom_tool():
            return ""
        if self._placing_marker():
            return CURSOR_HINT_THING
        if self.places_store is not None and self.places_store.is_visible():
            return CURSOR_HINT_FOI
        return CURSOR_HINT_THING

    def _sync_cursor_hint(self):
        """Update the canvas cursor caption and tooltip to match the active click mode."""
        text = self._cursor_mode_text()
        if self.cursor_hint is not None:
            self.cursor_hint.set_mode(text)
        try:
            self.iface.mapCanvas().setToolTip(text)
        except Exception as err:
            _logger.debug("Could not set map canvas tooltip: %s", err)

    def set_places_visible(self, visible):
        """Show or hide the SCK nearby places layer group."""
        if self.places_store is None:
            return
        self.places_store.set_visible(bool(visible))
        self._sync_cursor_hint()
        if self.dock is not None:
            self.dock.refresh()

    def _on_map_click_place_marker(self, lat, lon):
        """Move the Thing marker from a canvas click when nearby places are hidden."""
        if self.map_tool is not None:
            self.map_tool.set_wgs84(lat, lon)
        self.on_marker_moved(lat, lon)

    def on_place_selected(self, feature):
        """Use the clicked OSM place as FeatureOfInterest (Thing marker stays put)."""
        if not isinstance(feature, dict) or not feature.get("geometry"):
            return
        props = feature.get("properties") or {}
        name = str(props.get("name") or "").strip()
        if not name:
            return
        self.selected_foi = feature
        self._save_foi()
        kind = str(props.get("kind") or "").strip()
        label = "%s (%s)" % (name, kind) if kind else name
        self._note("Feature of Interest: %s" % label, Qgis.MessageLevel.Success)
        session = authenix.load_session() or {}
        if authenix.token_usable(session.get("access_token"), session.get("expires_at")):
            self._apply_foi_to_sta()
        else:
            self._note("FoI saved. Sign in to create it on STAplus.")
        if self.dock is not None:
            self.dock.refresh()

    def clear_selected_foi(self):
        """Drop the OSM FoI selection and go back to The World (no geometry)."""
        self.selected_foi = None
        self._save_foi()
        if self.places_store is not None:
            self.places_store.highlight(None)
        self._note("Using %s as Feature of Interest." % DEFAULT_FOI_LABEL)
        session = authenix.load_session() or {}
        if authenix.token_usable(session.get("access_token"), session.get("expires_at")):
            self._apply_foi_to_sta()
        if self.dock is not None:
            self.dock.refresh()

    def _apply_foi_to_sta(self):
        """Create or reuse the selected (or unset) FeatureOfInterest on STAplus."""
        try:
            session = authenix.ensure_fresh_session()
            client = StaClient(session)
            foi = client.ensure_feature_of_interest(self.selected_foi)
            pretty = json.dumps(
                {
                    "@iot.id": (foi or {}).get("@iot.id"),
                    "name": (foi or {}).get("name"),
                    "description": (foi or {}).get("description"),
                    "feature": (foi or {}).get("feature"),
                },
                indent=2,
                default=str,
            )
            log_success("FeatureOfInterest ready:\n%s" % pretty)
            if self.dock is not None:
                self.dock.append_log(pretty)
        except (authenix.AuthError, StaError) as err:
            self._fail("Could not update FeatureOfInterest", err, sign_out=isinstance(err, authenix.AuthError))
        except Exception as err:
            self._fail("Could not update FeatureOfInterest", err)

    def _forget_nearby_places(self, announce=False):
        """Clear place layers and FoI selection after the Thing marker moves."""
        had = self.selected_foi is not None or (
            self.places_store is not None and bool(self.places_store.layers)
        )
        self.selected_foi = None
        self._save_foi()
        if self.places_store is not None:
            self.places_store.clear()
        self._sync_cursor_hint()
        if announce and had:
            self._note("Marker moved. Find nearby places again.")

    def _stop_places_worker(self):
        worker = self.places_worker
        self.places_worker = None
        if worker is None:
            return
        try:
            worker.succeeded.disconnect(self._on_places_ready)
        except (TypeError, RuntimeError):
            pass
        try:
            worker.failed.disconnect(self._on_places_failed)
        except (TypeError, RuntimeError):
            pass
        if worker.isRunning():
            worker.requestInterruption()
            worker.wait(500)

    def _load_foi(self):
        """Restore the last selected OSM FoI from QgsSettings (label only until Find)."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            raw = settings.value(FOI_JSON_KEY)
        finally:
            settings.endGroup()
        feature = None
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("geometry"):
                feature = parsed
        self.selected_foi = feature
        if feature:
            props = feature.get("properties") or {}
            log_info(
                "Restored Feature of Interest %s (%s)"
                % (props.get("name") or "", props.get("osm_id") or "")
            )

    def _save_foi(self):
        """Persist the selected FoI GeoJSON, or remove it when cleared."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            if self.selected_foi:
                settings.setValue(FOI_JSON_KEY, json.dumps(self.selected_foi))
            else:
                settings.remove(FOI_JSON_KEY)
        finally:
            settings.endGroup()

    def _on_map_tool_set(self, *args):
        """Uncheck Place marker when the user switches to another QGIS map tool."""
        new_tool = args[0] if args else None
        if self.dock is not None:
            self.dock.set_placing(new_tool is self.map_tool)
        self._sync_cursor_hint()

    def _on_canvas_crs_changed(self):
        """Keep the vertex marker on the same WGS84 point after a project CRS change."""
        if self.map_tool is None or self.lat is None or self.lon is None:
            return
        self.map_tool.refresh_from_wgs84(self.lat, self.lon)

    def _load_marker(self):
        """Restore the last confirmed marker and Location name from QgsSettings."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            lat = settings.value(MARKER_LAT_KEY, None)
            lon = settings.value(MARKER_LON_KEY, None)
            confirmed = settings.value(MARKER_CONFIRMED_KEY, False)
            stored_name = settings.value(MARKER_NAME_KEY)
        finally:
            settings.endGroup()
        try:
            self.lat = float(lat) if lat not in (None, "") else None
            self.lon = float(lon) if lon not in (None, "") else None
        except (TypeError, ValueError):
            self.lat = None
            self.lon = None
        if isinstance(confirmed, str):
            confirmed = confirmed.lower() in ("1", "true", "yes")
        self.location_name = str(stored_name or "").strip() or None
        if self.dock is not None:
            self.dock.name_edit.setText(self.location_name or "")
        self.marker_confirmed = bool(confirmed) and self.lat is not None and self.lon is not None
        if self.lat is None or self.lon is None:
            return
        if self.map_tool is not None:
            self.map_tool.set_wgs84(self.lat, self.lon)
        log_info(
            "Restored map marker %.6f, %.6f name=%s (confirmed=%s)"
            % (self.lat, self.lon, self.sta_location_name(), self.marker_confirmed)
        )

    def _save_marker(self):
        """Persist confirmed WGS84 coordinates and Location name under SETTINGS_GROUP."""
        settings = QgsSettings()
        settings.beginGroup(SETTINGS_GROUP)
        try:
            if self.lat is None or self.lon is None:
                settings.remove(MARKER_LAT_KEY)
                settings.remove(MARKER_LON_KEY)
                settings.remove(MARKER_CONFIRMED_KEY)
                settings.remove(MARKER_NAME_KEY)
            else:
                settings.setValue(MARKER_LAT_KEY, float(self.lat))
                settings.setValue(MARKER_LON_KEY, float(self.lon))
                settings.setValue(MARKER_CONFIRMED_KEY, bool(self.marker_confirmed))
                settings.setValue(MARKER_NAME_KEY, self.location_name or "")
        finally:
            settings.endGroup()

    def _teardown_map_tool(self):
        """Deactivate the map tool and remove the vertex marker so QGIS does not keep a dead tool."""
        canvas = self.iface.mapCanvas()
        try:
            canvas.mapToolSet.disconnect(self._on_map_tool_set)
        except (TypeError, RuntimeError):
            pass
        if hasattr(canvas, "destinationCrsChanged"):
            try:
                canvas.destinationCrsChanged.disconnect(self._on_canvas_crs_changed)
            except (TypeError, RuntimeError):
                pass
        if self.map_tool is not None:
            if canvas.mapTool() is self.map_tool:
                canvas.unsetMapTool(self.map_tool)
            self.map_tool.remove_marker()
            self.map_tool = None
