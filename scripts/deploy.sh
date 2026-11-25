#!/usr/bin/env bash
# Deploy the project to the VPS.
#
# CRITICAL: runtime DATA on the VPS must never be overwritten by the
# local working copy.
#
# Incident (2026-11-01): the local git checkout still tracked
# vietnam_algo/data/prices/*.json from before .gitignore covered
# vietnam_algo/data/. A plain `rsync -avz` pushed those stale price
# files over the VPS's freshly-synced ones — the VPS price data went
# from 2026-10-30 back to 2026-08-13, and every open Darvas Box deal
# on the dashboard was marked against a 3-month-old price.
#
# The price files are now untracked (see .gitignore), but a stale
# working copy or a stray `git checkout` can still bring them back.
# So this script excludes ALL runtime data explicitly, and it uses
# `--delete` so that files REMOVED on the VPS are also removed locally
# (without --delete, deleted files linger forever).
#
# Usage:
#   bash scripts/deploy.sh              # deploy
#   bash scripts/deploy.sh --dry-run    # show what would change
#   bash scripts/deploy.sh --delete     # also remove files only on the VPS
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPS="${VPS:-root@185.227.135.227}"
REMOTE_DIR="${REMOTE_DIR:-/root/hft-trading-algo}"
LOG_DIR="${LOG_DIR:-/root/logs}"

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN="--dry-run"
fi

# ---------------------------------------------------------------------------
# Excludes
# ---------------------------------------------------------------------------
# Never sync these from the local working copy:
#   .git                  — version control
#   __pycache__ / *.pyc  — build artifacts
#   vietnam_algo/data/    — ALL runtime data: prices, live state, backtests
#   darvas_box_strategy/data/ — cache, bot state, position tracker
#   *.log                 — logs
#
# The two data/ dirs are excluded wholesale on purpose. Everything in
# them is either (a) regenerated on the VPS by the sync jobs, or
# (b) runtime state that must survive a deploy.
EXCLUDES=(
    --exclude='.git'
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='*.log'
    --exclude='vietnam_algo/data/'
    --exclude='darvas_box_strategy/data/'
    --exclude='*.env'
    --exclude='*.bin'
)

# --delete is OFF by default: the VPS hosts files that are NOT in the
# local checkout (postgres/initdb.d/*.sql — the real Postgres init
# scripts, plus operator helper scripts). A blanket --delete would
# wipe them. Enable it only with --delete when you know the local
# tree is a superset of the remote one.
DELETE=""
for arg in "$@"; do
    case "$arg" in
        --delete) DELETE="--delete" ;;
    esac
done

echo "=== Deploying $PROJECT_DIR -> $VPS:$REMOTE_DIR ==="
echo "--- excluded from sync (runtime data) ---"
printf '    %s\n' "${EXCLUDES[@]}"
if [[ -z "$DELETE" ]]; then
    echo "--- --delete is OFF (files only on the VPS are kept) ---"
else
    echo "--- --delete is ON (files only on the VPS WILL BE REMOVED) ---"
fi

RSYNC_ARGS=(-avz "${EXCLUDES[@]}")
if [[ -n "$DELETE" ]]; then
    RSYNC_ARGS+=(--delete)
fi
if [[ -n "$DRY_RUN" ]]; then
    RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

rsync "${RSYNC_ARGS[@]}" "$PROJECT_DIR/" "$VPS:$REMOTE_DIR/"

if [[ -n "$DRY_RUN" ]]; then
    echo "(dry run — nothing was changed)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Post-deploy: clear stale bytecode, restart the web service
# ---------------------------------------------------------------------------
echo "--- clearing __pycache__ ---"
ssh "$VPS" "find '$REMOTE_DIR' -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true"

echo "--- restarting darvas-web ---"
ssh "$VPS" "systemctl restart darvas-web 2>/dev/null || true; sleep 2; systemctl is-active darvas-web"

echo
echo "=== Done ==="
echo "NOTE: the web cache (darvas_box_strategy/data/web_cache.json) is NOT"
echo "      regenerated automatically. To refresh it:"
echo "      curl -X POST http://localhost:$REMOTE_DIR/../api/refresh"
echo "      (or: ssh $VPS 'curl -m 900 -s -X POST http://localhost:5001/api/refresh')"
