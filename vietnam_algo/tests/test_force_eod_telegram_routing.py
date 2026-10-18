"""Tests for the per-strategy Telegram env loader in force_eod.

The launcher scripts in scripts/launch_*.sh are careful to source
the right per-bot Telegram env file (.env.telegram-autofin vs
.env.telegram) so each bot posts to its own chat. The force_eod
tool mirrors that override so a one-off EOD triggered by hand
also routes to the correct chat. This test pins that contract.

Why this matters: a regression that loaded .env.telegram for the
autofin bot would silently send the autofin EOD summary to the
momentum channel and confuse the operator (2026-10-18 incident
where the autofin EOD landed in the momentum chat and the user
asked for it to be fixed).

The loader is a simple file-to-env mapping, so we test it with
temporary env files (no real chat ids needed).
"""
import os
import tempfile
import unittest
from pathlib import Path


def _make_fake_env(path: Path, chat_id: str) -> None:
    """Write a fake .env-style file with one chat_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"TELEGRAM_BOT_TOKEN=123:abc_{chat_id}\n"
        f"TELEGRAM_CHAT_ID={chat_id}\n"
    )


class ForceEodTelegramEnvRoutingTests(unittest.TestCase):
    """The force_eod tool must load the right Telegram env file
    per strategy so the EOD message goes to the correct channel.
    """

    def setUp(self) -> None:
        from vietnam_algo.tools import force_eod
        self.force_eod = force_eod
        self._orig_chat = os.environ.pop("TELEGRAM_CHAT_ID", None)
        self._orig_token = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.tmp = Path(tempfile.mkdtemp(prefix="force_eod_tg_test_"))
        # Replace PROJECT_ROOT-derived env paths with our temp dir.
        self._orig_project_root = force_eod.PROJECT_ROOT
        force_eod.PROJECT_ROOT = self.tmp
        self.fake_env = self.tmp / "vietnam_algo" / "env"
        self.fake_env.mkdir(parents=True)
        _make_fake_env(self.fake_env / ".env.telegram-autofin", "-1004382203924")
        _make_fake_env(self.fake_env / ".env.telegram", "-1001234567890")

    def tearDown(self) -> None:
        import shutil
        self.force_eod.PROJECT_ROOT = self._orig_project_root
        if self._orig_chat is not None:
            os.environ["TELEGRAM_CHAT_ID"] = self._orig_chat
        if self._orig_token is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = self._orig_token
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_autofin_loads_autofin_telegram_env(self) -> None:
        """``force_eod('autofin_signal')`` must set TELEGRAM_CHAT_ID
        to the autofin channel id, not the momentum one.
        """
        self.force_eod._load_telegram_env_for_strategy("autofin_signal")
        self.assertEqual(
            os.environ.get("TELEGRAM_CHAT_ID"), "-1004382203924",
            "force_eod(autofin_signal) must load .env.telegram-autofin, "
            "not .env.telegram (2026-10-18 bug: autofin EOD was sent to "
            "the momentum channel)",
        )
        self.assertNotEqual(
            os.environ.get("TELEGRAM_CHAT_ID"), "-1001234567890",
            "force_eod(autofin_signal) leaked the momentum chat id",
        )

    def test_momentum_loads_momentum_telegram_env(self) -> None:
        """``force_eod('momentum')`` must set TELEGRAM_CHAT_ID to
        the momentum channel id, not the autofin one.
        """
        self.force_eod._load_telegram_env_for_strategy("momentum")
        self.assertEqual(
            os.environ.get("TELEGRAM_CHAT_ID"), "-1001234567890",
            "force_eod(momentum) must load .env.telegram",
        )
        self.assertNotEqual(
            os.environ.get("TELEGRAM_CHAT_ID"), "-1004382203924",
            "force_eod(momentum) leaked the autofin chat id",
        )

    def test_unknown_strategy_falls_back_to_momentum(self) -> None:
        """An unknown strategy must still load SOMETHING (the
        momentum env is the safe default since it has been
        around longer). The point is to not silently drop
        TELEGRAM_CHAT_ID to None.
        """
        self.force_eod._load_telegram_env_for_strategy("foo_bar")
        self.assertEqual(
            os.environ.get("TELEGRAM_CHAT_ID"), "-1001234567890",
            "unknown strategies fall back to .env.telegram (momentum)",
        )


if __name__ == "__main__":
    unittest.main()
