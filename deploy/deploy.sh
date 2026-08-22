#!/usr/bin/env bash
# DAGmate — push the current commit to the server and restart.
#
#     DAGMATE_DEPLOY_HOST=root@1.2.3.4 deploy/deploy.sh
#
# Deploys `git archive HEAD` — the committed tree, never the working tree. A
# copy-the-files-I-changed deploy drifts: the server ends up a mix of versions
# nobody has ever run locally, and the first symptom is usually a money path
# failing in a way that can't be reproduced. What ships here is a commit, so
# `git log -1` on either side answers "what is actually running".
#
# What it will not touch: /etc/dagmate/dagmate.env (the secrets) and
# /var/lib/dagmate (the databases). Both live outside the deploy root on
# purpose, so no deploy can overwrite a seed or truncate a match history.
set -euo pipefail

HOST="${DAGMATE_DEPLOY_HOST:?set DAGMATE_DEPLOY_HOST=user@host}"
ROOT="${DAGMATE_DEPLOY_ROOT:-/opt/dagmate}"

cd "$(dirname "$0")/.."

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "!! uncommitted changes — commit them first, or they won't be what deploys" >&2
    git status --short >&2
    exit 1
fi

REV="$(git rev-parse --short HEAD)"
echo "==> deploying ${REV} to ${HOST}:${ROOT}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git archive HEAD | tar -x -C "$TMP"

# --delete so a file removed in git is removed on the server. Without it, a
# deleted module keeps running until the next reboot.
rsync -az --delete \
    --exclude 'node_modules/' \
    --exclude 'venv/' \
    --exclude 'state/' \
    "$TMP/" "${HOST}:${ROOT}/"

ssh "$HOST" bash -euo pipefail <<EOF
    cd "${ROOT}/service" && npm ci --omit=dev
    "${ROOT}/venv/bin/pip" install -q -r "${ROOT}/site/backend/requirements.txt"
    "${ROOT}/venv/bin/pip" install -q -r "${ROOT}/bot/requirements.txt"
    chown -R dagmate:dagmate "${ROOT}"

    # Sidecar first: the backend calls routes on it, so restarting the backend
    # against an older sidecar is exactly the version skew that produces
    # "Kaspa service has no route ..." (see site/backend/service_client.py).
    systemctl restart dagmate-service
    sleep 3
    systemctl restart dagmate-site dagmate-bot
    sleep 3
    systemctl is-active dagmate-service dagmate-site dagmate-bot
EOF

echo "==> health check"
URL="${DAGMATE_PUBLIC_URL:-https://dagmate.org}"
HEALTH="$(curl -fsS "${URL}/api/health")"
echo "$HEALTH"

# /api/health answers 200 whenever the site is up, because the site is meant to
# survive the sidecar being down — challenges, the board and the lobby all work
# without chain data. That is right for an uptime monitor and wrong for a
# deploy gate: a deploy where the sidecar can't reach a node has shipped a site
# that can't take a deposit or pay anyone out, so check the field, not the code.
case "$HEALTH" in
    *'"service_ok":true'*) ;;
    *) echo "!! site is up but the Kaspa sidecar is not answering — no deposits, no settlement." >&2
       echo "   journalctl -u dagmate-service -n 50" >&2
       exit 1 ;;
esac
echo "==> ${REV} live"
