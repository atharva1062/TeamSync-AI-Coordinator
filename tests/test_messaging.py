import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden

from config import coordinator_chat_id
from db import assign_chat, connection, initialize, list_tasks, record_followup, seed_tasks
from messaging import followup, receive_reply


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.sqlite3"
        seed_tasks(self.path)
        assign_chat(self.path, 1, 101)
        self.context = SimpleNamespace(
            bot_data={"database_path": self.path, "coordinator_chat_id": 900},
            bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=50))),
        )

    def update(self, text="Started", chat_id=101, user_id=None, target=None, message_id=60, chat_type="private"):
        return SimpleNamespace(
            effective_message=SimpleNamespace(
                text=text, message_id=message_id, reply_text=AsyncMock(),
                reply_to_message=SimpleNamespace(message_id=target) if target is not None else None,
            ),
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            effective_user=SimpleNamespace(id=chat_id if user_id is None else user_id),
        )

    def rows(self, table):
        if table not in {"followups", "replies", "status_history"}:
            raise ValueError(table)
        with connection(self.path) as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    async def test_followup_only_coordinator_and_private(self):
        for update in (self.update(), self.update(chat_id=900, user_id=101), self.update(chat_id=900, chat_type="group")):
            await followup(update, self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_missing_coordinator_disables_sends(self):
        self.context.bot_data["coordinator_chat_id"] = None
        await followup(self.update(chat_id=900), self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_send_skips_unassigned_and_complete_and_records_success(self):
        assign_chat(self.path, 2, 102)
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status = 'Complete' WHERE id = 2")
        await followup(self.update(chat_id=900), self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.rows("followups")[0]["task_id"], 1)
        tasks = list_tasks(self.path)
        self.assertIsNotNone(tasks[0]["last_followup_at"])
        self.assertIsNone(tasks[1]["last_followup_at"])

    async def test_failed_send_does_not_mark_sent_and_continues(self):
        assign_chat(self.path, 2, 102)
        self.context.bot.send_message.side_effect = [Forbidden("blocked"), SimpleNamespace(message_id=51)]
        update = self.update(chat_id=900)
        await followup(update, self.context)
        self.assertEqual(self.context.bot.send_message.await_count, 2)
        self.assertIsNone(list_tasks(self.path)[0]["last_followup_at"])
        self.assertEqual(self.rows("followups")[0]["task_id"], 2)
        self.assertIn("1 failed", update.effective_message.reply_text.call_args.args[0])

    async def test_long_emoji_title_fits_one_followup(self):
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET title = ? WHERE id = 1", ("🚀" * 3000,))
        await followup(self.update(chat_id=900), self.context)
        text = self.context.bot.send_message.call_args.kwargs["text"]
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4000)
        self.assertIn("task #1", text)

    async def test_storage_failure_reports_delivered_but_untracked(self):
        update = self.update(chat_id=900)
        with patch("messaging.record_followup", side_effect=sqlite3.OperationalError("locked")):
            await followup(update, self.context)
        self.assertIn("tracking failed", update.effective_message.reply_text.call_args.args[0])
        self.assertIsNone(list_tasks(self.path)[0]["last_followup_at"])

    async def test_direct_reply_matches_after_reinitialize(self):
        record_followup(self.path, 1, 101, 50)
        initialize(self.path)
        await receive_reply(self.update(target=50), self.context)
        self.assertEqual(self.rows("replies")[0]["task_id"], 1)
        self.assertEqual(self.rows("replies")[0]["raw_message"], "Started")
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("status_history"), [])

    async def test_multiple_tasks_asks_without_saving(self):
        assign_chat(self.path, 2, 101)
        update = self.update()
        await receive_reply(update, self.context)
        self.assertEqual(self.rows("replies"), [])
        self.assertIn("Which task", update.effective_message.reply_text.call_args.args[0])

    async def test_explicit_task_selection(self):
        assign_chat(self.path, 2, 101)
        await receive_reply(self.update("/update #2 Haan ho gaya, review baaki hai"), self.context)
        row = self.rows("replies")[0]
        self.assertEqual(row["task_id"], 2)
        self.assertEqual(row["raw_message"], "Haan ho gaya, review baaki hai")

    async def test_single_open_task_can_match_unthreaded_reply(self):
        await receive_reply(self.update(), self.context)
        self.assertEqual(self.rows("replies")[0]["task_id"], 1)

    async def test_unknown_prompt_does_not_fall_back(self):
        await receive_reply(self.update(target=999), self.context)
        self.assertEqual(self.rows("replies"), [])

    async def test_cross_owner_and_spoofed_sender_rejected(self):
        record_followup(self.path, 1, 101, 50)
        for update in (self.update(chat_id=102, target=50), self.update(user_id=102), self.update("/update 2 done")):
            await receive_reply(update, self.context)
        self.assertEqual(self.rows("replies"), [])

    async def test_reassigned_task_invalidates_old_prompt(self):
        record_followup(self.path, 1, 101, 50)
        assign_chat(self.path, 1, 102)
        await receive_reply(self.update(target=50), self.context)
        self.assertEqual(self.rows("replies"), [])

    async def test_duplicate_reply_collected_once(self):
        update = self.update()
        await receive_reply(update, self.context)
        await receive_reply(update, self.context)
        self.assertEqual(len(self.rows("replies")), 1)

    async def test_invalid_selection_and_whitespace_are_safe(self):
        for text in ("/update", "/update 1", "/update abc done", "/update ² done", "/update " + "9" * 100 + " done", "   "):
            await receive_reply(self.update(text), self.context)
        self.assertEqual(self.rows("replies"), [])

    async def test_completed_task_not_collected(self):
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status = 'Complete' WHERE id = 1")
        await receive_reply(self.update("/update 1 done"), self.context)
        self.assertEqual(self.rows("replies"), [])


class CoordinatorConfigTests(unittest.TestCase):
    def test_missing_valid_and_invalid_ids(self):
        for value, expected in (("", None), ("123", 123)):
            with patch.dict(os.environ, {"COORDINATOR_CHAT_ID": value}):
                self.assertEqual(coordinator_chat_id(), expected)
        for value in ("abc", "-100", "0"):
            with patch.dict(os.environ, {"COORDINATOR_CHAT_ID": value}), self.assertRaises(ValueError):
                coordinator_chat_id()
