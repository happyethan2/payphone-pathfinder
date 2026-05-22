# Payphone Pathfinder

A route optimiser for [Payphone Tag](https://payphonetag.com). Select a bunch of phones on the map, set a start and end point, and it figures out the optimal order to visit all of them. Phones are colour-coded by ownership so you can see what's yours, your cell's, and what's hostile at a glance.

Built this because manually planning routes was getting tedious — lasso a cluster of phones, hit find route, and it sends legs straight to Google Maps for CarPlay navigation.

![screenshot](docs/route_screenshot.png)

## How it works

- Pulls live phone data from the Payphone Tag API and colour-codes by ownership
- Uses [OSRM](https://project-osrm.org/) for real-world routing (foot, bike, car)
- Uses [Vroom](https://github.com/VROOM-Project/vroom) to solve the travelling salesman problem and find the optimal visit order
- Splits long routes into legs of ≤9 stops and opens each directly in Google Maps

Everything runs locally via Docker. No cloud services, no API keys required.

## Stack

- **Backend**: Python + FastAPI
- **Routing**: OSRM (self-hosted, full Australia dataset)
- **Optimiser**: Vroom
- **Frontend**: Single HTML file, Leaflet.js + OpenStreetMap

---

## Self-hosting

### Prerequisites

- Docker + Docker Compose
- ~16 GB free disk space (OSRM data for all three profiles)
- Git Bash or WSL on Windows (the preprocessing commands use bash syntax)

### 1. Clone and configure

```bash
git clone https://github.com/happyethan2/payphone-pathfinder.git
cd payphone-pathfinder
cp .env.example .env
```

Edit `.env` with your Payphone Tag username and cell tag. These just pre-populate the identity fields in the UI — you can also type them in manually at runtime.

### 2. Preprocess OSRM data

Download the Australia PBF (~1.4 GB) and run the three-stage preprocessing for each transport profile. Each profile takes 30–60 minutes — run all three in parallel to save time.

```bash
mkdir -p osrm-data/{foot,bicycle,car}
wget -O australia-latest.osm.pbf \
  "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
```

**On Windows (Git Bash), prefix every `docker run` below with `MSYS_NO_PATHCONV=1`** — otherwise Git Bash mangles the `/opt/foot.lua` path and OSRM fails.

Run each block in a separate terminal:

```bash
# Foot
cp australia-latest.osm.pbf osrm-data/foot/
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /opt/foot.lua /data/australia-latest.osm.pbf
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/australia-latest.osrm
docker run --rm -v "$(pwd)/osrm-data/foot:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/australia-latest.osrm
rm osrm-data/foot/australia-latest.osm.pbf

# Bicycle (same pattern, swap foot → bicycle, foot.lua → bicycle.lua)

# Car (same pattern, swap foot → car, foot.lua → car.lua)
```

### 3. Start

```bash
docker compose up -d
```

Open **http://localhost:8000**. The OSRM containers take a minute or two to load the Australia dataset on first start.

---

## Mobile access + GPS

GPS requires HTTPS. The app runs HTTP by default, which is fine for desktop (Chrome allows GPS on localhost over HTTP). For mobile you have two options:

**Option A — Tailscale serve** (easiest, no cert warnings):
```bash
tailscale serve --bg 8000
```
Access via your machine's Tailscale HTTPS URL. GPS works straight away.

**Option B — Self-signed cert with IP SANs**:

Generate a cert for your machine's IPs:
```bash
mkdir -p backend/certs
docker run --rm -v "$(pwd)/backend/certs:/certs" alpine/openssl \
  req -x509 -newkey rsa:2048 -days 365 -nodes \
  -keyout /certs/key.pem -out /certs/cert.pem \
  -subj '/CN=payphone-pathfinder' \
  -addext "subjectAltName=IP:YOUR_LOCAL_IP,IP:YOUR_TAILSCALE_IP,IP:127.0.0.1" \
  -addext "extendedKeyUsage=serverAuth"
```

Then update `backend/Dockerfile` to use the cert:
```dockerfile
COPY certs/cert.pem certs/key.pem ./
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--ssl-keyfile", "/app/key.pem", "--ssl-certfile", "/app/cert.pem"]
```

Rebuild with `docker compose up -d --build backend`.

On iOS you need to install **and** trust the cert:
1. Navigate to `http://YOUR_IP:8000/cert.pem` in Safari → install the profile
2. **Settings → General → About → Certificate Trust Settings → toggle on**

Step 2 is easy to miss. If Safari won't let you past the "not private" warning, that's why.

---

## Troubleshooting

**OSRM preprocessing fails on Windows** — add `MSYS_NO_PATHCONV=1` before every `docker run` command.

**GPS denied on iOS** — Settings → Privacy & Security → Location Services → Safari/Firefox → set to "While Using".

**OSRM not responding after startup** — give it 2 minutes, it's loading the Australia dataset into memory.

**Vroom returns no routes** — some phone combinations are unreachable on certain profiles (e.g. pedestrian paths on car mode). Try a different transport mode or a smaller selection.

**Containers not accessible via Tailscale on Linux** — Docker's iptables rules can block traffic from the Tailscale interface. Fix with:
```bash
sudo iptables -I FORWARD -i tailscale0 -j ACCEPT
sudo iptables -I FORWARD -o tailscale0 -j ACCEPT
```
