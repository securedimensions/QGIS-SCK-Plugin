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

"""MQTT 5 CONNECT/PUBLISH for STAplus ObservationGroups (QoS 1 + PUBACK reasons)."""

import json
import logging
import socket
import struct
import time

from . import authenix
from .config import MQTT_KEEP_ALIVE, MQTT_PUBLISH_QOS, MQTT_PUBLISH_TIMEOUT, MQTT_PUBLISH_TOPIC

_logger = logging.getLogger("sck.mqtt")

# MQTT 3.1.1 CONNACK codes and MQTT 5 0x87 Not Authorized.
MQTT_UNAUTHORIZED = {4, 5, 0x87}
MQTT5_NOT_AUTHORIZED = 0x87

MQTT5_REASON_NAMES = {
    0x00: "Success",
    0x10: "No matching subscribers",
    0x80: "Unspecified error",
    0x83: "Implementation specific error",
    0x87: "Not authorized",
    0x8B: "Quota exceeded",
    0x90: "Topic name invalid",
    0x91: "Packet identifier in use",
    0x93: "Payload format invalid",
    0x97: "Quota exceeded",
    0x99: "Payload format invalid",
    0x9B: "Retain not supported",
    0x9C: "QoS not supported",
    0x9D: "Use another server",
    0x9E: "Server moved",
    0x9F: "Shared subscriptions not supported",
}

_PROP_FIXED = {
    0x01: 1,
    0x17: 1,
    0x24: 1,
    0x25: 1,
    0x28: 1,
    0x29: 1,
    0x2A: 1,
    0x13: 2,
    0x21: 2,
    0x22: 2,
    0x23: 2,
    0x33: 2,
    0x02: 4,
    0x11: 4,
    0x18: 4,
    0x27: 4,
}
_PROP_UTF8 = {0x03, 0x08, 0x12, 0x15, 0x1A, 0x1C, 0x1F}
_PROP_UTF8_PAIR = {0x26}
_PROP_BINARY = {0x09, 0x16}
_PROP_VARINT = {0x0B}

PKT_CONNACK = 2
PKT_PUBLISH = 3
PKT_PUBACK = 4
PKT_PINGRESP = 13
PKT_DISCONNECT = 14


class MqttError(RuntimeError):
    """Raised when CONNECT, PUBLISH, or broker acknowledgement fails."""


def _encode_remaining_length(length):
    """MQTT remaining-length encoding (variable-length integer)."""
    output = bytearray()
    while True:
        encoded = length % 128
        length //= 128
        if length > 0:
            encoded |= 0x80
        output.append(encoded)
        if length == 0:
            break
    return bytes(output)


def _utf8(value):
    """MQTT UTF-8 string: 2-byte length prefix plus bytes."""
    data = (value or "").encode("utf-8")
    return struct.pack("!H", len(data)) + data


def _read_utf8(data, offset):
    """Read an MQTT UTF-8 string at offset; return (text, new_offset)."""
    if offset + 2 > len(data):
        raise MqttError("Truncated MQTT UTF-8 string")
    length = struct.unpack_from("!H", data, offset)[0]
    offset += 2
    end = offset + length
    if end > len(data):
        raise MqttError("Truncated MQTT UTF-8 string")
    return data[offset:end].decode("utf-8", "replace"), end


def _read_varint(data, offset):
    """Decode an MQTT variable-byte integer; return (value, new_offset)."""
    multiplier = 1
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise MqttError("Truncated MQTT variable-byte integer")
        encoded = data[offset]
        offset += 1
        value += (encoded & 127) * multiplier
        if not encoded & 128:
            return value, offset
        multiplier *= 128
    raise MqttError("Invalid MQTT variable-byte integer")


def _parse_properties(data, offset):
    """Parse MQTT 5 properties starting at offset; return (dict, new_offset)."""
    if offset >= len(data):
        return {}, offset
    length, offset = _read_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise MqttError("Truncated MQTT properties")
    props = {}
    while offset < end:
        identifier = data[offset]
        offset += 1
        if identifier in _PROP_UTF8:
            text, offset = _read_utf8(data, offset)
            if identifier == 0x1F:
                props["ReasonString"] = text
            else:
                props.setdefault("utf8", []).append(text)
        elif identifier in _PROP_UTF8_PAIR:
            key, offset = _read_utf8(data, offset)
            value, offset = _read_utf8(data, offset)
            props.setdefault("UserProperty", []).append((key, value))
        elif identifier in _PROP_BINARY:
            if offset + 2 > end:
                break
            blen = struct.unpack_from("!H", data, offset)[0]
            offset += 2 + blen
        elif identifier in _PROP_VARINT:
            _, offset = _read_varint(data, offset)
        elif identifier in _PROP_FIXED:
            offset += _PROP_FIXED[identifier]
        else:
            _logger.warning("Skipping unknown MQTT 5 property id %s", identifier)
            break
    return props, end


