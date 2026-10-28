# Payphone Pathfinder

Payphone Pathfinder plans routes for [Payphone Tag](https://payphonetag.com) anywhere in Australia, on foot, by bicycle or by car. You select phones on the map. The app calculates the fastest order to visit them, then guides you from stop to stop. You host it yourself with Docker.

## Requirements

- Docker with Docker Compose
- About 16 GB of free disk space
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

./deploy.sh --reprocess-osrm   # builds the routing data (1–3 hours), then starts the app
```

On Windows Git Bash, run the last command as `MSYS_NO_PATHCONV=1 ./deploy.sh --reprocess-osrm`.

Open **http://localhost:8000**. Do not delete the `.osm.pbf` files. `deploy.sh` needs them to rebuild the routing data.

## Use

1. Enter your in-game name with the top-right button. Without a name, the app cannot show which phones you or your cellmates hold.
2. Select phones. Tap a phone, or use **Lasso** to draw around a group.
3. Set a start point. The end point is optional. Without an end point, the route finishes at the last phone.
4. Tap **Find Optimal Route**.
5. Follow the route. **Open in Google Maps** opens the current leg. When the game shows that you hold the current phone, the app marks the stop as done and moves to the next stop. **Skip** moves to the next stop without tagging.

**Orienteering** mode chooses the phones for you. It finds the most phones you can tag between the start and the end before the **Arrive By** time. It needs your in-game name and an end point.

The gear button opens **Settings**: highlight filters, map style, and navigation options. The **ⓘ** button in Settings opens the map legend.

---

## Details

### Updating

```bash
./deploy.sh
```

This command pulls the latest code and images, rebuilds the backend, and runs a health check. It stops if tracked files have local changes.

If the OSRM image has changed, the command asks to rebuild the routing data. Answer yes. If you answer no, OSRM crash-loops on startup. To rebuild the routing data at any time, run `./deploy.sh --reprocess-osrm`.

### CARTO basemap key

Since August 2026, CARTO requires an API key for its basemaps. Without a key, the map works, but every tile shows "API KEY REQUIRED". The key is free, needs no account, and allows 5 million tiles per month.

- Each instance needs its own key. The quota is per key.
- The key is visible in tile URLs, so it is not secret. Keep it in `.env`, which git ignores. Do not put it in `.env.example`.
- Keep the CARTO and OpenStreetMap attribution on the map. The free tier requires it.

### GPS on phones

Browsers allow GPS only over HTTPS. Use one of these two methods. The phone steps are for iOS.

**Tailscale:** run `tailscale serve --bg 8000`, then open your Tailscale HTTPS URL.

**Self-signed certificate (access by IP address):** create a certificate in `backend/certs/`. When a certificate is there, the backend serves HTTPS.

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

Then, on the iPhone:

1. Open `https://YOUR_IP:8000/cert.pem` in Safari and install the profile.
2. Go to **Settings → General → About → Certificate Trust Settings** and turn the certificate on. Step 2 is required. Installing the profile alone is not enough.

### Indoor phones

`backend/data/hints.json` lists phones that are probably indoors or probably never open to the public. It can also list other phones that need a warning, for example a phone in a remote community. The app uses it in three places:

- The **Indoor** filter adds a dashed ring to the phones that are probably indoors or never public.
- A route stop for one of these phones always has a dashed ring.
- During navigation, the current stop shows up to three lines:
  1. "Probably Indoors", "Possibly Not Public" or "Probably Locked", with a confidence level (High or Medium). The line is red and says "Probably Locked" outside the phone's hours or on a day it is probably closed.
  2. The type of place and a warning, for example "University · May be closed weekends".
  3. The hours: listed opening hours from OpenStreetMap when they are known, otherwise the hours when players usually tag the phone, for example "Est. 08:00–21:00 from 42 captures". The estimate is a rough guide only.

The file is a fixed snapshot. This repository does not include the tool that made it. To change the list, edit the file, then run `./deploy.sh`. The file is built into the backend image, so a restart alone does not load a new file. Format:

```json
{"phones": {"<phone id>": {"kind": "indoor", "conf": "high", "place": "University", "warn": "May be closed weekends",
                           "days": [0, 1, 2, 3, 4], "listed": null, "open": [8, 21], "n": 42}}}
```

- `kind`: `indoor` (probably indoors), `never` (probably never public), or `null` for a phone that is only listed for its warning.
- `conf`: `high` or `medium`; `null` when `kind` is `null`.
- `place`: a short name for the type of place, or `null`.
- `warn`: a short warning, or `null`.
- `days`: the days the phone is probably reachable, Monday = 0 to Sunday = 6, or `null` for every day.
- `listed`: opening hours from OpenStreetMap: 7 lists (Monday first) of `[from, to]` in minutes after midnight, or `null`. An empty list means closed that day.
- `open`: `[from_hour, to_hour]` estimated from capture times, or `null` if unknown. The range can cross midnight, for example `[22, 6]`.
- `n`: the number of captures the estimate is based on.

All times are the phone's local time. The app compares them with the device's clock.

### Address search

The start and end fields search for addresses with [Photon](https://github.com/komoot/photon), through this app's backend (`/api/geocode`). By default, the backend uses komoot's free public server at photon.komoot.io. That server is for light use. The backend caches each search for one day and limits results to Australia.

An instance with many users should run its own Photon server and set `PHOTON_URL` in `.env`, for example `PHOTON_URL=http://photon:2322`. GraphHopper publishes a weekly Photon database for Australia and Oceania (about 2 GB). See the Photon documentation for setup. If address search is unavailable, **Choose on Map** still works.

### Share links

The share button next to **Find Optimal Route** copies a link to your current plan. During navigation, the share icon at the top of the navigation card does the same. The link contains the transport mode, the start, the end, and every selected phone. Opening the link replaces the recipient's own selection.

The link does not contain your in-game name or the calculated route. The recipient's app calculates the route again from the recipient's own game data. The recipient does not need a name to open the link. Phone IDs are the same for every player, so a link works on any instance that runs this version.

### Running tests

The backend tests replace every external service with a mock, so they need no Docker and no network access. GitHub Actions runs them on every push and pull request.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -v
```

### Troubleshooting

- **OSRM commands fail on Windows:** put `MSYS_NO_PATHCONV=1` before the command.
- **GPS is denied on iOS:** go to **Settings → Privacy & Security → Location Services → Safari** and choose "While Using".
- **Tiles show "API KEY REQUIRED":** `CARTO_API_KEY` is not set. If you set it recently, do a hard refresh, because the browser can keep old tiles. To see what the server sends, run `curl http://localhost:8000/api/config.js`.
- **"A routing service (OSRM or Vroom) did not respond":** a routing container is stopped or still loading. OSRM needs one to two minutes to load after startup. Check `docker compose ps` and `docker compose logs osrm-car vroom`.
- **Vroom returns no routes:** the selected phones cannot be reached with that transport mode. Try a different mode or area.
- **OSRM reports a fingerprint mismatch on startup:** the routing data does not match the OSRM image. Run `./deploy.sh --reprocess-osrm`.
- **The app is not reachable over Tailscale on Linux:** Docker's iptables rules can block the traffic. Run:
  ```bash
  sudo iptables -I FORWARD -i tailscale0 -j ACCEPT
  sudo iptables -I FORWARD -o tailscale0 -j ACCEPT
  ```
