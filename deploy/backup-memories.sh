#!/usr/bin/env bash
# Snapshot the transcendence-memory-server tm-data volume into a tarball.
#
# Why: any deploy that touches container/volume layout must be preceded by an
# explicit backup of the LanceDB tables and memory_objects.jsonl files inside
# the tm-data volume. A botched deploy can corrupt the volume; this gives us a
# point-in-time restore artifact.
#
# Usage:
#   bash deploy/backup-memories.sh                       # writes /var/backups/tm-data-<ts>.tar.gz
#   bash deploy/backup-memories.sh /custom/dir           # writes <dir>/tm-data-<ts>.tar.gz
#   bash deploy/backup-memories.sh --restore <tarball>   # restores the named tarball into tm-data
#
# Implementation notes:
#   * We use a throwaway alpine container to tar the volume contents instead
#     of `docker cp` so we capture the exact mount point, not a re-export.
#   * The volume name is derived from the compose project (default: directory
#     name). Override with COMPOSE_PROJECT_NAME if needed.
#   * Backups are gzipped; checksum stored alongside.

set -euo pipefail

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}"
VOLUME_NAME="${PROJECT_NAME}_tm-data"
BACKUP_DIR="${1:-/var/backups}"

if [ "${1:-}" = "--restore" ]; then
    TARBALL="${2:?usage: $0 --restore <tarball>}"
    [ -f "$TARBALL" ] || { echo "tarball not found: $TARBALL" >&2; exit 1; }
    echo "[backup-memories] RESTORE from $TARBALL into volume $VOLUME_NAME"
    echo "[backup-memories] WARNING: this will overwrite current volume contents."
    read -r -p "Proceed? (yes/NO) " confirm
    [ "$confirm" = "yes" ] || { echo "aborted"; exit 1; }
    docker volume create "$VOLUME_NAME" >/dev/null
    docker run --rm \
        -v "$VOLUME_NAME":/dest \
        -v "$(realpath "$(dirname "$TARBALL")")":/src:ro \
        alpine:3.20 sh -c "cd /dest && rm -rf ./* ./.[!.]* 2>/dev/null; tar xzf /src/$(basename "$TARBALL")"
    echo "[backup-memories] restore complete"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/tm-data-${TIMESTAMP}.tar.gz"

if ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
    echo "[backup-memories] volume $VOLUME_NAME does not exist — nothing to back up" >&2
    exit 1
fi

echo "[backup-memories] snapshotting volume $VOLUME_NAME → $OUT"
docker run --rm \
    -v "$VOLUME_NAME":/src:ro \
    -v "$(dirname "$OUT")":/backup \
    alpine:3.20 sh -c "tar czf /backup/$(basename "$OUT") -C /src ."

if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$OUT" > "${OUT}.sha256"
elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$OUT" > "${OUT}.sha256"
fi

SIZE="$(du -h "$OUT" | cut -f1)"
echo "[backup-memories] OK — $OUT ($SIZE)"
echo "[backup-memories] checksum: $(cut -d' ' -f1 "${OUT}.sha256" 2>/dev/null || echo 'n/a')"
echo ""
echo "Restore later with:"
echo "  bash deploy/backup-memories.sh --restore $OUT"

