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
# 2026-10-18 fix: vixie-cron 3.0pl1-184ubuntu2 (the Ubuntu/Debian
# fork shipped on this VPS) ignores ``CRON_TZ=UTC`` at the top
# of the crontab — it always uses the system timezone
# (Europe/Berlin = CEST = UTC+2 in summer). All the entry hours
# below are therefore in SYSTEM LOCAL TIME (CEST), pre-converted
# from the intended UTC fire time so the actual ICT fire matches
# intent. Conversion table:
#
#   intended ICT │ intended UTC │ system-local CEST
#   ─────────────┼──────────────┼──────────────────
#   08:30 ICT     │ 01:30 UTC     │ 03:30 CEST
#   09:00 ICT     │ 02:00 UTC     │ 04:00 CEST
#   15:15 ICT     │ 08:15 UTC     │ 10:15 CEST
#   16:00 ICT     │ 09:00 UTC     │ 11:00 CEST
#
# The 2026-10-14 fix (putting CRON_TZ=UTC at the top) was correct
# in spirit but the daemon silently ignored it. Live evidence
# from the syslog: 2026-10-14T08:15:01+02:00 = 06:15 UTC = 13:15
# ICT (2 hours early) despite CRON_TZ=UTC being at the top of
# the crontab. Switching to pre-converted local times is the
# portable fix that works regardless of whether the cron
# daemon honors CRON_TZ.
#
# Earlier comments on this script claimed the cron was running
# in UTC; that was wrong from the start. The VPS /etc/timezone
# has been Europe/Berlin since the host was built.
#
# Schedule (all times ICT, converted to CEST in the crontab below):
#   08:30 ICT  — get_otp (DNSE email OTP -> token)
#   09:00 ICT  — Darvas Box pre-market signal scan
#   15:15 ICT  — common_sync --post (post-close CA + prices)
#   16:00 ICT  — Darvas Box end-of-day backtest report
#   */5       — bot watchdog (restart dead autofin/momentum)
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
        echo "# All times are SYSTEM LOCAL TIME (CEST = UTC+2 in summer)."
        echo "# See the script header for the ICT → CEST conversion table."
        cat <<EOF
CRON_TZ=UTC
0 23 * * * /root/hft-trading-algo/scripts/backup_postgres.sh >> /var/log/hft-postgres-backup.log 2>&1
# 09:00 ICT (04:00 CEST) — Darvas Box pre-market signal scan
0 4 * * 1-5  cd $PROJECT_DIR && set -a && . $PROJECT_DIR/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=$PROJECT_DIR python3 -u -m darvas_box_strategy.bot.cli scan-signals --lookback 1 >> $LOG_DIR/darvas_signals.log 2>&1
# 16:00 ICT (11:00 CEST) — Darvas Box end-of-day backtest report
0 11 * * 1-5  cd $PROJECT_DIR && set -a && . $PROJECT_DIR/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=$PROJECT_DIR python3 -u -m darvas_box_strategy.bot.cli send-report >> $LOG_DIR/darvas_report.log 2>&1
# 08:30 ICT (03:30 CEST) — DNSE email OTP → trading token
30 3 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
# 15:15 ICT (10:15 CEST) — common_sync (post-close price + CA refresh)
15 10 * * 1-5 cd $PROJECT_DIR && $_env_sources && DNSE_PROXY_HTTP_URL="" PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
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
# See the script header for why all hours below are in SYSTEM
# LOCAL TIME (CEST = UTC+2 in summer). CRON_TZ=UTC is kept at the
# top for documentation; vixie-cron 3.0pl1 ignores it but the
# pre-converted hours below compensate.
new_entries="$(cat <<EOF
CRON_TZ=UTC
0 23 * * * /root/hft-trading-algo/scripts/backup_postgres.sh >> /var/log/hft-postgres-backup.log 2>&1
# 09:00 ICT (04:00 CEST) — Darvas Box pre-market signal scan
0 4 * * 1-5  cd $PROJECT_DIR && set -a && . $PROJECT_DIR/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=$PROJECT_DIR python3 -u -m darvas_box_strategy.bot.cli scan-signals --lookback 1 >> $LOG_DIR/darvas_signals.log 2>&1
# 16:00 ICT (11:00 CEST) — Darvas Box end-of-day backtest report
0 11 * * 1-5  cd $PROJECT_DIR && set -a && . $PROJECT_DIR/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=$PROJECT_DIR python3 -u -m darvas_box_strategy.bot.cli send-report >> $LOG_DIR/darvas_report.log 2>&1
# 08:30 ICT (03:30 CEST) — DNSE email OTP → trading token
30 3 * * 1-5 cd $PROJECT_DIR && $_env_sources && PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-get-otp >> $LOG_DIR/get_otp.log 2>&1
# 15:15 ICT (10:15 CEST) — common_sync (post-close price + CA refresh)
15 10 * * 1-5 cd $PROJECT_DIR && $_env_sources && DNSE_PROXY_HTTP_URL="" PYTHONPATH=$PROJECT_DIR python3 -u -m vietnam_algo.main run-common-sync --force >> $LOG_DIR/common_sync.log 2>&1
*/5 * * * * $_watchdog
EOF
)"
printf '%s\n%s\n' "$filtered" "$new_entries" | crontab -
echo "Installed."
echo "Active vietnam-algo entries:"
crontab -l | grep -E 'run-(get-otp|common-sync)|bot_watchdog|launch_' || true
