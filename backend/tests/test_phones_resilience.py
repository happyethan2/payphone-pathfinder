"""Resilience behaviour of the phones cache: stale fallback, single-flight,
failure cooldown, cold-start 503, cache busting."""
import asyncio
import time

import httpx

import main
from conftest import SAMPLE_PHONES, mock_phones_ok


def _expire_phones_cache():
    # Simulate the passage of time: both the TTL marker and the true
    # last-success timestamp move into the past.
    expired = time.monotonic() - main._PHONES_CACHE_TTL - 1
    main._PHONES_CACHE_AT   = expired
    main._PHONES_FETCHED_AT = expired


async def test_fresh_fetch_and_ttl_reuse(client, mock_api):
    route = mock_phones_ok(mock_api)

    r1 = await client.get("/api/phones", params={"username": "GhostScout"})
    r2 = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r1.status_code == 200
    assert r1.json()["stale"] is False
    assert r1.json()["data_age_s"] == 0
    assert r2.status_code == 200
    # Second request within TTL must not touch upstream
    assert route.call_count == 1


async def test_cold_start_failure_returns_503(client, mock_api):
    route = mock_api.get(main.PHONES_URL).mock(return_value=httpx.Response(500))

    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 503
    assert r.json()["detail"] == main.UPSTREAM_DOWN_DETAIL
    # First attempt + one retry
    assert route.call_count == 1 + main._UPSTREAM_RETRIES


async def test_cold_start_timeout_returns_503(client, mock_api):
    mock_api.get(main.PHONES_URL).mock(side_effect=httpx.ConnectTimeout("boom"))

    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 503
    assert r.json()["detail"] == main.UPSTREAM_DOWN_DETAIL


async def test_garbage_payload_treated_as_failure(client, mock_api):
    mock_api.get(main.PHONES_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 503


async def test_stale_fallback_when_upstream_dies(client, mock_api):
    route = mock_phones_ok(mock_api)
    r = await client.get("/api/phones", params={"username": "GhostScout"})
    assert r.status_code == 200 and r.json()["stale"] is False
    fresh_phones = r.json()["phones"]

    # TTL expires, then the game API starts failing
    _expire_phones_cache()
    route.mock(return_value=httpx.Response(500))

    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["data_age_s"] >= main._PHONES_CACHE_TTL
    assert body["phones"] == fresh_phones  # last good snapshot still served


async def test_failure_cooldown_stops_upstream_hammering(client, mock_api):
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})

    _expire_phones_cache()
    route.mock(return_value=httpx.Response(500))
    await client.get("/api/phones", params={"username": "GhostScout"})
    calls_after_first_failure = route.call_count

    # Polls during the cooldown window must not touch upstream at all
    for _ in range(5):
        r = await client.get("/api/phones", params={"username": "GhostScout"})
        assert r.status_code == 200
        assert r.json()["stale"] is True
    assert route.call_count == calls_after_first_failure


async def test_recovery_after_cooldown(client, mock_api):
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})

    _expire_phones_cache()
    route.mock(return_value=httpx.Response(500))
    r = await client.get("/api/phones", params={"username": "GhostScout"})
    assert r.json()["stale"] is True

    # Upstream comes back; force the cooldown to lapse
    main._PHONES_FAIL_UNTIL = 0.0
    route.mock(return_value=httpx.Response(200, json=SAMPLE_PHONES))

    r = await client.get("/api/phones", params={"username": "GhostScout"})
    assert r.status_code == 200
    assert r.json()["stale"] is False
    assert r.json()["data_age_s"] == 0


async def test_single_flight_one_upstream_fetch(client, mock_api):
    async def slow_ok(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=SAMPLE_PHONES)

    route = mock_api.get(main.PHONES_URL).mock(side_effect=slow_ok)

    results = await asyncio.gather(
        *(client.get("/api/phones", params={"username": "GhostScout"}) for _ in range(10))
    )

    assert all(r.status_code == 200 for r in results)
    # Ten genuinely-concurrent requests, exactly one upstream fetch
    assert route.call_count == 1


async def test_cache_bust_forces_refetch(client, mock_api):
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 1

    r = await client.post("/api/phones/refresh")
    assert r.status_code == 200

    await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 2


async def test_cache_bust_respects_failure_cooldown(client, mock_api):
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})

    _expire_phones_cache()
    route.mock(return_value=httpx.Response(500))
    await client.get("/api/phones", params={"username": "GhostScout"})
    calls_after_failure = route.call_count

    # Cache-busting during the cooldown must not bypass it
    await client.post("/api/phones/refresh")
    r = await client.get("/api/phones", params={"username": "GhostScout"})

    assert r.status_code == 200 and r.json()["stale"] is True
    assert route.call_count == calls_after_failure
