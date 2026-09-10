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

"""Smart Citizen Kit serial connection and monitor-line parsing."""

import logging
import re
import time
import traceback
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .config import SCK_BAUD, SCK_SAMPLE_INTERVAL

_logger = logging.getLogger("sck.kit")

SCK_SHELL_ON = "shell -on\n"
SCK_CONFIG_CMD = "config\n"
SCK_NETINFO_CMD = "netinfo\n"
SCK_MONITOR_CMD = (
    "monitor -noms Temperature,Humidity,Light,Noise dBA,Barometric pressure,PM 1.0,PM 2.5,PM 10.0\n"
)
_MAC_RE = re.compile(
    r"(?i)(?:sta\s+)?mac(?:\s*address)?\s*:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})"
)

READING_ROWS = [
    ("phenomenon_time", "Time"),
    ("temperature", "Temperature C"),
    ("humidity", "Humidity %"),
    ("light", "Light lux"),
    ("noise", "Noise dBA"),
    ("pressure", "Pressure kPa"),
    ("pm1", "PM1"),
    ("pm25", "PM2.5"),
    ("pm10", "PM10"),
]
CHART_KEYS = [key for key, _label in READING_ROWS if key != "phenomenon_time"]
CHART_SERIES = (
    ("temperature", "Temp °C", "#d9480f"),
    ("humidity", "Humidity %", "#1c7ed6"),
    ("light", "Light lux", "#f59f00"),
    ("noise", "Noise dBA", "#7048e8"),
    ("pressure", "Pressure kPa", "#0ca678"),
    ("pm1", "PM1", "#495057"),
    ("pm25", "PM2.5", "#c92a2a"),
    ("pm10", "PM10", "#e8590c"),
)


class KitError(RuntimeError):
    """Raised when pyserial / QtSerialPort is missing or the kit cannot be opened."""


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _phenomenon_time(raw):
    """SCK reports Time 0 when its clock is unset; use the host UTC time instead."""
    text = str(raw).strip()
    if not text:
        return _utc_now_iso()
    try:
        if float(text) == 0:
            return _utc_now_iso()
    except ValueError:
        pass
    return text


def parse_sck_mac(data):
    """Parse a kit MAC from a serial config/netinfo line, or None."""
    if data is None:
        return None
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    text = data.strip()
    if not text:
        return None
    if re.search(r"(?i)\bap\s+mac\b", text) and not re.search(r"(?i)\bsta\s+mac\b", text):
        return None
    match = _MAC_RE.search(text)
    if not match:
        return None
    parts = match.group(1).split(":")
    if len(parts) != 6:
        return None
    return ":".join(part.upper() for part in parts)


def _drain_lines(read_line, seconds, should_stop):
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline and not should_stop():
        read_line()


def _read_mac_until(read_line, seconds, should_stop):
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline and not should_stop():
        line = read_line()
        if not line:
            continue
        mac = parse_sck_mac(line)
        if mac:
            return mac
    return ""


def query_kit_mac(write, read_line, should_stop):
    """shell -on, then config (netinfo fallback). Returns '' if the kit does not print a MAC."""
    _drain_lines(read_line, 1.0, should_stop)
    write(SCK_SHELL_ON.encode("ASCII"))
    _drain_lines(read_line, 0.4, should_stop)
    write(SCK_CONFIG_CMD.encode("ASCII"))
    mac = _read_mac_until(read_line, 6.0, should_stop)
    if mac or should_stop():
        return mac
    write(SCK_NETINFO_CMD.encode("ASCII"))
    return _read_mac_until(read_line, 4.0, should_stop)


def parse_sck_line(data):
    """Parse one SCK monitor line into a sample dict, or None if it is not a reading."""
    if data is None:
        return None
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    data = data.strip()
    if not data or data.startswith("SCK"):
        return None
    obs = list(filter(None, data.split()))
    if len(obs) != 9:
        return None
    stamp, temp, humidity, light, noise, pressure, pm1, pm25, pm10 = obs
    return {
        "phenomenon_time": _phenomenon_time(stamp),
        "temperature": float(temp),
        "humidity": float(humidity),
        "light": float(light),
        "noise": float(noise),
        "pressure": float(pressure),
        "pm1": float(pm1),
        "pm25": float(pm25),
        "pm10": float(pm10),
        "raw": data,
    }


def sample_chart_point(sample, now=None):
    """Build one chart history point from a parsed sample."""
    now = time.time() if now is None else now
    point = {"ts": now, "t": str((sample or {}).get("phenomenon_time") or "")}
    for key in CHART_KEYS:
        try:
            point[key] = float(sample[key])
        except (TypeError, ValueError, KeyError):
            continue
    return point


def _pyserial_module():
    try:
        from serial import Serial
        from serial.tools import list_ports
        return Serial, list_ports
    except ImportError:
        return None, None


def _qtserial_modules():
    try:
        from qgis.PyQt.QtCore import QIODevice
        from qgis.PyQt.QtSerialPort import QSerialPort, QSerialPortInfo
        return QSerialPort, QSerialPortInfo, QIODevice
    except ImportError:
        try:
            from PyQt6.QtCore import QIODevice
            from PyQt6.QtSerialPort import QSerialPort, QSerialPortInfo
            return QSerialPort, QSerialPortInfo, QIODevice
        except ImportError:
            return None, None, None


