# Payphone Pathfinder

A route optimiser for [Payphone Tag](https://payphonetag.com). Select phones on the map, set a start and end point, and it finds the optimal order to visit all of them.

Phones are colour-coded by who holds them — yours, your cell's, hostile, and uncaptured. You can click to select individual phones or draw a lasso around a group. Once you've got a route, it splits it into legs and opens each one directly in Google Maps.

Runs locally using Docker.

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
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /opt/foot.lua /data/australia-latest.osm.pbf
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/australia-latest.osrm
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/australia-latest.osrm
rm osrm-data/foot/australia-latest.osm.pbf
```

Repeat swapping `foot` → `bicycle` / `bicycle.lua`, and `foot` → `car` / `car.lua`.

### 3. Start

```bash
docker compose up -d
```

Open **http://localhost:8000**. Give the OSRM containers a couple of minutes on first start to load the dataset.

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

**OSRM slow to respond after startup** — it needs a minute or two to load the Australia dataset into memory.

**Vroom returns no routes** — usually means the selected phones aren't reachable under the chosen transport mode. Try switching profiles or selecting a different area.

**Can't reach the app via Tailscale on Linux** — Docker's iptables can block traffic from the Tailscale interface:
```bash
sudo iptables -I FORWARD -i tailscale0 -j ACCEPT
sudo iptables -I FORWARD -o tailscale0 -j ACCEPT
```
