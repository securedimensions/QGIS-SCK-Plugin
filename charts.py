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

"""Live SCK charts as a QGIS map-canvas overlay at the Thing marker."""

import logging
import time
from datetime import datetime

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import CHART_WINDOW_S
from .kit import CHART_SERIES
from .maptool import map_point_to_pixel, wgs84_to_canvas_point

_logger = logging.getLogger("sck.charts")


def _qcolor(hex_color):
    color = QColor(hex_color)
    if color.isValid():
        return color
    return QColor(30, 90, 200)


def _fmt_num(value):
    """Y-axis numbers, same rules as STAplus-SCK-App formatAxisNum."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    abs_v = abs(value)
    if abs_v >= 1000:
        return "%.0f" % value
    if abs_v >= 100:
        return "%.1f" % value
    return "%.2f" % value


def _fmt_fixed2(value):
    try:
        return "%.2f" % float(value)
    except (TypeError, ValueError):
        return "—"


def _fmt_time(ts):
    """X-axis clock time, same as STAplus-SCK-App formatAxisTime (local HH:MM:SS)."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def _qt_align():
    try:
        return (
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
    except AttributeError:
        return (
            Qt.AlignRight | Qt.AlignTop,
            Qt.AlignRight | Qt.AlignBottom,
            Qt.AlignLeft | Qt.AlignVCenter,
            Qt.AlignRight | Qt.AlignVCenter,
        )


class PlotCanvas(QWidget):
    """Sparkline with left and bottom axis lines, matching the SCK-App canvas."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self._color = _qcolor(color)
        self._points = []
        self.setFixedHeight(48)
        try:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        except AttributeError:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_points(self, points):
        self._points = list(points or [])
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        except AttributeError:
            painter.setRenderHint(QPainter.Antialiasing, True)
        width = max(1, self.width())
        height = max(1, self.height())
        pad_l, pad_r, pad_t, pad_b = 1, 3, 3, 3
        inner_w = max(1, width - pad_l - pad_r)
        inner_h = max(1, height - pad_t - pad_b)
        axis = QPen(QColor(0, 0, 0, 31))
        axis.setWidth(1)
        painter.setPen(axis)
        painter.drawLine(pad_l, pad_t, pad_l, height - pad_b)
        painter.drawLine(pad_l, height - pad_b, width - pad_r, height - pad_b)
        pts = self._points
        if not pts:
            painter.end()
            return
        ys = [p[1] for p in pts]
        ymin = min(ys)
        ymax = max(ys)
        span = (ymax - ymin) or 1.0
        t0 = pts[0][0]
        t1 = pts[-1][0]
        tspan = (t1 - t0) or 1.0

        def xy(ts, value):
            x = pad_l if len(pts) == 1 else pad_l + inner_w * ((ts - t0) / tspan)
            y = height - pad_b - inner_h * ((value - ymin) / span)
            return x, y

        path = QPainterPath()
        for index, (ts, value) in enumerate(pts):
            x, y = xy(ts, value)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        pen = QPen(self._color)
        pen.setWidthF(1.6)
        try:
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        except AttributeError:
            pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        last_x, last_y = xy(pts[-1][0], pts[-1][1])
        painter.setBrush(self._color)
        painter.setPen(self._color)
        painter.drawEllipse(int(last_x - 2), int(last_y - 2), 4, 4)
        painter.end()


class Sparkline(QWidget):
    """One quantity: header, Y min/max, sparkline, X start/end times."""

    def __init__(self, label, color, parent=None):
        super().__init__(parent)
        self._label = label
        right_top, right_bottom, left_mid, right_mid = _qt_align()
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setStyleSheet("font-weight: 600;")
        self._latest = QLabel("—")
        self._latest.setStyleSheet("font-variant-numeric: tabular-nums; font-weight: 500;")
        self._latest.setAlignment(right_mid)
        head.addWidget(name, 1)
        head.addWidget(self._latest)

        axis_style = (
            "color: #555; font-size: 9px; font-variant-numeric: tabular-nums;"
        )
        self._ymax = QLabel("—")
        self._ymin = QLabel("—")
        self._tmin = QLabel("—")
        self._tmax = QLabel("—")
        for lab in (self._ymax, self._ymin, self._tmin, self._tmax):
            lab.setStyleSheet(axis_style)
        self._ymax.setAlignment(right_top)
        self._ymin.setAlignment(right_bottom)
        self._tmin.setAlignment(left_mid)
        self._tmax.setAlignment(right_mid)
        self._ymax.setFixedWidth(36)
        self._ymin.setFixedWidth(36)

        y_col = QVBoxLayout()
        y_col.setContentsMargins(0, 1, 0, 1)
        y_col.setSpacing(0)
        y_col.addWidget(self._ymax)
        y_col.addStretch(1)
        y_col.addWidget(self._ymin)

        self._plot = PlotCanvas(color, self)
        plot_row = QHBoxLayout()
        plot_row.setContentsMargins(0, 0, 0, 0)
        plot_row.setSpacing(4)
        plot_row.addLayout(y_col)
        plot_row.addWidget(self._plot, 1)

        x_row = QHBoxLayout()
        x_row.setContentsMargins(40, 2, 0, 0)
        x_row.setSpacing(6)
        x_row.addWidget(self._tmin)
        x_row.addStretch(1)
        x_row.addWidget(self._tmax)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addLayout(plot_row)
        layout.addLayout(x_row)

    def set_points(self, points, latest):
        pts = list(points or [])
        self._latest.setText(latest if latest else "—")
        if pts:
            ys = [p[1] for p in pts]
            self._ymax.setText(_fmt_num(max(ys)))
            self._ymin.setText(_fmt_num(min(ys)))
            self._tmin.setText(_fmt_time(pts[0][0]))
            self._tmax.setText(_fmt_time(pts[-1][0]))
        else:
            self._ymax.setText("—")
            self._ymin.setText("—")
            self._tmin.setText("—")
            self._tmax.setText("—")
        self._plot.set_points(pts)


class MarkerChartOverlay(QFrame):
    """Chart popup on the QGIS canvas, anchored to the Thing marker (not a Leaflet map)."""

    visibilityChanged = pyqtSignal(bool)

    def __init__(self, canvas, parent=None):
        host = canvas.viewport() if hasattr(canvas, "viewport") else canvas
        super().__init__(host)
        self.canvas = canvas
        self._lat = None
        self._lon = None
        self._history = []
        self.setObjectName("sckMarkerCharts")
        self.setStyleSheet(
            "QFrame#sckMarkerCharts { background: rgba(255,255,255,245); border: 1px solid #888; "
            "border-radius: 4px; }"
        )
        header = QHBoxLayout()
        title = QLabel("Kit charts")
        title.setStyleSheet("font-weight: bold;")
        self.caption = QLabel("Up to 30 minutes")
        self.caption.setStyleSheet("color: #555;")
        close_btn = QPushButton("×")
        close_btn.setFixedSize(22, 22)
        close_btn.setToolTip("Close charts")
        close_btn.clicked.connect(self.hide)
        header.addWidget(title)
        header.addWidget(self.caption, 1)
        header.addWidget(close_btn)

        self.empty = QLabel("Connect the kit to plot readings.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #666; padding: 4px;")

        self._sparks = []
        grid = QGridLayout()
        grid.setContentsMargins(4, 2, 4, 4)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        for index, (_key, label, color) in enumerate(CHART_SERIES):
            spark = Sparkline(label, color, self)
            self._sparks.append(spark)
            grid.addWidget(spark, index // 2, index % 2)
        inner = QWidget()
        inner.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame if hasattr(QFrame, "Shape") else QFrame.NoFrame)
        scroll.setMinimumHeight(280)
        scroll.setMaximumHeight(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(header)
        layout.addWidget(self.empty)
        layout.addWidget(scroll)
        self.setFixedWidth(380)
        try:
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        except AttributeError:
            self.setFocusPolicy(Qt.NoFocus)
        self.hide()
        self._connect_canvas()

    def showEvent(self, event):
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibilityChanged.emit(False)

    def _connect_canvas(self):
        canvas = self.canvas
        for name in ("extentsChanged", "extentChanged", "scaleChanged", "destinationCrsChanged"):
            signal = getattr(canvas, name, None)
            if signal is None:
                continue
            try:
                signal.connect(self.reposition)
            except Exception:
                pass

    def close_overlay(self):
        """Plugin unload."""
        self.hide()
        canvas = self.canvas
        for name in ("extentsChanged", "extentChanged", "scaleChanged", "destinationCrsChanged"):
            signal = getattr(canvas, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect(self.reposition)
            except Exception:
                pass
        self.setParent(None)
        self.deleteLater()

    def set_anchor(self, lat, lon):
        """Keep the overlay next to this WGS84 Thing marker."""
        self._lat = lat
        self._lon = lon
        if self.isVisible():
            self.reposition()

    def set_history(self, history):
        """Replace chart history (list of {ts, temperature, ...})."""
        cutoff = time.time() - CHART_WINDOW_S
        kept = []
        for row in history or []:
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                kept.append(row)
        if len(kept) > 3600:
            kept = kept[-3600:]
        self._history = kept
        self.empty.setVisible(not kept)
        count = len(kept)
        self.caption.setText(
            "%d sample%s · up to 30 min" % (count, "" if count == 1 else "s") if count else "Up to 30 minutes"
        )
        for spark, (key, _label, _color) in zip(self._sparks, CHART_SERIES):
            pts = []
            latest = "—"
            for row in kept:
                try:
                    value = float(row[key])
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError, KeyError):
                    continue
                pts.append((ts, value))
                latest = _fmt_fixed2(value)
            spark.set_points(pts, latest)
        if self.isVisible():
            self.reposition()

    def show_at_marker(self):
        """Show the overlay if a Thing marker exists."""
        if self._lat is None or self._lon is None:
            return False
        self.show()
        self.raise_()
        self.reposition()
        return True

    def toggle(self):
        if self.isVisible():
            self.hide()
            return False
        return self.show_at_marker()

    def reposition(self):
        """Move the popup next to the marker in canvas pixels; do not pan the map."""
        if not self.isVisible() or self._lat is None or self._lon is None:
            return
        try:
            map_point = wgs84_to_canvas_point(self.canvas, self._lat, self._lon)
            px, py = map_point_to_pixel(self.canvas, map_point)
        except Exception as err:
            _logger.info("Could not position kit charts: %s", err)
            return
        host = self.parent()
        max_x = 400
        max_y = 400
        if host is not None:
            max_x = max(8, host.width() - self.width() - 8)
            max_y = max(8, host.height() - self.height() - 8)
        x = int(px) + 18
        y = int(py) - 24
        x = min(max(8, x), max_x)
        y = min(max(8, y), max_y)
        self.move(x, y)
        self.raise_()