def serial_backend():
    """'pyserial', 'qtserial', or None."""
    serial_cls, _ports = _pyserial_module()
    if serial_cls is not None:
        return "pyserial"
    port_cls, _info, _io = _qtserial_modules()
    if port_cls is not None:
        return "qtserial"
    return None


def _unique_serial_ports(ports):
    """Drop duplicate paths. On macOS, keep /dev/cu.* and skip the matching /dev/tty.* twin."""
    devices = [device for device, _label in ports]
    callouts = set()
    for device in devices:
        if device.startswith("/dev/cu."):
            callouts.add(device[8:])
        elif device.startswith("cu."):
            callouts.add(device[3:])
    unique = []
    seen = set()
    for device, label in ports:
        if not device or device in seen:
            continue
        twin = None
        if device.startswith("/dev/tty."):
            twin = device[9:]
        elif device.startswith("tty."):
            twin = device[4:]
        if twin is not None and twin in callouts:
            continue
        seen.add(device)
        unique.append((device, label))
    return unique


def list_serial_ports():
    """Return [(device, label), ...] for USB serial ports."""
    ports = []
    _serial_cls, list_ports = _pyserial_module()
    if list_ports is not None:
        for info in list_ports.comports():
            label = info.device
            if info.description and info.description != "n/a":
                label = "%s (%s)" % (info.device, info.description)
            ports.append((info.device, label))
        return _unique_serial_ports(ports)
    _port_cls, info_cls, _io = _qtserial_modules()
    if info_cls is None:
        return []
    try:
        available = info_cls.availablePorts()
    except Exception:
        return []
    for info in available:
        device = ""
        if hasattr(info, "systemLocation"):
            device = info.systemLocation() or ""
        if not device and hasattr(info, "portName"):
            device = info.portName() or ""
        if not device:
            continue
        desc = ""
        if hasattr(info, "description"):
            desc = info.description() or ""
        label = "%s (%s)" % (device, desc) if desc else device
        ports.append((device, label))
    return _unique_serial_ports(ports)


class SerialWorker(QThread):
    """Open the SCK, send monitor, emit parsed samples about every SCK_SAMPLE_INTERVAL seconds."""

    sample = pyqtSignal(dict)
    failed = pyqtSignal(str)
    connected = pyqtSignal(str, str)
    finished_ok = pyqtSignal()

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        backend = serial_backend()
        try:
            if backend == "pyserial":
                self._run_pyserial()
            elif backend == "qtserial":
                self._run_qtserial()
            else:
                raise KitError(
                    "No serial library in this QGIS Python. Install pyserial, or use a QGIS build with QtSerialPort."
                )
        except KitError as err:
            self.failed.emit(str(err))
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            self.finished_ok.emit()

    def _run_pyserial(self):
        serial_cls, _ports = _pyserial_module()
        sck = serial_cls(self.port, SCK_BAUD, timeout=1)
        try:
            mac = query_kit_mac(sck.write, lambda: sck.readline(), lambda: self._stop)
            if self._stop:
                return
            sck.write(SCK_MONITOR_CMD.encode("ASCII"))
            self.connected.emit(self.port, mac or "")
            _logger.info(
                "SCK monitor started on %s (pyserial) sck_id=%s",
                self.port,
                mac or "(none)",
            )
            self._emit_loop(lambda: sck.readline())
        finally:
            try:
                sck.close()
            except Exception:
                pass

    def _run_qtserial(self):
        port_cls, _info, io_device = _qtserial_modules()
        port = port_cls()
        port.setPortName(self.port)
        if hasattr(port, "setBaudRate"):
            port.setBaudRate(SCK_BAUD)
        mode = getattr(getattr(io_device, "OpenModeFlag", io_device), "ReadWrite", None)
        if mode is None:
            mode = io_device.ReadWrite
        if not port.open(mode):
            err = ""
            if hasattr(port, "errorString"):
                err = port.errorString()
            raise KitError("Could not open %s: %s" % (self.port, err or "unknown error"))
        try:
            buf = b""

            def read_line():
                nonlocal buf
                if port.waitForReadyRead(200):
                    chunk = port.readAll()
                    if hasattr(chunk, "data"):
                        buf += bytes(chunk.data())
                    else:
                        buf += bytes(chunk)
                if b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    return line
                return b""

            def write_cmd(data):
                port.write(data)
                if hasattr(port, "waitForBytesWritten"):
                    port.waitForBytesWritten(500)

            mac = query_kit_mac(write_cmd, read_line, lambda: self._stop)
            if self._stop:
                return
            write_cmd(SCK_MONITOR_CMD.encode("ASCII"))
            self.connected.emit(self.port, mac or "")
            _logger.info(
                "SCK monitor started on %s (QtSerialPort) sck_id=%s",
                self.port,
                mac or "(none)",
            )
            self._emit_loop(read_line)
        finally:
            try:
                port.close()
            except Exception:
                pass

    def _emit_loop(self, read_line):
        next_due = 0.0
        latest = None
        while not self._stop:
            line = read_line()
            if line:
                parsed = parse_sck_line(line)
                if parsed is not None:
                    latest = parsed
            now = time.time()
            if latest is not None and now >= next_due:
                self.sample.emit(latest)
                latest = None
                next_due = now + SCK_SAMPLE_INTERVAL
            else:
                self.msleep(20)
