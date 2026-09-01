"""Endpoint contract tests with all upstreams (game API, OSRM, Vroom) mocked."""
import json
import time

import httpx
import pytest

import main
from conftest import SAMPLE_PHONES, mock_phones_ok


def _host(url: str) -> str:
    return httpx.URL(url).host


OSRM_HOSTS  = {profile: _host(url) for profile, url in main.OSRM_URLS.items()}
VROOM_HOST  = _host(main.VROOM_URL)
UNREACH     = main.UNREACHABLE


def _matrix(n: int, value: int = 100) -> list[list[int]]:
    return [[0 if i == j else value for j in range(n)] for i in range(n)]


def mock_osrm_table(router, profile: str, durations, distances=None):
    return router.get(host=OSRM_HOSTS[profile], path__regex=r"/table/.*").mock(
        return_value=httpx.Response(200, json={
            "code": "Ok",
            "durations": durations,
            "distances": distances or durations,
        })
    )


def mock_osrm_routes(router, profile: str):
    leg = {
        "geometry": {"type": "LineString", "coordinates": [[138.60, -34.92], [138.61, -34.93]]},
        "distance": 1000.0,
        "duration": 120.0,
        "legs": [{"steps": [{"maneuver": {"type": "turn"}, "name": "Main St",
                             "distance": 500, "duration": 60}]}],
    }
    return router.get(host=OSRM_HOSTS[profile], path__regex=r"/route/.*").mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": [leg]})
    )


def mock_vroom_rejects_empty(router):
    """Mimic vroom-express refusing a problem with no jobs (HTTP 400).

    This is what actually produced the reported HTTP 500: the backend filtered every
    selected phone out of the jobs list, posted an empty problem anyway, and let the
    resulting HTTPStatusError escape.
    """
    return router.post(host=VROOM_HOST).mock(
        return_value=httpx.Response(400, json={"code": 1, "error": "Invalid jobs."})
    )


def mock_vroom(router, job_order, duration=240, service=0):
    steps = [{"type": "start"}] + [{"type": "job", "id": i} for i in job_order] + [{"type": "end"}]
    return router.post(host=VROOM_HOST).mock(
        return_value=httpx.Response(200, json={
            "code": 0,
            "routes": [{"steps": steps, "duration": duration, "service": service}],
        })
    )


# ── /api/phones ─────────────────────────────────────────────────────

async def test_phones_shaping(client, mock_api):
    mock_phones_ok(mock_api)

    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is False
    phones = {p["id"]: p for p in body["phones"]}
    # Inactive phone 5 filtered out
    assert set(phones) == {1, 2, 3, 4, 6}
    assert phones[1]["status"] == "uncaptured"
    assert phones[2]["status"] == "mine"
    assert phones[3]["status"] == "cellmate"
    assert phones[4]["status"] == "hostile"
    assert phones[6]["status"] == "uncaptured"
    # holder_name resolved from the players map (escaping is the frontend's job)
    assert phones[4]["holder_name"] == "<b>Rival</b>"
    assert phones[1]["holder_name"] is None


async def test_phones_username_case_insensitive(client, mock_api):
    mock_phones_ok(mock_api)

    r = await client.get("/api/phones", params={"username": "ghostscout"})

    phones = {p["id"]: p for p in r.json()["phones"]}
    assert phones[2]["status"] == "mine"


# ── /api/past-captures ──────────────────────────────────────────────

async def test_past_captures_happy_path(client, mock_api):
    mock_phones_ok(mock_api)
    captures = mock_api.get(f"{main.PAYPHONE_API_BASE}/player/7/past-captures").mock(
        return_value=httpx.Response(200, json={"payphoneIds": [10, 2, "11"]})
    )

    r = await client.get("/api/past-captures", params={"username": "GhostScout"})

    assert r.status_code == 200
    assert r.json()["captured"] == [2, 10, 11]
    assert captures.call_count == 1

    # Cached per-user: a second request stays off the game API
    await client.get("/api/past-captures", params={"username": "GhostScout"})
    assert captures.call_count == 1


