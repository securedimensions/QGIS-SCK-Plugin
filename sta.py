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

"""STAplus HTTP client used to set up Party, Thing, FoI, License, PartyLocation, and SCK Datastreams."""

import json
import logging
from urllib.parse import urljoin, urlparse

import requests

from . import authenix
from .catalog import DATASTREAMS, OBSERVED_PROPERTIES, OM_MEASUREMENT, SENSORS
from .config import (
    DEFAULT_LOCATION_NAME,
    DGGS_CORE_SPEC,
    ELEVATION_API_URL,
    H3_CELL_RESOLUTION,
    MARKER_COORD_EPS,
    MQTT_CREATE_SPEC,
    PARTY_LOCATION_ENCODING,
    PARTY_LOCATION_ENVIRONMENT,
    STA_URL,
    THING_NAME,
)
from .h3cell import latlng_to_cell

_logger = logging.getLogger("sck.sta")


class StaError(RuntimeError):
    """Raised when a STAplus HTTP request fails or returns an error status."""


def is_template_license(entity):
    """True for system-wide license templates (CC_BY, CC_PD, …), not user clones."""
    iot_id = str((entity or {}).get("@iot.id") or "")
    return iot_id.startswith("CC_")


def is_attribution_license(entity):
    """True when the license requires an attributionText instance (CC BY family)."""
    entity = entity or {}
    iot_id = str(entity.get("@iot.id") or "")
    definition = str(entity.get("definition") or "").lower()
    name = str(entity.get("name") or "").lower()
    if iot_id == "CC_PD" or "publicdomain" in definition or "/zero/" in definition:
        return False
    if "/licenses/by" in definition:
        return True
    if "attribution" in name:
        return True
    return False


def parse_mqtt_endpoint(uri):
    """Parse mqtt://host:port from a SensorThings landing-page endpoint URI."""
    parsed = urlparse(str(uri or "").strip())
    host = parsed.hostname
    if not host:
        return None
    if parsed.port:
        port = int(parsed.port)
    elif parsed.scheme in ("mqtts", "ssl", "tls"):
        port = 8883
    else:
        port = 1883
    return {"host": host, "port": port, "uri": uri, "scheme": parsed.scheme or "mqtt"}


def mqtt_broker_from_landing(landing):
    """MQTT broker advertised under create-observations-via-mqtt on the STA root document."""
    settings = (landing or {}).get("serverSettings") or {}
    endpoints = []
    block = settings.get(MQTT_CREATE_SPEC)
    if isinstance(block, dict):
        endpoints = list(block.get("endpoints") or [])
    if not endpoints:
        for value in settings.values():
            if not isinstance(value, dict):
                continue
            for item in value.get("endpoints") or []:
                if isinstance(item, str) and item.lower().startswith("mqtt"):
                    endpoints.append(item)
    for endpoint in endpoints:
        parsed = parse_mqtt_endpoint(endpoint)
        if parsed:
            return parsed
    raise StaError(
        "STAplus landing page has no MQTT endpoint under %s" % MQTT_CREATE_SPEC
    )


def grid_system_from_landing(landing):
    """DGGS gridSystem from serverSettings under sensorthings-dggs core, or None."""
    settings = (landing or {}).get("serverSettings") or {}
    block = settings.get(DGGS_CORE_SPEC)
    if isinstance(block, dict):
        grid = str(block.get("gridSystem") or "").strip()
        if grid:
            return grid
    return None


def cell_id_for_marker(grid_system, lat, lon, resolution=H3_CELL_RESOLUTION):
    """Cell @iot.id for the marker. H3 uses the hex index; other grids are unsupported."""
    if not grid_system:
        return None
    if grid_system.upper() == "H3":
        try:
            return latlng_to_cell(lat, lon, resolution)
        except (TypeError, ValueError) as err:
            raise StaError("Could not encode H3 Cell id: %s" % err) from err
    raise StaError(
        "Unsupported DGGS gridSystem %r (this plugin encodes H3 cell ids)." % grid_system
    )


