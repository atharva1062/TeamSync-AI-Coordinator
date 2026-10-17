"""
Vercel serverless webhook endpoint for the Telegram bot.

Telegram sends each update as an HTTP POST to this URL.
The webhook was registered with:
    https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://teamsync-ai-coordinator.vercel.app/api/bot

This replaces the long-polling bot.py for cloud deployments.
Flask's built-in async support is NOT used here to stay compatible with
the standard @vercel/python runtime which does not install aiohttp extras.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from flask import Flask, Response, request

# Make project root importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["DATABASE_PATH"] = "/tmp/coordinator.sqlite3"

from config import load_environment
load_environment()

app = Flask(__name__)
logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine from synchronous code safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _dispatch(token, path, coordinator, data):
    """Build a minimal bot application and process one update."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters
    from messaging import followup, receive_reply
    from team_bot import commands, enroll
    from db import initialize

    initialize(path)

    # Build a lightweight application — no post_init worker, no post_stop.
    application = (
        Application.builder()
        .token(token)
        .build()
    )
    application.bot_data["database_path"] = path
    application.bot_data["coordinator_chat_id"] = coordinator
    application.bot_data["classifier"] = None  # AI classifier not needed for routing

    # Wire up the same handlers as bot.py.
    from bot import start, status, risk, handle_error
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("risk", risk))
    application.add_handler(CommandHandler("followup", followup))
    application.add_handler(CommandHandler("update", receive_reply))
    application.add_handler(CommandHandler(["confirm", "progress", "helpme"], commands))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reply))
    application.add_error_handler(handle_error)

    await application.initialize()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    await application.shutdown()


@app.post("/api/bot")
def webhook():
    """Receive a single Telegram update and dispatch it synchronously."""
    try:
        from config import bot_token, coordinator_chat_id, database_path

        data = request.get_json(force=True, silent=True) or {}
        path = database_path()
        token = bot_token()
        coordinator = coordinator_chat_id()

        _run(_dispatch(token, path, coordinator, data))
    except Exception:
        log.exception("Webhook processing failed")

    # Always return 200 — Telegram retries on any other status.
    return Response("ok", status=200)
