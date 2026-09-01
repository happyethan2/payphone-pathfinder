# Payphone Pathfinder

A route optimiser for [Payphone Tag](https://payphonetag.com). Select phones on the map, set a start and end point, and it finds the optimal order to visit all of them.

Phones are colour-coded by who holds them — yours, your cell's, hostile, and uncaptured. You can click to select individual phones or draw a lasso around a group. Once you've got a route, it splits it into legs and opens each one directly in Google Maps.

Runs locally using Docker.

Sorry Android users, instructions for mobile setup are for iOS.

## Stack

- **Backend**: Python + FastAPI
- **Routing**: OSRM (self-hosted, full Australia dataset)
- **Optimiser**: Vroom (TSP solver)
- **Frontend**: Single HTML file, Leaflet.js

---

## Setup

### Prerequisites

- Docker + Docker Compose
- ~16 GB free disk space
- Git Bash or WSL on Windows

### 1. Clone and configure

```bash
git clone https://github.com/happyethan2/payphone-pathfinder.git
cd payphone-pathfinder
cp .env.example .env
```

Edit `.env` with your Payphone Tag username and cell tag. You can also just type them in the UI at runtime.

**Get a CARTO basemap key.** As of August 2026 CARTO requires an API key for their raster
basemaps. Without one the map still works, but every tile is stamped "API KEY REQUIRED".
Grab a free key at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) — no
account or approval needed, 5,000,000 tiles/month — and put it in `.env`:

```
CARTO_API_KEY=your_key_here
```

Each instance needs its own key; don't reuse someone else's, since the quota is per-key.
The key is served to the browser (it has to be — it travels in the tile URLs), so it isn't
a secret, but keep it in `.env`, which is gitignored, and never in `.env.example`. Please
also leave the CARTO and OpenStreetMap attribution on the map: that's the condition of the
free tier.

### 2. Preprocess OSRM data

Download the Australia PBF and run preprocessing for each profile. Takes 30–60 min per profile — run all three in separate terminals at the same time.

```bash
mkdir -p osrm-data/{foot,bicycle,car}
wget -O australia-latest.osm.pbf \
  "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
```

**Windows (Git Bash): prefix every `docker run` with `MSYS_NO_PATHCONV=1`** or the path to the lua profile gets mangled.

```bash
# Foot
cp australia-latest.osm.pbf osrm-data/foot/
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend:v26.5.0-amd64-alpine osrm-extract -p /opt/foot.lua /data/australia-latest.osm.pbf
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend:v26.5.0-amd64-alpine osrm-partition /data/australia-latest.osrm
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend:v26.5.0-amd64-alpine osrm-customize /data/australia-latest.osrm
rm osrm-data/foot/australia-latest.osm.pbf
```

Repeat swapping `foot` → `bicycle` / `bicycle.lua`, and `foot` → `car` / `car.lua`.

### 3. Start

```bash
docker compose up -d
```

Open **http://localhost:8000**. Give the OSRM containers a couple of minutes on first start to load the dataset.

---

## Deploying updates

Use the deploy script instead of running Docker commands manually. It pulls the latest code, rebuilds the backend, pulls new images, and does a health check afterward.

```bash
./deploy.sh
```

If the OSRM image version has changed since the last deploy, the script will detect it and ask whether to reprocess. Say yes — skipping it will cause OSRM to crash-loop on startup. Reprocessing takes 15–30 min but only happens when the image actually changes, not on every deploy.

If you ever need to force a reprocess manually:

```bash
./deploy.sh --reprocess-osrm
```

---

## Running tests

The backend has a pytest suite covering the API endpoints and the game-API
failure handling (stale-cache fallback, retry/cooldown behaviour, request
coalescing). All upstream services are mocked — no Docker or network needed.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -v
```

The same suite runs in GitHub Actions on every push and pull request.

---

## Mobile + GPS

GPS needs HTTPS. Two options:

**Tailscale serve** (easiest):
```bash
tailscale serve --bg 8000
```
Access via your Tailscale HTTPS URL. GPS works without any cert setup.

**Self-signed cert** (for direct IP access):
```bash
mkdir -p backend/certs
docker run --rm -v "$(pwd)/backend/certs:/certs" alpine/openssl \
  req -x509 -newkey rsa:2048 -days 365 -nodes \
  -keyout /certs/key.pem -out /certs/cert.pem \
  -subj '/CN=payphone-pathfinder' \
  -addext "subjectAltName=IP:YOUR_LOCAL_IP,IP:YOUR_TAILSCALE_IP,IP:127.0.0.1" \
  -addext "extendedKeyUsage=serverAuth"
```

Update `backend/Dockerfile` to copy the cert and pass it to uvicorn, then rebuild.

On iOS you have to do two things or it won't work:
1. Open `http://YOUR_IP:8000/cert.pem` in Safari and install the profile
2. Go to **Settings → General → About → Certificate Trust Settings** and enable it

Step 2 is the one people miss.

---

## Troubleshooting

**OSRM preprocessing fails on Windows** — add `MSYS_NO_PATHCONV=1` before every `docker run`.

**GPS denied on iOS** — Settings → Privacy & Security → Location Services → Safari/Firefox → set to "While Using".

**Map tiles say "API KEY REQUIRED"** — this instance has no `CARTO_API_KEY` set. See
[step 1](#1-clone-and-configure). If you've just added one and the watermark is still there,
hard-refresh: both your browser and CARTO's CDN cache tiles, so an old one can linger. You
can check what the server is handing out with `curl http://localhost:8000/api/config.js`.

**"A routing service (OSRM or Vroom) did not respond"** — one of the routing containers is
down, restarting, or still loading the Australia dataset. Check `docker compose ps` and
`docker compose logs osrm-car vroom`.

**OSRM slow to respond after startup** — it needs a minute or two to load the Australia dataset into memory.

**Vroom returns no routes** — usually means the selected phones aren't reachable under the chosen transport mode. Try switching profiles or selecting a different area.

**OSRM fingerprint mismatch on startup** — the preprocessed data doesn't match the running image version. Re-run `./deploy.sh --reprocess-osrm`. Make sure you used the same image tag listed in `docker-compose.yml` when you first preprocessed.

**Can't reach the app via Tailscale on Linux** — Docker's iptables can block traffic from the Tailscale interface:
```bash
sudo iptables -I FORWARD -i tailscale0 -j ACCEPT
sudo iptables -I FORWARD -o tailscale0 -j ACCEPT
```
