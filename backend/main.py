import os
import re
import time
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Payphone Pathfinder")

OSRM_URLS = {
    "foot":    os.getenv("OSRM_FOOT_URL",    "http://osrm-foot:5000"),
    "bicycle": os.getenv("OSRM_BICYCLE_URL", "http://osrm-bicycle:5000"),
    "car":     os.getenv("OSRM_CAR_URL",     "http://osrm-car:5000"),
}
VROOM_URL    = os.getenv("VROOM_URL", "http://vroom:3000")
PAYPHONE_API = "https://payphonetag.com/api/payphones"
WIKI_API     = "https://wiki.payphonetag.com/api.php"

# Payphone tuple indices — adjust here if the live API changes
IDX_ID     = 0
IDX_LON    = 1
IDX_LAT    = 2
IDX_HOLDER = 3
IDX_STATUS = 4

# Sentinel for unreachable OSRM pairs (must fit in Vroom's int32)
UNREACHABLE = 999_999_999

# ---------------------------------------------------------------------------
# Phone data cache — one upstream fetch per TTL window, all clients share it
# ---------------------------------------------------------------------------
_PHONES_CACHE: dict | None = None
_PHONES_CACHE_AT: float    = 0.0
_PHONES_CACHE_TTL: float   = 30.0  # seconds

# ---------------------------------------------------------------------------
# Wiki photo cache — paginate allimages once per TTL, extract phone IDs
# ---------------------------------------------------------------------------
_WIKI_CACHE: set | None = None
_WIKI_CACHE_AT: float   = 0.0
_WIKI_CACHE_TTL: float  = 300.0  # 5 minutes


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


async def _fetch_phones_data() -> dict:
    """Return raw payphone API data, served from a 30s server-side cache."""
    global _PHONES_CACHE, _PHONES_CACHE_AT
    now = time.monotonic()
    if _PHONES_CACHE is not None and (now - _PHONES_CACHE_AT) < _PHONES_CACHE_TTL:
        return _PHONES_CACHE
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(PAYPHONE_API)
        resp.raise_for_status()
    _PHONES_CACHE    = resp.json()
    _PHONES_CACHE_AT = time.monotonic()
    return _PHONES_CACHE


async def _fetch_wiki_photo_ids() -> set[int]:
    """Return the set of sequential payphone API IDs that have at least one wiki photo.

    Two-step process:
      1. Paginate allimages?aiprefix=Payphone- to collect unique CAB codes
         (e.g. "08835506X2") from filenames like Payphone-08835506X2-timestamp.jpg
      2. Batch-fetch the corresponding wiki pages (50 at a time) and parse the
         `| id = XXXX` template field, which holds the sequential payphone API ID.
    """
    global _WIKI_CACHE, _WIKI_CACHE_AT
    now = time.monotonic()
    if _WIKI_CACHE is not None and (now - _WIKI_CACHE_AT) < _WIKI_CACHE_TTL:
        return _WIKI_CACHE

    # ── Step 1: collect unique CAB codes from all uploaded images ──
    cab_ids: set[str] = set()
    img_params: dict = {
        "action":   "query",
        "list":     "allimages",
        "aiprefix": "Payphone-",
        "ailimit":  "500",
        "format":   "json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            resp = await client.get(WIKI_API, params=img_params)
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
    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(cab_list), batch_size):
            batch  = cab_list[i : i + batch_size]
            titles = "|".join(f"Payphone:{cab}" for cab in batch)
            resp = await client.get(WIKI_API, params={
                "action":  "query",
                "titles":  titles,
                "prop":    "revisions",
                "rvprop":  "content",
                "rvslots": "*",
                "format":  "json",
            })
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

    _WIKI_CACHE    = has_photo
    _WIKI_CACHE_AT = time.monotonic()
    return _WIKI_CACHE


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
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, params={"annotations": "duration,distance"})
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params={
            "overview": "full",
            "geometries": "geojson",
            "steps": "true",
        })
        resp.raise_for_status()
    body = resp.json()
    if body.get("code") != "Ok" or not body.get("routes"):
        raise HTTPException(502, f"OSRM route error: {body.get('message')}")
    return body["routes"][0]


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
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(VROOM_URL, json=vroom_req)
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
    """Force-expire the phone data cache so the next /api/phones call re-fetches upstream."""
    global _PHONES_CACHE_AT
    _PHONES_CACHE_AT = 0.0
    return {"ok": True}


@app.get("/api/wiki-photos")
async def get_wiki_photos():
    """Return IDs of phones that have at least one user photo on the payphonetag wiki."""
    ids = await _fetch_wiki_photo_ids()
    return {"has_photo": sorted(ids)}


@app.get("/api/phones")
async def get_phones(
    username: str = Query(...),
    cell: str = Query(default=""),
):
    data = await _fetch_phones_data()
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

    return {"phones": result}


@app.post("/api/route")
async def solve_route(req: RouteRequest):
    if req.profile not in OSRM_URLS:
        raise HTTPException(400, f"Unknown profile '{req.profile}'. Use: foot, bicycle, car")
    if not req.phone_ids:
        raise HTTPException(400, "phone_ids must not be empty")

    # Resolve start/end coords — if phone_id given, look them up
    phone_lookup: dict[int, tuple[float, float]] = {}
    if req.start.phone_id is not None or req.end.phone_id is not None:
        data = await _fetch_phones_data()
        for p in data.get("payphones", []):
            if len(p) > IDX_LAT:
                phone_lookup[p[IDX_ID]] = (p[IDX_LAT], p[IDX_LON])

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
        pid for pid in req.phone_ids
        if pid != snapped_start_id and pid != snapped_end_id
    ]

    # Fetch coords for job phones
    if job_phone_ids and not phone_lookup:
        data = await _fetch_phones_data()
        for p in data.get("payphones", []):
            if len(p) > IDX_LAT:
                phone_lookup[p[IDX_ID]] = (p[IDX_LAT], p[IDX_LON])

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

    # Fetch per-leg geometry from OSRM
    features = []
    total_distance = 0.0
    total_duration = 0.0
    legs_summary = []

    for i in range(len(visit_coords) - 1):
        leg = await _osrm_route(visit_coords[i], visit_coords[i + 1], req.profile)
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


@app.get("/cert.pem")
async def download_cert():
    """Serve the self-signed CA cert so mobile devices can install it."""
    from fastapi.responses import FileResponse
    return FileResponse("/app/cert.pem", media_type="application/x-pem-file",
                        headers={"Content-Disposition": "attachment; filename=payphone-pathfinder.pem"})


@app.get("/")
async def serve_index():
    """Serve index.html with no-cache headers so browsers always get the latest version."""
    from fastapi.responses import FileResponse
    return FileResponse(
        "/app/frontend/index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )


# Static files — MUST be mounted after all API routes
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
