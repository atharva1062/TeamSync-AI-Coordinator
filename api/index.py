"""Vercel serverless entry point – exposes the Flask app as a WSGI handler."""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so all modules resolve.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_environment

load_environment()

from dashboard import create_app

# Vercel expects a module-level `app` (WSGI callable).
app = create_app(workspace=True)
