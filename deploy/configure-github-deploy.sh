#!/usr/bin/env bash
# One-shot helper to wire up the auto-deploy workflow:
#   1. Generates a dedicated ed25519 deploy key (separate from your personal key)
#   2. Pins the host's SSH fingerprint via ssh-keyscan
#   3. Writes the required GitHub Secrets and Variables via the gh CLI
#   4. Prints the one command you must run on the deploy host to authorize the key
#
# Idempotent — re-running rotates the deploy key and refreshes the secrets.
#
# Requirements:
#   * gh CLI logged in (`gh auth status`) with `repo` + `workflow` scopes
#   * Run from inside a clone of the repo (gh resolves the repo from the remote)
#
# Usage:
#   bash deploy/configure-github-deploy.sh \
#        --host <hostname-or-ip> \
#        [--user <ssh-user>]            (default: ubuntu)
#        [--port <ssh-port>]            (default: 22)
#        [--path <repo-path-on-host>]   (default: /opt/transcendence-memory-server)
#        [--sudo "sudo"|""]             (default: sudo)
#        [--smoke true|false]           (default: true)
#        [--key-dir <local-dir>]        (default: ~/.ssh/transcendence-memory-deploy)

set -euo pipefail

HOST=""
USER_NAME="ubuntu"
PORT="22"
REMOTE_PATH="/opt/transcendence-memory-server"
SUDO="sudo"
SMOKE="true"
KEY_DIR="${HOME}/.ssh/transcendence-memory-deploy"

while [ $# -gt 0 ]; do
    case "$1" in
        --host)     HOST="$2"; shift 2 ;;
        --user)     USER_NAME="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --path)     REMOTE_PATH="$2"; shift 2 ;;
        --sudo)     SUDO="$2"; shift 2 ;;
        --smoke)    SMOKE="$2"; shift 2 ;;
        --key-dir)  KEY_DIR="$2"; shift 2 ;;
        -h|--help)  sed -n '1,30p' "$0"; exit 0 ;;
        *)          echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[ -n "$HOST" ] || { echo "--host is required" >&2; exit 1; }
command -v gh >/dev/null || { echo "gh CLI not found in PATH" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated; run 'gh auth login'" >&2; exit 1; }

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
echo "[deploy-config] target repo: $REPO"
echo "[deploy-config] target host: ${USER_NAME}@${HOST}:${PORT}  path: ${REMOTE_PATH}"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
KEY_PATH="${KEY_DIR}/id_ed25519"

if [ ! -f "$KEY_PATH" ]; then
    echo "[deploy-config] generating new ed25519 deploy key at $KEY_PATH"
    ssh-keygen -t ed25519 -N "" -C "github-actions-deploy@${REPO//\//-}" -f "$KEY_PATH" >/dev/null
else
    echo "[deploy-config] reusing existing deploy key at $KEY_PATH"
fi

echo "[deploy-config] pinning host fingerprint via ssh-keyscan (one-shot)"
KNOWN_HOSTS=$(ssh-keyscan -p "$PORT" -T 10 "$HOST" 2>/dev/null || true)
if [ -z "$KNOWN_HOSTS" ]; then
    echo "[deploy-config] WARNING: ssh-keyscan returned nothing — host unreachable from here?" >&2
    echo "[deploy-config] you can re-run later with --host once the host is reachable, or set DEPLOY_KNOWN_HOSTS manually." >&2
fi

echo "[deploy-config] writing GitHub Secrets"
gh secret set DEPLOY_HOST    --body "$HOST"
gh secret set DEPLOY_SSH_KEY --body "$(cat "$KEY_PATH")"
if [ -n "$KNOWN_HOSTS" ]; then
    gh secret set DEPLOY_KNOWN_HOSTS --body "$KNOWN_HOSTS"
fi

echo "[deploy-config] writing GitHub Variables"
gh variable set DEPLOY_USER  --body "$USER_NAME"
gh variable set DEPLOY_PORT  --body "$PORT"
gh variable set DEPLOY_PATH  --body "$REMOTE_PATH"
gh variable set DEPLOY_SUDO  --body "$SUDO"
gh variable set DEPLOY_SMOKE --body "$SMOKE"

PUB_KEY=$(cat "${KEY_PATH}.pub")

cat <<EOF

──────────────────────────────────────────────────────────────────────
✓ GitHub side configured.

NEXT — authorize the deploy key on the host (run this on YOUR workstation,
not in CI; you only need to do it once):

  ssh -p ${PORT} ${USER_NAME}@${HOST} "mkdir -p ~/.ssh && chmod 700 ~/.ssh \\
      && grep -qxF '${PUB_KEY}' ~/.ssh/authorized_keys 2>/dev/null \\
      || echo '${PUB_KEY}' >> ~/.ssh/authorized_keys \\
      && chmod 600 ~/.ssh/authorized_keys"

ALSO ensure the SSH user can run docker + systemctl without a password.
On the host, as root:

  sudo tee /etc/sudoers.d/transcendence-memory-deploy >/dev/null <<'SUDOERS'
  ${USER_NAME} ALL=(root) NOPASSWD: /usr/bin/docker, /bin/systemctl reload rag-everything, /bin/systemctl restart rag-everything, /bin/systemctl start rag-everything, /bin/systemctl stop rag-everything
  SUDOERS
  sudo chmod 440 /etc/sudoers.d/transcendence-memory-deploy

To test the workflow without waiting for a tag:

  gh workflow run deploy.yml -f ref=<existing-tag>

──────────────────────────────────────────────────────────────────────
EOF
