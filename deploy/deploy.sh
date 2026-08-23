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
# What it will not touch: /etc/dagmate/*.env (the per-process secrets) and
# /var/lib/dagmate (the databases). Both live outside the deploy root on
# purpose, so no deploy can overwrite a seed or truncate a match history.
set -euo pipefail

HOST="${DAGMATE_DEPLOY_HOST:?set DAGMATE_DEPLOY_HOST=user@host}"
ROOT="${DAGMATE_DEPLOY_ROOT:-/opt/dagmate}"

cd "$(dirname "$0")/.."

# Refuse on ANY dirty state — modified OR untracked. `git archive HEAD` ships
# only what's committed, so an untracked-but-forgotten file (e.g. a new
# deploy/*.env.example the server's setup steps reference) would silently not
# ship. `git status --porcelain` catches modified, staged, and untracked alike.
if [ -n "$(git status --porcelain)" ]; then
    echo "!! uncommitted or untracked changes — commit (and 'git add') them first," >&2
    echo "   or they won't be what deploys ('git archive HEAD' ships only committed files)" >&2
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
    # --ignore-scripts: do NOT run any package's install lifecycle scripts.
    # These installs run as root on the box that holds the arbiter seed, so one
    # compromised transitive dependency with a postinstall hook would otherwise
    # execute as root on every deploy. --omit=dev drops build-only deps too.
    cd "${ROOT}/service" && npm ci --omit=dev --ignore-scripts
    "${ROOT}/venv/bin/pip" install -q -r "${ROOT}/site/backend/requirements.txt"
    "${ROOT}/venv/bin/pip" install -q -r "${ROOT}/bot/requirements.txt"

    # The code is left owned by root (the deploy user), NOT by any service
    # account: none of the three service users can rewrite the code they execute
    # under Restart=always. State dirs under /var/lib/dagmate are owned by their
    # respective users at provisioning time and are not touched here.

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
# Match the field tolerantly — don't depend on the serializer's exact spacing
# (a `"service_ok": true` with a space would slip past a literal glob and pass a
# broken deploy). grep -E allows optional whitespace around the colon.
if ! printf '%s' "$HEALTH" | grep -qE '"service_ok"[[:space:]]*:[[:space:]]*true'; then
    echo "!! site is up but the Kaspa sidecar is not answering — no deposits, no settlement." >&2
    echo "   journalctl -u dagmate-service -n 50" >&2
    exit 1
fi
echo "==> ${REV} live"
