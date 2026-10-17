"""
Vercel serverless webhook endpoint for the Telegram bot.

Telegram sends each update as an HTTP POST to this URL.
Register the webhook once with:
    https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://teamsync-ai-coordinator.vercel.app/api/bot

This replaces the long-polling bot.py for cloud deployments.
"""

import json
import logging
import os
import sys
from pathlib import Path

from flask import Flask, Response, request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["DATABASE_PATH"] = "/tmp/coordinator.sqlite3"

from config import load_environment
load_environment()

app = Flask(__name__)
logging.basicConfig(level=logging.WARNING)


@app.post("/api/bot")
async def webhook():
    """Receive a Telegram update and dispatch it to the bot handlers."""
    try:
        from telegram import Update
        from bot import build_application
        from classifier import create_classifier
        from config import bot_token, coordinator_chat_id, database_path
        from db import initialize

        path = database_path()
        initialize(path)

        token = bot_token()
        coordinator = coordinator_chat_id()
        classifier = create_classifier()

        application = build_application(token, path, coordinator, classifier)
        await application.initialize()

        data = request.get_json(force=True, silent=True) or {}
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        await application.shutdown()
    except Exception:
        logging.getLogger(__name__).exception("Webhook processing failed")

    # Always return 200 so Telegram does not retry.
    return Response("ok", status=200)
