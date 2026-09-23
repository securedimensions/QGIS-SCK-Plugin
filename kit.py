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

import errno
import logging
import math
import os
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
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_LINK_LOST_ERRNOS = frozenset({
    errno.EIO,
    errno.ENXIO,
    errno.ENODEV,
    errno.EBADF,
    errno.EPIPE,
    errno.ENOENT,
})

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


def _reading_value(token):
    """Float for one monitor field, or ValueError when the token is not a finite number."""
    if not _NUMBER_RE.match(token):
        raise ValueError(token)
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(token)
    return value


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
    try:
        values = [
            _reading_value(temp),
            _reading_value(humidity),
            _reading_value(light),
            _reading_value(noise),
            _reading_value(pressure),
            _reading_value(pm1),
            _reading_value(pm25),
            _reading_value(pm10),
        ]
    except ValueError:
        return None
    temperature, humidity, light, noise, pressure, pm1, pm25, pm10 = values
    return {
        "phenomenon_time": _phenomenon_time(stamp),
        "temperature": temperature,
        "humidity": humidity,
        "light": light,
        "noise": noise,
        "pressure": pressure,
        "pm1": pm1,
        "pm25": pm25,
        "pm10": pm10,
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


def _is_link_lost(err):
    """True when a serial read failed because the USB device is gone."""
    seen = set()
    current = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and getattr(current, "errno", None) in _LINK_LOST_ERRNOS:
            return True
        text = str(current).lower()
        if (
            "disconnected" in text
            or "device not configured" in text
            or "no such file" in text
            or "device not found" in text
            or "resourceerror" in text
            or "resource error" in text
        ):
            return True
        current = getattr(current, "__cause__", None)
    return False


def _device_path(port):
    """Filesystem path for a macOS/Linux serial node, or None for COM ports."""
    text = str(port or "").strip()
    if not text or text.upper().startswith("COM"):
        return None
    if text.startswith("/dev/"):
        return text
    if text.startswith("cu.") or text.startswith("tty."):
        return "/dev/" + text
    if os.name != "nt" and text.startswith("/"):
        return text
    return None


def _port_listed(port):
    """True when port is still enumerated. Enumeration failure keeps the link up."""
    try:
        devices = {device for device, _label in list_serial_ports()}
    except Exception as err:
        _logger.debug("Could not list serial ports while watching %s: %s", port, err)
        return True
    return port in devices


class _PortWatch:
    """Notice when the USB serial device node disappears after the cable is unplugged."""

    def __init__(self, port):
        self.port = port
        self._listed_ok = True
        self._listed_at = 0.0

    def present(self, force=False):
        path = _device_path(self.port)
        if path is not None:
            return os.path.exists(path)
        now = time.time()
        if not force and now - self._listed_at < 0.5:
            return self._listed_ok
        self._listed_ok = _port_listed(self.port)
        self._listed_at = now
        return self._listed_ok


def _binary_noise(line):
    """True when a chunk is mostly non-text, as a floating USB line produces after unplug."""
    if line is None:
        return False
    raw = line if isinstance(line, bytes) else str(line).encode("utf-8", errors="replace")
    stripped = raw.strip(b"\r\n")
    if not stripped:
        return False
    weird = sum(1 for byte in stripped if byte not in (9, 13) and not 32 <= byte < 127)
    return weird * 4 >= len(stripped)


def _qtserial_removed(port):
    """True when Qt reports the USB serial device was removed.

    Match the error name. Qt 6 renumbered SerialPortError, and TimeoutError is 9
    there — the same integer as Qt 5 ResourceError — so a bare integer 9 must not
    count as unplug (it is the normal waitForReadyRead timeout).
    """
    try:
        err = port.error()
    except Exception as exc:
        _logger.debug("Could not read QSerialPort error: %s", exc)
        return False
    name = getattr(err, "name", None) or ""
    if name in ("ResourceError", "DeviceNotFoundError"):
        return True
    if name:
        return False
    text = str(err)
    if "ResourceError" in text or "DeviceNotFoundError" in text:
        return True
    if "Timeout" in text or "NoError" in text:
        return False
    try:
        code = int(err)
    except (TypeError, ValueError):
        return False
    # Plain ints from Qt 6: DeviceNotFoundError=1, ResourceError=6.
    return code in (1, 6)


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
        device = None
        if hasattr(info, "systemLocation"):
            device = info.systemLocation() or None
        if not device and hasattr(info, "portName"):
            device = info.portName() or None
        if not device:
            continue
        desc = None
        if hasattr(info, "description"):
            desc = info.description() or None
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
        self.link_lost = False

    def stop(self):
        self._stop = True

    def _mark_link_lost(self, reason):
        """Stop after the USB cable is unplugged. A user Disconnect wins over this."""
        if self._stop:
            return
        self.link_lost = True
        _logger.info("SCK serial link lost on %s: %s", self.port, reason)

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
        except Exception as err:
            if _is_link_lost(err):
                self._mark_link_lost(err)
            elif not self._stop:
                self.failed.emit(traceback.format_exc())
        finally:
            self.finished_ok.emit()

    def _run_pyserial(self):
        serial_cls, _ports = _pyserial_module()
        try:
            sck = serial_cls(self.port, SCK_BAUD, timeout=1)
        except Exception as err:
            raise KitError("Could not open %s: %s" % (self.port, err)) from err
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

            port_watch = _PortWatch(self.port)

            def link_ok(force=False):
                if not port_watch.present(force=force):
                    return False
                try:
                    _ = sck.in_waiting
                except Exception as err:
                    if _is_link_lost(err):
                        return False
                    _logger.debug("Could not query in_waiting on %s: %s", self.port, err)
                return True

            self._emit_loop(lambda: sck.readline(), link_ok)
        finally:
            try:
                sck.close()
            except Exception as err:
                _logger.debug("Could not close pyserial port %s: %s", self.port, err)

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
            err = None
            if hasattr(port, "errorString"):
                err = port.errorString()
            raise KitError("Could not open %s: %s" % (self.port, err or "unknown error"))
        try:
            buf = b""

            def read_line():
                nonlocal buf
                if _qtserial_removed(port):
                    buf = b""
                    raise OSError(errno.EIO, "serial device removed")
                if port.waitForReadyRead(200) and not _qtserial_removed(port):
                    chunk = port.readAll()
                    if _qtserial_removed(port):
                        buf = b""
                        raise OSError(errno.EIO, "serial device removed")
                    if hasattr(chunk, "data"):
                        buf += bytes(chunk.data())
                    else:
                        buf += bytes(chunk)
                elif _qtserial_removed(port):
                    buf = b""
                    raise OSError(errno.EIO, "serial device removed")
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
            port_watch = _PortWatch(self.port)

            def link_ok(force=False):
                if _qtserial_removed(port):
                    return False
                return port_watch.present(force=force)

            self._emit_loop(read_line, link_ok)
        finally:
            try:
                port.close()
            except Exception as err:
                _logger.debug("Could not close QtSerialPort %s: %s", self.port, err)

    def _link_ok_now(self, link_ok, force=False):
        try:
            return bool(link_ok(force=force))
        except Exception as err:
            if _is_link_lost(err):
                return False
            raise

    def _link_still_up(self, link_ok):
        """False when the USB device is gone. A second check ignores a one-off enumeration glitch."""
        if self._link_ok_now(link_ok):
            return True
        self.msleep(250)
        if self._stop:
            return False
        return self._link_ok_now(link_ok, force=True)

    def _emit_loop(self, read_line, link_ok):
        next_due = 0.0
        latest = None
        noise_hits = 0
        while not self._stop:
            try:
                line = read_line()
            except Exception as err:
                if self._stop:
                    return
                if _is_link_lost(err):
                    self._mark_link_lost(err)
                    return
                raise
            if self._stop:
                return
            if not self._link_still_up(link_ok):
                if not self._stop:
                    self._mark_link_lost("USB serial device removed")
                return
            if line and _binary_noise(line):
                noise_hits += 1
                if noise_hits >= 3:
                    self._mark_link_lost("serial noise after unplug")
                    return
                continue
            noise_hits = 0
            if line:
                parsed = parse_sck_line(line)
                if parsed is not None:
                    latest = parsed
            now = time.time()
            if latest is not None and now >= next_due:
                if not self._link_still_up(link_ok):
                    if not self._stop:
                        self._mark_link_lost("USB serial device removed")
                    return
                self.sample.emit(latest)
                latest = None
                next_due = now + SCK_SAMPLE_INTERVAL
            else:
                self.msleep(20)
