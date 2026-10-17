"""Rehearse the real handlers with simulated Telegram/Claude and an isolated board."""

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from board import format_board, format_risk
from bot import risk, status
from classifier import validate_result
from config import ROOT
from db import assign_chat, connection, list_tasks, seed_tasks
from messaging import followup, receive_reply


class SimulatedTelegram:
    def __init__(self):
        self.transcript = []
        self.next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        self.transcript.append({"direction": "bot", "chat_id": chat_id, "message_id": self.next_id, "text": text})
        return SimpleNamespace(message_id=self.next_id)

    def update(self, chat_id, text, target=None):
        self.next_id += 1
        message_id = self.next_id
        self.transcript.append({"direction": "owner", "chat_id": chat_id, "message_id": message_id, "text": text})

        async def reply_text(text, **kwargs):
            return await self.send_message(chat_id=chat_id, text=text, **kwargs)

        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id, type="private"),
            effective_user=SimpleNamespace(id=chat_id),
            effective_message=SimpleNamespace(text=text, message_id=message_id, reply_text=reply_text,
                reply_to_message=SimpleNamespace(message_id=target) if target is not None else None),
        )


class ScriptedClassifier:
    def __init__(self):
        self.answers = deque([
            {"status": "Blocked", "blocker": "Waiting for test credentials", "progress_note": "API connection done", "confidence": 0.95, "dependency_open": True, "clarifying_question": None},
            {"status": None, "blocker": None, "progress_note": "", "confidence": 0.2, "dependency_open": False, "clarifying_question": "Have you started, finished, or are you waiting on something?"},
            {"status": "In Progress", "blocker": None, "progress_note": "Login page in progress", "confidence": 0.93, "dependency_open": False, "clarifying_question": None},
            {"status": "Complete", "blocker": None, "progress_note": "Credentials received, tested and merged", "confidence": 0.98, "dependency_open": False, "clarifying_question": None},
        ])

    async def classify(self, task, evidence):
        return validate_result(json.dumps(self.answers.popleft()))


async def run_rehearsal(output: Path) -> dict:
    # Refuse an existing destination rather than overwriting prior evidence.
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="coordinator-rehearsal-") as directory:
        path = Path(directory) / "board.sqlite3"
        seed_tasks(path)
        for task_id in range(1, 5):
            assign_chat(path, task_id, 100 + task_id)
        telegram = SimulatedTelegram()
        classifier = ScriptedClassifier()
        context = SimpleNamespace(bot=telegram, bot_data={"database_path": path, "coordinator_chat_id": 900, "classifier": classifier})
        before = format_board(list_tasks(path))
        await status(telegram.update(900, "/status"), context)
        await followup(telegram.update(900, "/followup"), context)
        prompt = next(entry["message_id"] for entry in telegram.transcript if entry["direction"] == "bot" and entry["chat_id"] == 101)
        await receive_reply(telegram.update(101, "Connected, but waiting for test credentials", prompt), context)
        blocked_tasks = list_tasks(path)
        if blocked_tasks[0]["status"] != "Blocked" or not blocked_tasks[0]["blocker"]:
            raise RuntimeError("Blocker scenario failed")
        blocked = format_board(blocked_tasks)
        risk_report = format_risk(blocked_tasks)
        # Keep the blocked state as a separate, clearly simulated dashboard fixture.
        with connection(path) as source:
            source.execute("CREATE TABLE board_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            source.execute("INSERT INTO board_metadata VALUES ('execution', 'simulated')")
        with connection(path) as source:
            target = sqlite3.connect(output / "rehearsal.sqlite3")
            try:
                source.backup(target)
            finally:
                target.close()
        await risk(telegram.update(900, "/risk"), context)
        await receive_reply(telegram.update(102, "Will see"), context)
        if list_tasks(path)[1]["status"] != "In Progress":
            raise RuntimeError("Vague reply changed the board")
        question = telegram.transcript[-1]["message_id"]
        await receive_reply(telegram.update(102, "Started designing, still in progress", question), context)
        await receive_reply(telegram.update(101, "Credentials received, tested and merged. Nothing pending."), context)
        tasks = list_tasks(path)
        if tasks[0]["status"] != "Complete" or tasks[0]["blocker"] is not None:
            raise RuntimeError("Blocker resolution failed")
        with connection(path) as conn:
            history = [dict(row) for row in conn.execute("SELECT * FROM status_history ORDER BY id")]
            states = [row[0] for row in conn.execute("SELECT processing_state FROM replies ORDER BY id")]
        if len(history) != 3 or states != ["processed", "needs_clarification", "processed", "processed"] or classifier.answers:
            raise RuntimeError("Audit or clarification scenario failed")
        for name, text in (("before_board.txt", before), ("blocked_board.txt", blocked),
                           ("final_board.txt", format_board(tasks)), ("risk_report.txt", risk_report)):
            (output / name).write_text("SIMULATED REHEARSAL — NOT LIVE INTEGRATION EVIDENCE\n\n" + text, encoding="utf-8")
        for name, content in (("transcript.json", telegram.transcript), ("status_history.json", history)):
            (output / name).write_text(json.dumps({"execution": "simulated", "records": content}, indent=2, ensure_ascii=False), encoding="utf-8")
        manifest = {"execution": "simulated", "passed": True, "telegram_requests": 0, "claude_requests": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(), "history_rows": len(history),
                    "scenarios": ["followup", "blocker_capture", "risk_report", "vague_reply", "clarification", "blocker_resolution"],
                    "dashboard_fixture": "rehearsal.sqlite3 (blocked snapshot; synthetic owners and chat IDs)"}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (output / "README.txt").write_text(
            "SIMULATED REHEARSAL ONLY. Telegram and Claude were replaced by scripted adapters.\n"
            "The real project database and credentials were not used.\n"
            "rehearsal.sqlite3 shows the intermediate blocked state; final_board.txt shows resolution.\n"
            "Do not present these files as live integration or model accuracy evidence.\n", encoding="utf-8")
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "rehearsal" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    try:
        asyncio.run(run_rehearsal(args.output))
    except FileExistsError:
        parser.error("Output folder already exists. Choose a new folder to preserve previous evidence.")
    print(f"SIMULATED rehearsal passed. Zero live Telegram/Claude calls. Artifacts: {args.output.resolve()}")


if __name__ == "__main__":
    main()
