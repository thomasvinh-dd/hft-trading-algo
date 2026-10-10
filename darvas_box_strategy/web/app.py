"""Flask app for the Darvas Box web dashboard.

Routes:
  GET  /                 — HTML dashboard
  GET  /api/health       — { "ok": true }
  GET  /api/signals      — current opening signals (today)
  GET  /api/positions    — currently open positions
  GET  /api/backtest     — backtest summary + equity curve
  POST /api/refresh      — re-run scan + backtest (slow; ~3 min)

Run: python3 -m darvas_box_strategy.web.app
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "darvas_box_strategy"))

from darvas_box_strategy.web.data import (
    CACHE_PATH,
    CACHE_MAX_AGE_SECONDS,
    get_ticker_history,
    load_cache,
    refresh_cache,
    save_cache,
)

log = logging.getLogger("darvas.web")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["JSON_SORT_KEYS"] = False

    # Ensure we have a cache on disk; if not, run a refresh in the background
    # so the first GET / returns something useful instead of 503.
    if not CACHE_PATH.exists():
        log.warning("Cache missing at %s — spawning background refresh", CACHE_PATH)
        threading.Thread(target=_safe_refresh, daemon=True).start()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/health")
    def health():
        cache = load_cache()
        return jsonify({
            "ok": True,
            "cache_path": str(CACHE_PATH),
            "cache_last_updated": cache.last_updated,
            "cache_exists": CACHE_PATH.exists(),
        })

    @app.route("/api/signals")
    def api_signals():
        cache = load_cache()
        if not cache.signals:
            # First load or stale — trigger refresh in background
            threading.Thread(target=_safe_refresh, daemon=True).start()
        return jsonify({
            "last_updated": cache.last_updated,
            "scan_lookback": cache.scan_lookback,
            "count": len(cache.signals),
            "signals": cache.signals,
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
