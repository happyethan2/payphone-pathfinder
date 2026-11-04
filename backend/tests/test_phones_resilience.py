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
    main._PHONES_FETCHED_AT -= main._PHONES_REFRESH_MIN  # the snapshot is 10 s old, still inside the TTL

    r = await client.post("/api/phones/refresh")
    assert r.status_code == 200

    await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 2


async def test_cache_bust_at_most_one_pull_per_10s(client, mock_api):
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})
    # Everyone tapping refresh on a fresh snapshot: no upstream pull at all
    for _ in range(3):
        await client.post("/api/phones/refresh")
        await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 1
    # Ten seconds on, one tap pulls once; the next tap is inside the limit again
    main._PHONES_FETCHED_AT -= main._PHONES_REFRESH_MIN
    for _ in range(2):
        await client.post("/api/phones/refresh")
        await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 2


async def test_phones_etag_304_until_a_new_snapshot(client, mock_api):
    mock_phones_ok(mock_api)
    params = {"username": "GhostScout"}
    r1 = await client.get("/api/phones", params=params)
    etag = r1.headers["etag"]
    assert etag.startswith('W/"') and r1.headers["cache-control"] == "no-cache"
    assert abs(r1.json()["fetched_at"] - time.time()) < 5

    r2 = await client.get("/api/phones", params=params, headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == etag

    _expire_phones_cache()
    main._PHONES_FETCHED_WALL -= 100  # time.time() could repeat within the clock's resolution
    r3 = await client.get("/api/phones", params=params, headers={"If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["etag"] != etag and r3.json()["phones"]


async def test_phones_etag_changes_when_served_stale(client, mock_api):
    route = mock_phones_ok(mock_api)
    etag = (await client.get("/api/phones", params={"username": "GhostScout"})).headers["etag"]
    _expire_phones_cache()
    route.mock(return_value=httpx.Response(500))
    r = await client.get("/api/phones", params={"username": "GhostScout"}, headers={"If-None-Match": etag})
    assert r.status_code == 200 and r.json()["stale"] is True  # same snapshot, but the client must learn it is stale


async def test_phones_gzipped(client, mock_api):
    big = {**SAMPLE_PHONES, "payphones": [[i, 138.6, -34.9, 0, "active"] for i in range(1, 500)]}
    mock_phones_ok(mock_api, big)
    r = await client.get("/api/phones", params={"username": "GhostScout"}, headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and len(r.json()["phones"]) == 499


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


def _mock_holder(router, pid, captured_by):
    return router.get(f"{main.PAYPHONE_API_BASE}/payphone/{pid}").mock(
        return_value=httpx.Response(200, json={"id": pid, "captured_by": captured_by, "owner_name": "x"}))


async def test_capture_check_reports_only_my_phones(client, mock_api):
    mock_phones_ok(mock_api)
    _mock_holder(mock_api, 1, 7)    # GhostScout (player 7) just took it; the feed still says unheld
    _mock_holder(mock_api, 4, 9)    # a rival holds it
    _mock_holder(mock_api, 6, None)
    r = await client.get("/api/capture-check", params={"username": "ghostscout", "ids": "1,4,6"})
    assert r.status_code == 200 and r.json() == {"mine": [1]}


async def test_capture_check_shares_one_answer_per_phone(client, mock_api):
    mock_phones_ok(mock_api)
    one = _mock_holder(mock_api, 1, 7)
    for name in ("GhostScout", "Mate"):
        await client.get("/api/capture-check", params={"username": name, "ids": "1"})
    assert one.call_count == 1  # cached for the 10 s the fastest client waits anyway


async def test_capture_check_survives_a_failing_phone(client, mock_api):
    mock_phones_ok(mock_api)
    _mock_holder(mock_api, 1, 7)
    mock_api.get(f"{main.PAYPHONE_API_BASE}/payphone/4").mock(return_value=httpx.Response(500))
    r = await client.get("/api/capture-check", params={"username": "GhostScout", "ids": "4,1"})
    assert r.status_code == 200 and r.json() == {"mine": [1]}


async def test_capture_check_rejects_bad_input_and_unknown_players(client, mock_api):
    mock_phones_ok(mock_api)
    for ids in ("", "1,x", "1,2,3,4,6"):
        assert (await client.get("/api/capture-check", params={"username": "GhostScout", "ids": ids})).status_code == 400
    r = await client.get("/api/capture-check", params={"username": "Nobody", "ids": "1"})
    assert r.json() == {"mine": []}


class _JustBooted:
    """main's clock on a machine that booted a few seconds ago: time.monotonic() counts from boot."""
    now = 5.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return self.now


async def test_cache_bust_soon_after_boot(client, mock_api, monkeypatch):
    # A fresh CI runner or server can still be inside the TTL measured from zero
    clock = _JustBooted()
    monkeypatch.setattr(main, "time", clock)
    route = mock_phones_ok(mock_api)
    await client.get("/api/phones", params={"username": "GhostScout"})
    clock.now = 16.0  # past the refresh limit, still inside the TTL counted from zero
    await client.post("/api/phones/refresh")
    await client.get("/api/phones", params={"username": "GhostScout"})
    assert route.call_count == 2


async def test_photos_then_capture_check_share_one_upstream_call(client, mock_api):
    mock_phones_ok(mock_api)
    one = _mock_holder(mock_api, 1, 7)
    assert (await client.get("/api/phone-photos/1")).status_code == 200
    r = await client.get("/api/capture-check", params={"username": "GhostScout", "ids": "1"})
    assert r.json() == {"mine": [1]}
    assert one.call_count == 1
