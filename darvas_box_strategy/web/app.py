"""Flask app for the Darvas Box web dashboard.

Routes:
  GET  /                 — HTML dashboard
  GET  /api/health       — { "ok": true }
  GET  /api/signals      — current opening signals (today)
  GET  /api/positions    — currently open positions
  GET  /api/backtest     — backtest summary + equity curve
  POST /api/refresh      — re-run scan + backtest (slow; ~3 min)

Run: python3 -m darvas_box_strategy.web.app

URL prefix
----------
The dashboard can be served under a path prefix, e.g. ``/darvas``, when it is
reverse-proxied by a server that forwards the prefix as a stripped path. Set
the environment variable ``DARVAS_URL_PREFIX`` to that prefix (with a leading
slash, no trailing slash) and the app adapts:

  - ``url_for`` produces prefixed URLs for the static assets.
  - The template renders a ``<base href>`` so relative API calls resolve
    against the prefix.
  - The JS reads its API base from that ``<base href>`` instead of assuming
    the app is mounted at the site root.

The reverse proxy is expected to rewrite the prefix away before forwarding
the request, so Flask still sees its normal routes.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, has_request_context, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "darvas_box_strategy"))

from darvas_box_strategy.web.data import (
    CACHE_PATH,
    CACHE_MAX_AGE_SECONDS,
    cache_age_hours,
    get_recent_signals,
    get_ticker_history,
    is_cache_stale,
    load_cache,
    refresh_cache,
    save_cache,
)

log = logging.getLogger("darvas.web")


def url_prefix() -> str:
    """The URL prefix the app is served under, e.g. ``/darvas``.

    The prefix is taken from the ``X-Forwarded-Prefix`` header that the
    reverse proxy sets, falling back to the ``DARVAS_URL_PREFIX`` environment
    variable. The header wins because it reflects how the request actually
    arrived, which matters when the proxy strips the prefix before
    forwarding.

    The value is normalised: a leading slash is added if missing, and a
    trailing slash is stripped. An empty value means the app is mounted at
    the site root.
    """
    raw = ""
    # request is only available inside a request context; outside one (e.g. a
    # background thread) fall back to the environment variable.
    if has_request_context():
        raw = (request.headers.get("X-Forwarded-Prefix") or "").strip()
    if not raw:
        raw = (os.environ.get("DARVAS_URL_PREFIX") or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


def static_url(filename: str) -> str:
    """URL for a static asset, including the URL prefix when one is set.

    ``url_for('static', ...)`` returns an absolute path from the site root
    (``/static/app.js``), which would bypass the prefix. This joins the
    prefix in front of it instead.
    """
    from flask import url_for

    path = url_for("static", filename=filename)
    prefix = url_prefix()
    return f"{prefix}{path}" if prefix else path



def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["JSON_SORT_KEYS"] = False

    # Make the prefix and the static-asset helper available to templates.
    app.jinja_env.globals["url_prefix"] = url_prefix
    app.jinja_env.globals["static_url"] = static_url

    # Ensure we have a cache on disk; if not, run a refresh in the background
    # so the first GET / returns something useful instead of 503.
    if not CACHE_PATH.exists():
        log.warning("Cache missing at %s — spawning background refresh", CACHE_PATH)
        threading.Thread(target=_safe_refresh, daemon=True).start()

    @app.route("/")
    def index():
        # The base href makes relative URLs in the page resolve against the
        # prefix, so the static assets and the API calls keep working when
        # the app is served from a sub-path.
        return render_template("index.html", url_prefix=url_prefix())

    @app.route("/api/health")
    def health():
        cache = load_cache()
        from darvas_box_strategy.darvas.db_prices import db_price_stats
        age = cache_age_hours(cache.last_updated)
        return jsonify({
            "ok": True,
            "cache_path": str(CACHE_PATH),
            "cache_last_updated": cache.last_updated,
            "cache_age_hours": round(age, 1) if age is not None else None,
            "cache_stale": is_cache_stale(cache.last_updated),
            "cache_max_age_hours": CACHE_MAX_AGE_SECONDS / 3600,
            "cache_exists": CACHE_PATH.exists(),
            # Postgres is the source of truth for market data; the JSON
            # files under vietnam_algo/data/prices/ are the fallback.
            "price_source": db_price_stats(),
        })

    @app.route("/api/signals")
    def api_signals():
        cache = load_cache()
        # Refresh when the cache is stale. Previously the trigger was
        # "cache.signals is empty", which had two problems:
        #   - a cache that was merely old but still held signals was
        #     served indefinitely;
        #   - a cache with no signals on the latest bar (a normal
        #     condition) triggered a full backtest on every request.
        # The cache age is the only trigger now.
        _maybe_start_background_refresh(cache)
        # Also include recent signals (last 7 days) so the UI is
        # useful even when there are no signals on the latest bar.
        recent = get_recent_signals(days=7)
        return jsonify({
            "last_updated": cache.last_updated,
            "scan_lookback": cache.scan_lookback,
            "count": len(cache.signals),
            "signals": cache.signals,
            "recent": recent,
            "recent_count": len(recent),
        })

    @app.route("/api/positions")
    def api_positions():
        cache = load_cache()
        return jsonify({
            "last_updated": cache.last_updated,
            "count": len(cache.open_positions),
            "positions": cache.open_positions,
        })

    @app.route("/api/ticker/<ticker>")
    def api_ticker(ticker):
        ticker = ticker.upper()
        # Reject anything that isn't a normal ticker symbol
        if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
            return jsonify({"error": "invalid ticker"}), 400
        if len(ticker) > 10:
            return jsonify({"error": "ticker too long"}), 400
        try:
            history = get_ticker_history(ticker)
        except Exception as e:
            log.exception("ticker history failed")
            return jsonify({"error": str(e)}), 500
        return jsonify(history)

    @app.route("/api/deal/<deal_id>")
    def api_deal(deal_id):
        """Return the full timeline (fills) for one deal.

        deal_id is the trade's identifier, e.g. ``MSN_2026-10-07``.
        Searches the cached history for the matching trade, including
        any currently-open position for the same ticker+date.

        Returns the trade summary + a list of fills showing every
        entry, tier exit, SL hit, trail, and dividend event in
        chronological order. Used to render the deal-detail modal.
        """
        # Validate the deal_id format (TICKER_YYYY-MM-DD)
        if "_" not in deal_id or len(deal_id) > 25:
            return jsonify({"error": "invalid deal_id format"}), 400
        ticker, _, entry_date_str = deal_id.partition("_")
        ticker = ticker.upper()
        if not ticker or not ticker.isalnum():
            return jsonify({"error": "invalid ticker in deal_id"}), 400

        cache = load_cache()
        ticker_hist = cache.ticker_history.get(ticker, {})
        if not ticker_hist:
            return jsonify({"error": f"no history for {ticker}"}), 404

        # Find the matching deal
        for d in ticker_hist.get("deals", []):
            if d.get("deal_id") == deal_id:
                return jsonify({
                    "ticker": ticker,
                    "deal_id": deal_id,
                    "entry_date": d.get("entry_date"),
                    "signal_date": d.get("signal_date"),
                    "entry_price": d.get("entry_price"),
                    "exit_price": d.get("exit_price"),
                    "exit_date": d.get("exit_date"),
                    "exit_reason": d.get("exit_reason"),
                    "shares": d.get("shares"),
                    "is_open": d.get("is_open"),
                    "bars_held": d.get("bars_held"),
                    "highest_close": d.get("highest_close"),
                    "last_close": d.get("last_close"),
                    "pnl_pct": d.get("pnl_pct"),
                    "pnl_vnd": d.get("pnl_vnd"),
                    "size_pct": d.get("size_pct"),
                    "sl_F": d.get("sl_F"),
                    "sl_T": d.get("sl_T"),
                    # The timeline — what happened bar-by-bar
                    "fills": d.get("fills", []),
                })
        return jsonify({"error": f"deal {deal_id} not found"}), 404

    @app.route("/api/backtest")
    def api_backtest():
        cache = load_cache()
        if not cache.backtest:
            threading.Thread(target=_safe_refresh, daemon=True).start()
        return jsonify({
            "last_updated": cache.last_updated,
            "config": cache.config,
            "backtest": cache.backtest,
        })

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        # Run synchronously — caller wants confirmation that it's done
        t0 = time.time()
        try:
            cache = refresh_cache()
            return jsonify({
                "ok": True,
                "elapsed_seconds": round(time.time() - t0, 1),
                "last_updated": cache.last_updated,
                "signal_count": len(cache.signals),
                "position_count": len(cache.open_positions),
            })
        except Exception as e:
            log.exception("refresh failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "not found"}), 404

    return app


def _safe_refresh():
    try:
        log.info("background refresh starting")
        refresh_cache()
        log.info("background refresh done")
    except Exception:
        log.exception("background refresh failed")


# Guards against a stampede: several concurrent requests could each start
# a ~5 minute backtest. One refresh at a time is enough.
_refresh_lock = threading.Lock()
_refresh_running = False


def _maybe_start_background_refresh(cache) -> bool:
    """Start a background refresh if the cache is stale. Returns True if one
    was started.

    The previous implementation only refreshed when the cache had no
    signals at all. That had two problems: a cache that was merely old —
    with signals still in it — was served indefinitely, and a cache with
    no signals on the latest bar (a normal condition) triggered a full
    backtest on every request. The cache age is the only trigger now,
    using CACHE_MAX_AGE_SECONDS.
    """
    global _refresh_running

    if not is_cache_stale(cache.last_updated):
        return False

    with _refresh_lock:
        if _refresh_running:
            return False
        _refresh_running = True

    def _runner():
        global _refresh_running
        try:
            _safe_refresh()
        finally:
            with _refresh_lock:
                _refresh_running = False

    try:
        threading.Thread(target=_runner, daemon=True).start()
    except Exception:
        # If the thread could not be started the flag would stay set
        # forever and no refresh would ever run again.
        log.exception("could not start the background refresh thread")
        with _refresh_lock:
            _refresh_running = False
        return False
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Darvas Box web dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-reload", action="store_true",
                       help="Disable auto-reload (recommended for systemd)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = create_app()
    log.info("Darvas Box web dashboard on http://%s:%d", args.host, args.port)
    log.info("Cache path: %s", CACHE_PATH)
    app.run(host=args.host, port=args.port, debug=args.debug,
            use_reloader=args.debug and not args.no_reload)


if __name__ == "__main__":
    main()
