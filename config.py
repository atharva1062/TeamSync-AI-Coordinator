"""Configuration shared by the bot and local commands."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_environment() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)


def database_path() -> Path:
    path = Path(os.environ.get("DATABASE_PATH") or "data/coordinator.sqlite3")
    return path if path.is_absolute() else ROOT / path


def bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN in your local .env file first.")
    return token


def coordinator_chat_id() -> int | None:
    value = os.environ.get("COORDINATOR_CHAT_ID", "").strip()
    if not value:
        return None
    try:
        chat_id = int(value)
    except ValueError:
        raise ValueError("COORDINATOR_CHAT_ID must be a positive private chat ID.") from None
    if chat_id <= 0:
        raise ValueError("COORDINATOR_CHAT_ID must be a positive private chat ID.")
    return chat_id


def classification_settings() -> tuple[str, str]:
    if ai_provider() == "gemini":
        return (
            (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip(),
            os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite",
        )
    return (
        os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-4-6",
    )


def ai_provider() -> str:
    provider = os.environ.get("AI_PROVIDER", "claude").strip().lower() or "claude"
    if provider not in ("claude", "gemini"):
        raise ValueError("AI_PROVIDER must be claude or gemini.")
    return provider
