#!/bin/sh
set -x

mkdir -p /root/.burr_server

cd /app

# Initialize aerich if not already done
aerich init -t burr.tracking.server.s3.settings.TORTOISE_ORM --location /root/.burr_server/migrations || true

# Make the database's tables. With a snapshot in S3 (BURR_LOAD_SNAPSHOT_ON_START), the server
# replaces this database with the snapshot when it starts.
timeout 10s aerich init-db || echo "aerich init-db may have already been run"

# nginx forwards port 80 to uvicorn. It runs as a daemon, in the background.
nginx

# uvicorn is PID 1, so it gets SIGTERM (docker stop, a Kubernetes pod stop). It stops the event
# consumer, saves a last snapshot and closes the database. Open connections get 10 s, so the
# snapshot has time before the usual 30 s grace period ends.
exec uvicorn burr.tracking.server.run:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10
