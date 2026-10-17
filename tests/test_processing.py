import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden

from classifier import validate_result
from db import assign_chat, connection, finish_classification, initialize, list_tasks, seed_tasks
from messaging import receive_reply
from test_classifier import output


class ProcessingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.sqlite3"
        seed_tasks(self.path)
        assign_chat(self.path, 1, 101)
        self.model = SimpleNamespace(classify=AsyncMock(return_value=validate_result(output())))
        self.context = SimpleNamespace(
            bot_data={"database_path": self.path, "coordinator_chat_id": 900, "classifier": self.model},
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

    def update(self, text="Started", message_id=60, target=None):
        return SimpleNamespace(
            effective_message=SimpleNamespace(
                text=text, message_id=message_id,
                reply_to_message=SimpleNamespace(message_id=target) if target else None,
                reply_text=AsyncMock(return_value=SimpleNamespace(message_id=150)),
            ),
            effective_chat=SimpleNamespace(id=101, type="private"),
            effective_user=SimpleNamespace(id=101),
        )

    def rows(self, table):
        allowed = {"classification_results", "status_history", "replies", "clarification_contexts"}
        if table not in allowed:
            raise ValueError(table)
        with connection(self.path) as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    async def test_blocker_then_resolution_clears_reason_and_audits(self):
        self.model.classify.return_value = validate_result(output(status="Complete", blocker="Waiting for test credentials"))
        await receive_reply(self.update("Connected, but waiting for test credentials"), self.context)
        task = list_tasks(self.path)[0]
        self.assertEqual(task["status"], "Blocked")
        self.assertEqual(task["blocker"], "Waiting for test credentials")
        self.model.classify.return_value = validate_result(output(status="Complete", progress_note="Tested and merged"))
        await receive_reply(self.update("Credentials received, tested and merged", 61), self.context)
        task = list_tasks(self.path)[0]
        self.assertEqual(task["status"], "Complete")
        self.assertIsNone(task["blocker"])
        history = self.rows("status_history")
        self.assertEqual([(row["old_status"], row["new_status"]) for row in history], [("Pending", "Blocked"), ("Blocked", "Complete")])

    async def test_clarification_survives_restart_and_matches_question(self):
        assign_chat(self.path, 2, 101)
        self.model.classify.return_value = validate_result(output(status=None, confidence=0.4, clarifying_question="Is review still pending?"))
        await receive_reply(self.update("/update 1 Haan ho gaya, review baaki hai"), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("replies")[0]["processing_state"], "needs_clarification")
        initialize(self.path)
        self.model.classify.return_value = validate_result(output(status="Blocked", blocker="Review pending", dependency_open=True))
        await receive_reply(self.update("Yes, review pending", 61, target=150), self.context)
        evidence = self.model.classify.call_args.args[1]
        self.assertEqual([entry["role"] for entry in evidence], ["owner", "question", "owner"])
        self.assertIn("Haan ho gaya", evidence[0]["text"])
        self.assertEqual(list_tasks(self.path)[0]["status"], "Blocked")
        self.assertEqual(self.rows("clarification_contexts"), [])
        self.assertIn("Yes, review pending", self.rows("status_history")[0]["raw_message"])

    async def test_low_confidence_never_changes_board(self):
        self.model.classify.return_value = validate_result(output(status="Complete", confidence=0.59))
        await receive_reply(self.update("Maybe done"), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("status_history"), [])

    async def test_model_error_records_failure_and_alerts_coordinator(self):
        self.model.classify.side_effect = TimeoutError("secret must not be sent")
        await receive_reply(self.update(), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("replies")[0]["processing_state"], "failed")
        self.assertEqual(self.rows("classification_results")[0]["error_type"], "TimeoutError")
        alert = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(alert["chat_id"], 900)
        self.assertNotIn("secret", alert["text"])

    async def test_malformed_model_result_does_not_change_board(self):
        self.model.classify.return_value = SimpleNamespace(to_json=lambda: '{"status":"Complete"}')
        await receive_reply(self.update(), self.context)
        self.assertEqual(self.rows("status_history"), [])
        self.assertEqual(self.rows("replies")[0]["processing_state"], "failed")

    async def test_api_failure_alert_delivery_failure_is_contained(self):
        self.model.classify.side_effect = RuntimeError()
        self.context.bot.send_message.side_effect = Forbidden("blocked")
        await receive_reply(self.update(), self.context)
        self.assertEqual(self.rows("replies")[0]["processing_state"], "failed")

    async def test_duplicate_message_does_not_call_model_twice(self):
        await receive_reply(self.update(), self.context)
        duplicate = self.update()
        await receive_reply(duplicate, self.context)
        self.model.classify.assert_awaited_once()
        self.assertEqual(len(self.rows("status_history")), 1)
        self.assertIn("/status", duplicate.effective_message.reply_text.call_args.args[0])
        self.assertNotIn("unchanged", duplicate.effective_message.reply_text.call_args.args[0])

    async def test_corrupt_clarification_preserves_board_and_allows_fresh_retry(self):
        self.model.classify.return_value = validate_result(output(status=None, confidence=0.2))
        await receive_reply(self.update("Will see"), self.context)
        with connection(self.path) as conn:
            conn.execute("UPDATE clarification_contexts SET evidence_json = 'broken JSON'")
        self.model.classify.reset_mock()
        await receive_reply(self.update("Not sure", 61, 150), self.context)
        self.model.classify.assert_not_awaited()
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("clarification_contexts"), [])
        self.assertEqual(self.rows("classification_results")[-1]["error_type"], "ClarificationContextError")
        self.assertEqual(self.rows("replies")[-1]["processing_state"], "failed")
        self.model.classify.return_value = validate_result(output(status="In Progress"))
        await receive_reply(self.update("/update 1 Started implementing now", 62), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "In Progress")
        self.assertEqual(len(self.rows("status_history")), 1)

    async def test_stale_assignment_during_model_call_rejected(self):
        async def changed(task, evidence):
            assign_chat(self.path, 1, 102)
            return validate_result(output(status="Complete"))
        self.model.classify.side_effect = changed
        await receive_reply(self.update(), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("classification_results")[0]["error_type"], "StaleTask")

    async def test_atomic_rollback_if_history_insert_fails(self):
        with connection(self.path) as conn:
            conn.execute("CREATE TRIGGER reject_history BEFORE INSERT ON status_history BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await receive_reply(self.update(), self.context)
        self.assertEqual(list_tasks(self.path)[0]["status"], "Pending")
        self.assertEqual(self.rows("classification_results"), [])
        self.assertEqual(self.rows("replies")[0]["processing_state"], "received")

    async def test_whole_operation_timeout_is_handled(self):
        async def timeout(awaitable, timeout):
            awaitable.close()
            raise TimeoutError()
        with patch("processing.asyncio.wait_for", new=timeout):
            await receive_reply(self.update(), self.context)
        self.assertEqual(self.rows("replies")[0]["processing_state"], "failed")

    async def test_context_limit_requests_full_update_without_model_call(self):
        self.model.classify.return_value = validate_result(output(status=None, confidence=0.1))
        await receive_reply(self.update("x" * 4000), self.context)
        with connection(self.path) as conn:
            evidence = json.dumps([{"role": "owner", "text": "x" * 24000}])
            conn.execute("UPDATE clarification_contexts SET evidence_json = ?", (evidence,))
        self.model.classify.reset_mock()
        await receive_reply(self.update("Still unsure", 61, 150), self.context)
        self.model.classify.assert_not_awaited()
        self.assertEqual(self.rows("status_history"), [])
