"""Telegram notifier for the Darvas Box strategy.

Uses urllib (no extra deps) to send messages to a Telegram chat.
Reuses the credentials pattern from vietnam_algo/libs/notifier.py.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


def load_telegram_config(env_path: Optional[Path] = None) -> Optional[TelegramConfig]:
    """Load Telegram credentials.

    Lookup order:
    1. Environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    2. darvas_box_strategy/data/bot.env (this strategy's dedicated bot)
    3. The env_path argument (defaults to vietnam_algo/env/.env.telegram)

    The darvas-specific file is checked first so the new dedicated bot
    is independent of the autofin bot. The autofin .env is used as a
    fallback so existing setups continue to work.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    # Try darvas-specific file first (this bot's dedicated credentials).
    # __file__ = darvas_box_strategy/bot/telegram.py
    # parents[1] = darvas_box_strategy/
    darvas_env = Path(__file__).resolve().parents[1] / "data" / "bot.env"
    if (not token or not chat_id) and darvas_env.exists():
        for line in darvas_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN" and not token:
                token = v
            elif k == "TELEGRAM_CHAT_ID" and not chat_id:
                chat_id = v

    # Fall back to the autofin .env (shared file)
    if (not token or not chat_id) and env_path and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN" and not token:
                token = v
            elif k == "TELEGRAM_CHAT_ID" and not chat_id:
                chat_id = v

    enabled = os.environ.get("TELEGRAM_NOTIFICATIONS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }

    if not token or not chat_id:
        return None
    return TelegramConfig(bot_token=token, chat_id=chat_id, enabled=enabled)


def send_telegram(config: TelegramConfig, text: str, timeout: float = 10.0,
                  parse_mode: Optional[str] = None) -> dict:
    """Send a text message to Telegram. Returns Telegram API response dict."""
    if not config.enabled:
        return {"ok": False, "skipped": True, "reason": "telegram_disabled"}

    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    body = {
        "chat_id": config.chat_id,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        body["parse_mode"] = parse_mode

    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
        return json.loads(payload) if payload else {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send(config: Optional[TelegramConfig], text: str, **kwargs) -> Optional[dict]:
    """Convenience: send if config is set, return None otherwise."""
    if config is None:
        return None
    return send_telegram(config, text, **kwargs)


def main():
    """CLI test: send a test message."""
    config = load_telegram_config(
        env_path=Path(__file__).resolve().parents[2]
        / "vietnam_algo" / "env" / ".env.telegram"
    )
    if config is None:
        print("No Telegram config found. Set TELEGRAM_BOT_TOKEN/CHAT_ID or add .env.telegram")
        return 1
    print(f"Sending to chat_id={config.chat_id}...")
    result = send_telegram(config, "✅ Darvas Box bot: Telegram test message")
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
