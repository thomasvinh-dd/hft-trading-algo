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
# Schedule (all times ICT):
#   01:30 ICT  — get_otp (DNSE email OTP -> token)
#   15:15 ICT  — common_sync --post (post-close CA + prices)
#   */5       — bot watchdog (restart dead autofin/momentum)
#
# 2026-10-13 fix: the cron daemon on this VPS runs in the host's local
# timezone (Europe/Berlin, CEST = UTC+2 in summer) — NOT UTC as the
# older comment claimed. Without ``CRON_TZ=UTC`` at the top of the
# crontab, the ``15 8 * * 1-5`` entry fired at 08:15 CEST = 06:15
# UTC = 13:15 ICT (TWO HOURS before market close at 15:00 ICT). The
# bot's EOD path at 15:45 ICT would then wait for a sentinel that
# was already 2.5 hours stale, and the daily price refresh ran 2
# hours too early (covering the morning session, not the close).
# Setting ``CRON_TZ=UTC`` makes the existing ``15 8`` and ``30 1``
# entries fire at the intended UTC times: 08:15 UTC = 15:15 ICT and
# 01:30 UTC = 08:30 ICT. The bot's 15:45-ICT EOD then finds a
# fresh sentinel and runs the closing refresh.
#
# The 08:30-ICT price sync is intentionally NOT cron'd here because
# update_dataset takes ~25 min for 1500+ symbols; running it as a
# separate cron entry just before market open would block 09:00 ICT
# start-of-day briefings. Instead, the auto-trading-bot's EOD path
# (15:45 ICT) carries out the closing refresh when the cron job
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
# the canonical launcher scripts under ``scripts/``. This catches
# silent crashes that don't write a traceback (e.g. OOM kill,
# parent reaping) and was the root cause of the 2026-09-22
# "no SOD / no EOD" report — the old momentum bot had been dead
# for ~24 hours before anyone noticed, because the cron was
# restarting the autofin side but nobody owned the momentum side.
#
# Detection: ``pgrep -f '[a]uto-trading-bot.*--strategy <name>'`` —
# returns 0 if at least one matching process exists.
#
# IMPORTANT — the ``[a]`` trick. pgrep -f searches the FULL command
# line (including argv[0]) of every process. A naive pattern like
# ``auto-trading-bot.*--strategy autofin_signal`` matches the
# watchdog's own ``sh -c`` invocation because the pattern string
# appears verbatim in the watchdog's argv. That false-positive
# made the watchdog think the bot was always alive and NEVER
# relaunched after 2026-09-24's manual pkill, leaving the autofin
# bot dead for ~21 hours (no SOD on 2026-09-25). The ``[a]uto``
# bracket class matches ``auto`` in real argv but the literal
# ``[a]uto-trading-bot`` in the cron entry does NOT contain the
# substring ``auto-trading-bot``, so the watchdog's own command
# line no longer self-matches.
#
# Action:     if not, ``bash $PROJECT_DIR/scripts/launch_<name>.sh``
# — these scripts already pkill stale instances, source the per-bot
# Telegram env files, and pass through DNSE creds, so the watchdog
# can safely run them on every tick without leaking processes.
#
# Why ``scripts/launch_*``: the operator also keeps a legacy copy
# at ``/root/launch_*.sh`` for ad-hoc manual restarts. The watchdog
# only ever invokes the canonical git-tracked version so any
# future tweak to launcher behaviour is picked up automatically.
_watchdog='pgrep -f "[a]uto-trading-bot.*--strategy autofin_signal" >/dev/null 2>&1 || (echo "[$(date -Iseconds)] watchdog: autofin bot down, relaunching" >> '"$LOG_DIR"'/bot_watchdog.log && bash '"$PROJECT_DIR"'/scripts/launch_autofin.sh >> '"$LOG_DIR"'/bot_watchdog.log 2>&1); pgrep -f "[a]uto-trading-bot.*--strategy momentum" >/dev/null 2>&1 || (echo "[$(date -Iseconds)] watchdog: momentum bot down, relaunching" >> '"$LOG_DIR"'/bot_watchdog.log && bash '"$PROJECT_DIR"'/scripts/launch_momentum.sh >> '"$LOG_DIR"'/bot_watchdog.log 2>&1)'

case "${1:-}" in
    --print)
        echo "# vietnam-algo cron entries for $PROJECT_DIR"
        cat <<EOF
# 2026-10-13: see install_cron.sh header for why CRON_TZ=UTC matters.
CRON_TZ=UTC
30 1 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
15 8 * * 1-5 cd $PROJECT_DIR && $_env_sources && DNSE_PROXY_HTTP_URL="" PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
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
# 2026-10-13: force the cron daemon to interpret the times below in
# UTC. Without this, on hosts where /etc/timezone is set to
# Europe/Berlin or America/New_York, the entries fire at the local
# time and the 15:15-ICT intent becomes 13:15-ICT (or worse).
CRON_TZ=UTC
30 1 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
15 8 * * 1-5 cd $PROJECT_DIR && $_env_sources && DNSE_PROXY_HTTP_URL="" PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
*/5 * * * * $_watchdog
EOF
)"
printf '%s\n%s\n' "$filtered" "$new_entries" | crontab -
echo "Installed."
echo "Active vietnam-algo entries:"
crontab -l | grep -E 'run-(get-otp|common-sync)|bot_watchdog|launch_' || true
