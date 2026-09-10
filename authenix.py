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

"""AUTHENIX sign-in via authorization code + PKCE.

The plugin opens the system browser and listens on the loopback redirect. QGIS
OAuth2's updateNetworkRequest is not used: on Windows that C++ path access-violates
because QgsO2 runs on a background factory thread. This public client has no
client_secret, so access tokens cannot be refreshed in the background. When a
token expires, the plugin runs the browser login again.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QSettings, QThread, QUrl
from qgis.PyQt.QtGui import QDesktopServices

from .config import (
    ACCESS_TOKEN_LIFETIME,
    AUTH_BACKEND,
    AUTHENIX_AUTHORIZE,
    AUTHENIX_LOGOUT,
    AUTHENIX_TOKEN,
    AUTHENIX_TOKENINFO,
    AUTHENIX_USERINFO,
    OAUTH_AUTHCFG_ID,
    OAUTH_CLIENT_ID,
    OAUTH_LOGOUT_REDIRECT_URI,
    OAUTH_REDIRECT_HOST,
    OAUTH_REDIRECT_PATH,
    OAUTH_REDIRECT_PORT,
    OAUTH_SCOPES,
    SETTINGS_GROUP,
    TOKEN_REFRESH_SKEW,
)

_logger = logging.getLogger("sck.authenix")
_http_lock = threading.Lock()
# Match QGIS OAuth2: requestTimeout (60s) * 5 for the nested browser loop.
_LOOPBACK_TIMEOUT_S = 300

ID_TOKEN_HEADER = "X-Id-Token"
REFRESH_TOKEN_HEADER = "X-Refresh-Token"
EXPIRES_IN_HEADER = "X-Expires-In"
# QGIS O2 encrypts its token cache with this fixed SimpleCrypt key.
O2_ENCRYPTION_KEY = "12345678"
_CRYPTO_FLAG_COMPRESSION = 0x01
_CRYPTO_FLAG_CHECKSUM = 0x02
_CRYPTO_FLAG_HASH = 0x04
_SESSION_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "expires_at",
    "expires_in",
    "user_json",
    "auth_backend",
)


class AuthError(RuntimeError):
    """Sign-in or logout could not complete. The UI should prompt the user to sign in again."""


def _http_session():
    """Shared requests.Session that ignores OS proxy env vars (AUTHENIX must be reached directly)."""
    with _http_lock:
        session = getattr(_http_session, "_session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.proxies = {}
            session.headers.update({"Accept": "application/json"})
            _http_session._session = session
        return session


def jwt_payload(token):
    """Decode the JWT payload without verifying the signature. Opaque AUTHENIX tokens return {}."""
    text = (token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_prefix(token):
    """Short log-safe form of a token (prefix + length), never the full secret."""
    text = token or ""
    if not text:
        return "(none)"
    return "%s… (%s chars)" % (text[:12], len(text))


def _on_gui_thread():
    """True when this call can open the system browser (QGIS OAuth2 needs the UI thread)."""
    app = QCoreApplication.instance()
    if app is None:
        return False
    return QThread.currentThread() == app.thread()


def _scope_string():
    """Space-separated AUTHENIX scope list for authorize / token requests."""
    return " ".join(OAUTH_SCOPES)


def _redirect_uri():
    """Loopback URI registered at AUTHENIX (http://localhost:7070/sck-qgis-plugin)."""
    return "http://%s:%s/%s" % (OAUTH_REDIRECT_HOST, OAUTH_REDIRECT_PORT, OAUTH_REDIRECT_PATH)


def _pkce_verifier():
    """RFC 7636 code_verifier (43–128 URL-safe characters)."""
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier):
    """S256 code_challenge for the given verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _access_token_expires_at(access_token, issued_at=None):
    """Unix expiry time from JWT exp, or issued_at + ACCESS_TOKEN_LIFETIME for opaque tokens."""
    issued_at = time.time() if issued_at is None else float(issued_at)
    exp = jwt_payload(access_token).get("exp")
    if exp is not None:
        try:
            return float(exp)
        except (TypeError, ValueError):
            pass
    return issued_at + ACCESS_TOKEN_LIFETIME


def token_usable(access_token, expires_at=None, skew=60):
    """True when the token is present and not within skew seconds of expiry."""
    if not access_token:
        return False
    expires_at = _normalize_expires_at(expires_at)
    if expires_at:
        return time.time() + skew < float(expires_at)
    exp = jwt_payload(access_token).get("exp")
    if exp:
        return time.time() + skew < float(exp)
    return True


def _normalize_expires_at(value, now=None):
    """Unix expiry time from QGIS O2 cache (seconds, milliseconds, or remaining lifetime)."""
    now = time.time() if now is None else float(now)
    try:
        raw = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if raw <= 0:
        return 0.0
    if raw > 1e12:
        raw = raw / 1000.0
    if raw < 1e9:
        return now + raw
    return raw


def _oauth2_config_dict():
    """JSON stored in QgsAuthMethodConfig: PKCE, loopback redirect. No silent token refresh."""
    # AUTHENIX requires `state`. QGIS OAuth2 only sends it when queryPairs includes it.
    return {
        "accessMethod": 0,
        "clientId": OAUTH_CLIENT_ID,
        "configType": 1,
        "grantFlow": 3,
        "persistToken": True,
        "extraTokens": {
            "id_token": ID_TOKEN_HEADER,
            "expires_in": EXPIRES_IN_HEADER,
        },
        "queryPairs": {"state": secrets.token_urlsafe(24)},
        "redirectHost": OAUTH_REDIRECT_HOST,
        "redirectPort": OAUTH_REDIRECT_PORT,
        "redirectUrl": OAUTH_REDIRECT_PATH,
        "refreshTokenUrl": "",
        "requestTimeout": 60,
        "requestUrl": AUTHENIX_AUTHORIZE,
        "scope": _scope_string(),
        "tokenUrl": AUTHENIX_TOKEN,
        "version": 1,
    }


def _auth_manager():
    """QGIS QgsAuthManager, or AuthError if authentication is not available."""
    manager = QgsApplication.authManager()
    if manager is None:
        raise AuthError("QGIS authentication manager is not available.")
    return manager


def _ensure_master_password(manager):
    """Prompt for the QGIS master password; OAuth2 configs live in the encrypted auth database."""
    if manager.masterPasswordIsSet():
        return
    prompt = getattr(manager, "setMasterPassword", None)
    if prompt is None:
        raise AuthError("A QGIS master password is required to use OAuth2 authentication.")
    try:
        ok = prompt(True)
    except TypeError:
        ok = prompt()
    if not ok:
        raise AuthError("A QGIS master password is required to use OAuth2 authentication.")


def _stored_authcfg_id():
    """Auth config id last stored under SETTINGS_GROUP, or empty if none."""
    settings = QgsSettings()
    settings.beginGroup(SETTINGS_GROUP)
    try:
        return (settings.value("authcfg_id", "") or "").strip()
    finally:
        settings.endGroup()


def _save_authcfg_id(authcfg_id):
    """Remember the QgsAuthManager config id in plugin settings."""
    settings = QgsSettings()
    settings.beginGroup(SETTINGS_GROUP)
    try:
        settings.setValue("authcfg_id", authcfg_id or "")
    finally:
        settings.endGroup()


def _store_auth_config(manager, config):
    """Persist a QgsAuthMethodConfig. Return True on success (API differs across QGIS builds)."""
    try:
        result = manager.storeAuthenticationConfig(config, True)
    except TypeError:
        result = manager.storeAuthenticationConfig(config)
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)


def _config_exists(manager, authcfg_id):
    """True if QGIS already has this authcfg. Do not call loadAuthenticationConfig when missing."""
    if not authcfg_id:
        return False
    try:
        if authcfg_id in list(manager.configIds() or []):
            return True
    except Exception:
        pass
    try:
        return authcfg_id in (manager.availableAuthMethodConfigs() or {})
    except Exception:
        return False


def _load_auth_config(manager, authcfg_id):
    """Load an existing OAuth2 config, or None if it is not in the auth database."""
    if not _config_exists(manager, authcfg_id):
        return None
    config = QgsAuthMethodConfig()
    try:
        result = manager.loadAuthenticationConfig(authcfg_id, config, True)
    except TypeError:
        result = manager.loadAuthenticationConfig(authcfg_id, config)
    except Exception:
        return None
    if isinstance(result, tuple):
        if not result[0]:
            return None
        if len(result) > 1 and result[1]:
            return result[1]
        return config if config.id() else None
    return config if result and config.id() else None


def ensure_auth_config():
    """Create or update the plugin's QGIS OAuth2 (PKCE) configuration and return its authcfg id."""
    manager = _auth_manager()
    _ensure_master_password(manager)
    methods = []
    if hasattr(manager, "authMethodsKeys"):
        try:
            methods = list(manager.authMethodsKeys() or [])
        except Exception:
            methods = []
    if methods and "OAuth2" not in methods:
        raise AuthError("The QGIS OAuth2 authentication method is not available.")

    authcfg_id = _stored_authcfg_id() or OAUTH_AUTHCFG_ID
    config = _load_auth_config(manager, authcfg_id)
    if config is None:
        config = QgsAuthMethodConfig()
        if _config_exists(manager, authcfg_id):
            authcfg_id = manager.uniqueConfigId()
        config.setId(authcfg_id)

    payload = _oauth2_config_dict()
    config.setName("STAplus SCK AUTHENIX")
    config.setMethod("OAuth2")
    config.setConfig("oauth2config", json.dumps(payload))
    config.setConfig("querypairs", json.dumps(payload.get("queryPairs") or {}))
    if not _store_auth_config(manager, config):
        config.setId(manager.uniqueConfigId())
        if not _store_auth_config(manager, config):
            raise AuthError(
                "Could not store the QGIS OAuth2 configuration. Set a master password first."
            )
    authcfg_id = config.id() or authcfg_id
    _save_authcfg_id(authcfg_id)
    if not _config_exists(manager, authcfg_id):
        raise AuthError(
            "QGIS did not keep the OAuth2 configuration %s. Check the master password and try again."
            % authcfg_id
        )
    _logger.info(
        "QGIS OAuth2 config %s ready (PKCE, %s:%s/%s)",
        authcfg_id,
        OAUTH_REDIRECT_HOST,
        OAUTH_REDIRECT_PORT,
        OAUTH_REDIRECT_PATH,
    )
    return authcfg_id


