"""Smoke test: scripts/install_cron.sh --print must include env-source
lines for DNSE + Telegram. Regression guard for the 2026-09-19
incident where the cron job ran without env vars and silently
failed with "DNSE_API_KEY not set" — prices went 25h+ stale.
"""
import subprocess
from pathlib import Path

# tests/ → vietnam_algo/ → repo root
ROOT = Path(__file__).resolve().parents[2]


def test_install_cron_includes_env_sources() -> None:
    proc = subprocess.run(
        ["bash", "scripts/install_cron.sh", "--print"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    out = proc.stdout
    assert ".env.dnse-secrets" in out, (
        f"install_cron.sh --print missing .env.dnse-secrets source.\nGot:\n{out}"
    )
    assert ".env.telegram" in out, (
        f"install_cron.sh --print missing .env.telegram source — "
        f"without it, common_sync failures won't notify the operator.\nGot:\n{out}"
    )
    # Both jobs (get_otp + common_sync) should source the env files.
    assert out.count(".env.dnse-secrets") >= 2
    assert out.count(".env.telegram") >= 2
