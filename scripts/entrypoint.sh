#!/bin/bash
# Container entrypoint — minimal. Validation lives in /health (queryable from
# outside) so this script stays focused on actually starting the server.
#
# Logged banner gives operators a one-line confirmation of the build flavor;
# everything else is delegated to the application.
set -e

# Ensure runtime data dirs exist on the volume. The COPY in the Dockerfile
# made these for the build-time empty volume, but a re-mounted volume from
# a previous deploy may have its own layout — these mkdirs are no-ops in
# that case.
mkdir -p /data/tasks/active /data/tasks/archived /data/tasks/rag/containers \
         /data/memory /data/memory_archive

echo "[tm-server] flavor=${TM_BUILD_FLAVOR:-lite} workspace=${WORKSPACE:-/data}"

# exec replaces this shell so uvicorn becomes PID 1 and receives signals
# directly — important for graceful shutdown via `docker stop`.
exec uvicorn task_rag_server:app \
    --app-dir /app/scripts \
    --host 0.0.0.0 \
    --port 8711
