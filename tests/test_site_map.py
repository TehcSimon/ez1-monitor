"""Tests for the optional location lookup (app/site_map.py). All coordinates and
names are made up; no test talks to the network."""
import json
import logging
import math

import httpx
import pytest

from app import site_map
from app.site_map import (
    OSM_MAP_URL, OVERPASS_URL, RADIUS_M, SiteState, build_surroundings, height_of,
    load_cache, parse_coordinates, parse_osm, resolve_once, save_cache, site_payload,
)

LAT, LON = 10.0, 20.0
ADDRESS = "Musterweg 1, 12345 Beispielstadt"


def at(east, north):
    """Lat/lon of a point given in metres from (LAT, LON)."""
    return (
        LAT + north / site_map.METERS_PER_DEG_LAT,
        LON + east / (math.cos(math.radians(LAT)) * site_map.METERS_PER_DEG_LON),
    )


def rectangle(x0, y0, x1, y1):
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [at(*c) for c in corners]


def house(x0, y0, x1, y1, **tags):
    return rectangle(x0, y0, x1, y1), {"building": "yes", **tags}


class TestParseCoordinates:
    @pytest.mark.parametrize("text, expected", [
        ("50.5,7.25", (50.5, 7.25)),
        (" -33.9 , 151.2 ", (-33.9, 151.2)),
        ("50,7", (50.0, 7.0)),
    ])
    def test_a_pair_is_taken_as_it_is(self, text, expected):
        assert parse_coordinates(text) == expected

    @pytest.mark.parametrize("text", [ADDRESS, "91,7", "50,181", "50.5", ""])
    def test_anything_else_needs_the_address_search(self, text):
        assert parse_coordinates(text) is None


class TestHeight:
    FOOTPRINT = [(0, 0), (20, 0), (20, 10), (0, 10)]

    @pytest.mark.parametrize("tags, expected", [
        ({"height": "12.5"}, 12.5),
        ({"height": "12 m"}, 12.0),
        ({"building:height": "7"}, 7.0),
        ({"building:levels": "3"}, 10.0),
        ({"building": "garage"}, 3),
        ({"building": "apartments"}, 11),
        ({"building": "yes"}, 8),
    ])
    def test_the_best_available_source_wins(self, tags, expected):
        assert height_of(tags, self.FOOTPRINT) == expected

    def test_a_small_unknown_building_is_a_shed(self):
        assert height_of({"building": "yes"}, [(0, 0), (4, 0), (4, 4), (0, 4)]) == 3

    @pytest.mark.parametrize("value", ["nan", "inf", "-3", "tall", "12;14"])
    def test_junk_falls_through_to_the_guess(self, value):
        assert height_of({"height": value, "building": "yes"}, self.FOOTPRINT) == 8


class TestParseOsm:
    def test_ways_get_their_points_and_tags(self):
        xml = """<osm>
          <node id="1" lat="1.0" lon="2.0"/><node id="2" lat="3.0" lon="4.0"/>
          <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="service"/></way>
        </osm>"""
        assert parse_osm(xml) == [([(1.0, 2.0), (3.0, 4.0)], {"highway": "service"})]

    def test_a_multipolygon_hands_its_tags_to_the_outer_way(self):
        xml = """<osm>
          <node id="1" lat="1" lon="1"/>
          <way id="10"><nd ref="1"/></way>
          <way id="11"><nd ref="1"/></way>
          <relation id="20">
            <member type="way" ref="10" role="outer"/>
            <member type="way" ref="11" role="inner"/>
            <tag k="type" v="multipolygon"/><tag k="building" v="yes"/>
          </relation>
        </osm>"""
        tags = [t for _, t in parse_osm(xml)]
        assert tags[0]["building"] == "yes"
        assert "building" not in tags[1]


