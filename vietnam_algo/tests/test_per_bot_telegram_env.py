"""Regression tests for the per-bot Telegram env file split.

User feedback 2026-09-22: 'the autofin bot is sending start of day
message to momentum bot's channel, not its own channel — we need to
make sure the 2 bots run separately with separate env files'.

Root cause: both ``launch_autofin.sh`` and ``launch_momentum.sh``
sourced the same ``.env.telegram`` file, which only contained the
momentum bot's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. The autofin
bot never overrode them, so it inherited momentum's chat id and
posted start-of-day briefings, EOD summaries, and order-failure
alerts to Thomas Biin's personal channel.

Fix:
  - ``vietnam_algo/env/.env.telegram-autofin`` — autofin's
    dedicated bot token + chat id.
  - ``scripts/launch_autofin.sh`` — sources
    ``.env.telegram-autofin`` (after ``.env.dnse-secrets``)
    so TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID land on the autofin
    values for the bot's process environment.
  - ``scripts/launch_momentum.sh`` — unchanged: continues to
    source ``.env.telegram`` (momentum's chat).

These tests assert the structural separation so a future "let's
share one .env again" tweak doesn't regress the channel split.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent

# The launch scripts live in two possible places depending on the
# deployment: ``<PROJECT>/scripts/launch_*.sh`` for git-managed copies
# and ``/root/launch_*.sh`` for the VPS live deploy (the cron watchdog
# and historical operator habit). Check both so the test passes
# regardless of where the operator dropped the file.
_LAUNCHER_SEARCH_DIRS = [
    _PROJECT_ROOT / "scripts",
    Path("/root"),
]


def _find_launcher(name: str) -> Path:
    for d in _LAUNCHER_SEARCH_DIRS:
        candidate = d / name
        if candidate.exists():
            return candidate
    searched = ", ".join(str(d / name) for d in _LAUNCHER_SEARCH_DIRS)
    raise FileNotFoundError(searched)


def _launcher(name: str) -> str:
    return _find_launcher(name).read_text(encoding="utf-8")


def test_autofin_launcher_sources_autofin_telegram_env():
    """launch_autofin.sh must source ``.env.telegram-autofin`` so the
    autofin bot's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID override the
    momentum values. If a future change drops the line, autofin will
    post to momentum's chat again."""
    src = _launcher("launch_autofin.sh")
    assert ".env.telegram-autofin" in src, (
        "launch_autofin.sh must source vietnam_algo/env/.env.telegram-autofin — "
        "without it the autofin bot inherits the momentum bot's TELEGRAM_BOT_TOKEN "
        "and TELEGRAM_CHAT_ID and posts to the wrong channel."
    )


def test_autofin_launcher_does_not_source_shared_telegram():
    """launch_autofin.sh must NOT source the shared ``.env.telegram``
    because that file holds the momentum bot's chat id. If a future
    tweak re-adds it, the autofin env file's values get clobbered."""
    src = _launcher("launch_autofin.sh")
    # Strip out comment lines — the launcher's header comment
    # legitimately mentions ".env.telegram" to explain why the
    # autofin env file is sourced AFTER (instead of instead of)
    # the shared one. We only care about actual ``source`` lines.
    code_lines = [
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code_only = "\n".join(code_lines).replace(".env.telegram-autofin", "")
    assert ".env.telegram" not in code_only, (
        "launch_autofin.sh must NOT source .env.telegram directly (outside "
        "comments) — that file holds the momentum bot's TELEGRAM_CHAT_ID "
        "and would override the autofin values sourced from "
        ".env.telegram-autofin."
    )


def test_momentum_launcher_sources_momentum_telegram_env():
    """launch_momentum.sh must continue to source ``.env.telegram``
    (momentum's chat)."""
    src = _launcher("launch_momentum.sh")
    assert ".env.telegram" in src, (
        "launch_momentum.sh must source .env.telegram — it holds the momentum "
        "bot's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."
    )


def test_momentum_launcher_does_not_source_autofin_telegram():
    """launch_momentum.sh must NOT source ``.env.telegram-autofin``."""
    src = _launcher("launch_momentum.sh")
    assert ".env.telegram-autofin" not in src, (
        "launch_momentum.sh must NOT source .env.telegram-autofin — that file "
        "holds the autofin bot's TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."
    )


def test_autofin_telegram_env_file_present():
    """``vietnam_algo/env/.env.telegram-autofin`` must exist with
    both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set to non-empty
    values."""
    path = _PROJECT_ROOT / "vietnam_algo" / "env" / ".env.telegram-autofin"
    assert path.exists(), (
        f"{path} missing — autofin bot has no per-bot Telegram env file. "
        "Without it, launch_autofin.sh will fail at the source line."
    )
    content = path.read_text(encoding="utf-8")
    # Find each var's value: line starts with KEY= and value is the
    # rest of the line (no further ``=`` until next line). Reject
    # placeholders like KEY= and KEY=CHANGEME.
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        matching = [
            line.split("=", 1)[1].strip()
            for line in content.splitlines()
            if line.startswith(f"{key}=")
        ]
        assert matching, f"{path} missing {key}=<value>"
        value = matching[0]
        assert value and value not in {"CHANGEME", "TODO", "PLACEHOLDER"}, (
            f"{path}: {key} is set to placeholder {value!r}; fill in the real "
            "Telegram bot token / chat id from @BotFather and getUpdates."
        )


def test_watchdog_cron_uses_correct_launchers():
    """The watchdog cron entries installed by install_cron.sh must
    relaunch via ``launch_autofin.sh`` and ``launch_momentum.sh``
    (the canonical scripts) — not some hardcoded nohup line that
    would skip the new env sourcing."""
    src = (_PROJECT_ROOT / "scripts" / "install_cron.sh").read_text(encoding="utf-8")
    assert "launch_autofin.sh" in src
    assert "launch_momentum.sh" in src