async def test_past_captures_separate_users(client, mock_api):
    mock_phones_ok(mock_api)
    mine = mock_api.get(f"{main.PAYPHONE_API_BASE}/player/7/past-captures").mock(
        return_value=httpx.Response(200, json={"payphoneIds": [1]})
    )
    mates = mock_api.get(f"{main.PAYPHONE_API_BASE}/player/8/past-captures").mock(
        return_value=httpx.Response(200, json={"payphoneIds": [2]})
    )

    r1 = await client.get("/api/past-captures", params={"username": "GhostScout"})
    r2 = await client.get("/api/past-captures", params={"username": "Mate"})

    assert r1.json()["captured"] == [1]
    assert r2.json()["captured"] == [2]
    assert mine.call_count == 1 and mates.call_count == 1


async def test_past_captures_unknown_user_is_empty(client, mock_api):
    mock_phones_ok(mock_api)

    r = await client.get("/api/past-captures", params={"username": "NoSuchPlayer"})

    assert r.status_code == 200
    assert r.json()["captured"] == []


async def test_past_captures_stale_fallback(client, mock_api):
    mock_phones_ok(mock_api)
    captures = mock_api.get(f"{main.PAYPHONE_API_BASE}/player/7/past-captures").mock(
        return_value=httpx.Response(200, json={"payphoneIds": [10]})
    )
    await client.get("/api/past-captures", params={"username": "GhostScout"})

    # Entry expires, then the captures endpoint starts failing
    ids, _ = main._CAPTURES_CACHE["ghostscout"]
    main._CAPTURES_CACHE["ghostscout"] = (ids, time.monotonic() - main._CAPTURES_CACHE_TTL - 1)
    captures.mock(return_value=httpx.Response(500))

    r = await client.get("/api/past-captures", params={"username": "GhostScout"})

    assert r.status_code == 200
    assert r.json()["captured"] == [10]


# ── /api/route ──────────────────────────────────────────────────────

ROUTE_START_END = {
    "start": {"lat": -34.91, "lon": 138.59},
    "end":   {"lat": -34.98, "lon": 138.66},
}


async def test_route_unknown_profile(client):
    r = await client.post("/api/route", json={
        "phone_ids": [1], "profile": "helicopter", **ROUTE_START_END,
    })
    assert r.status_code == 400
    assert "Unknown profile" in r.json()["detail"]


async def test_route_empty_ids(client):
    r = await client.post("/api/route", json={
        "phone_ids": [], "profile": "car", **ROUTE_START_END,
    })
    assert r.status_code == 400


async def test_route_too_many_phones(client, mock_api):
    mock_phones_ok(mock_api)
    r = await client.post("/api/route", json={
        "phone_ids": list(range(1, main.MAX_TABLE_COORDS + 1)),  # 200 jobs + 2 endpoints
        "profile": "car", **ROUTE_START_END,
    })
    assert r.status_code == 400
    assert "Too many phones" in r.json()["detail"]


async def test_route_happy_path(client, mock_api):
    mock_phones_ok(mock_api)
    mock_osrm_table(mock_api, "car", _matrix(4))
    mock_osrm_routes(mock_api, "car")
    mock_vroom(mock_api, job_order=[4, 1])

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4], "profile": "car", **ROUTE_START_END,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ordered_ids"] == [4, 1]
    # start→4, 4→1, 1→end
    assert len(body["path"]["features"]) == 3
    assert len(body["legs"]) == 3
    assert body["total_distance_m"] == pytest.approx(3000.0)
    assert body["total_duration_s"] == pytest.approx(360.0)
    assert body["legs"][0]["steps"][0]["name"] == "Main St"
    # Leg endpoints line up with the visit order
    assert [l["to_id"] for l in body["legs"]] == [4, 1, None]