class StaClient:
    """Authenticated SensorThings / STAplus client. HTTP 401 tells the user to click Sign in."""

    def __init__(self, session=None, base_url=STA_URL):
        """session must contain access_token from authenix (browser PKCE login)."""
        self.base_url = base_url.rstrip("/") + "/"
        self.session = dict(session or authenix.load_session())
        self._http = requests.Session()
        self._http.trust_env = False
        self._http.proxies = {}

    def access_token(self):
        """Bearer token used on the current request."""
        return self.session.get("access_token") or ""

    def _headers(self, authenticate=True):
        """JSON Accept. Bearer is only for writes; STAplus reads are public."""
        headers = {"Accept": "application/json"}
        if authenticate:
            token = self.access_token()
            if token:
                headers["Authorization"] = "Bearer %s" % token
        return headers

    def request(self, method, path, payload=None, params=None, authenticate=None):
        """Send one STAplus request. GET/HEAD are unauthenticated; writes send Bearer."""
        method = method.upper()
        if authenticate is None:
            authenticate = method not in ("GET", "HEAD", "OPTIONS")
        href = path if path.startswith("http") else urljoin(self.base_url, path.lstrip("/"))
        kwargs = {
            "headers": self._headers(authenticate),
            "timeout": 60,
            "proxies": {},
        }
        if params:
            kwargs["params"] = params
        if payload is not None:
            kwargs["data"] = json.dumps(payload, default=str).encode("utf-8")
            kwargs["headers"]["Content-Type"] = "application/json"
        _logger.info("%s %s", method, href)
        try:
            response = self._http.request(method, href, **kwargs)
        except requests.RequestException as err:
            raise StaError("STAplus request failed: %s" % err) from err

        if response.status_code == 401:
            if authenticate:
                raise authenix.AuthError(
                    "STAplus HTTP 401. Click Sign in and try again."
                )
            raise StaError(
                _format_http_error(response, href, "", payload)
            )

        if response.status_code >= 400:
            raise StaError(
                _format_http_error(
                    response, href, self.access_token() if authenticate else "", payload
                )
            )
        if response.status_code == 204 or not (response.text or "").strip():
            return _entity_from_location_header(response) or {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def get(self, path, params=None):
        """GET a STAplus collection or entity. Reads do not send a Bearer token."""
        return self.request("GET", path, params=params)

    def post(self, path, payload):
        """POST a new STAplus entity (Bearer required)."""
        return self.request("POST", path, payload=payload)

    def patch(self, path, payload):
        """PATCH an existing STAplus entity (Bearer required)."""
        return self.request("PATCH", path, payload=payload)

    def landing_page(self):
        """GET the STAplus root document (public read, no Bearer token)."""
        return self.get("")

    def mqtt_broker(self, landing=None):
        """MQTT host/port from landing-page create-observations-via-mqtt endpoints."""
        if landing is None:
            landing = self.landing_page()
        broker = mqtt_broker_from_landing(landing)
        _logger.info("STA landing page MQTT broker %s:%s (%s)", broker["host"], broker["port"], broker["uri"])
        return broker

    def setup_publishing(
        self,
        display_name,
        sck_id,
        lat,
        lon,
        location_name=None,
        foi_spec=None,
        license_id=None,
        attribution_text="",
        license_iot_id=None,
    ):
        """Create Party, Thing, Location, PartyLocation, License instance if needed, and eight Datastreams.

        One Thing per Party and kit MAC (name is always Smart Citizen Kit). A marker
        position change adds a Location on that Thing. A marker name or position change
        creates a new PartyLocation. Existing Locations are never patched.
        """
        sck_id = str(sck_id or "").strip()
        if not sck_id:
            raise StaError("Connect the Smart Citizen Kit so its MAC can be used as sck_id.")
        if lat is None or lon is None:
            raise StaError("Confirm a Thing location on the map before publishing.")
        user = self.session.get("user") or {}
        sub = user.get("sub") or ""
        display_name = (display_name or "").strip() or (
            user.get("preferred_username") or user.get("name") or "QGIS SCK user"
        )
        site_name = _location_name(location_name)
        if license_iot_id is not None:
            license_iot = license_iot_id
        else:
            license_entity = self.resolve_publish_license(license_id, attribution_text, display_name)
            license_iot = license_entity.get("@iot.id")
        party = self._ensure_party(display_name, sub)
        thing = self._ensure_thing(party, sck_id=sck_id, lat=lat, lon=lon, location_name=site_name)
        thing = self._prepare_publish_thing(thing, sck_id)
        foi = self.ensure_feature_of_interest(foi_spec)
        ops = self._ensure_sck_observed_properties()
        sensors = self._ensure_sck_sensors()
        landing = self.landing_page()
        grid_system = grid_system_from_landing(landing)
        cell_id = cell_id_for_marker(grid_system, lat, lon)
        if cell_id:
            _logger.info(
                "DGGS gridSystem=%s Cell @iot.id=%s resolution=%s Point [%s, %s]",
                grid_system,
                cell_id,
                H3_CELL_RESOLUTION,
                lon,
                lat,
            )
        ds_ids = {}
        for spec in DATASTREAMS:
            created = self._create_sck_datastream(
                spec,
                party=party,
                thing=thing,
                license_id=license_iot,
                observed_property_id=ops[spec["observed_property"]],
                sensor_id=sensors[spec["sensor"]],
                cell_id=cell_id,
            )
            ds_ids[spec["config_key"]] = created.get("@iot.id")
        elevation = self._lookup_elevation(lat, lon)
        party_location = self._ensure_party_location(
            party, lat, lon, elevation=elevation
        )
        broker = self.mqtt_broker(landing)
        config = {
            "thing_id": thing.get("@iot.id"),
            "party_id": party.get("@iot.id"),
            "party_display_name": party.get("displayName"),
            "party_location_id": party_location.get("@iot.id"),
            "sck_id": sck_id,
            "license_id": license_iot,
            "license_template_id": license_id,
            "foi_id": foi.get("@iot.id"),
            "cell_id": cell_id,
            "grid_system": grid_system,
            "elevation": elevation,
            "lat": float(lat),
            "lon": float(lon),
            "location_name": site_name,
            "mqtt_host": broker["host"],
            "mqtt_port": broker["port"],
            "mqtt_uri": broker["uri"],
        }
        config.update(ds_ids)
        _logger.info("STAplus publishing setup ready: %s", json.dumps(config, default=str))
        return config

    def _prepare_publish_thing(self, thing, sck_id):
        """Name the Thing Smart Citizen Kit and keep properties.sck_id = MAC."""
        props = dict(thing.get("properties") or {})
        props["plugin"] = "sck"
        props["source"] = props.get("source") or "qgis"
        props["sck_id"] = sck_id
        payload = {
            "name": THING_NAME,
            "description": "The Smart Citizen Kit that publishes on STAplus",
            "properties": props,
        }
        self.patch(_entity_path("Things", thing.get("@iot.id")), payload)
        thing.update(payload)
        return thing

    def _ensure_sck_observed_properties(self):
        """Reuse ObservedProperties tagged properties/role=base, else create them."""
        by_definition = {}
        by_name = {}
        found = self._query("ObservedProperties", "properties/role eq 'base'", top=100)
        for item in found:
            if item.get("definition"):
                by_definition[item.get("definition")] = item
            if item.get("name"):
                by_name[item.get("name")] = item
        ids = {}
        for key, spec in OBSERVED_PROPERTIES.items():
            existing = by_definition.get(spec["definition"]) or by_name.get(spec["name"])
            if existing:
                ids[key] = existing.get("@iot.id")
                continue
            payload = dict(spec)
            payload["properties"] = {"role": "base"}
            created = self.post("ObservedProperties", payload)
            ids[key] = created.get("@iot.id")
            _logger.info("Created ObservedProperty %s @iot.id=%s", spec["name"], ids[key])
        return ids

    def _ensure_sck_sensors(self):
        """Reuse Sensors by name, else create the SCK hardware Sensors."""
        ids = {}
        for key, spec in SENSORS.items():
            found = self._query("Sensors", "name eq %s" % _odata_quote(spec["name"]))
            if found:
                ids[key] = found[0].get("@iot.id")
                continue
            created = self.post("Sensors", spec)
            ids[key] = created.get("@iot.id")
            _logger.info("Created Sensor %s @iot.id=%s", spec["name"], ids[key])
        return ids

    def _create_sck_datastream(
        self, spec, party, thing, license_id, observed_property_id, sensor_id, cell_id=None
    ):
        """Always create a new measurement Datastream for this publish session."""
        payload = {
            "name": spec["name"],
            "description": spec["description"],
            "observationType": OM_MEASUREMENT,
            "unitOfMeasurement": spec["unit"],
            "ObservedProperty": {"@iot.id": observed_property_id},
            "Sensor": {"@iot.id": sensor_id},
            "Thing": {"@iot.id": thing.get("@iot.id")},
            "Party": {"@iot.id": party.get("@iot.id")},
        }
        if license_id is not None:
            payload["License"] = {"@iot.id": license_id}
        if cell_id:
            payload["Cell"] = {"@iot.id": cell_id}
        created = self.post("Datastreams", payload)
        _logger.info(
            "Created Datastream @iot.id=%s name=%s Cell=%s",
            created.get("@iot.id"),
            spec["name"],
            cell_id or "(none)",
        )
        return created

    def set_thing_location(self, thing, lat, lon, name=None):
        """Reuse a matching Location, otherwise POST a new one. Never patch an existing Location."""
        lat = float(lat)
        lon = float(lon)
        thing_id = thing.get("@iot.id")
        if thing_id is None:
            raise StaError("Cannot set Location: Thing has no @iot.id.")
        site_name = _location_name(name)
        if _thing_has_location(thing, lat, lon, site_name):
            _logger.info(
                "Reusing Location on Thing @iot.id=%s name=%s Point [%s, %s]",
                thing_id,
                site_name,
                lon,
                lat,
            )
            return thing
        payload = _location_payload(lat, lon, site_name)
        created = self.post(_entity_path("Things", thing_id) + "/Locations", payload)
        _logger.info(
            "Added Location @iot.id=%s name=%s to Thing @iot.id=%s Point [%s, %s]",
            created.get("@iot.id"),
            payload["name"],
            thing_id,
            lon,
            lat,
        )
        return self.get(
            _entity_path("Things", thing_id),
            params={"$expand": "Locations,Party"},
        )

    def update_publish_location(self, config, lat, lon, location_name=None):
        """Add a Location to the current Thing and ensure a PartyLocation. Datastreams stay put."""
        config = dict(config or {})
        thing_id = config.get("thing_id")
        party_id = config.get("party_id")
        if thing_id is None:
            raise StaError("Cannot add Location: publishing config has no thing_id.")
        if party_id is None:
            raise StaError("Cannot add PartyLocation: publishing config has no party_id.")
        if lat is None or lon is None:
            raise StaError("Confirm a Thing location on the map before publishing.")
        site_name = _location_name(location_name)
        thing = self.get(
            _entity_path("Things", thing_id),
            params={"$expand": "Locations,Party"},
        )
        thing = self.set_thing_location(thing, lat, lon, name=site_name)
        elevation = self._lookup_elevation(lat, lon)
        party_location = self._ensure_party_location(
            {"@iot.id": party_id},
            lat,
            lon,
            elevation=elevation,
            reuse=False,
        )
        config.update(
            {
                "thing_id": thing.get("@iot.id"),
                "party_location_id": party_location.get("@iot.id"),
                "elevation": elevation,
                "lat": float(lat),
                "lon": float(lon),
                "location_name": site_name,
            }
        )
        _logger.info(
            "Updated publish Location on Thing @iot.id=%s PartyLocation @iot.id=%s name=%s Point [%s, %s]",
            config.get("thing_id"),
            config.get("party_location_id"),
            site_name,
            lon,
            lat,
        )
        return config

    def _ensure_party(self, display_name, sub):
        """Return the Party for this AUTHENIX subject, creating it if STAplus has none yet."""
        display_name = (display_name or "").strip() or "QGIS SCK user"
        if sub:
            found = self._query("Parties", "authId eq %s" % _odata_quote(sub))
            if found:
                party = found[0]
                _logger.info("Reusing Party @iot.id=%s authId=%s", party.get("@iot.id"), sub)
                if display_name and party.get("displayName") != display_name:
                    self.patch(_entity_path("Parties", party.get("@iot.id")), {"displayName": display_name})
                    party["displayName"] = display_name
                    _logger.info("Updated Party displayName=%s", display_name)
                return party
        payload = {
            "description": "Acting user of the STAplus SCK QGIS plugin",
            "displayName": display_name,
            "role": "individual",
        }
        party = self.post("Parties", payload)
        _logger.info(
            "Created Party @iot.id=%s displayName=%s authId=%s",
            party.get("@iot.id"),
            party.get("displayName"),
            party.get("authId"),
        )
        return party

    def find_party(self):
        """Party for the current AUTHENIX subject, or None."""
        sub = (self.session.get("user") or {}).get("sub") or ""
        if not sub:
            return None
        found = self._query("Parties", "authId eq %s" % _odata_quote(sub))
        return found[0] if found else None

    def list_template_licenses(self):
        """System License templates offered by the service (CC_BY, CC_PD, …)."""
        try:
            data = self.get("Licenses", params={"$top": "100", "$orderby": "name"})
        except StaError as err:
            raise StaError("Could not load Licenses: %s" % err) from err
        items = data.get("value") or []
        templates = [item for item in items if is_template_license(item)]
        return templates or items

    def resolve_publish_license(self, template_id, attribution_text="", display_name=""):
        """Use a template License, or clone it with attributionText when required."""
        template_id = str(template_id or "").strip()
        if not template_id:
            raise StaError("Select a License template.")
        template = self.get(_entity_path("Licenses", template_id))
        if is_attribution_license(template):
            text = (attribution_text or "").strip()
            if not text:
                raise StaError("This license requires attribution text.")
            payload = {
                "name": template.get("name") or template_id,
                "description": "License instance for %s" % (display_name or "STAplus SCK"),
                "definition": template.get("definition"),
            }
            if template.get("logo"):
                payload["logo"] = template.get("logo")
            payload["attributionText"] = text
            created = self.post("Licenses", payload)
            _logger.info(
                "Created License instance @iot.id=%s from template %s",
                created.get("@iot.id"),
                template_id,
            )
            return created
        _logger.info("Using License template @iot.id=%s", template.get("@iot.id"))
        return template

    def _ensure_thing(self, party, sck_id=None, lat=None, lon=None, location_name=None):
        """Reuse the Thing for this Party and kit MAC, or create one and add a Location."""
        party_id = party.get("@iot.id")
        sck_id = str(sck_id or "").strip()
        site_name = _location_name(location_name)
        found = []
        if sck_id:
            found = self._query(
                "Things",
                "Party/id eq %s and properties/sck_id eq %s"
                % (_odata_quote(str(party_id)), _odata_quote(sck_id)),
                expand="Locations,Party",
                top=100,
            )
        if found:
            thing = found[0]
            _logger.info(
                "Reusing Thing @iot.id=%s for Party @iot.id=%s sck_id=%s",
                thing.get("@iot.id"),
                party_id,
                sck_id,
            )
            if sck_id:
                thing = self._patch_thing_sck_id(thing, sck_id)
            if lat is not None and lon is not None:
                thing = self.set_thing_location(thing, lat, lon, name=site_name)
            return thing

        props = {"plugin": "sck", "source": "qgis"}
        if sck_id:
            props["sck_id"] = sck_id
        payload = {
            "name": THING_NAME,
            "description": "The Smart Citizen Kit that publishes on STAplus",
            "properties": props,
            "Party": {"@iot.id": party_id},
        }
        thing = self.post("Things", payload)
        if lat is not None and lon is not None:
            thing = self.set_thing_location(thing, lat, lon, name=site_name)
        _logger.info(
            "Created Thing @iot.id=%s sck_id=%s Location name=%s Point [%s, %s]",
            thing.get("@iot.id"),
            sck_id or "(none)",
            site_name,
            lon,
            lat,
        )
        return thing

    def _ensure_party_location(self, party, lat, lon, elevation=0, reuse=True):
        """Reuse a PartyLocation at this WGS84 position, or create one.

        PartyLocation has no name in the service model; the marker name is stored on
        Location. A marker name or position change from the plugin creates a new entity
        (reuse=False). Existing PartyLocations are never patched.
        """
        party_id = party.get("@iot.id")
        if reuse:
            found = self._query(
                "PartyLocations",
                "Party/id eq %s" % _odata_quote(str(party_id)),
                top=100,
            )
            for item in found:
                if _party_location_matches(item, lat, lon):
                    _logger.info(
                        "Reusing PartyLocation @iot.id=%s Point [%s, %s]",
                        item.get("@iot.id"),
                        lon,
                        lat,
                    )
                    return item
        payload = _party_location_payload(party_id, lat, lon, elevation=elevation)
        created = self.post("PartyLocations", payload)
        _logger.info(
            "Created PartyLocation @iot.id=%s Point [%s, %s]",
            created.get("@iot.id"),
            lon,
            lat,
        )
        return created

    def _patch_thing_sck_id(self, thing, sck_id):
        """Set Thing properties.sck_id to the kit MAC (replaces the old numeric kit_id)."""
        props = dict(thing.get("properties") or {})
        if props.get("sck_id") == sck_id and props.get("plugin") == "sck":
            return thing
        props["plugin"] = "sck"
        if not props.get("source"):
            props["source"] = "qgis"
        props["sck_id"] = sck_id
        thing_id = thing.get("@iot.id")
        self.patch(_entity_path("Things", thing_id), {"properties": props})
        thing["properties"] = props
        _logger.info("Updated Thing @iot.id=%s properties.sck_id=%s", thing_id, sck_id)
        return thing

    def _lookup_elevation(self, lat, lon, timeout=10.0):
        """Orthometric elevation in metres, or 0 if Open-Elevation is unavailable."""
        try:
            response = requests.get(
                ELEVATION_API_URL,
                params={"locations": "%s,%s" % (lat, lon)},
                timeout=timeout,
                proxies={},
            )
            response.raise_for_status()
            data = response.json()
            return float(data["results"][0]["elevation"])
        except Exception as err:
            _logger.warning("Elevation lookup failed (%s); using 0 m", err)
            return 0.0

    def ensure_feature_of_interest(self, foi_spec=None):
        """Reuse/create an OSM public FoI, or the unset World FoI when foi_spec is missing."""
        if foi_spec is not None:
            place = self._place_feature_of_interest(foi_spec)
            if place is not None:
                return place
        return self._ensure_feature_of_interest()

    def _place_feature_of_interest(self, foi_spec):
        """Reuse or create a FeatureOfInterest for a named public OSM geometry."""
        if not isinstance(foi_spec, dict):
            return None
        props = foi_spec.get("properties") if isinstance(foi_spec.get("properties"), dict) else {}
        geom = foi_spec.get("geometry")
        osm_id = str(props.get("osm_id") or "").strip()
        name = str(props.get("name") or "").strip()
        if not osm_id or not name or not isinstance(geom, dict) or not geom.get("type"):
            return None
        key = "osm:" + osm_id
        found = self._find_foi_by_osm_key(key)
        if found:
            _logger.info(
                "Reusing FeatureOfInterest @iot.id=%s for %s",
                found.get("@iot.id"),
                key,
            )
            return found
        payload = {
            "name": name[:200],
            "description": "Public OSM feature %s" % key,
            "encodingType": "application/geo+json",
            "feature": {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "osm_id": osm_id,
                    "kind": str(props.get("kind") or "").strip(),
                },
            },
        }
        foi = self.post("FeaturesOfInterest", payload)
        _logger.info(
            "Created FeatureOfInterest @iot.id=%s name=%s %s",
            foi.get("@iot.id"),
            foi.get("name"),
            key,
        )
        return foi

    def _find_foi_by_osm_key(self, key):
        """Find an existing FoI whose description contains osm:type/id."""
        quoted = _odata_quote(key)
        for expr in (
            "contains(description,%s)" % quoted,
            "substringof(%s,description)" % quoted,
        ):
            found = self._query("FeaturesOfInterest", expr)
            if found:
                return found[0]
        return None

    def _ensure_feature_of_interest(self):
        """Return the shared unset FeatureOfInterest (geometry is intentionally null)."""
        found = self._query(
            "FeaturesOfInterest",
            "name eq %s" % _odata_quote("QGIS SCK Unset Feature"),
        )
        if found:
            _logger.info("Reusing FeatureOfInterest @iot.id=%s", found[0].get("@iot.id"))
            return found[0]
        payload = {
            "name": "QGIS SCK Unset Feature",
            "description": "FeatureOfInterest geometry not assigned",
            "encodingType": "application/geo+json",
            "feature": {
                "type": "Feature",
                "geometry": None,
                "properties": {},
            },
        }
        foi = self.post("FeaturesOfInterest", payload)
        geometry = None
        feature = foi.get("feature")
        if isinstance(feature, dict):
            geometry = feature.get("geometry")
        _logger.info(
            "Created FeatureOfInterest @iot.id=%s feature.geometry=%s",
            foi.get("@iot.id"),
            geometry,
        )
        return foi

    def _query(self, collection, filter_expr, expand=None, top=5):
        """OData $filter lookup; returns [] if the query fails so callers can fall back to create."""
        params = {"$filter": filter_expr, "$top": str(top)}
        if expand:
            params["$expand"] = expand
        try:
            data = self.get(collection, params=params)
        except StaError as err:
            _logger.warning("Query %s failed: %s", collection, err)
            return []
        return data.get("value") or []


