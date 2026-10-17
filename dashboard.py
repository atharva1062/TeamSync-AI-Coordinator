"""Local, read-only project board: python dashboard.py --port 5050."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from flask import Flask, abort, render_template, request

from config import database_path, load_environment
from db import STATUSES

STATUS_CLASSES = {"Pending": "pending", "In Progress": "progress", "Blocked": "blocked", "Complete": "complete"}


def board_snapshot(path: Path) -> dict:
    # mode=ro cannot create a missing database or modify the bot's existing data.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        tasks = [dict(row) for row in conn.execute(
            "SELECT id, title, owner_name, status, blocker, updated_at, last_followup_at FROM tasks ORDER BY id"
        )]
        history = [dict(row) for row in conn.execute(
            "SELECT h.id, h.task_id, h.old_status, h.new_status, h.blocker, h.raw_message, h.created_at, "
            "t.title, t.owner_name FROM status_history h JOIN tasks t ON t.id = h.task_id ORDER BY h.id DESC LIMIT 15"
        )]
        metadata_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='board_metadata'").fetchone()
        simulated = bool(metadata_exists and conn.execute("SELECT 1 FROM board_metadata WHERE key='execution' AND value='simulated'").fetchone())
        return {"tasks": tasks, "history": history, "simulated": simulated}
    finally:
        conn.close()


def create_app(path: Path | None = None, *, workspace: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = Path(path) if path is not None else database_path()
    app.config["WORKSPACE_ENABLED"] = workspace
    if workspace:
        from workspace import install
        install(app)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/legacy")
    @app.get("/")
    def index():
        if workspace and request.path=="/":
            from flask import redirect, url_for
            return redirect(url_for("workspace_home"))
        selected = request.args.get("status", "")
        if selected and selected not in STATUSES:
            abort(400, description="Choose a status from the board filters.")
        query = request.args.get("q", "").strip()[:200]
        try:
            snapshot = board_snapshot(app.config["DATABASE_PATH"])
        except sqlite3.Error:
            app.logger.warning("Dashboard database unavailable")
            return render_template("unavailable.html"), 503
        tasks = snapshot["tasks"]
        counts = Counter(task["status"] for task in tasks)
        shown = [task for task in tasks if (not selected or task["status"] == selected)
                 and (not query or query.casefold() in f"{task['title']} {task['owner_name']} {task['blocker'] or ''}".casefold())]
        return render_template(
            "dashboard.html", tasks=shown, total=len(tasks), counts=counts,
            history=snapshot["history"], blockers=[task for task in tasks if task["status"] == "Blocked"],
            completion=round(100 * counts["Complete"] / len(tasks)) if tasks else 0,
            statuses=STATUSES, classes=STATUS_CLASSES, selected=selected, query=query,
            viewed_at=datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC"),
            simulated=snapshot["simulated"],
        )

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--database", type=Path, help="View a specific SQLite board, for example a simulated rehearsal snapshot")
    parser.add_argument("--workspace",action="store_true",help="Enable editable project workspace when using a custom database")
    parser.add_argument("--legacy",action="store_true",help="Open only the original read-only board")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    load_environment()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    create_app(args.database,workspace=not args.legacy and (args.workspace or args.database is None)).run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
