#!/bin/bash
# Container entrypoint.
#
# Two responsibilities:
#   1. Ensure /data is writable by the runtime user. Volumes carried over from
#      pre-v0.6 deploys can contain root-owned files that UID 10001 cannot
#      open — manifests as `sqlite3.OperationalError: unable to open database
#      file` on first start of the new image. We chown idempotently.
#   2. Start uvicorn as the unprivileged user (`tm`, UID 10001) via gosu so
#      it becomes PID 1 and receives signals directly (graceful `docker stop`).
#
# When the container is launched explicitly as a non-root user (compose
# `user:` override) we skip the privileged-only steps and just start uvicorn.
set -e

UID_TARGET="${TM_RUN_AS_UID:-10001}"
GID_TARGET="${TM_RUN_AS_GID:-10001}"

ensure_data_dirs() {
    mkdir -p /data/tasks/active /data/tasks/archived /data/tasks/rag/containers \
             /data/memory /data/memory_archive
}

if [ "$(id -u)" = "0" ]; then
    ensure_data_dirs
    # chown -R is idempotent; cost on a fresh volume is microseconds, on a
    # volume carried over from a root-owned pre-v0.6 install it heals the
    # ownership in one pass. Errors don't abort startup — operator can run
    # the container as root by setting TM_RUN_AS_UID=0 if they need to.
    chown -R "${UID_TARGET}:${GID_TARGET}" /data 2>/dev/null || \
        echo "[tm-server] WARNING: chown /data failed; continuing (volume may be on a host fs that disallows chown)"
    echo "[tm-server] flavor=${TM_BUILD_FLAVOR:-lite} workspace=${WORKSPACE:-/data} run-as=${UID_TARGET}:${GID_TARGET}"
    exec gosu "${UID_TARGET}:${GID_TARGET}" uvicorn task_rag_server:app \
        --app-dir /app/scripts \
        --host 0.0.0.0 \
        --port 8711
else
    ensure_data_dirs
    echo "[tm-server] flavor=${TM_BUILD_FLAVOR:-lite} workspace=${WORKSPACE:-/data} (already non-root: $(id -u))"
    exec uvicorn task_rag_server:app \
        --app-dir /app/scripts \
        --host 0.0.0.0 \
        --port 8711
fi