def _location_payload(lat, lon, name=None):
    """STAplus Location body. name is required by STA 1.1 / FROST."""
    location_name = _location_name(name)
    return {
        "name": location_name,
        "description": "Location chosen on the QGIS map",
        "encodingType": PARTY_LOCATION_ENCODING,
        "location": {"type": "Point", "coordinates": [float(lon), float(lat)]},
    }


def _party_location_payload(party_id, lat, lon, elevation=0):
    """STAplus v1.1 PartyLocation body. Marker name is not a PartyLocation property."""
    payload = {
        "position": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "encodingType": PARTY_LOCATION_ENCODING,
        "environment": PARTY_LOCATION_ENVIRONMENT,
        "Party": {"@iot.id": party_id},
    }
    if elevation is not None:
        payload["elevation"] = float(elevation)
    return payload


def _location_name(name=None):
    """STAplus Location.name from the marker, or the plugin default."""
    location_name = (str(name) if name is not None else "").strip() or DEFAULT_LOCATION_NAME
    return location_name or "QGIS SCK marker"


def _geojson_point_coords(value):
    """[lon, lat] from a GeoJSON Point or Feature, else None."""
    if not isinstance(value, dict):
        return None
    if value.get("type") == "Point" and isinstance(value.get("coordinates"), list):
        return value.get("coordinates")
    geometry = value.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") == "Point":
        coords = geometry.get("coordinates")
        if isinstance(coords, list):
            return coords
    return None


