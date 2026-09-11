#!/bin/bash
# autofin_signal launcher — sends Telegram summaries to the dedicated
# "Auto Fin Bot" channel (-1004382203924) using a dedicated bot token.
# This OVERRIDES the default TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from
# .env.telegram so the autofin bot never posts to the personal chat.
set -e
pkill -9 -f "auto-trading-bot.*--strategy autofin_signal" 2>/dev/null || true
sleep 2
cd /root/hft-trading-algo
mkdir -p /root/logs

set -a
source vietnam_algo/env/.env.dnse-secrets
source vietnam_algo/env/.env.telegram
source vietnam_algo/env/.env.autofin-signal
set +a

# Override Telegram creds with the dedicated "Auto Fin Bot" channel.
export TELEGRAM_BOT_TOKEN="8842093041:AAEtSmmOudrABTSGFXDrPfs7jOmiAJecJuc"
export TELEGRAM_CHAT_ID="-1004382203924"

echo "DNSE_API_KEY=${DNSE_API_KEY:0:10}..."
echo "TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID"

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
    python3 -u -m vietnam_algo.main auto-trading-bot \
        --account-no 0003937868 --loan-package-id 1775 \
        --sleep-seconds 60 --update-symbols --update-prices --strategy autofin_signal \
    > /root/logs/autofin.log 2>&1 &
disown
sleep 8
echo "--- ps ---"
ps -ef | grep auto-trading-bot | grep -v grep
echo "--- log ---"
head -15 /root/logs/autofin.log
