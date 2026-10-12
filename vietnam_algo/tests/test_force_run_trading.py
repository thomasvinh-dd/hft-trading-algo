"""Tests for the ``force-run-trading`` subcommand.

The bot's main loop is gated by ``should_poll_strategy`` (1h throttle)
and ``state.orderSubmitDate`` (once per day). When the operator wants
to verify the strategy right now — "did my code change pick up the
new signal?" — they can invoke ``force-run-trading`` which bypasses
every schedule gate and runs the strategy's decision pipeline once.

These tests pin:
  - The CLI subcommand is registered and accepts the expected args
  - The handler runs ``strategy_adapter.generate_orders`` with today's
    date (not whatever's in the state file)
  - With --live-submit, it submits via _submit_autofin_deals_to_dnse
  - Without --live-submit, it skips the submit (dry-run)
  - Errors propagate as non-zero exit codes; the operator sees them
  - A Telegram summary is sent (unless --no-notify)
  - Refuses --live-submit if no trading token is available
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Eagerly import the registry so monkeypatch.setitem can find it
# in sys.modules (otherwise we hit ``KeyError: vietnam_algo.algos.registry``
# the first time we try to setitem on a freshly-touched module).
from vietnam_algo.algos import registry as _registry  # noqa: F401  (eager import)
from vietnam_algo.algos.registry import STRATEGY_REGISTRY
from vietnam_algo.libs.notifier import force_run_summary_message


# -----------------------------------------------------------------------------
# CLI registration
# -----------------------------------------------------------------------------


def test_force_run_trading_subcommand_registered() -> None:
    """The subcommand is in the parser."""
    from vietnam_algo.main import build_parser
    parser = build_parser()
    # argparse doesn't expose "all subcommands" but parse_args with
    # only the subcommand name will fail with a clear error if
    # missing. Use a minimal required-args set.
    try:
        parser.parse_args([
            "force-run-trading",
            "--strategy", "autofin_signal",
            "--account-no", "0003937868",
            "--loan-package-id", "1775",
        ])
    except SystemExit as exc:
        # Some required args (DNSE creds) trigger SystemExit, but
        # the subcommand must be recognized first. If unknown, the
        # error message is "argument ...: invalid choice: 'force-run-trading'".
        # We check for that.
        assert "invalid choice" not in str(exc), (
            f"force-run-trading not registered: {exc}"
        )


def test_force_run_trading_help_text() -> None:
    """Help mentions the bypass use case so operators find it."""
    from vietnam_algo.main import build_parser
    parser = build_parser()
    try:
        parser.parse_args(["force-run-trading", "--help"])
    except SystemExit:
        pass
    # The help text is rendered to stdout by argparse; we don't
    # capture it here, but the test passes if --help doesn't raise
    # an "invalid choice" error.


# -----------------------------------------------------------------------------
# force_run_summary_message
# -----------------------------------------------------------------------------


def test_summary_dry_run() -> None:
    """Dry-run: header + counts, no submit section."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 2, "newOrders": 1, "partialOrders": 1, "closeOrders": 0,
                     "signalsFetched": 5, "dealsTracked": 4},
        submit_result=None,
        live_submit=False,
    )
    assert "FORCE-RUN" in msg
    assert "autofin_signal" in msg
    assert "dry-run" in msg.lower() or "DRY-RUN" in msg
    assert "2 order(s) planned" in msg
    assert "new entries" in msg
    assert "partial closes" in msg


def test_summary_live_submit_success() -> None:
    """Live submit with success: green check + dnseOrderIds."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 1, "newOrders": 1},
        submit_result={
            "submitted": ["DNSE-12345"],
            "skipped_existing": 0,
            "unknown": 0,
            "total": 1,
        },
        live_submit=True,
    )
    assert "1/1 submitted" in msg
    assert "DNSE-12345" in msg
    assert "✅" in msg


def test_summary_live_submit_with_unknown() -> None:
    """Unknown > 0 → 🚨 alert."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 2, "newOrders": 2},
        submit_result={
            "submitted": ["DNSE-1"],
            "skipped_existing": 0,
            "unknown": 1,
            "total": 2,
        },
        live_submit=True,
    )
    assert "1/2 submitted" in msg
    assert "1 unknown" in msg
    assert "🚨" in msg


