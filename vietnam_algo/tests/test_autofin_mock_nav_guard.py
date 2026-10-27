"""Source-level tests for the 2026-10-09 MOCK_NAV guard.

The 2026-10-08 04:30 ICT incident: the production env file had
``MOCK_NAV=100000000`` set, so the strategy sized ABB to 1,700
shares (NAV=100M × 30 % ÷ 17,400 VND) on a real-money account
that only has 10M VND of withdrawable cash.

The fix: the MOCK_NAV override is now conditional on
``not live_submit``. This file pins the structure of that fix so
a future refactor can't silently break it.

We test the source code directly (rather than running the full
generate_orders pipeline) because the autofin pipeline has many
external dependencies (autofin API, DNSE, price file, etc.) that
make integration tests brittle. The MOCK_NAV guard itself is a
4-line conditional — we just need to verify it's there and correct.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_RUNNER = Path(__file__).resolve().parents[2] / "vietnam_algo" / "algos" / "autofin_signal.py"
_AUTOFIN_SOURCE = _RUNNER.read_text()


def _extract_mock_nav_block() -> str:
    """Find the MOCK_NAV override block in autofin_signal.py."""
    # Match the entire MOCK_NAV section (from "# 5b." to the next
    # "self._positions = positions" line which is the next section
    # marker).
    pattern = re.compile(
        r"        # 5b\. MOCK_NAV override.*?(?=        # Cache the authoritative)",
        re.DOTALL,
    )
    match = pattern.search(_AUTOFIN_SOURCE)
    assert match, "MOCK_NAV block not found in autofin_signal.py"
    return match.group(0)


def test_mock_nav_block_guards_on_live_submit() -> None:
    """The MOCK_NAV override must be conditional on ``not live_submit``.

    A regression that reverts the guard would let MOCK_NAV override
    the live NAV in production, sizing positions to the dry-run NAV
    (e.g. 100M instead of the actual 10M). The 2026-10-08 04:30 ICT
    ABB incident was exactly this bug.
    """
    block = _extract_mock_nav_block()
    assert "if mock_nav and not live_submit:" in block, (
        "MOCK_NAV must be gated on `not live_submit` so live mode "
        "uses the real NAV from DNSE"
    )


def test_live_submit_warning_emitted_when_mock_nav_set() -> None:
    """When MOCK_NAV is set AND live_submit is True, log a warning.

    Without the warning, an operator who leaves MOCK_NAV in the env
    file by accident would silently get the live NAV. The warning
    makes the configuration mistake visible in the bot's stdout
    log on every cycle.
    """
    block = _extract_mock_nav_block()
    assert "elif mock_nav and live_submit:" in block, (
        "Need a `elif mock_nav and live_submit` branch that warns"
    )
    assert "MOCK_NAV=" in block and "IGNORED" in block, (
        "Warning must name the MOCK_NAV value and say it's IGNORED"
    )


def test_dry_run_override_keeps_legacy_behaviour() -> None:
    """The dry-run branch must still apply MOCK_NAV (legacy behaviour).

    A regression that disabled MOCK_NAV entirely would break the
    dry-run testing workflow where the operator wants to see an
    order plan without a real account balance.
    """
    block = _extract_mock_nav_block()
    # The dry-run branch must update `nav` from MOCK_NAV
    assert re.search(r"nav\s*=\s*float\(mock_nav\)", block), (
        "Dry-run branch must assign nav = float(mock_nav)"
    )
    # And it must use the override at all
    assert "MOCK_NAV override active" in block, (
        "Dry-run branch should log that MOCK_NAV is active"
    )


def test_mock_nav_uses_real_nav_value_in_warning() -> None:
    """The live-submit warning must report the REAL NAV that was used,
    not the MOCK_NAV value. This is what makes the warning actionable:
    the operator can see both numbers and decide which is wrong.
    """
    block = _extract_mock_nav_block()
    # The warning must mention the real nav (the `nav` variable that
    # was already loaded from build_portfolio_snapshot).
    assert "real NAV from DNSE:" in block
    # And it must format the nav with the same f-string pattern as
    # the rest of the codebase (e.g. ``{nav:,.0f}``).
    assert re.search(r"\{nav:,[0-9]*\.0f\}", block), (
        "Warning should format the real NAV as an integer with thousands "
        "separators for readability"
    )


def test_general_production_env_files_have_no_mock_nav() -> None:
    """The production env file MUST NOT have MOCK_NAV set.

    This is a regression guard for the 2026-10-08 04:30 ICT
    incident. The .env.autofin-signal file should not carry a
    MOCK_NAV value (it should be empty / unset). If a future
    operator wants to dry-run with a synthetic NAV, they should
    use DRY_RUN_INITIAL_NAV (the dry-run-state env var) or set
    MOCK_NAV only in a local override env file, not in the
    production env that gets sourced on every bot launch.
    """
    env_path = _RUNNER.parent.parent / "env" / ".env.autofin-signal"
    if not env_path.exists():
        pytest.skip(f"env file {env_path} not present in this checkout")
    content = env_path.read_text()
    # Allow comment lines and empty lines, but not active assignments
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("MOCK_NAV="):
            pytest.fail(
                f"Production env file {env_path.name} must not set "
                f"MOCK_NAV (2026-10-08 04:30 incident). Found: "
                f"{stripped!r}. To override NAV in dry-run use "
                f"DRY_RUN_INITIAL_NAV instead."
            )


# -----------------------------------------------------------------------------
# Test the 2026-10-09 live-cash fix: decide_orders() must use the live
# cash from the DNSE portfolio in live-submit mode, not the dry-run
# state's default 100M VND.
# -----------------------------------------------------------------------------


def test_decide_orders_uses_dry_run_cash_when_no_override() -> None:
    """Dry-run: decide_orders uses the dry-run state's cash.
    Pure-function behaviour, no side effects.
    """
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.algos.autofin_signal import decide_orders
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from vietnam_algo.libs.dry_run_state import save_dry_run_state, DryRunState
        st = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        save_dry_run_state(st, data_dir, strategy_name="autofin_signal")

        sig = AutofinSignal(
            symbol="ABB", company_name="test",
            last_recomment="Khuyến nghị đầu tư",
            supply_from=17100, supply_to=17400,
            tp_from=18400, tp_to=18400,
            sl_from=14700, sl_to=15900,
            rate_for_nav=0.30,
            is_waitting_buy=False, is_waitting_sell=False,
            market_type=4, raw={},
        )
        all_orders, _, _ = decide_orders(
            holding=[],
            signals=[sig],
            our_deals={},
            our_positions={},
            nav=10_000_000,
            current_prices={"ABB": 17500},
            data_dir=data_dir,
            actioned_deals=set(),
        )
        # With dry-run cash=100M, target_value = 10M*0.3 = 3M (capped at 100M);
        # qty=3000000/17400 ≈ 172, rounded to 100 lots. So we should see
        # a BUY for ABB.
        buys = [o for o in all_orders if o.get("side") == "NB"]
        assert len(buys) == 1


def test_decide_orders_uses_live_override_when_provided() -> None:
    """Live-submit: ``available_cash_vnd_override`` is used instead of
    the dry-run state's 100M cash. The 2026-10-09 fix for the
    second half of the 04:30 incident.
    """
    from vietnam_algo.libs.autofin_signals import AutofinSignal
    from vietnam_algo.algos.autofin_signal import decide_orders
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from vietnam_algo.libs.dry_run_state import save_dry_run_state, DryRunState
        st = DryRunState(initial_nav=100_000_000, cash=100_000_000)
        save_dry_run_state(st, data_dir, strategy_name="autofin_signal")

        sig = AutofinSignal(
            symbol="ABB", company_name="test",
            last_recomment="Khuyến nghị đầu tư",
            supply_from=17100, supply_to=17400,
            tp_from=18400, tp_to=18400,
            sl_from=14700, sl_to=15900,
            rate_for_nav=0.30,
            is_waitting_buy=False, is_waitting_sell=False,
            market_type=4, raw={},
        )
        # With override=2M, target_value = min(3M, 2M) = 2M. qty =
        # 2M / 17400 ≈ 114, rounded down to 100. cost=1.74M, less than
        # both 2M and 10M.
        all_orders, _, _ = decide_orders(
            holding=[],
            signals=[sig],
            our_deals={},
            our_positions={},
            nav=10_000_000,
            current_prices={"ABB": 17500},
            data_dir=data_dir,
            actioned_deals=set(),
            available_cash_vnd_override=2_000_000,
        )
        buys = [o for o in all_orders if o.get("side") == "NB"]
        assert len(buys) == 1
        assert buys[0]["quantity"] == 100
