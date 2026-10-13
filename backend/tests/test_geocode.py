"""Address search proxy (/api/geocode, /api/reverse) with Photon mocked."""
import httpx

import main

PHOTON_HOST = httpx.URL(main.PHOTON_URL).host

FEATURES = {"type": "FeatureCollection", "features": [{
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [144.3601837, -38.1473332]},
    "properties": {"name": "Malop St Dry Cleaners", "housenumber": "52", "street": "Malop Street",
                   "city": "Geelong", "state": "Victoria"},
}]}


def mock_search(router, **kw):
    return router.get(host=PHOTON_HOST, path="/api/").mock(**kw)


def mock_reverse(router, **kw):
    return router.get(host=PHOTON_HOST, path="/reverse").mock(**kw)


async def test_geocode_forwards_params_and_returns_photon_body(client, mock_api):
    route = mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    r = await client.get("/api/geocode", params={"q": "  malop  st geelong ", "lat": -38.14712, "lon": 144.36049})
    assert r.status_code == 200
    assert r.json() == FEATURES
    sent = route.calls.last.request
    assert sent.url.params["q"] == "malop st geelong"
    assert sent.url.params["limit"] == "5"
    assert sent.url.params["lang"] == "en"
    assert sent.url.params["bbox"] == main.AU_BBOX
    # Bias is rounded to ~1 km before it leaves the server
    assert sent.url.params["lat"] == "-38.15"
    assert sent.url.params["lon"] == "144.36"
    assert "payphone-pathfinder" in sent.headers["user-agent"]


async def test_geocode_without_bias_omits_lat_lon(client, mock_api):
    route = mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    r = await client.get("/api/geocode", params={"q": "geelong"})
    assert r.status_code == 200
    assert "lat" not in route.calls.last.request.url.params


async def test_geocode_is_cached(client, mock_api):
    route = mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    for q in ("Geelong Station", "geelong  station"):  # same query modulo case/spacing
        r = await client.get("/api/geocode", params={"q": q, "lat": -38.147, "lon": 144.36})
        assert r.status_code == 200
    assert route.call_count == 1


async def test_geocode_cache_is_capped(client, mock_api, monkeypatch):
    monkeypatch.setattr(main, "_GEO_CACHE_MAX", 2)
    mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    for q in ("aaa", "bbb", "ccc"):
        await client.get("/api/geocode", params={"q": q})
    assert [k[1] for k in main._GEO_CACHE] == ["bbb", "ccc"]


async def test_geocode_cache_key_cannot_be_spoofed(client, mock_api):
    # A query that spells out another request's bias must not share its cache entry
    route = mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    await client.get("/api/geocode", params={"q": "main street|-34.93,138.6"})
    await client.get("/api/geocode", params={"q": "main street", "lat": -34.93, "lon": 138.6})
    assert route.call_count == 2


async def test_geocode_short_query_rejected(client, mock_api):
    route = mock_search(mock_api, return_value=httpx.Response(200, json=FEATURES))
    for q in ("ab", "  a  "):
        r = await client.get("/api/geocode", params={"q": q})
        assert r.status_code == 422
    assert route.call_count == 0


async def test_geocode_upstream_error_is_502_with_search_message(client, mock_api):
    mock_search(mock_api, return_value=httpx.Response(500))
    r = await client.get("/api/geocode", params={"q": "geelong"})
    assert r.status_code == 502
    assert "Address search" in r.json()["detail"]


async def test_geocode_timeout_is_502(client, mock_api):
    mock_search(mock_api, side_effect=httpx.ConnectTimeout("slow"))
    r = await client.get("/api/geocode", params={"q": "geelong"})
    assert r.status_code == 502


async def test_geocode_unexpected_payload_is_502_and_not_cached(client, mock_api):
    route = mock_search(mock_api, return_value=httpx.Response(200, json={"error": "nope"}))
    for _ in range(2):
        r = await client.get("/api/geocode", params={"q": "geelong"})
        assert r.status_code == 502
    assert route.call_count == 2


async def test_geocode_uses_photon_url_override(client, mock_api, monkeypatch):
    monkeypatch.setattr(main, "PHOTON_URL", "http://photon.local:2322")
    route = mock_api.get("http://photon.local:2322/api/").mock(return_value=httpx.Response(200, json=FEATURES))
    r = await client.get("/api/geocode", params={"q": "geelong"})
    assert r.status_code == 200
    assert route.call_count == 1


async def test_reverse_rounds_and_proxies(client, mock_api):
    route = mock_reverse(mock_api, return_value=httpx.Response(200, json=FEATURES))
    r = await client.get("/api/reverse", params={"lat": -38.1473332, "lon": 144.3601837})
    assert r.status_code == 200
    assert r.json() == FEATURES
    sent = route.calls.last.request.url.params
    assert (sent["lat"], sent["lon"], sent["limit"]) == ("-38.14733", "144.36018", "1")


async def test_reverse_out_of_range_rejected(client, mock_api):
    r = await client.get("/api/reverse", params={"lat": 120, "lon": 144})
    assert r.status_code == 422
