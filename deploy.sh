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

# --- Pull new images (must happen before digest comparison) ---
echo ">>> docker compose pull"
docker compose pull

# --- Detect OSRM image change via digest, not tag string ---
# Tag renames (e.g. v26.5.0 -> v26.5.0-amd64-alpine) resolve to the same digest
# and do NOT require reprocessing. Only a genuine version bump does.
if [ "$REPROCESS" = false ]; then
  COMPOSE_IMAGE=$(grep 'image: ghcr.io/project-osrm' docker-compose.yml | head -1 | awk '{print $2}')
  CONTAINER_ID=$(docker compose ps -q osrm-foot 2>/dev/null || echo "")

  if [ -n "$CONTAINER_ID" ]; then
    RUNNING_DIGEST=$(docker inspect "$CONTAINER_ID" --format '{{.Image}}' 2>/dev/null || echo "")
    NEW_DIGEST=$(docker image inspect "$COMPOSE_IMAGE" --format '{{.Id}}' 2>/dev/null || echo "")

    if [ -n "$RUNNING_DIGEST" ] && [ -n "$NEW_DIGEST" ] && [ "$RUNNING_DIGEST" != "$NEW_DIGEST" ]; then
      RUNNING_TAG=$(docker inspect "$CONTAINER_ID" --format '{{.Config.Image}}' 2>/dev/null || echo "unknown")
      echo ""
      echo "WARNING: OSRM image content changed (different digest)"
      echo "  was: $RUNNING_TAG"
      echo "  now: $COMPOSE_IMAGE"
      echo ""
      echo "The routing data must be reprocessed against the new image or the OSRM"
      echo "containers will crash-loop with a fingerprint mismatch error on startup."
      echo "Reprocessing takes ~15-30 min depending on hardware."
      echo ""

      if [ -t 0 ]; then
        read -r -p "Reprocess OSRM data now? Skipping will break routing until you do. [Y/n]: " REPLY
        REPLY="${REPLY:-Y}"
        if [[ "$REPLY" =~ ^[Yy] ]]; then
          REPROCESS=true
        else
          echo ""
          echo "Skipping reprocess. If routing is broken, re-run: ./deploy.sh --reprocess-osrm"
          echo ""
        fi
      else
        echo "Non-interactive mode — skipping reprocess. Run ./deploy.sh --reprocess-osrm manually."
        echo ""
      fi
    fi
  fi
fi

# --- Reprocess OSRM data if requested ---
if [ "$REPROCESS" = true ]; then
  COMPOSE_IMAGE=$(grep 'image: ghcr.io/project-osrm' docker-compose.yml | head -1 | awk '{print $2}')
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

# --- Rebuild and restart ---
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
