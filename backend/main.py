import asyncio
import math
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    global _HTTP
    if _HTTP is not None:
        await _HTTP.aclose()
        _HTTP = None


app = FastAPI(title="Payphone Pathfinder", lifespan=_lifespan)

OSRM_URLS = {
    "foot":    os.getenv("OSRM_FOOT_URL",    "http://osrm-foot:5000"),
    "bicycle": os.getenv("OSRM_BICYCLE_URL", "http://osrm-bicycle:5000"),
    "car":     os.getenv("OSRM_CAR_URL",     "http://osrm-car:5000"),
}
VROOM_URL         = os.getenv("VROOM_URL", "http://vroom:3000")
PAYPHONE_API_BASE = os.getenv("PAYPHONE_API_BASE", "https://payphonetag.com/api")
PHONES_URL        = f"{PAYPHONE_API_BASE}/payphones"
WIKI_API          = os.getenv("WIKI_API_URL", "https://wiki.payphonetag.com/api.php")
FRONTEND_DIR      = os.getenv("FRONTEND_DIR", "/app/frontend")
CERT_FILE         = os.getenv("CERT_FILE", "/app/certs/cert.pem")

# Must match --max-table-size in docker-compose.yml
MAX_TABLE_COORDS = int(os.getenv("MAX_TABLE_COORDS", "200"))

# Payphone tuple indices — adjust here if the live API changes
IDX_ID     = 0
IDX_LON    = 1
IDX_LAT    = 2
IDX_HOLDER = 3
IDX_STATUS = 4

# Sentinel for unreachable OSRM pairs (must fit in Vroom's int32)
UNREACHABLE = 999_999_999

UPSTREAM_DOWN_DETAIL = (
    "The Payphone Tag game API is currently unavailable and no cached data "
    "exists yet. Please try again in a moment."
)

# ---------------------------------------------------------------------------
# Shared HTTP client — one connection pool for all upstream/OSRM/Vroom calls.
# Created lazily so importing this module (e.g. under pytest) needs no loop.
# ---------------------------------------------------------------------------
_HTTP: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _HTTP


# ---------------------------------------------------------------------------
# Phone data cache — one upstream fetch per TTL window, all clients share it.
# The last good snapshot is kept indefinitely: if the game API goes down we
# keep serving it (flagged stale) instead of erroring, and a short failure
# cooldown stops every client poll from hammering the struggling upstream.
# ---------------------------------------------------------------------------
_PHONES_CACHE: dict | None = None
_PHONES_CACHE_AT: float    = 0.0   # TTL marker (zeroed by the cache-bust endpoint)
_PHONES_FETCHED_AT: float  = 0.0   # last successful fetch — for truthful data-age reporting
_PHONES_CACHE_TTL: float   = 30.0  # seconds
_PHONES_LOCK  = asyncio.Lock()
_PHONES_FAIL_UNTIL: float  = 0.0   # no upstream attempts before this (monotonic)
_PHONES_FAILS: int         = 0     # consecutive upstream failures
_PHONES_FAIL_COOLDOWN: float = 10.0
_UPSTREAM_RETRIES: int     = 1     # extra attempts after the first failure
_UPSTREAM_RETRY_DELAY: float = 0.5
_UPSTREAM_TIMEOUT: float   = 6.0   # per-attempt; keeps polling latency bounded

# ---------------------------------------------------------------------------
# Wiki photo cache — paginate allimages once per TTL, extract phone IDs
# ---------------------------------------------------------------------------
_WIKI_CACHE: set | None = None
_WIKI_CACHE_AT: float   = 0.0
_WIKI_CACHE_TTL: float  = 300.0  # 5 minutes
_WIKI_LOCK  = asyncio.Lock()
_WIKI_FAIL_UNTIL: float = 0.0
_WIKI_FAIL_COOLDOWN: float = 60.0

# ---------------------------------------------------------------------------
# Past-captures cache — per-user with TTL; stale entries are kept as fallback
# ---------------------------------------------------------------------------
_CAPTURES_CACHE: dict[str, tuple[set[int], float]] = {}  # user_lower -> (ids, fetched_at)
_CAPTURES_CACHE_TTL: float = 30.0   # match phone state cache TTL
_CAPTURES_CACHE_MAX: int   = 200    # cap entries on shared instances
_CAPTURES_LOCK = asyncio.Lock()
_CAPTURES_FAIL_UNTIL: float = 0.0


