#!/bin/bash
# install_cron.sh — register the VPS cron jobs for one hft-trading-algo
# deployment (DNSE token refresh, common sync, Darvas Box reports, Postgres
# backup, bot watchdog).
#
# Usage:
#   bash scripts/install_cron.sh [--print]
#   bash scripts/install_cron.sh [--remove]
#
# --print   writes the cron entries to stdout for review (does not install)
# --remove  strips the entries this script manages from the live crontab
#
# ---------------------------------------------------------------------------
# WHY THE ENTRIES USE A TZ GUARD INSTEAD OF FIXED HOURS
# ---------------------------------------------------------------------------
# 2026-10-25 incident (DST fall-back). This host is Europe/Berlin and the
# cron daemon is vixie-cron 3.0pl1-184ubuntu2, which **ignores** a
# ``CRON_TZ=`` assignment when it decides *when* to fire a job — see
# ``man 5 crontab`` ("LIMITATIONS": the daemon runs every task in the
# configured system timezone; ``TZ=`` in a crontab only affects the
# commands, not the schedule).  The live evidence was that a
# ``CRON_TZ=UTC`` crontab still fired at the Berlin wall-clock hour.
#
# The old entries therefore hard-coded CEST hours (03:30 for 08:30 ICT,
# 04:00 for 09:00 ICT, ...) pre-converted from ICT.  When Berlin fell
# back to CET on 2026-10-25 every one of those jobs silently moved one
# hour later in ICT — worst of all the DNSE token refresh (08:30 ICT)
# became 09:30 ICT, i.e. AFTER the 09:00 ICT start-of-day cycle it
# exists to support.
#
# The fix makes the schedule independent of the host's UTC offset:
#
#   * the cron *trigger* fires every hour at the target minute
#     (e.g. ``30 * * * *`` for the 08:30 ICT token refresh), and
#   * a *guard* inside the command accepts the run only when the wall
#     clock in the job's own timezone (Asia/Ho_Chi_Minh for the trading
#     jobs, UTC for the Postgres backup) equals the intended time.
#
# Exactly one of the 24 hourly slots satisfies the guard, whatever the
# host offset is — before and after either DST transition.  The weekday
# gate is evaluated in the job's own timezone as well, so a host-local
# DOW field can never shift a job onto the wrong trading day.
#
# Intended fire times (unchanged intent, now offset-independent):
#
#   08:30 ICT  — get_otp (DNSE email OTP -> trading token)
#   09:00 ICT  — Darvas Box pre-market signal scan
#   15:15 ICT  — common_sync --force (post-close CA + prices)
#   16:00 ICT  — Darvas Box end-of-day report
#   23:00 UTC  — Postgres backup
#   */5        — bot watchdog (restart dead autofin/momentum)
#
# The 08:30-ICT price sync is intentionally NOT cron'd here because
# update_dataset takes ~25 min for 1500+ symbols; running it as a
# separate cron entry just before market open would block the 09:00 ICT
# start-of-day briefings. Instead, the auto-trading-bot's EOD path
# (15:45 ICT) carries out the closing refresh when the cron job
# above re-stamps the sentinel just after.
#
# To inspect existing entries:   crontab -l | grep vietnam_algo
set -e
PROJECT_DIR="${PROJECT_DIR:-/root/hft-trading-algo}"
LOG_DIR="${LOG_DIR:-/root/logs}"
ICT_TZ="Asia/Ho_Chi_Minh"

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

# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------
# A crontab field treats "%" specially (it is turned into a newline), so the
# "%H"/"%M"/"%u" in the date format strings MUST be backslash-escaped in the
# crontab file.  cron strips the backslash before handing the command to
# /bin/sh, which then sees a plain "%H:%M".
#
#   _guard_ict "08:30"  ->  weekday + wall-clock gate in Asia/Ho_Chi_Minh
#   _guard_utc "23:00"  ->  wall-clock gate in UTC (no weekday gate)
_guard_ict() {
    printf '[ "$(TZ=%s date +\\%%u)" -le 5 ] && [ "$(TZ=%s date +\\%%H:\\%%M)" = "%s" ] && ' \
        "$ICT_TZ" "$ICT_TZ" "$1"
}
_guard_utc() {
    printf '[ "$(TZ=UTC date +\\%%H:\\%%M)" = "%s" ] && ' "$1"
}

# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------
# Each entry is "<schedule> <guard><command>".  The schedule's hour field
# is "*" on purpose: the guard — not the hour — decides when the job runs.
_mk_get_otp() {
    printf '30 * * * * %s cd %s && %s && PYTHONPATH=%s python3 -u -m vietnam_algo.main run-get-otp >> %s/get_otp.log 2>&1\n' \
        "$(_guard_ict 08:30)" "$PROJECT_DIR" "$_env_sources" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_common_sync() {
    printf '15 * * * * %s cd %s && %s && DNSE_PROXY_HTTP_URL="" PYTHONPATH=%s python3 -u -m vietnam_algo.main run-common-sync --force >> %s/common_sync.log 2>&1\n' \
        "$(_guard_ict 15:15)" "$PROJECT_DIR" "$_env_sources" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_darvas_scan() {
    printf '0 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli scan-signals --lookback 1 >> %s/darvas_signals.log 2>&1\n' \
        "$(_guard_ict 09:00)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_darvas_report() {
    printf '0 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli send-report >> %s/darvas_report.log 2>&1\n' \
        "$(_guard_ict 16:00)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
# Exit-event scanner (TIER / TRAIL / SL_SCALE / SL_HARD notifications).
# Runs right after the pre-market scan and again after the close so
# exit events are picked up on the same trading day they fire.
_mk_darvas_exits() {
    printf '0 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli scan-exits >> %s/darvas_exits.log 2>&1\n' \
        "$(_guard_ict 09:05)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_darvas_exits_eod() {
    printf '0 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli scan-exits >> %s/darvas_exits.log 2>&1\n' \
        "$(_guard_ict 15:25)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_pg_backup() {
    printf '0 * * * * %s%s/scripts/backup_postgres.sh >> /var/log/hft-postgres-backup.log 2>&1\n' \
        "$(_guard_utc 23:00)" "$PROJECT_DIR"
}
# Rebuild the web dashboard cache (scan + backtest, ~5 min).
#
# Runs at 16:05 ICT — after the 15:15 common_sync job that refreshes the
# prices, and after the 16:00 Telegram report. Without this job the cache
# is only rewritten when somebody clicks the dashboard's refresh button,
# and the dashboard silently serves stale data.
#
# The command is idempotent: it exits immediately if the cache is still
# fresh, so a manual run does not cause a second backtest.
_mk_darvas_refresh_cache() {
    printf '5 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli refresh-cache >> %s/darvas_cache.log 2>&1\n' \
        "$(_guard_ict 16:05)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
# Alert when the dashboard cache is too old.
#
# This is a separate signal from the Vietnam Algo staleness alert, which
# covers the price data. The price data can be fresh while the darvas
# cache is days old, and the price alert stays silent in that case.
# The check itself is cheap; the alert is sent at most once a day.
_mk_darvas_check_stale() {
    printf '0 * * * * %s cd %s && set -a && . %s/darvas_box_strategy/data/bot.env && set +a && PYTHONPATH=%s python3 -u -m darvas_box_strategy.bot.cli check-stale >> %s/darvas_stale.log 2>&1\n' \
        "$(_guard_ict 09:20)" "$PROJECT_DIR" "$PROJECT_DIR" "$PROJECT_DIR" "$LOG_DIR"
}
_mk_watchdog() {
    printf '*/5 * * * * %s\n' "$_watchdog"
}

_mk_entries() {
    printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \
        "$(_mk_pg_backup)" \
        "$(_mk_darvas_scan)" \
        "$(_mk_darvas_exits)" \
        "$(_mk_darvas_exits_eod)" \
        "$(_mk_darvas_report)" \
        "$(_mk_darvas_refresh_cache)" \
        "$(_mk_darvas_check_stale)" \
        "$(_mk_get_otp)" \
        "$(_mk_common_sync)" \
        "$(_mk_watchdog)"
}

# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------
# The filter must match EVERY line this script owns, otherwise re-running the
# installer appends duplicates.  That is exactly how the 2026-10-25 duplicate
# block appeared: the old filter only knew about run-get-otp / run-common-sync
# / the watchdog markers, so the Darvas entries and the Postgres backup were
# appended again on every install while their old copies stayed behind.
#
# NOTE: this is a plain ``grep -v`` (BRE), so the alternation separator is
# "\|".  The pattern is written as a literal here on purpose — the
# regression test in vietnam_algo/tests/test_install_cron_watchdog_pattern.py
# parses this literal out of the script and asserts the watchdog markers are
# covered by it.
_filter_managed() {
    printf '%s\n' "$1" \
        | grep -v 'run-get-otp\|run-common-sync\|bot_watchdog\|launch_autofin\|launch_momentum\|scan-signals\|scan-exits\|send-report\|refresh-cache\|check-stale\|backup_postgres\|CRON_TZ=' \
        | sed '/^[[:space:]]*$/d' \
        || true
}

case "${1:-}" in
    --print)
        echo "# vietnam-algo cron entries for $PROJECT_DIR"
        echo "# Offset-independent schedule: hourly trigger + wall-clock guard"
        echo "# in the job's own timezone (ICT for trading jobs, UTC for backup)."
        echo "# See the script header for the 2026-10-25 DST incident."
        _mk_entries
        exit 0
        ;;
    --remove)
        existing="$(crontab -l 2>/dev/null || true)"
        _filter_managed "$existing" | crontab -
        echo "Removed vietnam-algo cron entries (if any)."
        exit 0
        ;;
esac

mkdir -p "$LOG_DIR"
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(_filter_managed "$existing")"
new_entries="$(_mk_entries)"
printf '%s\n%s\n' "$filtered" "$new_entries" | crontab -
echo "Installed."
echo "Active vietnam-algo entries:"
crontab -l | grep -E 'run-(get-otp|common-sync)|bot_watchdog|launch_|scan-signals|send-report|backup_postgres' || true
