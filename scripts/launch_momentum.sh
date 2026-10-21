#!/bin/bash
# Momentum bot launcher — reads from disk only, no data sync.
# The autofin bot owns data sync (prices + corporate actions); this bot
# relies on the autofin bot having refreshed the same data sources so
# the two bots don't both hit vietnamfinance.vn / Vietstock per EOD.
#
# Sources per-bot env files in order:
#   1. .env.dnse-secrets  — DNSE account creds (shared with autofin)
#   2. .env.telegram      — momentum bot token + chat id
#
# Run: bash scripts/launch_momentum.sh
# Logs: /root/logs/momentum.log (stdout+stderr)
# Watchdog: scripts/install_cron.sh installs a */5 cron entry that
# pgrep's the momentum bot and relaunches via this script if dead.
set -e
pkill -9 -f "auto-trading-bot.*--strategy momentum" 2>/dev/null || true
sleep 2
PROJECT_DIR="${PROJECT_DIR:-/root/hft-trading-algo}"
LOG_DIR="${LOG_DIR:-/root/logs}"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

set -a
source vietnam_algo/env/.env.dnse-secrets
source vietnam_algo/env/.env.telegram
set +a

echo "DNSE_API_KEY=${DNSE_API_KEY:0:10}..."
echo "TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"

nohup env PYTHONUNBUFFERED=1 \
    DNSE_API_KEY="$DNSE_API_KEY" \
    DNSE_API_SECRET="$DNSE_API_SECRET" \
    DNSE_ACCOUNT_NO="$DNSE_ACCOUNT_NO" \
    DNSE_PROXY_HTTP_URL="" \
    TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
    TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
    TELEGRAM_NOTIFICATIONS="$TELEGRAM_NOTIFICATIONS" \
    python3 -u -m vietnam_algo.main auto-trading-bot \
        --account-no 0003937868 --loan-package-id 1775 \
        --sleep-seconds 60 --no-sync-data --strategy momentum \
    > "$LOG_DIR/momentum.log" 2>&1 &
disown
sleep 8
echo "--- ps ---"
ps -ef | grep auto-trading-bot | grep -v grep
echo "--- log ---"
head -15 "$LOG_DIR/momentum.log"