def _reason_text(reason_code, properties=None):
    """Human-readable MQTT 5 reason code plus optional Reason String."""
    try:
        value = int(reason_code)
    except (TypeError, ValueError):
        value = reason_code
    bits = []
    name = MQTT5_REASON_NAMES.get(value)
    if name:
        bits.append(name)
    bits.append("code %s" % value)
    extra = properties or {}
    reason = extra.get("ReasonString") or extra.get("reasonString")
    if reason:
        bits.append(str(reason))
    elif extra.get("UserProperty"):
        bits.append(str(extra.get("UserProperty")))
    return "; ".join(bits)


def _reason_failed(reason_code):
    """True for MQTT 5 failure reason codes (0x80 and above)."""
    if reason_code is None:
        return False
    try:
        return int(reason_code) >= 0x80
    except (TypeError, ValueError):
        return True


def _sta_error_text(payload):
    """Return an error string if payload is a STAplus/FROST error JSON document."""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", "replace")
    else:
        text = str(payload or "")
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    kind = str(data.get("type") or "").lower()
    try:
        numeric = int(code) if code is not None else None
    except (TypeError, ValueError):
        numeric = None
    if kind == "error" or (numeric is not None and numeric >= 400):
        return stripped[:1500]
    return None


def _connect_packet(client_id, username, password, keep_alive=60):
    """Build an MQTT 5 CONNECT with username+password and clean start."""
    payload = _utf8(client_id) + _utf8(username) + _utf8(password)
    variable = (
        _utf8("MQTT")
        + bytes([5])  # protocol level MQTT 5
        + bytes([0xC2])  # username + password + clean start
        + struct.pack("!H", keep_alive)
        + bytes([0])  # empty properties
        + payload
    )
    return bytes([0x10]) + _encode_remaining_length(len(variable)) + variable


