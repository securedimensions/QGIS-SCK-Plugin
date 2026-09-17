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

"""In-process macOS CoreLocation (same PID as QGIS, so QGIS's TCC grant applies)."""

import ctypes
import ctypes.util
import logging
import re
from ctypes import (
    CFUNCTYPE,
    Structure,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_long,
    c_size_t,
    c_void_p,
)

_logger = logging.getLogger("sck.locate")

_STATUS_NOT_DETERMINED = 0
_STATUS_RESTRICTED = 1
_STATUS_DENIED = 2
_STATUS_AUTHORIZED_ALWAYS = 3
_STATUS_AUTHORIZED_WHEN_IN_USE = 4
_STATUS_AUTHORIZED_LEGACY = 3

_CL_ERROR_LOCATION_UNKNOWN = 0
_CL_ERROR_DENIED = 1

libobjc = None
_ready = False
_delegate_cls = None
_imps = []
_sessions_by_delegate = {}

_COORD_RE = re.compile(r"<([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)>")


class _CLLocationCoordinate2D(Structure):
    _fields_ = [("latitude", c_double), ("longitude", c_double)]


def _sel(name):
    libobjc.sel_registerName.restype = c_void_p
    libobjc.sel_registerName.argtypes = [c_char_p]
    return libobjc.sel_registerName(name.encode("utf-8"))


def _cls(name):
    libobjc.objc_getClass.restype = c_void_p
    libobjc.objc_getClass.argtypes = [c_char_p]
    return libobjc.objc_getClass(name.encode("utf-8"))


def _msg(obj, selector, restype=c_void_p, argtypes=(), args=()):
    prototype = CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)
    func = prototype(("objc_msgSend", libobjc))
    return func(obj, _sel(selector), *args)


def _nsstring_utf8(nsstr):
    if not nsstr:
        return ""
    try:
        raw = _msg(nsstr, "UTF8String", restype=c_char_p)
    except Exception:
        return ""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _load():
    global libobjc, _ready
    if _ready:
        return True
    path = ctypes.util.find_library("objc")
    if not path:
        return False
    libobjc = ctypes.cdll.LoadLibrary(path)
    for name in ("Foundation", "CoreLocation"):
        found = ctypes.util.find_library(name)
        if found:
            ctypes.cdll.LoadLibrary(found)
    _ready = True
    return True


def _register_delegate_class():
    """NSObject subclass that forwards CLLocationManagerDelegate to Python."""
    global _delegate_cls
    if _delegate_cls:
        return _delegate_cls
    if not _load():
        raise RuntimeError("objc runtime not found")
    nsobject = _cls("NSObject")
    if not nsobject:
        raise RuntimeError("NSObject not found")
    libobjc.objc_allocateClassPair.restype = c_void_p
    libobjc.objc_allocateClassPair.argtypes = [c_void_p, c_char_p, c_size_t]
    libobjc.class_addMethod.restype = c_bool
    libobjc.class_addMethod.argtypes = [c_void_p, c_void_p, c_void_p, c_char_p]
    libobjc.objc_registerClassPair.argtypes = [c_void_p]
    class_name = ("SckQgisCLDelegate%d" % id(_on_did_update)).encode("utf-8")
    cls = libobjc.objc_allocateClassPair(nsobject, class_name, 0)
    if not cls:
        raise RuntimeError("Could not allocate CLLocationManager delegate class")

    update_t = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_void_p)
    fail_t = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_void_p)
    auth_t = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
    auth_status_t = CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_int)

    imp_update = update_t(_on_did_update)
    imp_fail = fail_t(_on_did_fail)
    imp_auth = auth_t(_on_did_change_auth)
    imp_auth_status = auth_status_t(_on_did_change_auth_status)
    _imps.extend([imp_update, imp_fail, imp_auth, imp_auth_status])

    added = True
    added = libobjc.class_addMethod(
        cls, _sel("locationManager:didUpdateLocations:"), imp_update, b"v@:@@"
    ) and added
    added = libobjc.class_addMethod(
        cls, _sel("locationManager:didFailWithError:"), imp_fail, b"v@:@@"
    ) and added
    added = libobjc.class_addMethod(
        cls, _sel("locationManagerDidChangeAuthorization:"), imp_auth, b"v@:@"
    ) and added
    added = libobjc.class_addMethod(
        cls,
        _sel("locationManager:didChangeAuthorizationStatus:"),
        imp_auth_status,
        b"v@:@i",
    ) and added
    if not added:
        _logger.warning("Not all CoreLocation delegate methods could be added")
    libobjc.objc_registerClassPair(cls)
    _delegate_cls = cls
    return cls


