#!/bin/bash
# Signal-driven momentum bot launcher.
#
# This is the SIGNAL-DRIVEN momentum strategy: it computes today's
# entries and exits from the live price data plus the live DNSE
# portfolio, instead of replaying a historical backtest CSV.
#
# The replay-driven variant (scripts/launch_momentum.sh) is kept for
# comparison — it only trades on dates the backtest happened to have a
# signal, which in practice was almost never.
#
# Run: bash scripts/launch_momentum_signal.sh
# Logs: /root/logs/momentum_signal.log (stdout+stderr)
set -e
pkill -9 -f "auto-trading-bot.*--strategy momentum_signal" 2>/dev/null || true
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
        --sleep-seconds 60 --no-sync-data --strategy momentum_signal \
    > "$LOG_DIR/momentum_signal.log" 2>&1 &
disown
sleep 8
echo "--- ps ---"
ps -ef | grep auto-trading-bot | grep -v grep
echo "--- log ---"
head -15 "$LOG_DIR/momentum_signal.log"
