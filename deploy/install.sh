#!/usr/bin/env bash
# One-shot installer for the host-side bits: systemd units + permissions.
#
# Run this once per host after the first checkout. Idempotent — safe to re-run
# whenever a unit file changes. Does NOT pull or start the container; that's
# the job of `systemctl start rag-everything` afterwards.
#
# Usage:
#   sudo bash deploy/install.sh                           # interactive
#   sudo bash deploy/install.sh --no-rclone-sync          # skip rclone timer
#
# What it does:
#   1. Copy systemd units to /etc/systemd/system/
#   2. daemon-reload
#   3. enable units (no start)
#
# What it does NOT do:
#   * pull docker images (do that explicitly: `docker compose pull`)
#   * write .env files (those are host secrets — install separately)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_RCLONE_SYNC=1

for arg in "$@"; do
    case "$arg" in
        --no-rclone-sync) INSTALL_RCLONE_SYNC=0 ;;
        *) echo "unknown arg: $arg" >&2; exit 1 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "this script needs root (use sudo)" >&2
    exit 1
fi

echo "[install] copying systemd units"
install -m 644 "$SCRIPT_DIR/systemd/rag-everything.service" /etc/systemd/system/
if [ "$INSTALL_RCLONE_SYNC" -eq 1 ]; then
    install -m 644 "$SCRIPT_DIR/systemd/rclone-sync.service" /etc/systemd/system/
    install -m 644 "$SCRIPT_DIR/systemd/rclone-sync.timer"   /etc/systemd/system/
fi

echo "[install] systemctl daemon-reload"
systemctl daemon-reload

echo "[install] enabling units (will start at next boot)"
systemctl enable rag-everything.service
if [ "$INSTALL_RCLONE_SYNC" -eq 1 ]; then
    systemctl enable rclone-sync.timer
fi

echo "[install] done"
echo ""
echo "Next steps:"
echo "  cd \$(systemctl cat rag-everything | awk -F= '/WorkingDirectory/ {print \$2}')"
echo "  cp .env.example .env && \$EDITOR .env             # set your keys"
echo "  cp docker-compose.override.example.yml docker-compose.override.yml   # tune mem caps if needed"
echo "  docker compose pull"
echo "  systemctl start rag-everything"
if [ "$INSTALL_RCLONE_SYNC" -eq 1 ]; then
    echo "  systemctl start rclone-sync.timer"
fi
