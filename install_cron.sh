#!/bin/bash
# install_cron.sh — legacy entry point, KEPT FOR COMPATIBILITY.
#
# The canonical installer is ``scripts/install_cron.sh``.  This wrapper
# delegates to it so there is exactly ONE implementation of the cron
# schedule in the repo and the two can never drift apart.
#
# Why this matters (2026-10-25): the two copies had diverged.  This
# root-level copy still carried the pre-DST entries (``30 1`` for the
# 08:30-ICT token refresh, ``15 8`` for the 15:15-ICT common sync),
# written under the assumption that cron ran in UTC.  It does not — the
# cron daemon on this host schedules every task in Europe/Berlin and
# ignores ``CRON_TZ`` (see ``man 5 crontab``).  Running this copy would
# have re-installed a broken, DST-sensitive schedule and reintroduced the
# very bug the canonical installer was fixed for.
#
# Usage:
#   bash install_cron.sh [--print]
#   bash install_cron.sh [--remove]
#
# To inspect the live entries:  crontab -l
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/scripts/install_cron.sh" "$@"
