#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

REPROCESS=false
for arg in "$@"; do
  case $arg in
    --reprocess-osrm) REPROCESS=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# --- Pull latest code ---
echo ">>> git pull"
git pull

# --- Detect OSRM image change ---
COMPOSE_IMAGE=$(grep 'image: ghcr.io/project-osrm' docker-compose.yml | head -1 | awk '{print $2}')
RUNNING_IMAGE=$(docker inspect "$(docker compose ps -q osrm-foot 2>/dev/null)" --format '{{.Config.Image}}' 2>/dev/null || echo "")

if [ -n "$RUNNING_IMAGE" ] && [ "$RUNNING_IMAGE" != "$COMPOSE_IMAGE" ]; then
  echo ""
  echo "WARNING: OSRM image changed"
  echo "  was: $RUNNING_IMAGE"
  echo "  now: $COMPOSE_IMAGE"
  echo ""
  if [ "$REPROCESS" = false ]; then
    echo "If the app crashes (Fingerprint mismatch), re-run with --reprocess-osrm"
    echo ""
  fi
fi

# --- Reprocess OSRM data if requested ---
if [ "$REPROCESS" = true ]; then
  echo ">>> Reprocessing OSRM data with $COMPOSE_IMAGE"
  docker compose down osrm-foot osrm-bicycle osrm-car 2>/dev/null || true

  for PROFILE in foot bicycle car; do
    DATA_DIR="$(pwd)/osrm-data/${PROFILE}"
    PBF="$DATA_DIR/australia-latest.osm.pbf"
    if [ ! -f "$PBF" ]; then
      echo "ERROR: PBF not found at $PBF — cannot reprocess $PROFILE"
      exit 1
    fi
    echo "  -> $PROFILE"
    docker run --rm -v "$DATA_DIR:/data" "$COMPOSE_IMAGE" osrm-extract -p "/opt/${PROFILE}.lua" /data/australia-latest.osm.pbf
    docker run --rm -v "$DATA_DIR:/data" "$COMPOSE_IMAGE" osrm-partition /data/australia-latest.osrm
    docker run --rm -v "$DATA_DIR:/data" "$COMPOSE_IMAGE" osrm-customize /data/australia-latest.osrm
  done
fi

# --- Pull new images and rebuild ---
echo ">>> docker compose pull"
docker compose pull

echo ">>> docker compose up --build -d"
docker compose up --build -d

# --- Health check ---
echo ">>> Waiting for containers to stabilise..."
sleep 8

echo ""
docker compose ps
echo ""

RESTARTING=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -i "restart" | awk '{print $1}' || true)
if [ -n "$RESTARTING" ]; then
  echo "WARNING: These containers are crash-looping:"
  echo "$RESTARTING"
  echo ""
  echo "Check logs: docker compose logs <service-name>"
  exit 1
fi

echo "All containers running OK"
