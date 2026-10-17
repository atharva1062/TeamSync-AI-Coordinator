import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from board import format_board, message_chunks, truncate_text
from bot import start, status
from db import assign_chat, connection, initialize, list_tasks, seed_tasks


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "board.sqlite3"
        initialize(self.path)

    def test_seed_is_repeatable_and_preserves_assignment(self):
        self.assertEqual(seed_tasks(self.path), 4)
        self.assertTrue(assign_chat(self.path, 1, 123456))
        self.assertEqual(seed_tasks(self.path), 0)
        initialize(self.path)
        tasks = list_tasks(self.path)
        self.assertEqual(len(tasks), 4)
        self.assertEqual(tasks[0]["telegram_chat_id"], 123456)

    def test_empty_board(self):
        self.assertIn("No tasks yet", format_board(list_tasks(self.path)))

    def test_summary(self):
        seed_tasks(self.path)
        board = format_board(list_tasks(self.path))
        self.assertIn("4 tasks: 0 done, 1 in progress, 0 blocked, 3 pending", board)
        self.assertIn("Kabir", board)

    def test_invalid_status_rejected(self):
        seed_tasks(self.path)
        with self.assertRaises(sqlite3.IntegrityError), connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status = 'Finished' WHERE id = 1")

    def test_blocked_requires_reason(self):
        seed_tasks(self.path)
        with self.assertRaises(sqlite3.IntegrityError), connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status = 'Blocked' WHERE id = 1")

    def test_complete_cannot_retain_blocker(self):
        seed_tasks(self.path)
        with self.assertRaises(sqlite3.IntegrityError), connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status = 'Complete', blocker = 'Waiting' WHERE id = 1")

    def test_history_enforces_task_reference(self):
        with self.assertRaises(sqlite3.IntegrityError), connection(self.path) as conn:
            conn.execute("INSERT INTO status_history (task_id, old_status, new_status, raw_message) VALUES (999, 'Pending', 'Complete', 'done')")

    def test_assignment_validation(self):
        self.assertFalse(assign_chat(self.path, 999, 123))
        with self.assertRaises(ValueError):
            assign_chat(self.path, 1, -123)

    def test_message_splitting_preserves_content(self):
        original = "Task " * 2500
        chunks = message_chunks(original)
        self.assertEqual("".join(chunks), original)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))

    def test_emoji_chunks_preserve_text_and_fit_utf16_budget(self):
        original = "Started 🚀; review बाकी है ✅ " * 1000
        chunks = message_chunks(original)
        self.assertEqual("".join(chunks), original)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks))
        self.assertEqual(message_chunks("a🚀b", limit=2), ["a", "🚀", "b"])
        self.assertEqual(message_chunks(""), [])

    def test_unicode_prompt_truncation(self):
        self.assertEqual(truncate_text("a🚀b", 2), "a")
        self.assertEqual(truncate_text("🚀" * 2500, 2500), "🚀" * 1250)
        self.assertEqual(truncate_text("a", 0), "")
        with self.assertRaises(ValueError):
            message_chunks("🚀", 1)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_returns_chat_id(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(id=123))
        await start(update, None)
        self.assertIn("Your chat ID: 123", message.reply_text.call_args.args[0])

    async def test_status_reads_persisted_board(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.sqlite3"
            seed_tasks(path)
            message = SimpleNamespace(reply_text=AsyncMock())
            await status(SimpleNamespace(effective_message=message), SimpleNamespace(bot_data={"database_path": path}))
            self.assertIn("Connect the payment API", message.reply_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