def _publish_packet(topic, payload, packet_id, qos=1):
    """MQTT 5 PUBLISH. QoS 1 includes a packet identifier and waits for PUBACK."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    qos = int(qos)
    flags = 0x30 | ((qos & 0x03) << 1)
    body = _utf8(topic)
    if qos > 0:
        body += struct.pack("!H", packet_id)
    body += bytes([0])  # empty MQTT 5 properties
    body += payload
    return bytes([flags]) + _encode_remaining_length(len(body)) + body


def _read_exact(sock, size):
    """Read exactly size bytes or raise if the broker closes the connection."""
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise MqttError("MQTT broker closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_remaining_length(sock):
    """Decode the MQTT remaining-length field from the socket."""
    multiplier = 1
    value = 0
    for _ in range(4):
        encoded = _read_exact(sock, 1)[0]
        value += (encoded & 127) * multiplier
        if not encoded & 128:
            return value
        multiplier *= 128
    raise MqttError("Invalid MQTT remaining length")


def _read_packet(sock):
    """Read one MQTT packet; return (type, flags, payload)."""
    first = _read_exact(sock, 1)[0]
    packet_type = first >> 4
    flags = first & 0x0F
    remaining = _read_remaining_length(sock)
    payload = _read_exact(sock, remaining) if remaining else b""
    return packet_type, flags, payload


def _parse_connack(payload):
    """Parse MQTT 5 CONNACK (flags, reason, properties). MQTT 3.1.1 is 2 bytes."""
    if len(payload) < 2:
        raise MqttError("Short MQTT CONNACK")
    reason = payload[1]
    props = {}
    if len(payload) > 2:
        props, _ = _parse_properties(payload, 2)
    return reason, props


def _parse_puback(payload):
    """Parse MQTT 5 PUBACK: packet id, optional reason code, optional properties."""
    if len(payload) < 2:
        raise MqttError("Short MQTT PUBACK")
    packet_id = struct.unpack_from("!H", payload, 0)[0]
    reason = 0
    props = {}
    if len(payload) >= 3:
        reason = payload[2]
    if len(payload) > 3:
        props, _ = _parse_properties(payload, 3)
    return packet_id, reason, props


def _parse_disconnect(payload):
    """Parse MQTT 5 DISCONNECT reason and properties."""
    if not payload:
        return 0, {}
    reason = payload[0]
    props = {}
    if len(payload) > 1:
        props, _ = _parse_properties(payload, 1)
    return reason, props


def _parse_publish(flags, payload):
    """Parse MQTT 5 PUBLISH; return (topic, packet_id, body)."""
    topic, offset = _read_utf8(payload, 0)
    qos = (flags >> 1) & 0x03
    packet_id = None
    if qos > 0:
        if offset + 2 > len(payload):
            raise MqttError("Short MQTT PUBLISH packet id")
        packet_id = struct.unpack_from("!H", payload, offset)[0]
        offset += 2
    props, offset = _parse_properties(payload, offset)
    body = payload[offset:]
    return topic, packet_id, body, props


def _read_connack(sock):
    packet_type, _flags, payload = _read_packet(sock)
    if packet_type != PKT_CONNACK:
        raise MqttError("Expected MQTT CONNACK, got packet type %s" % packet_type)
    reason, props = _parse_connack(payload)
    _logger.info("MQTT CONNACK %s", _reason_text(reason, props))
    return reason, props


def _open_mqtt(host, port, token, timeout=15, keep_alive=MQTT_KEEP_ALIVE):
    """CONNECT and return (sock, connack_reason). sock is None when CONNACK is not 0."""
    client_id = "qgis-sck-%s" % int(time.time())
    packet = _connect_packet(client_id, "Bearer", token, keep_alive=keep_alive)
    _logger.info("MQTT CONNECT %s:%s client_id=%s username=Bearer protocol=5", host, port, client_id)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(packet)
        return_code, _props = _read_connack(sock)
        if return_code != 0:
            try:
                sock.close()
            except OSError:
                pass
            return None, return_code
        return sock, return_code
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def _connack(host, port, token, timeout=15):
    """CONNECT, read CONNACK, close. Used by the one-shot MQTT check."""
    sock, return_code = _open_mqtt(host, port, token, timeout=timeout)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
    return return_code


class MqttPublisher:
    """Keep an MQTT 5 connection open and PUBLISH ObservationGroups at QoS 1."""

    def __init__(self, host, port, keep_alive=MQTT_KEEP_ALIVE):
        self.host = host
        self.port = int(port)
        self.keep_alive = keep_alive
        self.sock = None
        self._last_activity = 0.0
        self._packet_id = 0

    def is_connected(self):
        return self.sock is not None

    def _next_packet_id(self):
        self._packet_id += 1
        if self._packet_id > 65535:
            self._packet_id = 1
        return self._packet_id

    def connect(self, token):
        self.disconnect()
        sock, return_code = _open_mqtt(self.host, self.port, token, keep_alive=self.keep_alive)
        unauthorized = return_code in MQTT_UNAUTHORIZED or return_code == MQTT5_NOT_AUTHORIZED
        if unauthorized:
            raise MqttError("MQTT CONNECT unauthorized (%s)" % _reason_text(return_code))
        if return_code != 0 or sock is None:
            raise MqttError("MQTT CONNECT failed (%s)" % _reason_text(return_code))
        self.sock = sock
        self._last_activity = time.time()
        _logger.info("MQTT publisher connected at %s:%s (MQTT 5)", self.host, self.port)
        return return_code

    def disconnect(self):
        sock = self.sock
        self.sock = None
        if sock is None:
            return
        try:
            sock.sendall(bytes([0xE0, 0x00]))
        except Exception as err:
            _logger.debug("Could not send MQTT DISCONNECT: %s", err)
        try:
            sock.close()
        except OSError:
            pass

    def publish(self, payload, topic=MQTT_PUBLISH_TOPIC, qos=MQTT_PUBLISH_QOS):
        if self.sock is None:
            raise MqttError("MQTT publisher is not connected")
        self._maybe_ping()
        qos = int(qos)
        packet_id = self._next_packet_id() if qos > 0 else 0
        packet = _publish_packet(topic, payload, packet_id, qos=qos)
        try:
            self.sock.sendall(packet)
        except OSError as err:
            self.disconnect()
            raise MqttError("MQTT publish failed: %s" % err) from err
        self._last_activity = time.time()
        if qos < 1:
            _logger.debug("MQTT published qos=0 topic=%s bytes=%s", topic, len(packet))
            return packet_id
        return self._await_puback(packet_id, topic)

    def _await_puback(self, packet_id, topic, timeout=MQTT_PUBLISH_TIMEOUT):
        """Wait for PUBACK; raise on broker rejection, error PUBLISH, or timeout."""
        deadline = time.time() + float(timeout)
        errors = []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MqttError(
                    "MQTT PUBACK timed out mid=%s qos=1 topic=%s after %ss"
                    % (packet_id, topic, timeout)
                )
            try:
                self.sock.settimeout(remaining)
                packet_type, flags, payload = _read_packet(self.sock)
            except socket.timeout as err:
                raise MqttError(
                    "MQTT PUBACK timed out mid=%s qos=1 topic=%s after %ss"
                    % (packet_id, topic, timeout)
                ) from err
            except (OSError, MqttError) as err:
                self.disconnect()
                raise MqttError("MQTT PUBACK wait failed mid=%s: %s" % (packet_id, err)) from err

            if packet_type == PKT_PINGRESP:
                continue
            if packet_type == PKT_DISCONNECT:
                reason, props = _parse_disconnect(payload)
                text = _reason_text(reason, props)
                self.disconnect()
                _logger.error("MQTT DISCONNECT during PUBACK wait mid=%s %s", packet_id, text)
                raise MqttError("MQTT disconnected: %s" % text)
            if packet_type == PKT_PUBLISH:
                pub_topic, _mid, body, props = _parse_publish(flags, payload)
                text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
                sta_error = _sta_error_text(text)
                reason = (props or {}).get("ReasonString")
                _logger.error(
                    "MQTT message during publish topic=%s reason=%s body=%s",
                    pub_topic,
                    reason,
                    text[:1500],
                )
                errors.append(sta_error or reason or text[:1500])
                continue
            if packet_type != PKT_PUBACK:
                _logger.warning(
                    "MQTT unexpected packet type %s while waiting for PUBACK mid=%s",
                    packet_type,
                    packet_id,
                )
                continue

            ack_id, reason, props = _parse_puback(payload)
            text = _reason_text(reason, props)
            if ack_id != packet_id:
                _logger.warning(
                    "MQTT PUBACK mid=%s does not match sent mid=%s %s",
                    ack_id,
                    packet_id,
                    text,
                )
                continue
            if _reason_failed(reason):
                _logger.error("MQTT PUBACK failed mid=%s %s", packet_id, text)
                raise MqttError("MQTT PUBACK failed: %s" % text)
            if errors:
                detail = errors[-1]
                _logger.error("MQTT broker error after PUBACK mid=%s: %s", packet_id, detail)
                raise MqttError("MQTT broker error: %s" % detail)
            _logger.info("MQTT PUBACK mid=%s %s topic=%s", packet_id, text, topic)
            return packet_id

    def _maybe_ping(self):
        if self.sock is None:
            return
        if time.time() - self._last_activity < max(5, self.keep_alive / 2.0):
            return
        try:
            self.sock.sendall(bytes([0xC0, 0x00]))
            self._last_activity = time.time()
        except OSError as err:
            self.disconnect()
            raise MqttError("MQTT ping failed: %s" % err) from err


def connect_with_token_retry(session=None, host=None, port=None):
    """CONNECT with the current access token. MQTT does not refresh tokens on its own."""
    if not host or not port:
        raise MqttError("MQTT host and port must come from the STAplus landing page.")
    session = dict(session or authenix.load_session() or {})
    token = session.get("access_token") or ""
    if not token:
        raise MqttError("No access token. Sign in first.")

    return_code = _connack(host, port, token)
    if return_code in MQTT_UNAUTHORIZED or return_code == MQTT5_NOT_AUTHORIZED:
        raise MqttError(
            "MQTT CONNECT unauthorized (%s). Sign in and click Start publishing again."
            % _reason_text(return_code)
        )

    if return_code != 0:
        raise MqttError("MQTT CONNECT failed (%s)" % _reason_text(return_code))

    _logger.info("MQTT CONNECT accepted at %s:%s", host, port)
    return {"host": host, "port": port, "connack": return_code, "session": session}
