"""Tests for the Darvas Box Telegram bot."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))


class TelegramConfigTests(unittest.TestCase):
    """Tests for telegram config loading."""

    def test_load_from_env(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_CHAT_ID": "12345",
        }):
            from darvas_box_strategy.bot.telegram import load_telegram_config
            config = load_telegram_config()
            self.assertIsNotNone(config)
            self.assertEqual(config.bot_token, "test_token")
            self.assertEqual(config.chat_id, "12345")
            self.assertTrue(config.enabled)

    def test_load_disabled(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_CHAT_ID": "12345",
            "TELEGRAM_NOTIFICATIONS": "0",
        }):
            from darvas_box_strategy.bot.telegram import load_telegram_config
            config = load_telegram_config()
            self.assertIsNotNone(config)
            self.assertFalse(config.enabled)

    def test_load_from_env_file(self):
        """Loading the autofin fallback .env works when env vars are
        empty AND we explicitly pass a fixture path. (The real darvas
        env takes precedence in production, but for this test we
        override the lookup to use the fixture directly.)"""
        env_path = Path(__file__).resolve().parent / "fixtures" / ".env.telegram.test"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "TELEGRAM_BOT_TOKEN=file_token\n"
            "TELEGRAM_CHAT_ID=99999\n"
            "TELEGRAM_NOTIFICATIONS=1\n"
        )
        try:
            from darvas_box_strategy.bot.telegram import _parse_env_file
            parsed = _parse_env_file(env_path)
            self.assertEqual(parsed["TELEGRAM_BOT_TOKEN"], "file_token")
            self.assertEqual(parsed["TELEGRAM_CHAT_ID"], "99999")
        finally:
            env_path.unlink()

    def test_env_vars_override_file(self):
        """Env vars take precedence over any .env file."""
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "env_token",
            "TELEGRAM_CHAT_ID": "11111",
        }):
            from darvas_box_strategy.bot.telegram import load_telegram_config
            config = load_telegram_config(
                env_path=Path("/nonexistent/.env.fallback.test")
            )
            self.assertEqual(config.bot_token, "env_token")
            self.assertEqual(config.chat_id, "11111")


class ChunkMessageTests(unittest.TestCase):
    """Tests for message chunking."""

    def test_short_message(self):
        from darvas_box_strategy.bot.report import chunk_message
        text = "Hello world"
        chunks = list(chunk_message(text))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_long_message_splits(self):
        from darvas_box_strategy.bot.report import chunk_message
        # Use newlines to allow proper splitting
        lines = ["line" + str(i) + "\n" for i in range(500)]
        text = "".join(lines)
        chunks = list(chunk_message(text, max_len=1000))
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)


class BlueChipListTests(unittest.TestCase):
    """Tests for the curated HOSE blue chip list."""

    def test_blue_chip_file_exists(self):
        path = ROOT / "data" / "hose_blue_chips.json"
        self.assertTrue(path.exists())

    def test_blue_chips_have_50_to_100(self):
        path = ROOT / "data" / "hose_blue_chips.json"
        data = json.loads(path.read_text())
        self.assertGreaterEqual(data["count"], 50)
        self.assertLessEqual(data["count"], 100)
        self.assertEqual(len(data["tickers"]), data["count"])

    def test_no_etf_in_blue_chips(self):
        """E1VFVN30 is an ETF, should be excluded."""
        path = ROOT / "data" / "hose_blue_chips.json"
        data = json.loads(path.read_text())
        self.assertNotIn("E1VFVN30", data["tickers"])


class DefaultConfigTests(unittest.TestCase):
    """Tests for the default StrategyConfig."""

    def test_default_config_builds(self):
        from darvas_box_strategy.bot.cli import build_default_config
        cfg = build_default_config()
        self.assertEqual(cfg.breakout.volume_mult, 2.0)
        self.assertEqual(cfg.breakout.high_conviction_mult, 3.0)
        self.assertEqual(cfg.breakout.require_close_above_ma, 20)
        self.assertEqual(cfg.backtest.cost_bps, 25.0)
        self.assertEqual(cfg.backtest.max_concurrent_positions, 10)
        self.assertEqual(cfg.backtest.cooldown_bars_after_stop, 5)
        self.assertEqual(cfg.backtest.vol_target_pct, 0.0)  # OFF: fixed sizing wins
        # size_low=0.10, size_high=0.20 + max_total_exposure_pct=0.75
        # (2026-11-11) — the drawdown cap. Capping the NUMBER of open
        # positions does not cap the risk: smaller positions mean more of
        # them fit, so exposure rises exactly when losers cluster. With
        # the exposure cap the max drawdown falls on BOTH halves of the
        # walk-forward split (TRAIN -23.2% -> -17.0%, TEST -26.0%
        # -> -18.5%). See README "Reducing the drawdown".
        self.assertEqual(cfg.backtest.size_low, 0.10)
        self.assertEqual(cfg.backtest.size_high, 0.20)
        self.assertEqual(cfg.backtest.max_total_exposure_pct, 0.75)
        self.assertEqual(cfg.trailing.trail_mode, "pct")
        self.assertEqual(cfg.trailing.trail_from_peak_pct, 0.10)  # trail after partial cut
        self.assertTrue(cfg.exits.use_entry_anchored_stops)
        self.assertEqual(cfg.exits.sl_T_pct, 0.10)
        self.assertEqual(cfg.exits.sl_F_pct, 0.18)

    def test_default_config_enables_the_market_volatility_filter(self):
        """The market-volatility filter is ON in production.

        It is the only market feature whose separation runs the
        expected direction, and on the corrected NAV accounting it
        raises the return AND cuts the drawdown on both halves of the
        walk-forward split:

            config          TEST ret   TEST dd   TRAIN dd   win%
            without filter    +80.8%   -17.78%    -11.00%   63.1%
            with filter      +110.6%   -15.01%     -8.99%   66.8%
        """
        from darvas_box_strategy.bot.cli import build_default_config
        cfg = build_default_config()
        self.assertEqual(cfg.breakout.min_market_volatility_pct, 1.2)
        self.assertEqual(cfg.breakout.market_volatility_window, 20)
        self.assertTrue(
            cfg.breakout.volatility_index_path,
            "the volatility filter needs an index to read",
        )

    def test_default_config_does_not_enable_the_harmful_regime_filter(self):
        """The regime filter (index above its MA) removes the GOOD trades.

        A feature screen over 256 trades found the separation running
        the wrong way for every trend/level feature: trades taken in a
        WEAKER market did better. It must stay off, and it must stay
        independently switchable from the volatility filter.
        """
        from darvas_box_strategy.bot.cli import build_default_config
        cfg = build_default_config()
        self.assertEqual(cfg.breakout.regime_index_path, "")

    def test_the_market_index_file_exists(self):
        """The volatility filter reads the index from disk."""
        from pathlib import Path
        from darvas_box_strategy.bot.cli import build_default_config
        cfg = build_default_config()
        self.assertTrue(Path(cfg.breakout.volatility_index_path).exists())


class ReportFormatTests(unittest.TestCase):
    """Tests for report formatting (without full backtest)."""

    def test_chunk_preserves_content(self):
        from darvas_box_strategy.bot.report import chunk_message
        lines = ["line " + str(i) for i in range(100)]
        text = "\n".join(lines)
        chunks = list(chunk_message(text, max_len=500))
        rejoined = "\n".join(chunks)
        self.assertEqual(rejoined, text)


if __name__ == "__main__":
    unittest.main()
