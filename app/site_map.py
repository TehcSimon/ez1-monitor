"""The plant's location and its surroundings, for the sun scene.

Optional, like the Electricity Maps integration in co2.py: without SITE_ADDRESS
nothing here talks to the outside world. With it, the server looks the address
up once (Nominatim), cuts a 50 m circle out of OpenStreetMap and keeps the
result next to the database, so a restart costs no request and the browser
never contacts anyone but this container. A changed address fetches once more.

The official OSM API is asked first and Overpass second: both answer in the
same XML, and Overpass in particular is often busy (502s and timeouts).

The address is private, so it is neither logged nor written to the cache
(which only holds a hash, to notice a change). httpx logs every request URL at
INFO level, query string included, hence the raised level below.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from . import __version__

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_MAP_URL = "https://api.openstreetmap.org/api/0.6/map"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Required by the usage policies of all three services.
USER_AGENT = f"ez1-monitor/{__version__} (+https://github.com/TehcSimon/ez1-monitor)"

CACHE_FILE = "site-map.json"
# Bump when the cut-out changes shape, so existing caches are fetched anew.
CACHE_VERSION = 1

REQUEST_TIMEOUT_S = 30
# A failed lookup is retried after 5 min, 10, 20 ... up to once a day: a typo
# in the address must not keep hitting a service run by volunteers.
RETRY_BASE_S = 300
RETRY_MAX_S = 86400

RADIUS_M = 50
# Enough around the point for the circle, which is recentred on the house.
FETCH_MARGIN_M = 130
# Streets slightly outside the circle still run to its edge.
ROAD_MARGIN_M = 12
CLIP_SIDES = 96
MIN_FOOTPRINT_M2 = 5
# An address point often sits on the outline or the entrance, not inside.
HOME_SEARCH_M = 25
MIN_LABEL_M = 24
METERS_PER_DEG_LAT = 110540
METERS_PER_DEG_LON = 111320

FLOOR_HEIGHT_M = 3
ROOF_ALLOWANCE_M = 1
ROAD_WIDTHS_M = {
    "primary": 11, "secondary": 10, "tertiary": 9, "unclassified": 7,
    "residential": 7, "living_street": 5.5, "pedestrian": 5, "service": 3.5,
}
DEFAULT_HEIGHTS_M = {
    "garage": 3, "garages": 3, "shed": 3, "carport": 2.5,
    "house": 8, "detached": 8, "apartments": 11, "residential": 10,
}
SMALL_BUILDING_M2 = 30
SMALL_BUILDING_HEIGHT_M = 3
BUILDING_HEIGHT_M = 8


@dataclass
class SiteState:
    """Runtime state, one instance per process."""
    address: str = ""             # SITE_ADDRESS (empty = feature off)
    floors: float = 0.0           # SITE_FLOORS (0 = what OSM says)
    cache_path: Optional[Path] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    surroundings: Optional[dict] = None   # buildings, roads, labels

    @property
    def complete(self) -> bool:
        return self.lat is not None and self.surroundings is not None


# --- Geometry ------------------------------------------------------------

def _area(poly: list) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1])) / 2


def _centroid(poly: list) -> tuple:
    return sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)


def _contains_origin(poly: list) -> bool:
    inside = False
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if (a[1] > 0) != (b[1] > 0) and 0 < (b[0] - a[0]) * (0 - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _distance_to_origin(a: tuple, b: tuple) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, -(a[0] * dx + a[1] * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(a[0] + dx * t, a[1] + dy * t)


def _clip_to_circle(poly: list, radius: float) -> list:
    """Sutherland-Hodgman against a regular polygon around the origin."""
    apothem = radius * math.cos(math.pi / CLIP_SIDES)
    out = poly
    for k in range(CLIP_SIDES):
        nx, ny = math.cos(2 * math.pi * k / CLIP_SIDES), math.sin(2 * math.pi * k / CLIP_SIDES)
        kept = []
        for a, b in zip(out, out[1:] + out[:1]):
            side_a = a[0] * nx + a[1] * ny - apothem
            side_b = b[0] * nx + b[1] * ny - apothem
            if side_a <= 0:
                kept.append(a)
            if (side_a <= 0) != (side_b <= 0):
                t = side_a / (side_a - side_b)
                kept.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out = kept
        if not out:
            return []
    return out


def _inside_chunks(points: list, radius: float) -> list:
    """The parts of a polyline that lie inside the circle."""
    chunks, current = [], []
    for a, b in zip(points, points[1:]):
        steps = max(1, int(math.dist(a, b) / 1.5))
        for i in range(steps + 1):
            q = (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            if math.hypot(*q) <= radius:
                current.append(q)
            elif current:
                chunks.append(current)
                current = []
    if current:
        chunks.append(current)
    return chunks


def _length(points: list) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _rounded(points: list) -> list:
    return [[round(x, 1), round(y, 1)] for x, y in points]


# --- OSM data ------------------------------------------------------------

def height_of(tags: dict, footprint: list) -> float:
    """Height in metres: tagged, from the storeys, or a guess from the type.

    Most buildings have neither height nor levels in OSM. The range check also
    keeps a stray "nan" or "inf" out of the JSON.
    """
    for key in ("height", "building:height"):
        try:
            value = float(tags[key].replace("m", "").strip())
        except (KeyError, ValueError):
            continue
        if 0 < value < 1000:
            return value
    try:
        levels = float(tags["building:levels"])
        if 0 < levels < 300:
            return levels * FLOOR_HEIGHT_M + ROOF_ALLOWANCE_M
    except (KeyError, ValueError):
        pass
    if tags.get("building") in DEFAULT_HEIGHTS_M:
        return DEFAULT_HEIGHTS_M[tags["building"]]
    return SMALL_BUILDING_HEIGHT_M if abs(_area(footprint)) < SMALL_BUILDING_M2 else BUILDING_HEIGHT_M


def parse_osm(xml_text: str) -> list:
    """The ways of an OSM XML document as (lat/lon list, tags) pairs.

    A building mapped as a multipolygon keeps its tags on the relation, so its
    closed outer ways inherit them. Courtyards (inner rings) stay filled, which
    the reduced map can live with.
    """
    root = ET.fromstring(xml_text)
    nodes = {
        n.get("id"): (float(n.get("lat")), float(n.get("lon")))
        for n in root.iter("node") if n.get("lat") and n.get("lon")
    }
    ways = {}
    for way in root.iter("way"):
        points = [nodes[nd.get("ref")] for nd in way.iter("nd") if nd.get("ref") in nodes]
        ways[way.get("id")] = (points, {t.get("k"): t.get("v") for t in way.iter("tag")})
    for relation in root.iter("relation"):
        tags = {t.get("k"): t.get("v") for t in relation.iter("tag")}
        if tags.get("type") != "multipolygon" or "building" not in tags:
            continue
        for member in relation.iter("member"):
            if member.get("type") == "way" and member.get("role") == "outer" and member.get("ref") in ways:
                points, own = ways[member.get("ref")]
                ways[member.get("ref")] = (points, {**own, **tags})
    return list(ways.values())


def build_surroundings(features: list, lat: float, lon: float) -> dict:
    """Cut buildings, streets and street names out of the parsed OSM ways.

    Coordinates in the result are metres east and north of the own building's
    centre, or of the point itself if no building is near it.
    """
    cos_lat = math.cos(math.radians(lat))

    def to_xy(point):
        return (point[1] - lon) * cos_lat * METERS_PER_DEG_LON, (point[0] - lat) * METERS_PER_DEG_LAT

    footprints = []
    for points, tags in features:
        if tags.get("building", "no") == "no" or len(points) < 4 or points[0] != points[-1]:
            continue
        footprints.append(([to_xy(p) for p in points[:-1]], tags))

    around = [f for f in footprints if _contains_origin(f[0])]
    home = min(around, key=lambda f: abs(_area(f[0])), default=None)
    if home is None and footprints:
        def distance(footprint):
            poly = footprint[0]
            return min(_distance_to_origin(a, b) for a, b in zip(poly, poly[1:] + poly[:1]))
        nearest = min(footprints, key=distance)
        home = nearest if distance(nearest) <= HOME_SEARCH_M else None

    cx, cy = _centroid(home[0]) if home else (0.0, 0.0)

    def shift(point):
        return point[0] - cx, point[1] - cy

    buildings = []
    for poly, tags in footprints:
        cut = _clip_to_circle([shift(p) for p in poly], RADIUS_M)
        if len(cut) < 3 or abs(_area(cut)) < MIN_FOOTPRINT_M2:
            continue
        item = {"f": _rounded(cut), "h": round(height_of(tags, poly), 1)}
        if home is not None and poly is home[0]:
            item["home"] = 1
        buildings.append(item)

    roads, named = [], {}
    for points, tags in features:
        width = ROAD_WIDTHS_M.get(tags.get("highway"))
        if width is None or tags.get("tunnel", "no") != "no" or len(points) < 2:
            continue
        line = [shift(to_xy(p)) for p in points]
        current = []
        for a, b in zip(line, line[1:]):
            if _distance_to_origin(a, b) < RADIUS_M + ROAD_MARGIN_M:
                current = current or [a]
                current.append(b)
            elif current:
                roads.append({"w": width, "p": _rounded(current)})
                current = []
        if current:
            roads.append({"w": width, "p": _rounded(current)})
        if tags.get("name"):
            named.setdefault(tags["name"], []).extend(_inside_chunks(line, RADIUS_M * 0.95))

    labels = []
    for name, chunks in named.items():
        best = max(chunks, key=_length, default=None)
        if best and _length(best) >= MIN_LABEL_M:
            thinned = best[::3] if best[::3][-1] == best[-1] else [*best[::3], best[-1]]
            labels.append({"t": name, "p": _rounded(thinned)})

    return {
        "lat": round(lat + cy / METERS_PER_DEG_LAT, 5),
        "lon": round(lon + cx / (cos_lat * METERS_PER_DEG_LON), 5),
        "buildings": buildings, "roads": roads, "labels": labels,
    }


# --- Lookups -------------------------------------------------------------

def parse_coordinates(text: str) -> Optional[tuple]:
    """(lat, lon) if the text is a "lat,lon" pair, which skips the address search."""
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if not match:
        return None
    lat, lon = float(match[1]), float(match[2])
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


async def _get(client: httpx.AsyncClient, what: str, url: str, params: dict) -> Optional[httpx.Response]:
    """One request. Returns None on any failure; never logs the URL (the
    query carries the address)."""
    try:
        response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as e:
        logger.warning(f"Site: {what} failed ({type(e).__name__})")
        return None
    if response.status_code != 200:
        logger.warning(f"Site: {what} answered HTTP {response.status_code}")
        return None
    return response


async def geocode(client: httpx.AsyncClient, address: str) -> Optional[tuple]:
    response = await _get(client, "address search", NOMINATIM_URL,
                          {"q": address, "format": "jsonv2", "limit": 1})
    if response is None:
        return None
    try:
        hit = response.json()[0]
        return float(hit["lat"]), float(hit["lon"])
    except (ValueError, IndexError, KeyError, TypeError):
        logger.warning("Site: SITE_ADDRESS was not found, check its spelling")
        return None


async def fetch_map(client: httpx.AsyncClient, lat: float, lon: float) -> Optional[list]:
    """The OSM ways around a point, from the first source that delivers."""
    dlat = FETCH_MARGIN_M / METERS_PER_DEG_LAT
    dlon = FETCH_MARGIN_M / (METERS_PER_DEG_LON * max(math.cos(math.radians(lat)), 0.01))
    south, west, north, east = lat - dlat, lon - dlon, lat + dlat, lon + dlon
    box = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    overpass = (
        f'[out:xml][timeout:25];(way["building"]({box});relation["building"]({box});'
        f'way["highway"]({box}););(._;>;);out body;'
    )
    sources = (
        ("OpenStreetMap API", OSM_MAP_URL, {"bbox": f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"}),
        ("Overpass API", OVERPASS_URL, {"data": overpass}),
    )
    for name, url, params in sources:
        response = await _get(client, name, url, params)
        if response is None:
            continue
        try:
            return parse_osm(response.text)
        except (ET.ParseError, ValueError):
            logger.warning(f"Site: {name} sent an unreadable answer")
    return None


# --- Cache and background task -------------------------------------------

def _fingerprint(address: str) -> str:
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


def load_cache(state: SiteState) -> None:
    """Restore a previous lookup, if it was made for the same address."""
    if state.cache_path is None:
        return
    try:
        cached = json.loads(state.cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if cached.get("version") != CACHE_VERSION or cached.get("address") != _fingerprint(state.address):
        return
    state.lat, state.lon = cached.get("lat"), cached.get("lon")
    state.surroundings = cached.get("surroundings")


def save_cache(state: SiteState) -> None:
    if state.cache_path is None:
        return
    payload = {
        "version": CACHE_VERSION, "address": _fingerprint(state.address),
        "lat": state.lat, "lon": state.lon, "surroundings": state.surroundings,
    }
    temporary = state.cache_path.with_name(state.cache_path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, state.cache_path)
    except OSError as e:
        logger.warning(f"Site: could not write the cache ({type(e).__name__})")


async def resolve_once(state: SiteState, client: httpx.AsyncClient) -> bool:
    """One attempt at whatever is still missing. Returns True when complete.

    The location is kept as soon as it is known: the sun path only needs that,
    so it works while the map is still being fetched.
    """
    if state.lat is None:
        point = parse_coordinates(state.address) or await geocode(client, state.address)
        if point is None:
            return False
        state.lat, state.lon = point
        save_cache(state)
    if state.surroundings is None:
        features = await fetch_map(client, state.lat, state.lon)
        if features is None:
            return False
        state.surroundings = build_surroundings(features, state.lat, state.lon)
        save_cache(state)
        logger.info(
            f"Site: surroundings fetched ({len(state.surroundings['buildings'])} buildings, "
            f"{len(state.surroundings['roads'])} streets)"
        )
    return True


async def resolve_loop(state: SiteState) -> None:
    """Background task: resolves the location, then ends."""
    if not state.address:
        logger.info("Site: no SITE_ADDRESS configured, sun path and map are off")
        return
    load_cache(state)
    failures = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        while not state.complete:
            try:
                if await resolve_once(state, client):
                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Site: lookup error ({type(e).__name__})")
            failures += 1
            await asyncio.sleep(min(RETRY_MAX_S, RETRY_BASE_S * 2 ** min(failures - 1, 9)))


def site_payload(state: SiteState) -> dict:
    """What the browser gets. SITE_FLOORS is applied here, not when fetching,
    so changing it takes effect without another download."""
    if not state.address:
        return {"configured": False, "ready": False}
    if state.lat is None:
        return {"configured": True, "ready": False}
    around = state.surroundings
    buildings = (around or {}).get("buildings", [])
    if state.floors > 0:
        own_height = state.floors * FLOOR_HEIGHT_M + ROOF_ALLOWANCE_M
        buildings = [{**b, "h": own_height} if b.get("home") else b for b in buildings]
    return {
        "configured": True, "ready": True, "has_map": around is not None,
        "lat": around["lat"] if around else state.lat,
        "lon": around["lon"] if around else state.lon,
        "radius": RADIUS_M,
        "buildings": buildings,
        "roads": (around or {}).get("roads", []),
        "labels": (around or {}).get("labels", []),
    }