def reset_caches() -> None:
    """Reset all module-level cache state. Used by the test suite.

    Also drops the shared HTTP client so each test event loop gets its own
    (requests are intercepted by respx in tests, so nothing real leaks).
    """
    global _HTTP
    global _PHONES_CACHE, _PHONES_CACHE_AT, _PHONES_FETCHED_AT, _PHONES_FAIL_UNTIL, _PHONES_FAILS
    global _WIKI_CACHE, _WIKI_CACHE_AT, _WIKI_FAIL_UNTIL
    global _CAPTURES_CACHE, _CAPTURES_FAIL_UNTIL
    _HTTP              = None
    _PHONES_CACHE      = None
    _PHONES_CACHE_AT   = 0.0
    _PHONES_FETCHED_AT = 0.0
    _PHONES_FAIL_UNTIL = 0.0
    _PHONES_FAILS      = 0
    _WIKI_CACHE        = None
    _WIKI_CACHE_AT     = 0.0
    _WIKI_FAIL_UNTIL   = 0.0
    _CAPTURES_CACHE    = {}
    _CAPTURES_FAIL_UNTIL = 0.0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class StartEnd(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    phone_id: Optional[int] = None


class RouteRequest(BaseModel):
    phone_ids: list[int]
    start: StartEnd
    end: StartEnd
    profile: str  # "foot" | "bicycle" | "car"


class OrienteerRequest(BaseModel):
    start: StartEnd
    end: StartEnd                   # destination (A→B, not a loop)
    profile: str                    # "foot" | "bicycle" | "car"
    time_budget_s: int              # available seconds (arrival - now - buffer, computed client-side)
    service_time_s: int = 60        # seconds spent tagging each phone (foot=60, bike=30, car=75)
    username: str
    cell: str = ""
    include_hostile: bool = True
    include_uncaptured: bool = True
    max_candidates: int = 50        # cap VROOM input; quality degrades above ~50 jobs for 1 vehicle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_phone(p, my_player_id: str | None, cellmate_player_ids: set[str]) -> str:
    holder = p[IDX_HOLDER]
    if not holder or str(holder) == "0":
        return "uncaptured"
    holder_str = str(holder)
    if my_player_id and holder_str == my_player_id:
        return "mine"
    if holder_str in cellmate_player_ids:
        return "cellmate"
    return "hostile"


async def _get_upstream_phones() -> dict:
    """One guarded upstream fetch with a short per-attempt timeout and retry."""
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(1 + _UPSTREAM_RETRIES):
        try:
            resp = await _http().get(PHONES_URL, timeout=_UPSTREAM_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("unexpected payload from game API")
            return data
        except (httpx.HTTPError, ValueError) as e:
            last_exc = e
            if attempt < _UPSTREAM_RETRIES:
                await asyncio.sleep(_UPSTREAM_RETRY_DELAY)
    raise last_exc


async def _get_phones_cached() -> tuple[dict, bool, float]:
    """Return (data, stale, age_s) for the payphone API payload.

    - Fresh cache hit: served without touching upstream.
    - Cache expired: one request refreshes it (single-flight lock); concurrent
      callers wait and reuse the result.
    - Upstream failure: last good snapshot is served flagged stale, and a
      cooldown prevents further upstream attempts for a few seconds.
    - Failure with no snapshot at all (cold start): 503 with a clear message.
    """
    global _PHONES_CACHE, _PHONES_CACHE_AT, _PHONES_FETCHED_AT, _PHONES_FAIL_UNTIL, _PHONES_FAILS

    now = time.monotonic()
    if _PHONES_CACHE is not None and (now - _PHONES_CACHE_AT) < _PHONES_CACHE_TTL:
        return _PHONES_CACHE, False, now - _PHONES_FETCHED_AT

    async with _PHONES_LOCK:
        now = time.monotonic()
        if _PHONES_CACHE is not None and (now - _PHONES_CACHE_AT) < _PHONES_CACHE_TTL:
            return _PHONES_CACHE, False, now - _PHONES_FETCHED_AT

        if now < _PHONES_FAIL_UNTIL:
            if _PHONES_CACHE is not None:
                return _PHONES_CACHE, True, now - _PHONES_FETCHED_AT
            raise HTTPException(503, UPSTREAM_DOWN_DETAIL)

        try:
            data = await _get_upstream_phones()
        except (httpx.HTTPError, ValueError) as e:
            _PHONES_FAILS     += 1
            _PHONES_FAIL_UNTIL = time.monotonic() + _PHONES_FAIL_COOLDOWN
            if _PHONES_CACHE is not None:
                return _PHONES_CACHE, True, time.monotonic() - _PHONES_FETCHED_AT
            raise HTTPException(503, UPSTREAM_DOWN_DETAIL) from e

        _PHONES_CACHE      = data
        _PHONES_CACHE_AT   = time.monotonic()
        _PHONES_FETCHED_AT = _PHONES_CACHE_AT
        _PHONES_FAILS      = 0
        return _PHONES_CACHE, False, 0.0


async def _fetch_phones_data() -> dict:
    """Payphone API payload, ignoring staleness (routing still works on stale coords)."""
    data, _, _ = await _get_phones_cached()
    return data


async def _fetch_wiki_photo_ids() -> set[int]:
    """Return the set of sequential payphone API IDs that have at least one wiki photo.

    Two-step process:
      1. Paginate allimages?aiprefix=Payphone- to collect unique CAB codes
         (e.g. "08835506X2") from filenames like Payphone-08835506X2-timestamp.jpg
      2. Batch-fetch the corresponding wiki pages (50 at a time) and parse the
         `| id = XXXX` template field, which holds the sequential payphone API ID.

    Guarded by a lock (one crawl at a time) with stale fallback on failure.
    """
    global _WIKI_CACHE, _WIKI_CACHE_AT, _WIKI_FAIL_UNTIL

    now = time.monotonic()
    if _WIKI_CACHE is not None and (now - _WIKI_CACHE_AT) < _WIKI_CACHE_TTL:
        return _WIKI_CACHE

    async with _WIKI_LOCK:
        now = time.monotonic()
        if _WIKI_CACHE is not None and (now - _WIKI_CACHE_AT) < _WIKI_CACHE_TTL:
            return _WIKI_CACHE
        if now < _WIKI_FAIL_UNTIL:
            if _WIKI_CACHE is not None:
                return _WIKI_CACHE
            raise HTTPException(503, "The Payphone Tag wiki is currently unavailable — try again shortly.")

        try:
            has_photo = await _crawl_wiki_photo_ids()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            _WIKI_FAIL_UNTIL = time.monotonic() + _WIKI_FAIL_COOLDOWN
            if _WIKI_CACHE is not None:
                return _WIKI_CACHE
            raise HTTPException(503, "The Payphone Tag wiki is currently unavailable — try again shortly.") from e

        _WIKI_CACHE    = has_photo
        _WIKI_CACHE_AT = time.monotonic()
        return _WIKI_CACHE


async def _crawl_wiki_photo_ids() -> set[int]:
    # ── Step 1: collect unique CAB codes from all uploaded images ──
    cab_ids: set[str] = set()
    img_params: dict = {
        "action":   "query",
        "list":     "allimages",
        "aiprefix": "Payphone-",
        "ailimit":  "500",
        "format":   "json",
    }
    while True:
        resp = await _http().get(WIKI_API, params=img_params, timeout=60.0)
        resp.raise_for_status()
        body = resp.json()
        for img in body.get("query", {}).get("allimages", []):
            m = re.match(r"^Payphone-(\d+X\d+)-", img["name"])
            if m:
                cab_ids.add(m.group(1))
        cont = body.get("continue", {}).get("aicontinue")
        if not cont:
            break
        img_params["aicontinue"] = cont

    # ── Step 2: batch-fetch wiki pages and extract sequential API id ──
    has_photo: set[int] = set()
    cab_list  = sorted(cab_ids)
    batch_size = 50
    for i in range(0, len(cab_list), batch_size):
        batch  = cab_list[i : i + batch_size]
        titles = "|".join(f"Payphone:{cab}" for cab in batch)
        resp = await _http().get(WIKI_API, params={
            "action":  "query",
            "titles":  titles,
            "prop":    "revisions",
            "rvprop":  "content",
            "rvslots": "*",
            "format":  "json",
        }, timeout=60.0)
        resp.raise_for_status()
        body = resp.json()
        for page in body.get("query", {}).get("pages", {}).values():
            if "revisions" not in page:
                continue
            rev = page["revisions"][0]
            # Support both old MediaWiki (rev["*"]) and new (rev["slots"]["main"])
            content = (
                rev.get("*")
                or rev.get("content")
                or (rev.get("slots") or {}).get("main", {}).get("*", "")
                or (rev.get("slots") or {}).get("main", {}).get("content", "")
                or ""
            )
            id_match = re.search(r"\|\s*id\s*=\s*(\d+)", content)
            if id_match:
                has_photo.add(int(id_match.group(1)))

    return has_photo


def _resolve_player(data: dict, username: str, cell_tag: str | None):
    """Return (my_player_id, cellmate_player_ids, players_lookup).

    API structure:
      data['players'] = { player_id: {name, color, cellId?, ...} }
      data['cells']   = { cell_id:   {name, tag, color} }
      payphone[3]     = holder player_id (int, 0 = uncaptured)

    Steps:
      1. Find current player by matching name in players dict.
      2. Get their cellId (if any).
      3. Collect all player_ids that share the same cellId → cellmates.
    """
    players: dict = data.get("players", {})
    cells: dict   = data.get("cells", {})

    my_player_id: str | None = None
    my_cell_id: int | None   = None

    # 1. Find current player by username
    for pid, pdata in players.items():
        if isinstance(pdata, dict) and pdata.get("name", "").lower() == username.lower():
            my_player_id = str(pid)
            my_cell_id   = pdata.get("cellId")
            break

    # 2. Resolve cell for cellmate colouring:
    #    - Prefer the player's own cellId from the API
    #    - Fall back to the cell_tag param (so new/solo players can still see teammates)
    if my_cell_id is None and cell_tag:
        for cid, cdata in cells.items():
            if isinstance(cdata, dict) and cdata.get("tag", "").upper() == cell_tag.upper():
                my_cell_id = int(cid)
                break

    # 3. Collect all player IDs in the resolved cell as cellmates (excluding self)
    cellmate_player_ids: set[str] = set()
    if my_cell_id is not None:
        for pid, pdata in players.items():
            if isinstance(pdata, dict) and pdata.get("cellId") == my_cell_id \
                    and str(pid) != my_player_id:
                cellmate_player_ids.add(str(pid))

    return my_player_id, cellmate_player_ids, players


async def _osrm_table(coords: list[tuple[float, float]], profile: str) -> tuple[list, list]:
    """Return (durations_matrix, distances_matrix).

    coords is a list of (lat, lon) tuples; OSRM wants lon,lat.
    """
    base = OSRM_URLS.get(profile, OSRM_URLS["car"])
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{base}/table/v1/driving/{coord_str}"
    resp = await _http().get(url, params={"annotations": "duration,distance"}, timeout=60.0)
    if resp.status_code == 400:
        raise HTTPException(
            502,
            f"OSRM rejected the table request ({len(coords)} coordinates). "
            "The --max-table-size limit may be too low — check docker-compose.yml.",
        )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "Ok":
        raise HTTPException(502, f"OSRM table error: {body.get('message')}")

    def _clean(matrix):
        return [
            [UNREACHABLE if v is None else int(v) for v in row]
            for row in matrix
        ]

    return _clean(body["durations"]), _clean(body["distances"])


async def _osrm_route(coord_a: tuple[float, float], coord_b: tuple[float, float], profile: str) -> dict:
    """Return the first OSRM route object for the a→b leg."""
    base = OSRM_URLS.get(profile, OSRM_URLS["car"])
    lat_a, lon_a = coord_a
    lat_b, lon_b = coord_b
    url = f"{base}/route/v1/driving/{lon_a},{lat_a};{lon_b},{lat_b}"
    resp = await _http().get(url, params={
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
    }, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "Ok" or not body.get("routes"):
        raise HTTPException(502, f"OSRM route error: {body.get('message')}")
    return body["routes"][0]


async def _build_route_geometry(
    visit_coords: list[tuple[float, float]],
    visit_labels: list[int | None],
    profile: str,
) -> tuple[list, list, float, float]:
    """Fetch per-leg OSRM geometry concurrently (bounded) and assemble the
    response features/legs in visit order.

    Returns (features, legs_summary, total_distance_m, total_duration_s).
    """
    sem = asyncio.Semaphore(8)

    async def leg_task(i: int) -> dict:
        async with sem:
            return await _osrm_route(visit_coords[i], visit_coords[i + 1], profile)

    legs = await asyncio.gather(*(leg_task(i) for i in range(len(visit_coords) - 1)))

    features = []
    legs_summary = []
    total_distance = 0.0
    total_duration = 0.0

    for i, leg in enumerate(legs):
        features.append({
            "type": "Feature",
            "geometry": leg["geometry"],
            "properties": {
                "leg_index":  i,
                "from_id":    visit_labels[i],
                "to_id":      visit_labels[i + 1],
                "distance_m": leg["distance"],
                "duration_s": leg["duration"],
            },
        })
        total_distance += leg["distance"]
        total_duration += leg["duration"]
        legs_summary.append({
            "from_id":    visit_labels[i],
            "to_id":      visit_labels[i + 1],
            "distance_m": leg["distance"],
            "duration_s": leg["duration"],
            "steps": [
                {
                    "instruction": s.get("maneuver", {}).get("type", ""),
                    "name":        s.get("name", ""),
                    "distance_m":  s.get("distance", 0),
                    "duration_s":  s.get("duration", 0),
                }
                for s in leg.get("legs", [{}])[0].get("steps", [])
            ],
        })

    return features, legs_summary, total_distance, total_duration


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two lat/lon points."""
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


async def _vroom_solve(
    coords: list[tuple[float, float]],
    start_idx: int,
    end_idx: int,
    job_indices: list[tuple[int, int]],  # [(phone_id, coord_index), ...]
    durations: list[list[int]],
    distances: list[list[int]],
) -> list[int]:
    """Return ordered list of phone_ids as solved by Vroom."""
    vroom_req = {
        "vehicles": [{
            "id": 0,
            "start_index": start_idx,
            "end_index": end_idx,
            "profile": "driving",
        }],
        "jobs": [
            {"id": phone_id, "location_index": coord_idx}
            for phone_id, coord_idx in job_indices
        ],
        "matrices": {
            "driving": {
                "durations": durations,
                "distances": distances,
            }
        },
    }
    resp = await _http().post(VROOM_URL, json=vroom_req, timeout=120.0)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code", 0) != 0:
        raise HTTPException(502, f"Vroom error: {body.get('error', 'unknown')}")

    routes = body.get("routes", [])
    if not routes:
        raise HTTPException(502, "Vroom returned no routes")

    steps = routes[0].get("steps", [])
    return [s["id"] for s in steps if s.get("type") == "job"]


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/phones/refresh")
async def bust_phones_cache():
    """Force-expire the phone data cache so the next /api/phones call re-fetches upstream.

    The failure cooldown still applies, so this can't be used to hammer the
    game API while it is down.
    """
    global _PHONES_CACHE_AT
    _PHONES_CACHE_AT = 0.0
    return {"ok": True}


@app.get("/api/wiki-photos")
async def get_wiki_photos():
    """Return IDs of phones that have at least one user photo on the payphonetag wiki."""
    ids = await _fetch_wiki_photo_ids()
    return {"has_photo": sorted(ids)}


async def _fetch_past_capture_ids(username: str, cell: str) -> set[int]:
    global _CAPTURES_FAIL_UNTIL

    key = username.lower()
    now = time.monotonic()
    cached = _CAPTURES_CACHE.get(key)
    if cached is not None and (now - cached[1]) < _CAPTURES_CACHE_TTL:
        return cached[0]

    async with _CAPTURES_LOCK:
        now = time.monotonic()
        cached = _CAPTURES_CACHE.get(key)
        if cached is not None and (now - cached[1]) < _CAPTURES_CACHE_TTL:
            return cached[0]

        data = await _fetch_phones_data()
        player_id, _, _ = _resolve_player(data, username, cell or None)
        if not player_id:
            return set()

        if now < _CAPTURES_FAIL_UNTIL:
            if cached is not None:
                return cached[0]
            raise HTTPException(503, UPSTREAM_DOWN_DETAIL)

        try:
            resp = await _http().get(
                f"{PAYPHONE_API_BASE}/player/{player_id}/past-captures",
                timeout=_UPSTREAM_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected payload from game API")
        except (httpx.HTTPError, ValueError) as e:
            _CAPTURES_FAIL_UNTIL = time.monotonic() + _PHONES_FAIL_COOLDOWN
            if cached is not None:
                return cached[0]  # stale fallback
            raise HTTPException(503, UPSTREAM_DOWN_DETAIL) from e

        ids: set[int] = set(int(i) for i in payload.get("payphoneIds", []))

        # Cap the per-user cache on shared instances — evict the oldest entry
        if key not in _CAPTURES_CACHE and len(_CAPTURES_CACHE) >= _CAPTURES_CACHE_MAX:
            oldest = min(_CAPTURES_CACHE, key=lambda k: _CAPTURES_CACHE[k][1])
            del _CAPTURES_CACHE[oldest]
        _CAPTURES_CACHE[key] = (ids, time.monotonic())
        return ids


@app.get("/api/past-captures")
async def get_past_captures(
    username: str = Query(...),
    cell:     str = Query(default=""),
):
    ids = await _fetch_past_capture_ids(username, cell)
    return {"captured": sorted(ids)}


@app.get("/api/phones")
async def get_phones(
    username: str = Query(...),
    cell: str = Query(default=""),
):
    data, stale, age_s = await _get_phones_cached()
    my_player_id, cellmate_player_ids, players = _resolve_player(data, username, cell or None)

    result = []
    for p in data.get("payphones", []):
        if len(p) <= IDX_STATUS:
            continue
        if p[IDX_STATUS] != "active":
            continue

        status = _classify_phone(p, my_player_id, cellmate_player_ids)
        holder = p[IDX_HOLDER]
        holder_name = None
        if holder and str(holder) != "0":
            player_data = players.get(str(holder))
            if isinstance(player_data, dict):
                holder_name = player_data.get("name")

        result.append({
            "id":          p[IDX_ID],
            "lon":         p[IDX_LON],
            "lat":         p[IDX_LAT],
            "status":      status,
            "holder_name": holder_name,
        })

    return {"phones": result, "stale": stale, "data_age_s": int(age_s)}


@app.get("/api/health")
async def health():
    """Liveness + upstream/service state, for status pages and debugging.

    Always returns 200; monitors should keyword-match on "status": "ok".
    Upstream state is reported passively from the cache — a health poll never
    adds load to the game API.
    """
    now = time.monotonic()

    async def probe(url: str) -> bool:
        try:
            resp = await _http().get(url, timeout=2.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    service_names = ["osrm_foot", "osrm_bicycle", "osrm_car", "vroom"]
    results = await asyncio.gather(
        probe(f"{OSRM_URLS['foot']}/nearest/v1/driving/138.60,-34.93"),
        probe(f"{OSRM_URLS['bicycle']}/nearest/v1/driving/138.60,-34.93"),
        probe(f"{OSRM_URLS['car']}/nearest/v1/driving/138.60,-34.93"),
        probe(f"{VROOM_URL}/health"),
    )
    services = dict(zip(service_names, results))

    upstream = {
        "last_success_age_s": int(now - _PHONES_FETCHED_AT) if _PHONES_CACHE is not None else None,
        "consecutive_failures": _PHONES_FAILS,
        "serving_stale": _PHONES_CACHE is not None and _PHONES_FAILS > 0,
    }

    upstream_ok = _PHONES_FAILS == 0
    status = "ok" if upstream_ok and all(services.values()) else "degraded"
    return {"status": status, "upstream": upstream, "services": services}


@app.post("/api/route")
async def solve_route(req: RouteRequest):
    if req.profile not in OSRM_URLS:
        raise HTTPException(400, f"Unknown profile '{req.profile}'. Use: foot, bicycle, car")
    if not req.phone_ids:
        raise HTTPException(400, "phone_ids must not be empty")

    # Dedupe while preserving selection order (duplicate job ids break Vroom)
    phone_ids = list(dict.fromkeys(req.phone_ids))

    data = await _fetch_phones_data()
    phone_lookup: dict[int, tuple[float, float]] = {
        p[IDX_ID]: (p[IDX_LAT], p[IDX_LON])
        for p in data.get("payphones", [])
        if len(p) > IDX_LAT
    }

    def _resolve_coord(se: StartEnd) -> tuple[float, float]:
        if se.phone_id is not None:
            if se.phone_id not in phone_lookup:
                raise HTTPException(404, f"Phone {se.phone_id} not found")
            return phone_lookup[se.phone_id]
        if se.lat is None or se.lon is None:
            raise HTTPException(400, "start/end must have lat+lon or phone_id")
        return (se.lat, se.lon)

    start_coord = _resolve_coord(req.start)
    end_coord   = _resolve_coord(req.end)

    # Build the coordinate list and job index mapping
    # Layout: [start, phone_0, phone_1, ..., phone_N, end]
    # If start/end snap to a phone_id, remove that phone from the jobs list
    # (the vehicle's presence at start/end covers the capture).
    snapped_start_id = req.start.phone_id
    snapped_end_id   = req.end.phone_id

    job_phone_ids = [
        pid for pid in phone_ids
        if pid != snapped_start_id and pid != snapped_end_id
    ]

    # OSRM's table service rejects requests above --max-table-size coordinates
    if len(job_phone_ids) + 2 > MAX_TABLE_COORDS:
        raise HTTPException(
            400,
            f"Too many phones selected ({len(job_phone_ids)}). The maximum is "
            f"{MAX_TABLE_COORDS - 2} per route — split the selection into smaller batches.",
        )

    # Build ordered coordinate list
    # index 0 = start, 1..N = phones, N+1 = end
    all_coords: list[tuple[float, float]] = [start_coord]
    job_indices: list[tuple[int, int]] = []  # (phone_id, coord_index)

    for pid in job_phone_ids:
        if pid not in phone_lookup:
            raise HTTPException(404, f"Phone {pid} not found in Payphone Tag data")
        coord_idx = len(all_coords)
        all_coords.append(phone_lookup[pid])
        job_indices.append((pid, coord_idx))

    end_idx = len(all_coords)
    all_coords.append(end_coord)

    # OSRM distance/duration matrix
    durations, distances = await _osrm_table(all_coords, req.profile)

    # Fail early with a clear message instead of a cryptic error at the
    # geometry stage when the road network can't connect the selection.
    if durations[0][end_idx] >= UNREACHABLE:
        raise HTTPException(
            400,
            f"Start and end are not connected by {req.profile} routing. "
            "Move an endpoint or switch transport mode.",
        )
    unreachable = [
        pid for pid, ci in job_indices
        if durations[0][ci] >= UNREACHABLE or durations[ci][end_idx] >= UNREACHABLE
    ]
    if unreachable:
        shown = ", ".join(f"#{pid}" for pid in unreachable[:10])
        more  = f" (+{len(unreachable) - 10} more)" if len(unreachable) > 10 else ""
        raise HTTPException(
            400,
            f"{len(unreachable)} selected phone(s) can't be reached by {req.profile}: "
            f"{shown}{more}. Deselect them or switch transport mode.",
        )

    # Vroom TSP solve
    ordered_ids = await _vroom_solve(
        coords=all_coords,
        start_idx=0,
        end_idx=end_idx,
        job_indices=job_indices,
        durations=durations,
        distances=distances,
    )

    # Prepend/append snapped phones back into the final ordered list
    final_order: list[int] = []
    if snapped_start_id is not None:
        final_order.append(snapped_start_id)
    final_order.extend(ordered_ids)
    if snapped_end_id is not None:
        final_order.append(snapped_end_id)

    # Build visit sequence for geometry: start → phone_0 → ... → phone_N → end
    # Each "stop" has (phone_id_or_None, coord)
    visit_coords = [start_coord] + [phone_lookup[pid] for pid in final_order] + [end_coord]
    visit_labels = [None] + final_order + [None]

    features, legs_summary, total_distance, total_duration = \
        await _build_route_geometry(visit_coords, visit_labels, req.profile)

    return {
        "ordered_ids":      final_order,
        "total_distance_m": total_distance,
        "total_duration_s": total_duration,
        "path": {
            "type":     "FeatureCollection",
            "features": features,
        },
        "legs": legs_summary,
    }


@app.post("/api/orienteer")
async def solve_orienteer(req: OrienteerRequest):
    """Time-budget orienteering: find the most phones capturable travelling from start to end
    within req.time_budget_s seconds (includes req.service_time_s per stop for tagging).

    Candidates are filtered to phones within a corridor ellipse between start and end
    (scaled to the time budget and profile speed), capped at req.max_candidates.
    """
    if req.profile not in OSRM_URLS:
        raise HTTPException(400, f"Unknown profile '{req.profile}'. Use: foot, bicycle, car")
    if not (req.include_hostile or req.include_uncaptured):
        raise HTTPException(400, "At least one of include_hostile or include_uncaptured must be true")

    # Clamp client-supplied candidate cap to what the OSRM table service allows
    max_candidates = min(req.max_candidates, MAX_TABLE_COORDS - 2)

    # Build phone lookup
    data = await _fetch_phones_data()
    phone_lookup: dict[int, tuple[float, float]] = {}
    for p in data.get("payphones", []):
        if len(p) > IDX_LAT:
            phone_lookup[p[IDX_ID]] = (p[IDX_LAT], p[IDX_LON])

    # Resolve start coords
    if req.start.phone_id is not None:
        if req.start.phone_id not in phone_lookup:
            raise HTTPException(404, f"Start phone {req.start.phone_id} not found")
        start_lat, start_lon = phone_lookup[req.start.phone_id]
    elif req.start.lat is not None and req.start.lon is not None:
        start_lat, start_lon = req.start.lat, req.start.lon
    else:
        raise HTTPException(400, "start must have lat+lon or phone_id")

    # Resolve end coords
    if req.end.phone_id is not None:
        if req.end.phone_id not in phone_lookup:
            raise HTTPException(404, f"End phone {req.end.phone_id} not found")
        end_lat, end_lon = phone_lookup[req.end.phone_id]
    elif req.end.lat is not None and req.end.lon is not None:
        end_lat, end_lon = req.end.lat, req.end.lon
    else:
        raise HTTPException(400, "end must have lat+lon or phone_id")

    # Classify phones
    my_player_id, cellmate_player_ids, _ = _resolve_player(data, req.username, req.cell or None)

    # Corridor ellipse filter — keep phones where dist(start,p) + dist(p,end) ≤ max_corridor
    # Conservative profile speeds so the ellipse doesn't miss reachable phones
    PROFILE_SPEED_KMH: dict[str, float] = {"foot": 4.0, "bicycle": 12.0, "car": 40.0}
    speed = PROFILE_SPEED_KMH.get(req.profile, 10.0)
    max_corridor_km = (req.time_budget_s / 3600.0) * speed * 1.5  # 1.5× slack factor

    candidates: list[tuple[int, float, float, float]] = []  # (phone_id, lat, lon, corridor_km)

    for p in data.get("payphones", []):
        if len(p) <= IDX_STATUS or p[IDX_STATUS] != "active":
            continue
        status = _classify_phone(p, my_player_id, cellmate_player_ids)
        if status in ("mine", "cellmate"):
            continue
        if status == "hostile"    and not req.include_hostile:
            continue
        if status == "uncaptured" and not req.include_uncaptured:
            continue
        lat, lon   = p[IDX_LAT], p[IDX_LON]
        d_start    = _haversine_km(start_lat, start_lon, lat, lon)
        d_end      = _haversine_km(end_lat,   end_lon,   lat, lon)
        corridor   = d_start + d_end
        if corridor <= max_corridor_km:
            candidates.append((p[IDX_ID], lat, lon, corridor))

    if not candidates:
        raise HTTPException(404, "No candidate phones found within the route corridor and time budget")

    # Cap to nearest-corridor-first N candidates
    if len(candidates) > max_candidates:
        candidates.sort(key=lambda c: c[3])
        candidates = candidates[:max_candidates]

    # Build coordinate list: [start, phone_0…phone_N, end]
    all_coords: list[tuple[float, float]] = [(start_lat, start_lon)]
    job_indices: list[tuple[int, int]] = []
    for phone_id, lat, lon, _ in candidates:
        coord_idx = len(all_coords)
        all_coords.append((lat, lon))
        job_indices.append((phone_id, coord_idx))
    all_coords.append((end_lat, end_lon))
    end_coord_idx = len(all_coords) - 1

    # OSRM matrix
    durations, distances = await _osrm_table(all_coords, req.profile)

    # VROOM solve — A→B with time budget and per-stop service time
    vroom_req = {
        "vehicles": [{
            "id":          0,
            "start_index": 0,
            "end_index":   end_coord_idx,
            "profile":     "driving",
            "time_window": [0, req.time_budget_s],
        }],
        "jobs": [
            {"id": phone_id, "location_index": coord_idx, "service": req.service_time_s}
            for phone_id, coord_idx in job_indices
        ],
        "matrices": {
            "driving": {
                "durations": durations,
                "distances": distances,
            }
        },
    }
    resp = await _http().post(VROOM_URL, json=vroom_req, timeout=120.0)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code", 0) != 0:
        raise HTTPException(502, f"Vroom error: {body.get('error', 'unknown')}")

    routes = body.get("routes", [])
    if not routes:
        raise HTTPException(502, "Vroom returned no routes")

    ordered_ids = [s["id"] for s in routes[0].get("steps", []) if s.get("type") == "job"]

    if not ordered_ids:
        raise HTTPException(404, "No phones can be reached within the time budget")

    # Use VROOM's own duration accounting (includes travel + per-stop service time)
    vroom_travel_s  = routes[0].get("duration", 0)
    vroom_service_s = routes[0].get("service", 0)
    total_duration_vroom = vroom_travel_s + vroom_service_s

    # Build geometry: start → phones → end
    visit_coords = [(start_lat, start_lon)] + [phone_lookup[pid] for pid in ordered_ids] + [(end_lat, end_lon)]
    visit_labels = [None] + ordered_ids + [None]

    features, legs_summary, total_distance, _ = \
        await _build_route_geometry(visit_coords, visit_labels, req.profile)

    return {
        "ordered_ids":      ordered_ids,
        "total_distance_m": total_distance,
        "total_duration_s": total_duration_vroom,
        "path": {
            "type":     "FeatureCollection",
            "features": features,
        },
        "legs": legs_summary,
    }


@app.get("/cert.pem")
async def download_cert():
    """Serve the self-signed CA cert so mobile devices can install it."""
    from fastapi.responses import FileResponse
    if not os.path.isfile(CERT_FILE):
        raise HTTPException(404, "No certificate is configured on this instance")
    return FileResponse(CERT_FILE, media_type="application/x-pem-file",
                        headers={"Content-Disposition": "attachment; filename=payphone-pathfinder.pem"})


@app.get("/")
async def serve_index():
    """Serve index.html with no-cache headers so browsers always get the latest version."""
    from fastapi.responses import FileResponse
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"),
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )


# Static files — MUST be mounted after all API routes.
# Conditional so the module also imports outside Docker (tests, local uvicorn).
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