class TestBuildSurroundings:
    def test_the_map_is_centred_on_the_house_around_the_point(self):
        result = build_surroundings([house(-5, -5, 15, 5)], LAT, LON)
        home = result["buildings"][0]
        assert home["home"] == 1
        xs, ys = [p[0] for p in home["f"]], [p[1] for p in home["f"]]
        assert (min(xs), max(xs)) == pytest.approx((-10, 10), abs=0.2)
        assert (min(ys), max(ys)) == pytest.approx((-5, 5), abs=0.2)
        assert result["lon"] > LON   # moved east with the house's centre

    def test_a_point_just_outside_finds_the_nearest_house(self):
        result = build_surroundings([house(3, -5, 13, 5), house(30, -5, 40, 5)], LAT, LON)
        assert [bool(b.get("home")) for b in result["buildings"]] == [True, False]

    def test_no_house_nearby_keeps_the_point_as_centre(self):
        result = build_surroundings([house(40, -5, 48, 5)], LAT, LON)
        assert not any(b.get("home") for b in result["buildings"])
        assert result["lat"] == LAT and result["lon"] == LON

    def test_buildings_are_cut_at_the_circle(self):
        result = build_surroundings([house(-5, -5, 5, 5), house(40, -10, 80, 10)], LAT, LON)
        for building in result["buildings"]:
            assert all(math.hypot(*p) <= RADIUS_M + 0.1 for p in building["f"])
        assert len(result["buildings"]) == 2

    def test_what_is_not_a_house_is_dropped(self):
        open_way = (rectangle(-5, -5, 5, 5)[:-1], {"building": "yes"})
        result = build_surroundings(
            [open_way, house(-5, -5, 5, 5, building="no"), house(20, 20, 21, 21)], LAT, LON,
        )
        assert result["buildings"] == []

    def test_streets_keep_their_width_and_paths_are_dropped(self):
        street = ([at(-80, 20), at(80, 20)], {"highway": "residential", "name": "Musterweg"})
        path = ([at(-80, 10), at(80, 10)], {"highway": "footway"})
        tunnel = ([at(-80, 0), at(80, 0)], {"highway": "primary", "tunnel": "yes"})
        result = build_surroundings([street, path, tunnel], LAT, LON)
        assert [r["w"] for r in result["roads"]] == [7]

    def test_a_street_far_away_is_not_kept(self):
        street = ([at(-80, 100), at(80, 100)], {"highway": "residential"})
        assert build_surroundings([street], LAT, LON)["roads"] == []

    def test_the_name_follows_the_longest_part_inside(self):
        street = ([at(-80, 20), at(80, 20)], {"highway": "residential", "name": "Musterweg"})
        short = ([at(-5, 47), at(5, 47)], {"highway": "service", "name": "Kurz"})
        labels = build_surroundings([street, short], LAT, LON)["labels"]
        assert [label["t"] for label in labels] == ["Musterweg"]
        assert all(math.hypot(*p) <= RADIUS_M for p in labels[0]["p"])


OSM_ANSWER = """<osm>
  <node id="1" lat="{a[0]}" lon="{a[1]}"/><node id="2" lat="{b[0]}" lon="{b[1]}"/>
  <node id="3" lat="{c[0]}" lon="{c[1]}"/><node id="4" lat="{d[0]}" lon="{d[1]}"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/></way>
</osm>""".format(a=at(-5, -5), b=at(5, -5), c=at(5, 5), d=at(-5, 5))


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def happy(request):
    if "nominatim" in str(request.url):
        return httpx.Response(200, json=[{"lat": str(LAT), "lon": str(LON)}])
    return httpx.Response(200, text=OSM_ANSWER)


@pytest.fixture
def state(tmp_path):
    return SiteState(address=ADDRESS, cache_path=tmp_path / site_map.CACHE_FILE)


