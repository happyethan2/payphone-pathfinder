# Payphone Pathfinder

A route optimiser for [Payphone Tag](https://payphonetag.com): select phones on the map, set a start and end point, and it finds the best order to visit them all. Phones are colour-coded by who holds them, and a finished route opens leg by leg in Google Maps. It is self-hosted with Docker and routes across all of Australia.

## Prerequisites

- Docker with Docker Compose
- ~16 GB free disk space
- On Windows: Git Bash or WSL

## Install

```bash
git clone https://github.com/happyethan2/payphone-pathfinder.git
cd payphone-pathfinder
cp .env.example .env   # then set CARTO_API_KEY (free key: https://carto.com/basemaps/apikey)

mkdir -p osrm-data/{foot,bicycle,car}
curl -L -o osrm-data/foot/australia-latest.osm.pbf \
  https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf
cp osrm-data/foot/australia-latest.osm.pbf osrm-data/bicycle/
cp osrm-data/foot/australia-latest.osm.pbf osrm-data/car/

./deploy.sh --reprocess-osrm   # builds the routing data (1–3 hours), then starts everything
```

On Windows Git Bash, run the last line as `MSYS_NO_PATHCONV=1 ./deploy.sh --reprocess-osrm`.

Open **http://localhost:8000**. Keep the `.osm.pbf` files: `deploy.sh` needs them to rebuild the routing data later.

---

## Extra details

### Updating

```bash
./deploy.sh
```

This pulls the latest code and images, rebuilds the backend, and runs a health check. If the OSRM image has changed, it asks to rebuild the routing data. Say yes, or OSRM will crash-loop on startup. To force a rebuild, run `./deploy.sh --reprocess-osrm`.

### CARTO basemap key

Since August 2026, CARTO requires an API key for its basemaps. Without one, the map still works, but every tile is stamped "API KEY REQUIRED". The key is free, needs no account, and allows 5M tiles a month.

- Each instance needs its own key, because the quota is per key.
- The key travels in tile URLs, so it isn't secret. Still, keep it in `.env` (gitignored), never in `.env.example`.
- Leave the CARTO and OpenStreetMap attribution on the map. That is a condition of the free tier.

### Phones and GPS

GPS needs HTTPS. The mobile steps below are for iOS.

**Tailscale (easiest):** run `tailscale serve --bg 8000` and open your Tailscale HTTPS URL.

**Self-signed cert (direct IP access):** generate a cert into `backend/certs/`. The backend serves HTTPS automatically when a cert is there.

```bash
mkdir -p backend/certs
docker run --rm -v "$(pwd)/backend/certs:/certs" alpine/openssl \
  req -x509 -newkey rsa:2048 -days 365 -nodes \
  -keyout /certs/key.pem -out /certs/cert.pem \
  -subj '/CN=payphone-pathfinder' \
  -addext "subjectAltName=IP:YOUR_LOCAL_IP,IP:YOUR_TAILSCALE_IP,IP:127.0.0.1" \
  -addext "extendedKeyUsage=serverAuth"
docker compose restart backend
```

Then, on iOS:

1. Open `https://YOUR_IP:8000/cert.pem` in Safari and install the profile.
2. Turn the cert on under **Settings → General → About → Certificate Trust Settings**. This is the step people miss.

### Address search

The start and end fields search addresses with [Photon](https://github.com/komoot/photon), through this app's backend (`/api/geocode`). By default that's komoot's free public instance at photon.komoot.io. It's fine for light use; searches are cached for a day and limited to Australia.

A busy instance should run its own Photon and set `PHOTON_URL` in `.env` (for example `PHOTON_URL=http://photon:2322`). GraphHopper publishes a weekly Photon database for Australia/Oceania (about 2 GB); see Photon's docs for setup. Without address search, tapping **Choose on Map** still works.

### Share links

The share button next to **Find Optimal Route** copies a link to your current plan. The link holds the transport mode, start, end, and every selected phone. Opening it replaces the recipient's own selection.

The recipient doesn't need a username: phones load uncoloured until they enter one. The link leaves out your username and the solved route, so the recipient re-solves it for their own game state. Phone IDs are the same for every player, so a link works on any instance running this version.

### Running tests

The backend tests mock all upstream services, so they need no Docker or network access. They also run in GitHub Actions on every push and pull request.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -v
```

### Troubleshooting

- **OSRM commands fail on Windows:** prefix them with `MSYS_NO_PATHCONV=1`.
- **GPS denied on iOS:** go to Settings → Privacy & Security → Location Services → Safari, and set it to "While Using".
- **Tiles say "API KEY REQUIRED":** `CARTO_API_KEY` isn't set. If you just set it, hard-refresh, because old tiles can stay cached. Check what the server sends with `curl http://localhost:8000/api/config.js`.
- **"A routing service (OSRM or Vroom) did not respond":** a routing container is down or still loading. OSRM takes a minute or two to load after startup. Check `docker compose ps` and `docker compose logs osrm-car vroom`.
- **Vroom returns no routes:** the selected phones can't be reached with that transport mode. Try another mode or area.
- **OSRM fingerprint mismatch on startup:** the routing data doesn't match the OSRM image. Run `./deploy.sh --reprocess-osrm`.
- **Can't reach the app over Tailscale on Linux:** Docker's iptables rules can block it. Run:
  ```bash
  sudo iptables -I FORWARD -i tailscale0 -j ACCEPT
  sudo iptables -I FORWARD -o tailscale0 -j ACCEPT
  ```
