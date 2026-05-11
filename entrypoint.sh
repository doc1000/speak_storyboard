#!/bin/sh
set -e

DATA_DIR="${STORAGE_DIR:-/data}"

# Seed the volume if the img/ tree is missing (robust against lost+found
# and any other files that may exist on a freshly-formatted ext4 volume).
if [ ! -d "$DATA_DIR/img" ]; then
    echo "[entrypoint] img/ not found: seeding $DATA_DIR from baked-in assets..."
    mkdir -p "$DATA_DIR"
    cp -r /app/img "$DATA_DIR/img"
    [ -f /app/history.json ]     && cp /app/history.json     "$DATA_DIR/history.json"
    [ -f /app/storyboards.json ] && cp /app/storyboards.json "$DATA_DIR/storyboards.json"
    echo "[entrypoint] Seeding complete."
else
    echo "[entrypoint] Volume already seeded, skipping copy."
fi

exec gunicorn --workers=1 --timeout=120 --bind=0.0.0.0:8080 app:app
