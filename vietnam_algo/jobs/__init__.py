"""Standalone jobs that the trading bots should NOT do themselves.

Each subpackage here is a script that's run on a fixed schedule (cron,
systemd timer, etc). They own:

  - get_otp       : DNSE email OTP → trading token
  - common_sync   : daily price + corporate-action refresh

Both jobs are idempotent: they skip work that's already done for the
current VN trading day and stamp a sentinel file so downstream bots
can tell when the work has finished.

Why this exists
---------------

We used to do all three of (price sync, CA sync, OTP) inside the
auto-trading-bot main loop. With two bots (autofin + momentum) running
on the same VPS for the same DNSE account, that:

  1. Doubled the cost of every vietnamfinance / Vietstock call.
  2. Made both bots call DNSE ``/registration/send-email-otp`` and the
     follow-up ``/token`` exchange at the same minute. DNSE may only
     allow one in-flight OTP per session; the second request would
     fail.

Decoupling common work into scheduled jobs fixes both issues:

  - DNSE is hit exactly once for OTP per trading day, by ``get_otp``.
  - Prices and CA are refreshed exactly once, by ``common_sync``.
  - Both bots loop on their 60s heart-beat and listen for the
    ``data_ready_for_eod`` sentinel the common job stamps.

The bot's auto-trading-bot command stays backwards-compatible: if the
caller passes ``--auto-email-otp`` or just relies on lazy refresh, the
existing behaviour still works for a one-bot deployment. The cron
jobs are the recommended path going forward.
"""