def _coords_match(coords, lat, lon, eps=MARKER_COORD_EPS):
    """True when GeoJSON [lon, lat] matches the marker within eps degrees."""
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return False
    try:
        return abs(float(coords[0]) - float(lon)) <= eps and abs(float(coords[1]) - float(lat)) <= eps
    except (TypeError, ValueError):
        return False


def _iter_locations(thing):
    """Location entities expanded on a Thing, or an empty list."""
    locations = (thing or {}).get("Locations") or []
    if not isinstance(locations, list):
        return []
    return [item for item in locations if isinstance(item, dict)]


def _thing_has_location(thing, lat, lon, name=None):
    """True when any Location on the Thing has the marker name and WGS84 point."""
    expected = _location_name(name)
    for location in _iter_locations(thing):
        if (location.get("name") or "").strip() != expected:
            continue
        if _coords_match(_geojson_point_coords(location.get("location")), lat, lon):
            return True
    return False


def _party_location_matches(entity, lat, lon):
    """True when PartyLocation.position matches the marker WGS84 point."""
    return _coords_match(_geojson_point_coords((entity or {}).get("position")), lat, lon)


def _iot_id_from_self_link(url):
    """Parse Things(1) / Things('uuid') from a FROST Location header or selfLink."""
    text = str(url or "").rstrip("/").split("/")[-1]
    if "(" not in text or not text.endswith(")"):
        return None
    raw = text[text.index("(") + 1 : -1]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    return raw or None