def _authcfg_ready():
    """Return a usable authcfg id, creating or updating the QGIS OAuth2 config."""
    return ensure_auth_config()


def _looks_like_id_token(token):
    """True if token is a JWT whose aud matches this plugin's AUTHENIX client_id."""
    claims = jwt_payload(token)
    if not claims or not claims.get("sub"):
        return False
    aud = claims.get("aud")
    if isinstance(aud, list):
        return OAUTH_CLIENT_ID in [str(item) for item in aud]
    return str(aud or "") == OAUTH_CLIENT_ID


def _introspect_access_token(access_token):
    """Return (active, expires_at). active is True, False, or None if AUTHENIX cannot say."""
    if not access_token:
        return False, None
    try:
        response = _http_session().post(
            AUTHENIX_TOKENINFO,
            data={
                "token": access_token,
                "token_type_hint": "access_token",
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
            proxies={},
        )
        if response.status_code in (401, 403, 404, 405):
            _logger.info(
                "AUTHENIX tokeninfo returned HTTP %s; skipping liveness check",
                response.status_code,
            )
            return None, None
        if response.status_code != 200:
            _logger.warning(
                "AUTHENIX tokeninfo HTTP %s for %s: %s",
                response.status_code,
                token_prefix(access_token),
                (response.text or "")[:200],
            )
            return None, None
        body = response.json()
        if not isinstance(body, dict):
            return None, None
        expires_at = None
        if body.get("exp") is not None:
            try:
                expires_at = float(body.get("exp"))
            except (TypeError, ValueError):
                expires_at = None
        if expires_at is None and body.get("expires_in") is not None:
            try:
                expires_at = time.time() + max(0, int(body.get("expires_in")))
            except (TypeError, ValueError):
                expires_at = None
        if body.get("active") is False:
            _logger.warning(
                "AUTHENIX tokeninfo marked %s inactive",
                token_prefix(access_token),
            )
            return False, expires_at
        if body.get("active") is True:
            return True, expires_at
        return None, expires_at
    except Exception as err:
        _logger.warning("AUTHENIX tokeninfo failed: %s", err)
        return None, None


def _expires_at_from_tokeninfo(access_token):
    """Ask AUTHENIX tokeninfo for exp / expires_in; used because opaque at_… tokens have no JWT exp."""
    _active, expires_at = _introspect_access_token(access_token)
    return expires_at


def _o2_crypt_keys():
    """Candidate SimpleCrypt keys derived from the QGIS O2 hardcoded passphrase."""
    digest = hashlib.sha1(O2_ENCRYPTION_KEY.encode("latin1")).digest()
    keys = [0]
    if len(digest) >= 8:
        keys.append(int.from_bytes(digest[:8], "little"))
        keys.append(int.from_bytes(digest[:8], "big"))
    return keys


def _o2_key_parts(key):
    """Eight key bytes used by Qt SimpleCrypt's stream XOR."""
    parts = []
    for i in range(8):
        part = key
        for _ in range(i):
            part >>= 8
        parts.append(part & 0xFF)
    return parts


def _looks_like_secret(value):
    """Heuristic: decrypted cache values that look like a token, JWT, or expiry timestamp."""
    text = (value or "").strip()
    if len(text) < 8:
        return False
    if text.count(".") >= 2:
        return True
    if text.startswith("at_") or text.startswith("rt_") or text.startswith("eyJ"):
        return True
    return all(32 <= ord(char) < 127 for char in text[:24])


def _simplecrypt_decrypt(cipher_text, key):
    """Decrypt one QGIS O2 cache field (Qt SimpleCrypt: flags, XOR, optional hash/checksum/zlib)."""
    text = (cipher_text or "").strip().strip('"')
    if not text:
        return ""
    try:
        raw = bytearray(base64.b64decode(text))
    except Exception:
        return text if _looks_like_secret(text) else ""
    if len(raw) < 2:
        return ""
    flags = raw[0]
    data = raw[1:]
    parts = _o2_key_parts(key)
    last_char = 0
    decoded = bytearray()
    for index, current in enumerate(data):
        decoded.append(current ^ last_char ^ parts[index % 8])
        last_char = current
    if not decoded:
        return ""
    decoded = decoded[1:]
    if flags & _CRYPTO_FLAG_HASH:
        if len(decoded) < 20:
            return ""
        decoded = decoded[20:]
    elif flags & _CRYPTO_FLAG_CHECKSUM:
        if len(decoded) < 2:
            return ""
        decoded = decoded[2:]
    payload = bytes(decoded)
    if flags & _CRYPTO_FLAG_COMPRESSION and len(payload) >= 4:
        try:
            payload = zlib.decompress(payload[4:])
        except Exception:
            try:
                payload = zlib.decompress(payload)
            except Exception:
                return ""
    try:
        return payload.decode("utf-8")
    except Exception:
        return payload.decode("latin1", "replace")


def _decrypt_o2_value(cipher_text):
    """Decrypt a cache value, or return it unchanged if it is already plaintext."""
    text = (cipher_text or "").strip()
    if not text:
        return ""
    if _looks_like_secret(text) and not text.startswith("{"):
        if " " not in text and len(text) < 4000:
            try:
                base64.b64decode(text)
            except Exception:
                return text
    for key in _o2_crypt_keys():
        plain = _simplecrypt_decrypt(text, key)
        if plain and (_looks_like_secret(plain) or plain.isdigit()):
            return plain
    return ""


def _qt_ini_format():
    """QSettings IniFormat enum (Qt6 name, with Qt5 fallback)."""
    try:
        return QSettings.Format.IniFormat
    except AttributeError:
        return QSettings.IniFormat


def _oauth2_token_cache_paths(authcfg_id):
    """Likely paths of QGIS's authcfg-{id}.ini O2 cache (profile and temp)."""
    paths = []
    try:
        from qgis.core import QgsAuthOAuth2Config

        for temporary in (False, True):
            try:
                path = QgsAuthOAuth2Config.tokenCachePath(authcfg_id, temporary)
            except Exception:
                path = ""
            if path:
                paths.append(path)
    except Exception:
        pass
    profile = QgsApplication.qgisSettingsDirPath() or ""
    folders = [os.path.join(tempfile.gettempdir(), "oauth2-cache")]
    if profile:
        folders.insert(0, os.path.join(profile, "oauth2-cache"))
    for folder in folders:
        paths.append(os.path.join(folder, "authcfg-%s.ini" % authcfg_id))
    seen = set()
    unique = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _read_qgis_oauth_tokens(authcfg_id):
    """Read access_token, refresh_token, expires, and id_token from the encrypted O2 cache file."""
    result = {"refresh_token": "", "access_token": "", "expires_at": 0.0, "id_token": ""}
    client_id = OAUTH_CLIENT_ID
    group = "authcfg_%s" % authcfg_id
    for path in _oauth2_token_cache_paths(authcfg_id):
        if not path or not os.path.isfile(path):
            continue
        settings = QSettings(path, _qt_ini_format())
        settings.beginGroup(group)
        try:
            raw_refresh = settings.value("refreshtoken.%s" % client_id, "") or ""
            raw_token = settings.value("token.%s" % client_id, "") or ""
            raw_expires = settings.value("expires.%s" % client_id, "") or ""
            raw_extra = settings.value("extratokens.%s" % client_id, "") or ""
        finally:
            settings.endGroup()
        refresh_token = _decrypt_o2_value(str(raw_refresh))
        access_token = _decrypt_o2_value(str(raw_token))
        expires_plain = _decrypt_o2_value(str(raw_expires)) or str(raw_expires)
        extra_plain = _decrypt_o2_value(str(raw_extra)) or str(raw_extra)
        expires_at = 0.0
        try:
            expires_at = _normalize_expires_at(expires_plain or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        id_token = ""
        if extra_plain:
            try:
                extra = json.loads(extra_plain)
                if isinstance(extra, dict):
                    id_token = extra.get("id_token") or ""
            except (TypeError, ValueError):
                pass
        if refresh_token or access_token or expires_at:
            result.update(
                {
                    "refresh_token": refresh_token,
                    "access_token": access_token or result["access_token"],
                    "expires_at": expires_at,
                    "id_token": id_token,
                }
            )
            _logger.info(
                "Read QGIS OAuth2 cache %s; refresh_token %s expires_at=%s",
                path,
                token_prefix(refresh_token),
                int(expires_at) if expires_at else 0,
            )
            break
    return result


def _remove_oauth_token_cache(authcfg_id):
    """Delete leftover QGIS O2 cache files so port 7070 is not held by an old authenticator."""
    if not authcfg_id:
        return
    for path in _oauth2_token_cache_paths(authcfg_id):
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            _logger.info("Removed QGIS OAuth2 token cache %s", path)
        except OSError as err:
            _logger.warning("Could not remove token cache %s: %s", path, err)


def _invalidate_qgis_tokens(authcfg_id=None):
    """Drop QGIS's in-memory OAuth2 cache and the on-disk token file for this authcfg."""
    authcfg_id = authcfg_id or _stored_authcfg_id() or OAUTH_AUTHCFG_ID
    manager = QgsApplication.authManager()
    if manager is not None and hasattr(manager, "clearCachedConfig"):
        try:
            manager.clearCachedConfig(authcfg_id)
        except Exception as err:
            _logger.warning("Could not clear cached QGIS OAuth2 config: %s", err)
    _remove_oauth_token_cache(authcfg_id)
    _logger.info("Cleared QGIS OAuth2 token cache for %s", authcfg_id)


def _redirect_path_ok(path):
    """True if the request path is the registered loopback path (with or without trailing slash)."""
    expected = "/" + OAUTH_REDIRECT_PATH.strip("/")
    normalized = (path or "").split("?", 1)[0].rstrip("/") or "/"
    return normalized == expected


def _loopback_handler(result):
    """HTTP handler that stores the AUTHENIX callback query and ignores other paths (favicon)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            parsed = urlparse(self.path)
            if not _redirect_path_ok(parsed.path):
                self.send_response(204)
                self.send_header("Connection", "close")
                self.end_headers()
                return
            query = parse_qs(parsed.query)
            result.update(
                {
                    "code": (query.get("code") or [""])[0],
                    "state": (query.get("state") or [""])[0],
                    "error": (query.get("error") or [""])[0],
                    "error_description": (query.get("error_description") or [""])[0],
                    "done": True,
                }
            )
            body = (
                "<!DOCTYPE html><html><body><p>You can close this window and return to QGIS.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


def _http_server(host, port, handler):
    """HTTPServer bound to IPv4 or IPv6 loopback."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class Server(HTTPServer):
        address_family = family
        allow_reuse_address = True

        def server_bind(self):
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    pass
            HTTPServer.server_bind(self)

    return Server((host, port), handler)


def _start_loopback_servers(handler):
    """Listen on 127.0.0.1 and ::1 so Windows localhost (IPv4 or IPv6) can redirect."""
    servers = []
    errors = []
    for host in ("127.0.0.1", "::1"):
        try:
            servers.append(_http_server(host, OAUTH_REDIRECT_PORT, handler))
        except OSError as err:
            errors.append("%s:%s (%s)" % (host, OAUTH_REDIRECT_PORT, err))
    if not servers:
        raise AuthError(
            "Could not listen on port %s (%s). Keep that port free for redirect URI %s."
            % (OAUTH_REDIRECT_PORT, "; ".join(errors), _redirect_uri())
        )
    return servers


def _wait_loopback_result(result, timeout_s):
    """Wait for the browser redirect without freezing Qt; pump the GUI event loop."""
    deadline = time.time() + timeout_s
    app = QCoreApplication.instance()
    while not result.get("done") and time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.05)


def _stop_loopback_servers(servers, threads):
    """Stop serve_forever listeners started for the AUTHENIX redirect."""
    for server in servers:
        try:
            server.shutdown()
        except Exception:
            pass
    for thread in threads:
        thread.join(2)
    for server in servers:
        try:
            server.server_close()
        except Exception:
            pass


def _exchange_authorization_code(code, verifier):
    """POST authorization_code + PKCE verifier to AUTHENIX; return token dict."""
    try:
        response = _http_session().post(
            AUTHENIX_TOKEN,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": verifier,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
            proxies={},
        )
    except Exception as err:
        raise AuthError("AUTHENIX token exchange failed: %s" % err)
    if response.status_code != 200:
        raise AuthError(
            "AUTHENIX token exchange failed (HTTP %s): %s"
            % (response.status_code, (response.text or "")[:200])
        )
    try:
        body = response.json()
    except ValueError:
        raise AuthError("AUTHENIX token response was not JSON.")
    if not isinstance(body, dict):
        raise AuthError("AUTHENIX token response was not JSON.")
    access_token = (body.get("access_token") or "").strip()
    if not access_token:
        raise AuthError("AUTHENIX did not return an access token. Sign in again.")
    expires_at = 0.0
    if body.get("expires_in") is not None:
        try:
            expires_at = time.time() + max(0, int(float(body.get("expires_in"))))
        except (TypeError, ValueError):
            expires_at = 0.0
    return {
        "access_token": access_token,
        "id_token": (body.get("id_token") or "").strip(),
        "refresh_token": (body.get("refresh_token") or "").strip(),
        "expires_at": expires_at,
    }


def _authorization_code_pkce():
    """Open the system browser, receive the loopback code, exchange it for tokens."""
    if not _on_gui_thread():
        raise AuthError(
            "Sign in from the QGIS user interface so the browser login can open."
        )
    verifier = _pkce_verifier()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "scope": _scope_string(),
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    authorize_url = AUTHENIX_AUTHORIZE + "?" + urlencode(params)
    result = {}
    servers = _start_loopback_servers(_loopback_handler(result))
    threads = []
    try:
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
            thread.daemon = True
            thread.start()
            threads.append(thread)
        _logger.info("Listening for AUTHENIX redirect on %s", _redirect_uri())
        QDesktopServices.openUrl(QUrl(authorize_url))
        _wait_loopback_result(result, _LOOPBACK_TIMEOUT_S)
    finally:
        _stop_loopback_servers(servers, threads)
    if result.get("error"):
        detail = result.get("error_description") or result.get("error")
        raise AuthError("AUTHENIX sign-in was denied: %s" % detail)
    if not result.get("done"):
        raise AuthError(
            "AUTHENIX sign-in did not complete. Finish login in the browser, "
            "keep port %s free, and use redirect URI %s."
            % (OAUTH_REDIRECT_PORT, _redirect_uri())
        )
    if (result.get("state") or "") != state:
        raise AuthError("AUTHENIX sign-in failed: state mismatch. Sign in again.")
    code = (result.get("code") or "").strip()
    if not code:
        raise AuthError("AUTHENIX did not return an authorization code. Sign in again.")
    return _exchange_authorization_code(code, verifier)


def _extract_tokens(authcfg_id):
    """Run authorization code + PKCE in the system browser; never call QGIS updateNetworkRequest."""
    extracted = _authorization_code_pkce()
    if extracted.get("access_token") and not _looks_like_id_token(extracted.get("id_token")):
        cache = _read_qgis_oauth_tokens(authcfg_id)
        extracted["id_token"] = cache.get("id_token") or extracted.get("id_token") or ""
    return extracted


def fetch_userinfo(access_token):
    """AUTHENIX userinfo for display name. Opaque access tokens may return {}; that is not a login failure."""
    try:
        response = _http_session().get(
            AUTHENIX_USERINFO,
            headers={"Authorization": "Bearer %s" % access_token, "Accept": "application/json"},
            timeout=30,
            proxies={},
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return data
    except Exception as err:
        _logger.warning("AUTHENIX userinfo failed: %s", err)
    return {}


def _session_from_tokens(access_token, id_token="", refresh_token="", expires_at=0):
    """Build the plugin session dict (tokens, expiry, user claims) stored in QgsSettings."""
    user = jwt_payload(id_token) or jwt_payload(access_token)
    extra = fetch_userinfo(access_token)
    if extra:
        merged = dict(user)
        merged.update(extra)
        user = merged
    now = time.time()
    expires_at = _normalize_expires_at(expires_at, now)
    if expires_at <= now + 1:
        tokeninfo = _expires_at_from_tokeninfo(access_token)
        expires_at = _normalize_expires_at(tokeninfo, now) if tokeninfo else 0.0
    if expires_at <= now + 1:
        expires_at = _access_token_expires_at(access_token, now)
    expires_in = max(0, int(expires_at - time.time())) if expires_at else 0
    return {
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "id_token": id_token or "",
        "expires_at": expires_at,
        "expires_in": expires_in,
        "user": user,
        "scope": _scope_string(),
        "authcfg_id": _stored_authcfg_id() or OAUTH_AUTHCFG_ID,
    }


def _session_from_extracted(extracted, previous=None):
    """Merge tokens from QGIS with the previous session so refresh_token / id_token are not dropped."""
    previous = dict(previous or {})
    session = _session_from_tokens(
        extracted.get("access_token") or "",
        extracted.get("id_token") or previous.get("id_token") or "",
        extracted.get("refresh_token") or previous.get("refresh_token") or "",
        extracted.get("expires_at") or 0,
    )
    if not session.get("refresh_token"):
        session["refresh_token"] = previous.get("refresh_token") or ""
    return session


def acquire_access_token(force_reauth=False):
    """Obtain tokens via authorization code + PKCE. force_reauth=True drops leftover QGIS O2 cache files."""
    if force_reauth:
        _invalidate_qgis_tokens()
        app = QCoreApplication.instance()
        if app is not None:
            for _ in range(5):
                app.processEvents()
    authcfg_id = _stored_authcfg_id() or OAUTH_AUTHCFG_ID
    _logger.info("Requesting AUTHENIX access token via authorization code + PKCE")
    extracted = _extract_tokens(authcfg_id)
    session = _session_from_extracted(extracted, load_session())
    if session.get("id_token"):
        _logger.info("Captured AUTHENIX id_token for RP logout")
    _logger.info(
        "AUTHENIX login succeeded; access token %s expires_in=%ss "
        "(public client: re-open the browser when fewer than %ss remain)",
        token_prefix(session["access_token"]),
        session.get("expires_in") or 0,
        TOKEN_REFRESH_SKEW,
    )
    return session


def refresh_access_token(refresh_token=None):
    """Re-run authorization code + PKCE in the system browser.

    This public client has no client_secret, so AUTHENIX cannot issue a
    refresh_token grant. The plugin opens the system browser again.
    """
    del refresh_token
    if not _on_gui_thread():
        raise AuthError(
            "The AUTHENIX access token has expired. This public client has no "
            "client_secret, so it cannot refresh tokens in the background. "
            "Sign in again in the browser."
        )
    _logger.info("AUTHENIX access token expired; opening the system browser to sign in again")
    session = acquire_access_token(force_reauth=True)
    save_session(session)
    return session


def logout_url(id_token=""):
    """AUTHENIX RP-logout URL. AUTHENIX requires id_token_hint."""
    id_token = (id_token or "").strip()
    if not id_token:
        raise AuthError(
            "AUTHENIX logout requires id_token_hint. Sign in again so QGIS can keep the ID token."
        )
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "id_token_hint": id_token,
        "post_logout_redirect_uri": OAUTH_LOGOUT_REDIRECT_URI,
    }
    return AUTHENIX_LOGOUT + "?" + urlencode(params)


def id_token_for_logout(session=None):
    """ID token from the stored session or the O2 cache; needed as id_token_hint on logout."""
    session = dict(session or load_session())
    id_token = (session.get("id_token") or "").strip()
    if _looks_like_id_token(id_token):
        return id_token
    cache = _read_qgis_oauth_tokens(_stored_authcfg_id() or OAUTH_AUTHCFG_ID)
    cached = (cache.get("id_token") or "").strip()
    return cached if _looks_like_id_token(cached) else cached


def load_session():
    """Restore tokens and user from QgsSettings, or empty if auth_backend is not this plugin's OAuth2."""
    empty = {
        "access_token": "",
        "refresh_token": "",
        "id_token": "",
        "expires_at": 0.0,
        "expires_in": 0,
        "user": {},
    }
    settings = QgsSettings()
    settings.beginGroup(SETTINGS_GROUP)
    try:
        if (settings.value("auth_backend", "") or "") != AUTH_BACKEND:
            return empty
        user_raw = settings.value("user_json", "")
        try:
            user = json.loads(user_raw) if user_raw else {}
        except (TypeError, json.JSONDecodeError):
            user = {}
        expires_at = settings.value("expires_at", 0)
        try:
            expires_at = float(expires_at or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        expires_in = settings.value("expires_in", 0)
        try:
            expires_in = int(float(expires_in or 0))
        except (TypeError, ValueError):
            expires_in = max(0, int(expires_at - time.time())) if expires_at else 0
        return {
            "access_token": settings.value("access_token", "") or "",
            "refresh_token": settings.value("refresh_token", "") or "",
            "id_token": settings.value("id_token", "") or "",
            "expires_at": expires_at,
            "expires_in": expires_in,
            "user": user if isinstance(user, dict) else {},
            "authcfg_id": settings.value("authcfg_id", "") or "",
        }
    finally:
        settings.endGroup()


def save_session(session):
    """Persist tokens, expiry, and user JSON under SETTINGS_GROUP."""
    settings = QgsSettings()
    settings.beginGroup(SETTINGS_GROUP)
    try:
        settings.setValue("auth_backend", AUTH_BACKEND)
        settings.setValue("access_token", session.get("access_token") or "")
        settings.setValue("refresh_token", session.get("refresh_token") or "")
        settings.setValue("id_token", session.get("id_token") or "")
        settings.setValue("expires_at", float(session.get("expires_at") or 0))
        settings.setValue("expires_in", int(session.get("expires_in") or 0))
        settings.setValue("user_json", json.dumps(session.get("user") or {}))
        if session.get("authcfg_id"):
            settings.setValue("authcfg_id", session.get("authcfg_id") or "")
    finally:
        settings.endGroup()


def clear_session():
    """Remove stored tokens and the QGIS OAuth2 cache. Does not delete the authcfg itself."""
    _invalidate_qgis_tokens()
    settings = QgsSettings()
    settings.beginGroup(SETTINGS_GROUP)
    try:
        for key in _SESSION_KEYS:
            settings.remove(key)
    finally:
        settings.endGroup()
    _logger.info("Local AUTHENIX session and QGIS OAuth2 tokens cleared")


def ensure_fresh_session(session=None, skew=None):
    """Return a still-valid access token for STAplus HTTP.

    Does not open the browser. When the token is missing or expired, raise
    AuthError so the user can click Sign in.
    """
    if skew is None:
        skew = TOKEN_REFRESH_SKEW
    session = dict(session or load_session())
    token = session.get("access_token") or ""
    expires_at = session.get("expires_at") or 0
    remaining = max(0, int(float(expires_at) - time.time())) if expires_at else 0
    if token_usable(token, expires_at, skew=skew):
        _logger.info(
            "Using stored AUTHENIX access token %s; expires_in=%ss",
            token_prefix(token),
            remaining,
        )
        return session
    if not token:
        raise AuthError("Not signed in. Sign in with AUTHENIX first.")
    raise AuthError(
        "The AUTHENIX access token has expired (expires_in=%ss). Click Sign in and try again."
        % remaining
    )