async def test_route_dedupes_phone_ids(client, mock_api):
    mock_phones_ok(mock_api)
    mock_osrm_table(mock_api, "car", _matrix(4))
    mock_osrm_routes(mock_api, "car")
    vroom = mock_vroom(mock_api, job_order=[1, 4])

    r = await client.post("/api/route", json={
        "phone_ids": [1, 1, 4, 4, 1], "profile": "car", **ROUTE_START_END,
    })

    assert r.status_code == 200
    vroom_req = json.loads(vroom.calls.last.request.content)
    assert [j["id"] for j in vroom_req["jobs"]] == [1, 4]


async def test_route_unreachable_phone_clear_error(client, mock_api):
    mock_phones_ok(mock_api)
    durations = _matrix(4)
    durations[0][1] = UNREACH  # start → phone 1 impossible
    mock_osrm_table(mock_api, "foot", durations)

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4], "profile": "foot", **ROUTE_START_END,
    })

    assert r.status_code == 400
    assert "#1" in r.json()["detail"]
    assert "foot" in r.json()["detail"]


async def test_route_disconnected_endpoints_clear_error(client, mock_api):
    mock_phones_ok(mock_api)
    durations = _matrix(4)
    durations[0][3] = UNREACH  # start → end impossible
    mock_osrm_table(mock_api, "foot", durations)

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4], "profile": "foot", **ROUTE_START_END,
    })

    assert r.status_code == 400
    assert "Start and end are not connected" in r.json()["detail"]


async def test_route_unknown_phone_404(client, mock_api):
    mock_phones_ok(mock_api)
    r = await client.post("/api/route", json={
        "phone_ids": [999], "profile": "car", **ROUTE_START_END,
    })
    assert r.status_code == 404


async def test_route_snapped_endpoints_cover_captures(client, mock_api):
    mock_phones_ok(mock_api)
    # Start snapped to phone 1, end snapped to phone 4 → only phone 6 remains a job
    mock_osrm_table(mock_api, "car", _matrix(3))
    mock_osrm_routes(mock_api, "car")
    vroom = mock_vroom(mock_api, job_order=[6])

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4, 6],
        "start": {"phone_id": 1}, "end": {"phone_id": 4},
        "profile": "car",
    })

    assert r.status_code == 200
    assert r.json()["ordered_ids"] == [1, 6, 4]
    vroom_req = json.loads(vroom.calls.last.request.content)
    assert [j["id"] for j in vroom_req["jobs"]] == [6]
    # A snapped endpoint's coordinate *is* start_coord/end_coord, so the visit list
    # must not repeat it: start(=1)→6 and 6→end(=4), not four legs with two of
    # zero length. Endpoint legs are still attributed to their phone id.
    legs = r.json()["legs"]
    assert len(legs) == 2
    assert [(l["from_id"], l["to_id"]) for l in legs] == [(1, 6), (6, 4)]


async def test_route_single_phone_that_is_the_endpoint(client, mock_api):
    """The bug from Discord: one selected phone which is also the end point.

    Filtering endpoint-snapped phones out of the jobs list leaves nothing to solve.
    Vroom rejects a jobs-less problem with a 400, which used to surface as a bare
    HTTP 500. It's a legitimate request, so it must return a direct start→end route.
    """
    mock_phones_ok(mock_api)
    mock_osrm_table(mock_api, "car", _matrix(2))
    mock_osrm_routes(mock_api, "car")
    vroom = mock_vroom_rejects_empty(mock_api)

    r = await client.post("/api/route", json={
        "phone_ids": [1],
        "start": {"lat": -34.91, "lon": 138.59},
        "end":   {"phone_id": 1},
        "profile": "car",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ordered_ids"] == [1]
    assert len(body["legs"]) == 1
    assert body["legs"][0]["to_id"] == 1
    # Nothing to sequence → Vroom should never have been asked.
    assert not vroom.called


async def test_route_all_selected_are_endpoints(client, mock_api):
    mock_phones_ok(mock_api)
    mock_osrm_table(mock_api, "car", _matrix(2))
    mock_osrm_routes(mock_api, "car")
    vroom = mock_vroom_rejects_empty(mock_api)

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4],
        "start": {"phone_id": 1}, "end": {"phone_id": 4},
        "profile": "car",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ordered_ids"] == [1, 4]
    assert len(body["legs"]) == 1
    assert (body["legs"][0]["from_id"], body["legs"][0]["to_id"]) == (1, 4)
    assert not vroom.called


async def test_route_start_and_end_same_phone_not_duplicated(client, mock_api):
    mock_phones_ok(mock_api)
    mock_osrm_table(mock_api, "car", _matrix(3))
    mock_osrm_routes(mock_api, "car")
    mock_vroom(mock_api, job_order=[6])

    r = await client.post("/api/route", json={
        "phone_ids": [1, 6],
        "start": {"phone_id": 1}, "end": {"phone_id": 1},
        "profile": "car",
    })

    assert r.status_code == 200
    ordered = r.json()["ordered_ids"]
    assert ordered == [1, 6]
    assert ordered.count(1) == 1


async def test_route_upstream_failure_returns_502(client, mock_api):
    """OSRM/Vroom falling over must not read as a bare 500 in the frontend."""
    mock_phones_ok(mock_api)
    mock_api.get(host=OSRM_HOSTS["car"], path__regex=r"/table/.*").mock(
        return_value=httpx.Response(500, text="osrm exploded")
    )

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4], "profile": "car", **ROUTE_START_END,
    })

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "OSRM" in detail and "Vroom" in detail


