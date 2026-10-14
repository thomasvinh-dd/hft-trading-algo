#!/bin/bash
# autofin_signal bot launcher — posts Telegram summaries to the
# dedicated "Auto Fin Bot" channel (-1004382203924).
#
# Sources per-bot env files in order:
#   1. .env.dnse-secrets     — DNSE account creds (shared with momentum)
#   2. .env.telegram-autofin — autofin's Telegram bot token + chat id
#   3. .env.autofin-signal   — autofin.vn API creds + USER_SL_PCT
#
# The autofin Telegram env file is sourced AFTER any shared .env.telegram
# file so its TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID override the momentum
# bot's values. Without this override the autofin bot would post start-
# of-day briefings, end-of-day summaries, and order-failure alerts to
# the momentum bot's chat (Thomas Biin's personal channel) instead of
# the dedicated Auto Fin Bot channel.
#
# LIVE-SUBMIT MODE (2026-10-06+):
#   This launcher runs the bot with --live-submit, meaning autofin's
#   planned BUYs and SELLs are actually placed on DNSE (not just
#   simulated locally). NAV for sizing is pulled from the live DNSE
#   account balance via the autofin adapter's build_portfolio_snapshot,
#   so the 10M VND test account will size ~10x smaller than the
#   100M dry-run reference.
#
#   Reverting to dry-run: remove ``--live-submit`` from the python3
#   invocation below and restart the bot.
#
#   Order flow: ``strategy_adapter.generate_orders(live_submit=True)``
#   returns ``deals_actioned``; the runner funnels the resulting
#   ``dnse_orders`` through ``_submit_autofin_deals_to_dnse`` which
#   builds ``DnseOrder`` records (stable sourceOrderId for ledger
#   dedup) and calls ``execute_orders_with_ledger``. Telegram
#   notifications include the dnseOrderIds returned by DNSE so the
#   operator can verify on the web/app.
#
# LIVE-QUOTE GATE (2026-10-07+):
#   The submit helper now fetches a LIVE DNSE quote via
#   ``client.get_latest_quote`` for each new-entry candidate, and
#   re-checks the buy-zone gate against the LIVE best-ask. The
#   planning step uses the local cached close (hours stale by
#   mid-day) so a real-time quote catches a price that has since
#   moved out of the zone. Default: enabled (``AUTOFIN_LIVE_QUOTE_GATE=1``).
#   To disable (e.g. for diagnosing a flapping connection), set
#   ``AUTOFIN_LIVE_QUOTE_GATE=0`` in the environment below.
#
# Run: bash scripts/launch_autofin.sh
# Logs: /root/logs/autofin.log (stdout+stderr)
# Watchdog: scripts/install_cron.sh installs a */5 cron entry that
# pgrep's the autofin bot and relaunches via this script if dead.
set -e
pkill -9 -f "auto-trading-bot.*--strategy autofin_signal" 2>/dev/null || true
sleep 2
PROJECT_DIR="${PROJECT_DIR:-/root/hft-trading-algo}"
LOG_DIR="${LOG_DIR:-/root/logs}"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

set -a
source vietnam_algo/env/.env.dnse-secrets
source vietnam_algo/env/.env.telegram-autofin
source vietnam_algo/env/.env.autofin-signal
set +a

echo "DNSE_API_KEY=${DNSE_API_KEY:0:10}..."
echo "TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"
echo "AUTOFIN_USER_SL_PCT=$AUTOFIN_USER_SL_PCT"

nohup env DNSE_API_KEY="$DNSE_API_KEY" \
    DNSE_API_SECRET="$DNSE_API_SECRET" \
    DNSE_ACCOUNT_NO="$DNSE_ACCOUNT_NO" \
    DNSE_PROXY_HTTP_URL="" \
    TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
    TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
    TELEGRAM_NOTIFICATIONS="$TELEGRAM_NOTIFICATIONS" \
    AUTOFIN_EMAIL="$AUTOFIN_EMAIL" \
    AUTOFIN_PASSWORD="$AUTOFIN_PASSWORD" \
    AUTOFIN_USER_SL_PCT="$AUTOFIN_USER_SL_PCT" \
    AUTOFIN_LIVE_QUOTE_GATE="${AUTOFIN_LIVE_QUOTE_GATE:-1}" \
    python3 -u -m vietnam_algo.main auto-trading-bot \
        --account-no 0003937868 --loan-package-id 1775 \
        --sleep-seconds 60 --no-sync-data --strategy autofin_signal \
        --live-submit \
    > "$LOG_DIR/autofin.log" 2>&1 &
disown
sleep 8
echo "--- ps ---"
ps -ef | grep auto-trading-bot | grep -v grep
echo "--- log ---"
head -15 "$LOG_DIR/autofin.log"
