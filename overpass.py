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

"""OpenStreetMap Overpass lookup of named public places near a WGS84 point."""

import json
import logging
import math

import requests

from .config import (
    NEARBY_PLACES_LIMIT,
    NEARBY_PLACES_RADIUS_M,
    OVERPASS_ENDPOINTS,
    OVERPASS_USER_AGENT,
)

_logger = logging.getLogger("sck.overpass")

_OVERPASS_LEISURE = "park|playground|garden|nature_reserve"
_OVERPASS_NATURAL = "water|wood"
_OVERPASS_AMENITY = (
    "school|university|college|library|hospital|clinic|townhall|"
    "community_centre|theatre|cinema|place_of_worship|arts_centre"
)
_OVERPASS_TOURISM = "museum|attraction|gallery"
_OVERPASS_BUILDING = "public|civic|school|university|hospital|church|cathedral"


class OverpassError(RuntimeError):
    """Raised when every Overpass endpoint fails."""


def _haversine_m(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))


def _overpass_coords(geom):
    coords = []
    for pt in geom or []:
        try:
            coords.append([float(pt["lon"]), float(pt["lat"])])
        except (KeyError, TypeError, ValueError):
            continue
    return coords


def _close_ring(coords):
    if len(coords) < 3:
        return coords
    if coords[0] != coords[-1]:
        return coords + [coords[0]]
    return coords


def _way_geometry(geom):
    coords = _overpass_coords(geom)
    if len(coords) < 2:
        return None
    closed = len(coords) >= 4 and coords[0] == coords[-1]
    if not closed and len(coords) >= 3:
        first, last = coords[0], coords[-1]
        if abs(first[0] - last[0]) < 1e-7 and abs(first[1] - last[1]) < 1e-7:
            coords = _close_ring(coords)
            closed = True
    if closed and len(coords) >= 4:
        return {"type": "Polygon", "coordinates": [_close_ring(coords)]}
    return {"type": "LineString", "coordinates": coords}


def _relation_geometry(el):
    outers = []
    inners = []
    for member in el.get("members") or []:
        if member.get("type") != "way":
            continue
        coords = _overpass_coords(member.get("geometry"))
        if len(coords) < 3:
            continue
        ring = _close_ring(coords)
        if len(ring) < 4:
            continue
        role = (member.get("role") or "outer").lower()
        if role == "inner":
            inners.append(ring)
        else:
            outers.append(ring)
    if not outers:
        return None
    if len(outers) == 1:
        return {"type": "Polygon", "coordinates": [outers[0]] + inners}
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in outers]}


def _element_geometry(el):
    etype = el.get("type")
    if etype == "node":
        try:
            return {"type": "Point", "coordinates": [float(el["lon"]), float(el["lat"])]}
        except (KeyError, TypeError, ValueError):
            return None
    if etype == "way":
        return _way_geometry(el.get("geometry"))
    if etype == "relation":
        return _relation_geometry(el)
    return None


def _geom_centroid_latlon(geom):
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point" and coords and len(coords) >= 2:
        return float(coords[1]), float(coords[0])
    if gtype == "LineString":
        pts = coords or []
    elif gtype == "Polygon":
        pts = (coords or [[]])[0]
    elif gtype == "MultiPolygon":
        pts = ((coords or [[[]]])[0] or [[]])[0]
    else:
        return None
    if not pts:
        return None
    lon = sum(float(p[0]) for p in pts) / len(pts)
    lat = sum(float(p[1]) for p in pts) / len(pts)
    return lat, lon


def _place_kind(tags):
    for key in ("leisure", "amenity", "tourism", "building", "natural", "landuse"):
        val = tags.get(key)
        if val:
            return str(val)
    return "place"


def _overpass_query(lat, lon, radius_m):
    around = "(around:%d,%.7f,%.7f)" % (int(radius_m), float(lat), float(lon))
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        "  nwr%s[name][leisure~\"^(%s)$\"];\n"
        "  nwr%s[name][landuse=recreation_ground];\n"
        "  nwr%s[name][natural~\"^(%s)$\"];\n"
        "  nwr%s[name][amenity~\"^(%s)$\"];\n"
        "  nwr%s[name][tourism~\"^(%s)$\"];\n"
        "  nwr%s[name][building~\"^(%s)$\"];\n"
        ");\n"
        "out geom;"
    ) % (
        around, _OVERPASS_LEISURE,
        around,
        around, _OVERPASS_NATURAL,
        around, _OVERPASS_AMENITY,
        around, _OVERPASS_TOURISM,
        around, _OVERPASS_BUILDING,
    )


def _overpass_post(url, query, timeout=30.0):
    return requests.post(
        url,
        data={"data": query},
        headers={
            "User-Agent": OVERPASS_USER_AGENT,
            "Accept": "application/json",
        },
        timeout=timeout,
        proxies={},
    )


def lookup_nearby_public_places(lat, lon, radius_m=NEARBY_PLACES_RADIUS_M):
    """Return a GeoJSON FeatureCollection of named public OSM places near lat/lon."""
    query = _overpass_query(lat, lon, radius_m)
    last_error = None
    data = None
    for index, url in enumerate(OVERPASS_ENDPOINTS):
        _logger.info("Overpass lookup %s around %.6f, %.6f r=%sm", url, lat, lon, radius_m)
        try:
            resp = _overpass_post(url, query)
            if resp.status_code in (429, 504) and index == 0:
                last_error = requests.HTTPError(
                    "%s %s" % (resp.status_code, url), response=resp
                )
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError, json.JSONDecodeError) as err:
            last_error = err
            if index == 0:
                continue
            break
    if data is None:
        if last_error is not None:
            raise OverpassError(str(last_error)) from last_error
        raise OverpassError("Overpass lookup failed")

    features = []
    seen = set()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        etype = el.get("type")
        eid = el.get("id")
        if etype not in ("node", "way", "relation") or eid is None:
            continue
        osm_id = "%s/%s" % (etype, eid)
        if osm_id in seen:
            continue
        geom = _element_geometry(el)
        if not geom:
            continue
        center = _geom_centroid_latlon(geom)
        if center is None:
            continue
        seen.add(osm_id)
        distance_m = round(_haversine_m(lat, lon, center[0], center[1]))
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "osm_id": osm_id,
                "name": name,
                "kind": _place_kind(tags),
                "distance_m": distance_m,
            },
        })
    features.sort(key=lambda item: item["properties"]["distance_m"])
    return {
        "type": "FeatureCollection",
        "features": features[:NEARBY_PLACES_LIMIT],
    }
