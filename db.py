"""SQLite persistence. Each operation owns and closes its connection."""

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

STATUSES = ("Pending", "In Progress", "Blocked", "Complete")


@contextmanager
def connection(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(path: Path) -> None:
    with connection(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                owner_name TEXT NOT NULL CHECK (length(trim(owner_name)) > 0),
                telegram_chat_id INTEGER,
                status TEXT NOT NULL DEFAULT 'Pending'
                    CHECK (status IN ('Pending', 'In Progress', 'Blocked', 'Complete')),
                blocker TEXT,
                last_followup_at TEXT,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                CHECK (
                    (status = 'Blocked' AND blocker IS NOT NULL AND length(trim(blocker)) > 0)
                    OR (status != 'Blocked' AND blocker IS NULL)
                )
            );
            CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                old_status TEXT NOT NULL CHECK (old_status IN ('Pending', 'In Progress', 'Blocked', 'Complete')),
                new_status TEXT NOT NULL CHECK (new_status IN ('Pending', 'In Progress', 'Blocked', 'Complete')),
                blocker TEXT,
                raw_message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(telegram_chat_id);
            CREATE INDEX IF NOT EXISTS idx_history_task ON status_history(task_id, id);
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                telegram_chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (telegram_chat_id, telegram_message_id)
            );
            CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                telegram_chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                raw_message TEXT NOT NULL,
                processing_state TEXT NOT NULL DEFAULT 'received' CHECK (
                    processing_state IN ('received', 'processed', 'needs_clarification', 'failed')
                ),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (telegram_chat_id, telegram_message_id)
            );
            CREATE TABLE IF NOT EXISTS classification_results (
                reply_id INTEGER PRIMARY KEY REFERENCES replies(id),
                result_json TEXT,
                evidence_json TEXT NOT NULL,
                error_type TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS clarification_contexts (
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                telegram_chat_id INTEGER NOT NULL,
                reply_id INTEGER NOT NULL REFERENCES replies(id),
                evidence_json TEXT NOT NULL,
                question TEXT NOT NULL,
                prompt_message_id INTEGER,
                PRIMARY KEY (task_id, telegram_chat_id)
            );
        """)
        from teamspace import ensure_schema
        ensure_schema(conn)


def list_tasks(path: Path) -> list[dict]:
    with connection(path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]


def seed_tasks(path: Path) -> int:
    """Seed only an empty board; never overwrite an existing project's work."""
    initialize(path)
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
            return 0
        rows = [
            ("Connect the payment API", "Kabir", "Pending"),
            ("Design the login page", "Asha", "In Progress"),
            ("Write the report intro", "Rohan", "Pending"),
            ("Prepare final slides", "Meera", "Pending"),
        ]
        conn.executemany("INSERT INTO tasks (title, owner_name, status) VALUES (?, ?, ?)", rows)
        return len(rows)


def assign_chat(path: Path, task_id: int, chat_id: int) -> bool:
    if chat_id <= 0:
        raise ValueError("Use a positive private Telegram chat ID from /start.")
    with connection(path) as conn:
        conn.execute(
            "DELETE FROM clarification_contexts WHERE task_id = ? AND telegram_chat_id != ?",
            (task_id, chat_id),
        )
        cursor = conn.execute(
            "UPDATE tasks SET telegram_chat_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (chat_id, task_id),
        )
        return cursor.rowcount == 1


def record_followup(path: Path, task_id: int, chat_id: int, message_id: int) -> None:
    """Call only after Telegram confirms delivery. Mapping and timestamp are atomic."""
    with connection(path) as conn:
        conn.execute(
            "INSERT INTO followups (task_id, telegram_chat_id, telegram_message_id) VALUES (?, ?, ?)",
            (task_id, chat_id, message_id),
        )
        conn.execute(
            "UPDATE tasks SET last_followup_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (task_id,),
        )


def task_for_prompt(path: Path, chat_id: int, message_id: int) -> dict | None:
    with connection(path) as conn:
        clarification = conn.execute(
            "SELECT t.* FROM clarification_contexts c JOIN tasks t ON t.id = c.task_id "
            "WHERE c.telegram_chat_id = ? AND c.prompt_message_id = ? AND t.telegram_chat_id = ?",
            (chat_id, message_id, chat_id),
        ).fetchone()
        if clarification:
            return dict(clarification)
        row = conn.execute(
            "SELECT t.* FROM followups f JOIN tasks t ON t.id = f.task_id "
            "WHERE f.telegram_chat_id = ? AND f.telegram_message_id = ? "
            "AND t.telegram_chat_id = ?",
            (chat_id, message_id, chat_id),
        ).fetchone()
        return dict(row) if row else None


def record_reply(path: Path, task_id: int, chat_id: int, message_id: int, text: str) -> bool:
    """Store evidence once, rechecking current ownership and open status."""
    with connection(path) as conn:
        cursor = conn.execute(
            "INSERT INTO replies (task_id, telegram_chat_id, telegram_message_id, raw_message) "
            "SELECT id, ?, ?, ? FROM tasks WHERE id = ? AND telegram_chat_id = ? AND status != 'Complete' "
            "ON CONFLICT (telegram_chat_id, telegram_message_id) DO NOTHING",
            (chat_id, message_id, text, task_id, chat_id),
        )
        return cursor.rowcount == 1


def clarification_context(path: Path, task_id: int, chat_id: int) -> dict | None:
    with connection(path) as conn:
        row = conn.execute(
            "SELECT * FROM clarification_contexts WHERE task_id = ? AND telegram_chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        return dict(row) if row else None


def set_clarification_prompt(path: Path, task_id: int, chat_id: int, message_id: int) -> None:
    with connection(path) as conn:
        conn.execute(
            "UPDATE clarification_contexts SET prompt_message_id = ? WHERE task_id = ? AND telegram_chat_id = ?",
            (message_id, task_id, chat_id),
        )


def finish_classification(path: Path, task: dict, chat_id: int, message_id: int,
                          evidence: list[dict], result=None, error_type: str | None = None) -> str:
    """Commit result, board, audit, and clarification state together, or roll back all."""
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        reply = conn.execute(
            "SELECT * FROM replies WHERE telegram_chat_id = ? AND telegram_message_id = ? AND task_id = ?",
            (chat_id, message_id, task["id"]),
        ).fetchone()
        if reply is None or reply["processing_state"] != "received":
            return "duplicate"
        current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        if (current is None or current["telegram_chat_id"] != chat_id
                or any(current[key] != task[key] for key in ("status", "blocker", "updated_at"))):
            error_type = "StaleTask"
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        conn.execute(
            "INSERT INTO classification_results (reply_id, result_json, evidence_json, error_type) VALUES (?, ?, ?, ?)",
            (reply["id"], result.to_json() if result else None, evidence_json, error_type),
        )
        if error_type:
            state = "failed"
            if error_type == "ClarificationContextError":
                conn.execute(
                    "DELETE FROM clarification_contexts WHERE task_id = ? AND telegram_chat_id = ?",
                    (task["id"], chat_id),
                )
        elif result is None:
            raise ValueError("Classification result required")
        elif result.needs_clarification:
            state = "needs_clarification"
            conn.execute(
                "INSERT INTO clarification_contexts (task_id, telegram_chat_id, reply_id, evidence_json, question) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (task_id, telegram_chat_id) DO UPDATE SET "
                "reply_id = excluded.reply_id, evidence_json = excluded.evidence_json, question = excluded.question, prompt_message_id = NULL",
                (task["id"], chat_id, reply["id"], evidence_json, result.clarifying_question),
            )
        else:
            state = "processed"
            conn.execute(
                "UPDATE tasks SET status = ?, blocker = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (result.status, result.blocker, task["id"]),
            )
            conn.execute("UPDATE tasks SET progress_percent=CASE WHEN status='Complete' THEN 100 WHEN status='Pending' THEN 0 ELSE progress_percent END WHERE id=?",(task["id"],))
            conn.execute(
                "INSERT INTO status_history (task_id, old_status, new_status, blocker, raw_message) VALUES (?, ?, ?, ?, ?)",
                (task["id"], current["status"], result.status, result.blocker,
                 "\n".join(f"{item['role']}: {item['text']}" for item in evidence)),
            )
            conn.execute("DELETE FROM clarification_contexts WHERE task_id = ?", (task["id"],))
        conn.execute("UPDATE replies SET processing_state = ? WHERE id = ?", (state, reply["id"]))
        return state