def _entity_from_location_header(response):
    """FROST 201 may return an empty body plus a Location header with the new @iot.id."""
    href = (response.headers.get("Location") or response.headers.get("location") or "").strip()
    if not href:
        return None
    iot_id = _iot_id_from_self_link(href)
    entity = {"@iot.selfLink": href}
    if iot_id is not None:
        entity["@iot.id"] = iot_id
    return entity


def _odata_quote(value):
    """Quote a string for an OData filter (escape embedded quotes)."""
    return "'%s'" % str(value).replace("'", "''")


def _entity_path(collection, iot_id):
    """SensorThings path Things(1) or Things('uuid') from an @iot.id."""
    if isinstance(iot_id, bool):
        return "%s(%s)" % (collection, _odata_quote(iot_id))
    if isinstance(iot_id, int):
        return "%s(%s)" % (collection, iot_id)
    text = str(iot_id)
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return "%s(%s)" % (collection, text)
    return "%s(%s)" % (collection, _odata_quote(text))


def _format_http_error(response, href, token, payload=None):
    """Human-readable STAplus error including status, body, and a short token prefix on 401."""
    status = response.status_code
    body = (response.text or "").strip()
    www = (response.headers.get("WWW-Authenticate") or "").strip()
    parts = ["STAplus HTTP %s for %s" % (status, href)]
    if body:
        parts.append(body[:1500])
    else:
        parts.append("The server returned an empty error body.")
    if payload is not None:
        parts.append("Request: %s" % json.dumps(payload, default=str)[:1500])
    if www:
        parts.append(www[:400])
    if status == 401:
        parts.append("Access token %s" % authenix.token_prefix(token))
    return "\n".join(parts)