def _ptr_key(ptr):
    if ptr is None:
        return 0
    if isinstance(ptr, int):
        return ptr
    value = ctypes.cast(ptr, c_void_p).value
    return int(value or 0)


def _session_for(delegate):
    return _sessions_by_delegate.get(_ptr_key(delegate))


def _on_did_update(_self, _cmd, _manager, locations):
    session = _session_for(_self)
    if session is None or not locations:
        return
    loc = _msg(locations, "lastObject")
    parsed = _location_wgs84(loc)
    if parsed is None:
        return
    session.handle_fix(parsed[0], parsed[1])


def _on_did_fail(_self, _cmd, _manager, error):
    session = _session_for(_self)
    if session is None:
        return
    code = 0
    if error:
        try:
            code = int(_msg(error, "code", restype=c_long))
        except Exception:
            code = 0
    if code == _CL_ERROR_LOCATION_UNKNOWN:
        return
    if code == _CL_ERROR_DENIED:
        session.handle_denied()
        return
    message = _nsstring_utf8(_msg(error, "localizedDescription")) if error else ""
    session.handle_fail(message or "CoreLocation failed to get a position.")


def _on_did_change_auth(_self, _cmd, manager):
    session = _session_for(_self)
    if session is not None:
        session.handle_authorization(manager)


def _on_did_change_auth_status(_self, _cmd, manager, status):
    session = _session_for(_self)
    if session is not None:
        session.handle_authorization(manager, status)


def _coordinate_via_invocation(loc):
    """Read CLLocation.coordinate without relying on objc_msgSend struct-return ABI."""
    try:
        selector = _sel("coordinate")
        sig = _msg(loc, "methodSignatureForSelector:", argtypes=[c_void_p], args=(selector,))
        if not sig:
            return None
        inv = _msg(
            _cls("NSInvocation"),
            "invocationWithMethodSignature:",
            argtypes=[c_void_p],
            args=(sig,),
        )
        if not inv:
            return None
        _msg(inv, "retain")
        try:
            _msg(inv, "setTarget:", restype=None, argtypes=[c_void_p], args=(loc,))
            _msg(inv, "setSelector:", restype=None, argtypes=[c_void_p], args=(selector,))
            _msg(inv, "invoke", restype=None)
            coord = _CLLocationCoordinate2D()
            _msg(
                inv,
                "getReturnValue:",
                restype=None,
                argtypes=[c_void_p],
                args=(ctypes.addressof(coord),),
            )
        finally:
            _msg(inv, "release")
        return _valid_wgs84(coord.latitude, coord.longitude)
    except Exception as err:
        _logger.info("NSInvocation coordinate failed: %s", err)
        return None


def _coordinate_via_description(loc):
    try:
        text = _nsstring_utf8(_msg(loc, "description"))
    except Exception:
        return None
    match = _COORD_RE.search(text or "")
    if not match:
        return None
    return _valid_wgs84(float(match.group(1)), float(match.group(2)))


def _valid_wgs84(lat, lon):
    lat = float(lat)
    lon = float(lon)
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        return None
    if lat == 0 and lon == 0:
        return None
    return lat, lon


def _location_wgs84(loc):
    if not loc:
        return None
    try:
        accuracy = float(_msg(loc, "horizontalAccuracy", restype=c_double))
        if accuracy < 0:
            return None
    except Exception as err:
        _logger.debug("CoreLocation horizontalAccuracy failed: %s", err)
    parsed = _coordinate_via_invocation(loc)
    if parsed:
        return parsed
    return _coordinate_via_description(loc)


def corelocation_available():
    """True when the CoreLocation Objective-C runtime can be loaded."""
    try:
        return _load() and bool(_cls("CLLocationManager"))
    except Exception as err:
        _logger.warning("CoreLocation is not available: %s", err)
        return False


