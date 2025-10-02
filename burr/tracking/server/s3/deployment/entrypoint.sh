#!/bin/sh
set -x
trap 'echo "Caught signal, continuing anyway"' INT TERM

mkdir -p /root/.burr_server

cd /app

# Initialize aerich if not already done
aerich init -t burr.tracking.server.s3.settings.TORTOISE_ORM --location /root/.burr_server/migrations || true

# Init DB with timeout
timeout 10s aerich init-db || echo "aerich init-db may have already been run"

# Start Burr server
burr --host 0.0.0.0 --backend s3 --port 8000