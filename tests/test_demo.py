import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from board import format_risk
from bot import risk
from capture_evidence import capture
from classifier import SYSTEM_PROMPT
from dashboard import create_app
from db import assign_chat, connection, list_tasks, seed_tasks
from evaluate import load_cases
from preflight import check_accuracy, check_readiness
from rehearse import run_rehearsal


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.sqlite3"
        seed_tasks(self.path)

    def test_counts_blockers_and_unassigned_open_tasks(self):
        assign_chat(self.path, 1, 101)
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status='Blocked', blocker='Waiting for credentials' WHERE id=1")
            conn.execute("UPDATE tasks SET status='Complete' WHERE id=3")
        report = format_risk(list_tasks(self.path))
        self.assertIn("1 of 4 done (25%)", report)
        self.assertIn("Kabir: Waiting for credentials", report)
        self.assertIn("2 open task(s) have no owner chat ID", report)
        self.assertNotIn("#3 Write", report)
        self.assertNotIn("101", report)

    def test_empty_and_complete_board(self):
        self.assertIn("No tasks yet", format_risk([]))
        with connection(self.path) as conn:
            conn.execute("UPDATE tasks SET status='Complete'")
        report = format_risk(list_tasks(self.path))
        self.assertIn("All tasks are complete", report)
        self.assertNotIn("Follow-up gap", report)


class RehearsalTests(unittest.IsolatedAsyncioTestCase):
    async def test_risk_handler_reads_existing_board(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.sqlite3"
            seed_tasks(path)
            message = SimpleNamespace(reply_text=AsyncMock())
            await risk(SimpleNamespace(effective_message=message), SimpleNamespace(bot_data={"database_path": path}))
            self.assertIn("0 of 4 done", message.reply_text.call_args.args[0])

    async def test_isolated_rehearsal_produces_labeled_artifacts_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rehearsal"
            manifest = await run_rehearsal(output)
            self.assertTrue(manifest["passed"])
            self.assertEqual(manifest["telegram_requests"], 0)
            self.assertEqual(manifest["claude_requests"], 0)
            self.assertEqual(manifest["execution"], "simulated")
            self.assertIn("Waiting for test credentials", (output / "blocked_board.txt").read_text(encoding="utf-8"))
            self.assertIn("Kabir | Complete", (output / "final_board.txt").read_text(encoding="utf-8"))
            history = json.loads((output / "status_history.json").read_text(encoding="utf-8"))["records"]
            self.assertEqual(len(history), 3)
            self.assertEqual(history[0]["new_status"], "Blocked")
            self.assertEqual(history[-1]["new_status"], "Complete")
            response = create_app(output / "rehearsal.sqlite3").test_client().get("/")
            self.assertIn("Simulated rehearsal.", response.get_data(as_text=True))
            self.assertIn("Waiting for test credentials", response.get_data(as_text=True))
            captured = capture(output / "rehearsal.sqlite3", output / "capture")
            self.assertEqual(captured["execution"], "simulated")
            self.assertNotIn("telegram_chat_id", json.dumps(captured))
            with self.assertRaises(FileExistsError):
                await run_rehearsal(output)

    async def test_capture_preserves_database_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "board.sqlite3"
            seed_tasks(path)
            assign_chat(path, 1, 987654321)
            before = path.read_bytes()
            captured = capture(path, root / "evidence")
            self.assertEqual(captured["execution"], "database_snapshot")
            self.assertNotIn("987654321", json.dumps(captured))
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(FileExistsError):
                capture(path, root / "evidence")


class PreflightTests(unittest.TestCase):
    def test_missing_db_not_created_and_secrets_not_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            with patch("preflight.bot_token", return_value="SECRET_TOKEN"), patch("preflight.classification_settings", return_value=("SECRET_KEY", "model")), patch("preflight.coordinator_chat_id", return_value=123):
                checks = check_readiness(path, Path(directory) / "no-report.json")
            self.assertFalse(path.exists())
            self.assertNotIn("SECRET", json.dumps(checks))
            self.assertFalse(next(check["ready"] for check in checks if check["name"] == "Database"))

    def test_unassigned_tasks_and_missing_settings_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.sqlite3"
            seed_tasks(path)
            with patch("preflight.bot_token", side_effect=ValueError()), patch("preflight.classification_settings", return_value=("", "model")), patch("preflight.coordinator_chat_id", return_value=None):
                checks = check_readiness(path, Path(directory) / "no-report.json")
            by_name = {check["name"]: check for check in checks}
            self.assertFalse(by_name["Telegram configuration"]["ready"])
            self.assertFalse(by_name["Demo owner assignments"]["ready"])
            self.assertTrue(by_name["Database"]["ready"])

    def test_accuracy_report_must_match_model_prompt_dataset_and_live_execution(self):
        cases = load_cases()
        report = {
            "execution": "live_claude", "model": "test-model", "passed": True, "total": 20,
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "cases": [{"id": case["id"], "expected_status": case["expected_status"], "correct": True, "unsafe_completion": False} for case in cases],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertTrue(check_accuracy(path, "test-model"))
            self.assertFalse(check_accuracy(path, "different-model"))
            for key, value in (("execution", "simulated"), ("prompt_sha256", "old"), ("cases_sha256", "old"), ("passed", False)):
                changed = {**report, key: value}
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertFalse(check_accuracy(path, "test-model"))