class CoreLocationSession:
    """CLLocationManager owned by the QGIS process (same TCC permission as QGIS)."""

    def __init__(self, on_fix=None, on_denied=None, on_fail=None):
        self.manager = None
        self.delegate = None
        self.on_fix = on_fix
        self.on_denied = on_denied
        self.on_fail = on_fail
        self._delivered = False
        if not _load():
            raise RuntimeError("objc runtime not found")
        manager_cls = _cls("CLLocationManager")
        if not manager_cls:
            raise RuntimeError("CLLocationManager class not found")
        manager = _msg(_msg(manager_cls, "alloc"), "init")
        if not manager:
            raise RuntimeError("Could not create CLLocationManager")
        _msg(manager, "retain")
        try:
            _msg(manager, "setDesiredAccuracy:", restype=None, argtypes=[c_double], args=(100.0,))
        except Exception as err:
            _logger.debug("setDesiredAccuracy failed: %s", err)
        delegate_cls = _register_delegate_class()
        delegate = _msg(_msg(delegate_cls, "alloc"), "init")
        _msg(delegate, "retain")
        _msg(manager, "setDelegate:", restype=None, argtypes=[c_void_p], args=(delegate,))
        self.manager = manager
        self.delegate = delegate
        _sessions_by_delegate[_ptr_key(delegate)] = self
        _logger.info("CoreLocation authorization status %s", self.authorization_status())

    def authorization_status(self):
        """Current CLAuthorizationStatus for this process."""
        manager = self.manager
        if not manager:
            return _STATUS_NOT_DETERMINED
        try:
            return int(_msg(manager, "authorizationStatus", restype=c_int))
        except Exception as err:
            _logger.debug("instance authorizationStatus failed: %s", err)
        try:
            return int(_msg(_cls("CLLocationManager"), "authorizationStatus", restype=c_int))
        except Exception:
            return _STATUS_NOT_DETERMINED

    def is_denied(self):
        return self.authorization_status() in (_STATUS_DENIED, _STATUS_RESTRICTED)

    def is_authorized(self):
        return self.authorization_status() in (
            _STATUS_AUTHORIZED_ALWAYS,
            _STATUS_AUTHORIZED_WHEN_IN_USE,
            _STATUS_AUTHORIZED_LEGACY,
        )

    def request_authorization(self):
        """Ask for When In Use if macOS has not decided yet."""
        if self.authorization_status() != _STATUS_NOT_DETERMINED:
            return
        try:
            _msg(self.manager, "requestWhenInUseAuthorization", restype=None)
        except Exception as err:
            _logger.info("requestWhenInUseAuthorization failed: %s", err)

    def _request_fix(self):
        try:
            responds = _msg(
                self.manager,
                "respondsToSelector:",
                restype=c_bool,
                argtypes=[c_void_p],
                args=(_sel("requestLocation"),),
            )
        except Exception:
            responds = False
        if responds:
            try:
                _msg(self.manager, "requestLocation", restype=None)
            except Exception as err:
                _logger.info("requestLocation failed: %s", err)
        try:
            _msg(self.manager, "startUpdatingLocation", restype=None)
        except Exception as err:
            raise RuntimeError("startUpdatingLocation failed: %s" % err)

    def start(self):
        """Start updates and request a one-shot location."""
        self._delivered = False
        if self.is_denied():
            self.handle_denied()
            return
        self.request_authorization()
        if self.is_authorized() or self.authorization_status() == _STATUS_NOT_DETERMINED:
            self._request_fix()

    def stop(self):
        if not self.manager:
            return
        try:
            _msg(self.manager, "stopUpdatingLocation", restype=None)
        except Exception as err:
            _logger.debug("stopUpdatingLocation failed: %s", err)

    def close(self):
        """Release the manager and delegate (plugin unload)."""
        self.stop()
        if self.delegate:
            _sessions_by_delegate.pop(_ptr_key(self.delegate), None)
            try:
                _msg(self.manager, "setDelegate:", restype=None, argtypes=[c_void_p], args=(None,))
            except Exception as err:
                _logger.debug("Could not clear CoreLocation delegate: %s", err)
            try:
                _msg(self.delegate, "release")
            except Exception as err:
                _logger.debug("Could not release CoreLocation delegate: %s", err)
            self.delegate = None
        if self.manager:
            try:
                _msg(self.manager, "release")
            except Exception as err:
                _logger.debug("Could not release CoreLocation manager: %s", err)
            self.manager = None

    def read_wgs84(self):
        """Return (lat, lon) from the manager's last location, or None."""
        if not self.manager:
            return None
        return _location_wgs84(_msg(self.manager, "location"))

    def handle_fix(self, lat, lon):
        if self._delivered:
            return
        self._delivered = True
        self.stop()
        if self.on_fix is not None:
            self.on_fix(lat, lon)

    def handle_denied(self):
        if self._delivered:
            return
        self._delivered = True
        self.stop()
        if self.on_denied is not None:
            self.on_denied()

    def handle_fail(self, message):
        if self._delivered:
            return
        self._delivered = True
        self.stop()
        if self.on_fail is not None:
            self.on_fail(message)

    def handle_authorization(self, _manager=None, status=None):
        if status is None:
            status = self.authorization_status()
        if status == _STATUS_NOT_DETERMINED:
            return
        if status in (_STATUS_DENIED, _STATUS_RESTRICTED):
            self.handle_denied()
            return
        if status in (
            _STATUS_AUTHORIZED_ALWAYS,
            _STATUS_AUTHORIZED_WHEN_IN_USE,
            _STATUS_AUTHORIZED_LEGACY,
        ):
            try:
                self._request_fix()
            except Exception as err:
                self.handle_fail(str(err))
