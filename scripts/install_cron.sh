#!/bin/bash
# install_cron.sh — register get_otp + common_sync jobs for one VPS
# deployment of hft-trading-algo.
#
# Usage:
#   bash scripts/install_cron.sh [--print]
#
# If --print is passed, the cron lines are written to stdout for review
# instead of being installed. Otherwise they're applied via ``crontab``.
#
# Schedule (all times ICT; cron runs in UTC on most VPS images):
#   01:30 UTC = 08:30 ICT  — get_otp (DNSE email OTP -> token)
#   08:15 UTC = 15:15 ICT  — common_sync --post (post-close CA + prices)
#   */5 * * * *           — bot watchdog (restart dead autofin/momentum)
#
# The 08:30-ICT price sync is intentionally NOT cron'd here because
# update_dataset takes ~25 min for 1500+ symbols; running it as a
# separate cron entry just before market open would block 09:00 ICT
# start-of-day briefings. Instead, the auto-trading-bot's EOD path
# (15:15 ICT) carries out the closing refresh when the cron job
# above re-stamps the sentinel just after.
#
# To inspect existing entries:   crontab -l | grep vietnam_algo
# To remove:                     bash scripts/install_cron.sh --remove
set -e
PROJECT_DIR="${PROJECT_DIR:-/root/hft-trading-algo}"
LOG_DIR="${LOG_DIR:-/root/logs}"

# Source env files in a way that survives the cron environment.
# Cron runs each command with a near-empty environment, so
# DNSE_API_KEY/SECRET/TELEGRAM_BOT_TOKEN/CHAT_ID aren't set
# unless we explicitly source them. Without these the common_sync
# job fails silently with "DNSE_API_KEY not set" and never
# notifies the operator (because notify() reads the same env vars
# and silently no-ops when missing).
#
# 2026-09-19 incident: prices went 25h+ stale because the
# 08:15-ICT common_sync cron ran but couldn't hit DNSE; the
# autofin bot's 10:30-ICT heartbeat correctly detected stale
# data and alerted the operator, but the alert was the first
# signal anyone had that the cron was broken.
#
# We inline the source lines into each cron entry so an operator
# inspecting ``crontab -l`` can see exactly which env files the
# job depends on.
_env_sources='set -a && . '"$PROJECT_DIR"'/vietnam_algo/env/.env.dnse-secrets && . '"$PROJECT_DIR"'/vietnam_algo/env/.env.telegram && set +a'

# Bot watchdog: every 5 minutes, check if the autofin and momentum
# trading bots are alive. If a bot's PID is gone, relaunch it via
# the standard launcher scripts. This catches silent crashes that
# don't write a traceback (e.g. OOM kill, parent reaping) and was
# the root cause of the 2026-09-22 "no SOD / no EOD" report —
# the old momentum bot had been dead for ~24 hours before anyone
# noticed, because the cron was restarting the autofin side but
# nobody owned the momentum side.
#
# Detection: ``pgrep -f 'auto-trading-bot.*--strategy <name>'`` —
# returns 0 if at least one matching process exists.
# Action:     if not, ``bash $PROJECT_DIR/launch_<name>.sh`` —
# these scripts already pkill stale instances, so the watchdog
# can safely run them on every tick without leaking processes.
_watchdog='pgrep -f "auto-trading-bot.*--strategy autofin_signal" >/dev/null 2>&1 || (echo "[$(date -Iseconds)] watchdog: autofin bot down, relaunching" >> '"$LOG_DIR"'/bot_watchdog.log && bash '"$PROJECT_DIR"'/launch_autofin.sh >> '"$LOG_DIR"'/bot_watchdog.log 2>&1); pgrep -f "auto-trading-bot.*--strategy momentum" >/dev/null 2>&1 || (echo "[$(date -Iseconds)] watchdog: momentum bot down, relaunching" >> '"$LOG_DIR"'/bot_watchdog.log && bash '"$PROJECT_DIR"'/launch_momentum.sh >> '"$LOG_DIR"'/bot_watchdog.log 2>&1)'

case "${1:-}" in
    --print)
        echo "# vietnam-algo cron entries for $PROJECT_DIR"
        cat <<EOF
30 1 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
15 8 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
*/5 * * * * $_watchdog
EOF
        exit 0
        ;;
    --remove)
        existing="$(crontab -l 2>/dev/null || true)"
        filtered="$(printf '%s\n' "$existing" | grep -v 'run-get-otp\|run-common-sync\|bot_watchdog\|launch_autofin\|launch_momentum' || true)"
        printf '%s\n' "$filtered" | crontab -
        echo "Removed vietnam-algo cron entries (if any)."
        exit 0
        ;;
esac

mkdir -p "$LOG_DIR"
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -v 'run-get-otp\|run-common-sync\|bot_watchdog\|launch_autofin\|launch_momentum' || true)"
new_entries="$(cat <<EOF
30 1 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
15 8 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
*/5 * * * * $_watchdog
EOF
)"
printf '%s\n%s\n' "$filtered" "$new_entries" | crontab -
echo "Installed."
echo "Active vietnam-algo entries:"
crontab -l | grep -E 'run-(get-otp|common-sync)|bot_watchdog|launch_' || true