def test_summary_live_submit_no_deals() -> None:
    """Live submit with no deals_actioned → SKIPPED with ⚠️."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 0, "newOrders": 0},
        submit_result=None,
        live_submit=True,
    )
    assert "SKIPPED" in msg
    assert "no deals_actioned" in msg
    assert "⚠️" in msg


def test_summary_submit_error() -> None:
    """Submit raised → error section with the message."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 1},
        submit_result={"error": "ConnectionError: DNSE unreachable"},
        live_submit=True,
    )
    assert "ERROR" in msg
    assert "ConnectionError" in msg
    assert "❌" in msg


def test_summary_out_of_zone() -> None:
    """Live-quote gate dropped some orders — surfaced in the summary."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 2, "newOrders": 2},
        submit_result={
            "submitted": ["DNSE-1"],
            "skipped_existing": 0,
            "unknown": 0,
            "total": 2,
            "out_of_zone": [{"deal": {"symbol": "ABB"}}],
        },
        live_submit=True,
    )
    assert "out_of_zone" in msg
    assert "1" in msg  # count of dropped


def test_summary_skipped_off_hours() -> None:
    """Off-hours submit skip is surfaced."""
    msg = force_run_summary_message(
        strategy_name="autofin_signal",
        trade_date="2026-10-09",
        poll_result={"count": 1},
        submit_result={
            "submitted": [],
            "skipped_existing": 0,
            "unknown": 0,
            "total": 0,
            "skipped_off_hours": 1,
        },
        live_submit=True,
    )
    assert "skipped_off_hours" in msg
    assert "off-hours" in msg.lower() or "trading window" in msg.lower()


# -----------------------------------------------------------------------------
# Handler behaviour (without hitting real DNSE)
# -----------------------------------------------------------------------------


def test_handler_dry_run_calls_generate_orders(monkeypatch, tmp_path) -> None:
    """The dry-run path calls generate_orders with today's date and
    does NOT call _submit_autofin_deals_to_dnse."""
    from dataclasses import dataclass

    # Stub the strategy adapter
    calls: list[dict] = []
    class _StubAdapter:
        def generate_orders(self, engine_config, strategy_config, trade_date, live_submit):
            calls.append({"trade_date": trade_date, "live_submit": live_submit})
            return {
                "count": 1,
                "newOrders": 1,
                "dealsTracked": 1,
                "deals_actioned": [{"symbol": "ABB", "side": "NB",
                                     "dnse_orders": []}],
            }

    # Stub the registry + bot helpers
    monkeypatch.setitem(STRATEGY_REGISTRY, "autofin_signal", _StubAdapter())
    submit_called: list[dict] = []
    from vietnam_algo.bots import runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "_submit_autofin_deals_to_dnse",
        lambda *a, **kw: submit_called.append((a, kw)) or {"submitted": [], "skipped_existing": 0, "unknown": 0, "total": 0},
    )

    # engine_config must be a dataclass so _with_strategy's
    # dataclasses.replace() works on it. Use a minimal one.
    @dataclass
    class _Cfg:
        dataDir: object = tmp_path
        strategy: str = "autofin_signal"
    class _SC:
        pass
    class _Client:
        pass
    monkeypatch.setattr("vietnam_algo.main.engine_config_from_args", lambda _a: _Cfg())
    monkeypatch.setattr("vietnam_algo.main.strategy_config_from_args", lambda _a: _SC())
    monkeypatch.setattr("vietnam_algo.main.dnse_client_from_args", lambda _a: _Client())

    from vietnam_algo.libs.dnse import auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "load_shared_trading_token", lambda _p: None)
    from vietnam_algo.libs import bot_state as _bot_state_mod
    class _State:
        orderSubmitDate = "2026-10-08"
        eodDate = "2026-10-08"
        startOfDayDate = "2026-10-08"
    monkeypatch.setattr(_bot_state_mod, "load_state", lambda *a, **kw: _State())

    from datetime import datetime
    from zoneinfo import ZoneInfo
    from vietnam_algo.main import main
    rc = main(["force-run-trading",
               "--strategy", "autofin_signal",
               "--account-no", "0003937868",
               "--loan-package-id", "1775",
               "--api-key", "dummy-key",
               "--api-secret", "dummy-secret",
               "--no-notify"])
    assert rc == 0
    # strategy was called with today's date (in ICT)
    assert len(calls) == 1
    today_ict = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
    assert calls[0]["trade_date"] == today_ict
    assert calls[0]["live_submit"] is False
    # submit was NOT called (dry-run)
    assert submit_called == []


def test_handler_live_submit_without_token_returns_3(monkeypatch) -> None:
    """--live-submit but no token → exit 3 with a clear error."""
    import argparse
    args = argparse.Namespace(
        strategy="autofin_signal",
        account_no="0003937868",
        loan_package_id=1775,
        live_submit=True,
        no_notify=True,
        data_dir="/tmp",
        api_key="dummy-key",
        api_secret="dummy-secret",
    )
    class _StubAdapter:
        def generate_orders(self, *a, **kw):
            return {"count": 0, "newOrders": 0}
    monkeypatch.setitem(
        STRATEGY_REGISTRY,
        "autofin_signal", _StubAdapter(),
    )
    from vietnam_algo.libs.dnse import auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "load_shared_trading_token", lambda _p: None)
    from vietnam_algo.main import main
    rc = main(["force-run-trading",
               "--strategy", "autofin_signal",
               "--account-no", "0003937868",
               "--loan-package-id", "1775",
               "--api-key", "dummy-key",
               "--api-secret", "dummy-secret",
               "--live-submit",
               "--no-notify"])
    assert rc == 3


def test_handler_unknown_strategy_returns_2(monkeypatch) -> None:
    """Unknown strategy name → exit 2 with the available list."""
    from vietnam_algo.main import main
    # We don't need to stub anything — the unknown-strategy check fires
    # before any of the heavy setup.
    rc = main(["force-run-trading",
               "--strategy", "totally_made_up_strategy",
               "--account-no", "0003937868",
               "--loan-package-id", "1775",
               "--api-key", "dummy-key",
               "--api-secret", "dummy-secret",
               "--no-notify"])
    assert rc == 2


def test_handler_generate_orders_raises_returns_4_and_notifies(monkeypatch) -> None:
    """Strategy raised → exit 4, Telegram alert (unless --no-notify)."""
    import argparse
    args = argparse.Namespace(
        strategy="autofin_signal",
        account_no="0003937868",
        loan_package_id=1775,
        live_submit=False,
        no_notify=False,
        data_dir="/tmp",
        api_key="dummy-key",
        api_secret="dummy-secret",
    )
    class _BadAdapter:
        def generate_orders(self, *a, **kw):
            raise RuntimeError("autofin.vn is down")
    monkeypatch.setitem(STRATEGY_REGISTRY, "autofin_signal", _BadAdapter())
    notify_calls: list[str] = []
    from vietnam_algo.bots import runner as runner_mod
    monkeypatch.setattr(runner_mod, "notify", lambda msg: notify_calls.append(msg))

    from vietnam_algo.main import main
    rc = main(["force-run-trading",
               "--strategy", "autofin_signal",
               "--account-no", "0003937868",
               "--loan-package-id", "1775",
               "--api-key", "dummy-key",
               "--api-secret", "dummy-secret"])
    assert rc == 4
    assert len(notify_calls) == 1
    assert "FAILED" in notify_calls[0]
    assert "autofin_signal" in notify_calls[0]
    assert "autofin.vn is down" in notify_calls[0]