class TestResolve:
    async def test_the_address_leads_to_a_map(self, state):
        async with client_for(happy) as client:
            assert await resolve_once(state, client)
        assert state.complete
        assert len(state.surroundings["buildings"]) == 1

    async def test_every_request_identifies_the_app(self, state):
        agents = []
        def handler(request):
            agents.append(request.headers["user-agent"])
            return happy(request)
        async with client_for(handler) as client:
            await resolve_once(state, client)
        assert len(agents) == 2
        assert all(a.startswith("ez1-monitor/") for a in agents)

    async def test_coordinates_skip_the_address_search(self, tmp_path):
        state = SiteState(address=f"{LAT},{LON}", cache_path=tmp_path / "c.json")
        seen = []
        def handler(request):
            seen.append(request.url.host)
            return happy(request)
        async with client_for(handler) as client:
            assert await resolve_once(state, client)
        assert seen == ["api.openstreetmap.org"]

    async def test_overpass_steps_in_when_the_osm_api_fails(self, state):
        seen = []
        def handler(request):
            seen.append(str(request.url).split("?")[0])
            if str(request.url).startswith(OSM_MAP_URL):
                return httpx.Response(509)
            return happy(request)
        async with client_for(handler) as client:
            assert await resolve_once(state, client)
        assert seen[-2:] == [OSM_MAP_URL, OVERPASS_URL]

    async def test_garbage_from_one_source_tries_the_next(self, state):
        def handler(request):
            if str(request.url).startswith(OSM_MAP_URL):
                return httpx.Response(200, text="<html>busy")
            return happy(request)
        async with client_for(handler) as client:
            assert await resolve_once(state, client)

    async def test_without_any_map_the_location_still_counts(self, state):
        def handler(request):
            if "nominatim" in str(request.url):
                return happy(request)
            return httpx.Response(502)
        async with client_for(handler) as client:
            assert not await resolve_once(state, client)
        assert state.lat == LAT and state.surroundings is None
        payload = site_payload(state)
        assert payload["ready"] and not payload["has_map"]
        assert payload["buildings"] == []

    async def test_the_next_attempt_only_fetches_what_is_missing(self, state):
        state.lat, state.lon = LAT, LON
        hosts = []
        def handler(request):
            hosts.append(request.url.host)
            return happy(request)
        async with client_for(handler) as client:
            assert await resolve_once(state, client)
        assert "nominatim.openstreetmap.org" not in hosts

    @pytest.mark.parametrize("answer", [
        httpx.Response(200, json=[]), httpx.Response(200, text="nope"), httpx.Response(503),
    ])
    async def test_a_failed_search_leaves_nothing_behind(self, state, answer):
        async with client_for(lambda request: answer) as client:
            assert not await resolve_once(state, client)
        assert state.lat is None and not state.cache_path.exists()

    async def test_a_connection_error_is_only_a_failed_attempt(self, state):
        def handler(request):
            raise httpx.ConnectError("no route")
        async with client_for(handler) as client:
            assert not await resolve_once(state, client)

    async def test_the_address_is_never_logged(self, state, caplog):
        caplog.set_level(logging.DEBUG)
        def handler(request):
            if "nominatim" in str(request.url):
                return httpx.Response(200, json=[])
            return happy(request)
        async with client_for(handler) as client:
            await resolve_once(state, client)
        assert "SITE_ADDRESS" in caplog.text
        assert "Musterweg" not in caplog.text


class TestCache:
    async def _resolved(self, state):
        async with client_for(happy) as client:
            await resolve_once(state, client)

    async def test_a_restart_needs_no_request(self, state):
        await self._resolved(state)
        fresh = SiteState(address=ADDRESS, cache_path=state.cache_path)
        load_cache(fresh)
        assert fresh.complete
        assert fresh.surroundings == state.surroundings

    async def test_the_address_is_not_written_to_the_cache(self, state):
        await self._resolved(state)
        assert "Musterweg" not in state.cache_path.read_text(encoding="utf-8")

    async def test_another_address_starts_over(self, state):
        await self._resolved(state)
        fresh = SiteState(address="Andere Straße 2", cache_path=state.cache_path)
        load_cache(fresh)
        assert fresh.lat is None and fresh.surroundings is None

    async def test_an_old_cache_format_starts_over(self, state):
        await self._resolved(state)
        cached = json.loads(state.cache_path.read_text(encoding="utf-8"))
        cached["version"] = site_map.CACHE_VERSION + 1
        state.cache_path.write_text(json.dumps(cached), encoding="utf-8")
        fresh = SiteState(address=ADDRESS, cache_path=state.cache_path)
        load_cache(fresh)
        assert fresh.lat is None

    @pytest.mark.parametrize("content", ["", "{broken"])
    def test_a_damaged_cache_is_ignored(self, state, content):
        state.cache_path.write_text(content, encoding="utf-8")
        load_cache(state)
        assert state.lat is None

    def test_an_unwritable_cache_is_not_fatal(self, tmp_path):
        state = SiteState(address=ADDRESS, cache_path=tmp_path / "missing" / "c.json", lat=LAT, lon=LON)
        save_cache(state)


class TestPayload:
    def test_off_without_an_address(self):
        assert site_payload(SiteState()) == {"configured": False, "ready": False}

    def test_waiting_for_the_lookup(self):
        assert site_payload(SiteState(address=ADDRESS)) == {"configured": True, "ready": False}

    def _with_home(self, floors, height=8.0):
        around = {
            "lat": LAT, "lon": LON, "roads": [], "labels": [],
            "buildings": [{"f": [], "h": height, "home": 1}, {"f": [], "h": 5.0}],
        }
        return SiteState(address=ADDRESS, floors=floors, lat=LAT, lon=LON, surroundings=around)

    def test_the_floors_set_the_own_height_only(self):
        heights = [b["h"] for b in site_payload(self._with_home(3))["buildings"]]
        assert heights == [10, 5.0]

    def test_without_floors_the_estimate_stays(self):
        heights = [b["h"] for b in site_payload(self._with_home(0))["buildings"]]
        assert heights == [8.0, 5.0]

    def test_the_floors_do_not_change_the_cached_data(self):
        state = self._with_home(3)
        site_payload(state)
        assert state.surroundings["buildings"][0]["h"] == 8.0
