"""Vercel serverless entry point – exposes the Flask app as a WSGI handler."""

import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so all modules resolve.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# On Vercel the filesystem is read-only except /tmp.
# Force the database path to /tmp so initialize() can create tables.
os.environ["DATABASE_PATH"] = "/tmp/coordinator.sqlite3"

from config import load_environment

load_environment()

from dashboard import create_app

# workspace=True enables the full project workspace (setup, join, tasks).
app = create_app(workspace=True)


@app.post("/api/bot")
def bot_webhook():
    """Handle Telegram webhook directly in the main serverless app."""
    try:
        from api.bot import _run, _dispatch
        from config import bot_token, coordinator_chat_id, database_path
        from flask import Response, request

        data = request.get_json(force=True, silent=True) or {}
        path = database_path()
        token = bot_token()
        coordinator = coordinator_chat_id()

        _run(_dispatch(token, path, coordinator, data))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Webhook processing failed")

    return ("ok", 200)