async def test_route_upstream_connect_error_returns_502(client, mock_api):
    mock_phones_ok(mock_api)
    mock_api.get(host=OSRM_HOSTS["car"], path__regex=r"/table/.*").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    r = await client.post("/api/route", json={
        "phone_ids": [1, 4], "profile": "car", **ROUTE_START_END,
    })

    assert r.status_code == 502


# ── /api/config.js ──────────────────────────────────────────────────

async def test_config_js_exposes_carto_key(client, monkeypatch):
    monkeypatch.setattr(main, "CARTO_API_KEY", "test-key-123")
    r = await client.get("/api/config.js")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "window.PP_CONFIG" in r.text
    assert '"cartoApiKey": "test-key-123"' in r.text


async def test_config_js_blank_key_is_valid_js(client, monkeypatch):
    monkeypatch.setattr(main, "CARTO_API_KEY", "")
    r = await client.get("/api/config.js")

    assert r.status_code == 200
    assert '"cartoApiKey": ""' in r.text


# ── /api/orienteer ──────────────────────────────────────────────────

async def test_orienteer_happy_path(client, mock_api):
    mock_phones_ok(mock_api)
    # Candidates: 1 + 6 (uncaptured) and 4 (hostile); mine/cellmate excluded → 5 coords
    mock_osrm_table(mock_api, "car", _matrix(5))
    mock_osrm_routes(mock_api, "car")
    mock_vroom(mock_api, job_order=[1, 4], duration=500, service=120)

    r = await client.post("/api/orienteer", json={
        "start": {"lat": -34.92, "lon": 138.60},
        "end":   {"lat": -34.97, "lon": 138.65},
        "profile": "car",
        "time_budget_s": 3600,
        "service_time_s": 60,
        "username": "GhostScout",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ordered_ids"] == [1, 4]
    assert body["total_duration_s"] == 620  # vroom travel + service accounting
    assert len(body["path"]["features"]) == 3


async def test_orienteer_excludes_own_and_cellmate_phones(client, mock_api):
    mock_phones_ok(mock_api)
    table = mock_osrm_table(mock_api, "car", _matrix(5))
    mock_osrm_routes(mock_api, "car")
    mock_vroom(mock_api, job_order=[1])

    r = await client.post("/api/orienteer", json={
        "start": {"lat": -34.92, "lon": 138.60},
        "end":   {"lat": -34.97, "lon": 138.65},
        "profile": "car",
        "time_budget_s": 3600,
        "service_time_s": 60,
        "username": "GhostScout",
    })

    assert r.status_code == 200
    # Table request: start + candidates {1, 4, 6} + end = 5 coordinates
    coord_str = str(table.calls.last.request.url.path).split("/")[-1]
    assert coord_str.count(";") == 4


# ── /api/photo-coverage ─────────────────────────────────────────────

async def test_photo_coverage_happy_path(client, mock_api):
    route = mock_api.get(main.PHOTO_COVERAGE_URL).mock(
        return_value=httpx.Response(200, json={"ids": [46, 17, 40]})
    )

    r = await client.get("/api/photo-coverage")

    assert r.status_code == 200
    assert r.json()["has_photo"] == [17, 40, 46]  # sorted
    assert route.call_count == 1

    # Second request within TTL is served from cache — no extra upstream call
    await client.get("/api/photo-coverage")
    assert route.call_count == 1


async def test_photo_coverage_missing_ids_key_is_empty(client, mock_api):
    route = mock_api.get(main.PHOTO_COVERAGE_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    r = await client.get("/api/photo-coverage")

    assert r.status_code == 200
    assert r.json()["has_photo"] == []

    # An empty coverage set must still cache — no re-fetch within the TTL
    await client.get("/api/photo-coverage")
    assert route.call_count == 1


async def test_photo_coverage_cold_failure_503(client, mock_api):
    mock_api.get(main.PHOTO_COVERAGE_URL).mock(return_value=httpx.Response(500))

    r = await client.get("/api/photo-coverage")

    assert r.status_code == 503
    assert r.json()["detail"] == main.UPSTREAM_DOWN_DETAIL


async def test_photo_coverage_cooldown_skips_upstream(client, mock_api):
    # With no cached snapshot, a hit inside the failure cooldown must 503
    # immediately without touching upstream.
    main._PHOTO_FAIL_UNTIL = time.monotonic() + main._PHONES_FAIL_COOLDOWN
    route = mock_api.get(main.PHOTO_COVERAGE_URL).mock(return_value=httpx.Response(200, json={"ids": [1]}))

    r = await client.get("/api/photo-coverage")

    assert r.status_code == 503
    assert route.call_count == 0


async def test_photo_coverage_stale_fallback(client, mock_api):
    main._PHOTO_CACHE    = {42}
    main._PHOTO_CACHE_AT = time.monotonic() - main._PHOTO_CACHE_TTL - 1
    mock_api.get(main.PHOTO_COVERAGE_URL).mock(return_value=httpx.Response(500))

    r = await client.get("/api/photo-coverage")

    assert r.status_code == 200
    assert r.json()["has_photo"] == [42]


# ── /api/health ─────────────────────────────────────────────────────

def _mock_services(router, vroom_up=True):
    for host in OSRM_HOSTS.values():
        router.get(host=host, path__regex=r"/nearest/.*").mock(
            return_value=httpx.Response(200, json={"code": "Ok"})
        )
    vroom_route = router.get(host=VROOM_HOST, path="/health")
    if vroom_up:
        vroom_route.mock(return_value=httpx.Response(200, text="OK"))
    else:
        vroom_route.mock(side_effect=httpx.ConnectError("refused"))


async def test_health_ok(client, mock_api):
    mock_phones_ok(mock_api)
    _mock_services(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})

    r = await client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["upstream"]["consecutive_failures"] == 0
    assert body["upstream"]["serving_stale"] is False
    assert body["upstream"]["last_success_age_s"] == 0
    assert all(body["services"].values())


async def test_health_degraded_when_service_down(client, mock_api):
    _mock_services(mock_api, vroom_up=False)

    r = await client.get("/api/health")

    body = r.json()
    assert body["status"] == "degraded"
    assert body["services"]["vroom"] is False
    assert body["services"]["osrm_foot"] is True


async def test_health_degraded_when_upstream_failing(client, mock_api):
    _mock_services(mock_api)
    main._PHONES_CACHE = SAMPLE_PHONES
    main._PHONES_CACHE_AT = time.monotonic()
    main._PHONES_FETCHED_AT = time.monotonic()
    main._PHONES_FAILS = 3

    r = await client.get("/api/health")

    body = r.json()
    assert body["status"] == "degraded"
    assert body["upstream"]["consecutive_failures"] == 3
    assert body["upstream"]["serving_stale"] is True
