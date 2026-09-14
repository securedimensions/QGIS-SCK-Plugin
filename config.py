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

"""Plugin constants: STAplus endpoint, MQTT broker, and AUTHENIX / QGIS OAuth2 settings."""

STA_URL = "https://citiobs.demo.secure-dimensions.de/stapluscelltest/v1.1"
# MQTT host/port come from the STA landing page (create-observations-via-mqtt endpoints).
MQTT_CREATE_SPEC = (
    "http://www.opengis.net/spec/iot_sensing/1.1/req/create-observations-via-mqtt/"
    "observations-creation"
)
DGGS_CORE_SPEC = "http://www.opengis.net/spec/sensorthings-dggs/1.0/conf/core"
# H3 cell for a Datastream is the hex index at this resolution (marker location).
H3_CELL_RESOLUTION = 9

# Public AUTHENIX client "QGIS SCK Plugin" (authorization code + PKCE, no client_secret).
# Access tokens cannot be refreshed in the background; the plugin re-opens the system browser.
OAUTH_CLIENT_ID = "66de66cb-5ba9-4536-9fc5-7cc7a5bd6b3f"
OAUTH_SCOPES = [
    "openid",
    "profile",
    "email",
    "idp",
    "citiobs.secd.eu#create",
    "citiobs.secd.eu#update",
]
# Redirect URI registered at AUTHENIX: http://localhost:7070/sck-qgis-plugin
# QGIS builds the loopback URI as http://{host}:{port}/{path} (path has no leading slash).
OAUTH_REDIRECT_HOST = "localhost"
OAUTH_REDIRECT_PORT = 7070
OAUTH_REDIRECT_PATH = "sck-qgis-plugin"
# Preferred QgsAuthManager config id; QGIS may assign another if this id is taken.
OAUTH_AUTHCFG_ID = "sckqgis"

AUTHENIX_ORIGIN = "https://authenix.eu"
AUTHENIX_AUTHORIZE = AUTHENIX_ORIGIN + "/oauth/authorize"
AUTHENIX_TOKEN = AUTHENIX_ORIGIN + "/oauth/token"
AUTHENIX_TOKENINFO = AUTHENIX_ORIGIN + "/oauth/tokeninfo"
AUTHENIX_LOGOUT = AUTHENIX_ORIGIN + "/openid/logout"
AUTHENIX_USERINFO = AUTHENIX_ORIGIN + "/openid/userinfo"
OAUTH_LOGOUT_REDIRECT_URI = AUTHENIX_ORIGIN + "/"

SETTINGS_GROUP = "STAplusSCK"
# QgsSettings keys (same group) for the last confirmed WGS84 marker.
MARKER_LAT_KEY = "marker_lat"
MARKER_LON_KEY = "marker_lon"
MARKER_CONFIRMED_KEY = "marker_confirmed"
MARKER_NAME_KEY = "marker_name"
# Used when the Location name field is empty (STAplus Location.name).
DEFAULT_LOCATION_NAME = "QGIS SCK marker"
# STAplus PartyLocation.encodingType (ValueCode) and environment (EnvCode).
PARTY_LOCATION_ENCODING = "application/geo+json"
PARTY_LOCATION_ENVIRONMENT = "outdoor"
# WGS84 degrees; ~0.1 m. Marker reuse compares Location / PartyLocation against this.
MARKER_COORD_EPS = 1e-6
# Dock label when no public OSM place is selected as FeatureOfInterest.
DEFAULT_FOI_LABEL = "The World (no geometry)"
FOI_JSON_KEY = "selected_foi_json"
# Overpass lookup around the Thing marker (named public places only).
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_USER_AGENT = (
    "STAplus-SCK-QGIS-plugin/0.1 (https://www.secure-dimensions.de)"
)
NEARBY_PLACES_RADIUS_M = 300
NEARBY_PLACES_LIMIT = 40
# Fallback lifetime (seconds) when AUTHENIX returns an opaque token with no exp claim.
ACCESS_TOKEN_LIFETIME = 300
# Re-open the browser this many seconds before expiry (no silent refresh_token grant).
TOKEN_REFRESH_SKEW = 60
# Marker stored with the session so older tokens from a different login method are ignored.
AUTH_BACKEND = "qgis_oauth2"

# XYZ tiles used when the QGIS project has no map layers yet.
OSM_XYZ_URI = (
    "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0&crs=EPSG3857"
)
OSM_LAYER_NAME = "OpenStreetMap"
# Default view when adding OSM and no marker exists yet (central Europe).
DEFAULT_MAP_LAT = 50.0
DEFAULT_MAP_LON = 10.0
DEFAULT_MAP_SCALE = 12000000
MARKER_MAP_SCALE = 25000

# Smart Citizen Kit serial (USB CDC). Charts stay on the QGIS canvas at the Thing marker.
SCK_BAUD = 115200
SCK_SAMPLE_INTERVAL = 10
SCK_PORT_KEY = "sck_port"
# Last kit MAC written as Thing properties.sck_id (replaces the old numeric kit_id).
SCK_ID_KEY = "sck_id"
THING_NAME = "Smart Citizen Kit"
CHART_WINDOW_S = 30 * 60
DISPLAY_NAME_KEY = "party_display_name"
LICENSE_ID_KEY = "license_template_id"

MQTT_PUBLISH_TOPIC = "v1.1/ObservationGroups"
MQTT_PUBLISH_QOS = 1
MQTT_PUBLISH_TIMEOUT = 5
MQTT_KEEP_ALIVE = 60
ELEVATION_API_URL = "https://api.open-elevation.com/api/v1/lookup"
