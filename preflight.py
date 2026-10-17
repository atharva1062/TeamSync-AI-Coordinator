"""Offline demo readiness checks; never prints credentials or sends messages."""

import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sqlite3

from classifier import SYSTEM_PROMPT
from config import ROOT, ai_provider, bot_token, classification_settings, coordinator_chat_id, database_path, load_environment
from evaluate import load_cases


def check_accuracy(path: Path, model: str, provider: str = "claude") -> bool:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        cases = load_cases()
        expected_hash = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        rows = report["cases"]
        return bool(
            report.get("execution") == f"live_{provider}" and report.get("model") == model
            and report.get("cases_sha256") == expected_hash
            and report.get("prompt_sha256") == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
            and report.get("passed") is True and report.get("total") == 20
            and len(rows) == 20 and {row["id"] for row in rows} == {case["id"] for case in cases}
            and sum(row.get("correct") is True for row in rows) >= 18
            and all(row.get("unsafe_completion") is False for row in rows)
            and all(row.get("correct") is True for row in rows if row.get("expected_status") is None)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def check_readiness(path: Path, report_path: Path) -> list[dict]:
    checks = []
    for package in ("python-telegram-bot", "python-dotenv", "anthropic", "Flask"):
        try:
            installed = version(package)
            checks.append({"name": package, "ready": True, "detail": f"Installed {installed}"})
        except PackageNotFoundError:
            checks.append({"name": package, "ready": False, "detail": "Install requirements.txt"})
    try:
        token_present = bool(bot_token())
    except ValueError:
        token_present = False
    checks.append({"name": "Telegram configuration", "ready": token_present, "detail": "Token present (not authenticated)" if token_present else "Set TELEGRAM_BOT_TOKEN locally"})
    api_key, model = classification_settings()
    provider = ai_provider()
    label = "Gemini" if provider == "gemini" else "Claude"
    key_name = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    checks.append({"name": f"{label} configuration", "ready": bool(api_key), "detail": "Key present (not authenticated)" if api_key else f"Set {key_name} locally"})
    try:
        coordinator = coordinator_chat_id()
    except ValueError:
        coordinator = None
    checks.append({"name": "Coordinator", "ready": coordinator is not None, "detail": "Private chat ID configured" if coordinator else "Set a positive COORDINATOR_CHAT_ID"})
    conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.execute("PRAGMA query_only = ON")
        healthy = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        rows = conn.execute("SELECT id, status, telegram_chat_id FROM tasks").fetchall()
        # Verify tables required by the full reply flow, without changing schema.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        healthy = healthy and {"tasks", "status_history", "followups", "replies", "classification_results", "clarification_contexts"}.issubset(tables)
        checks.append({"name": "Database", "ready": healthy and bool(rows), "detail": f"{len(rows)} task(s); {'schema and integrity OK' if healthy else 'run bot.py --preview to initialize schema'}"})
        open_rows = [row for row in rows if row[1] != "Complete"]
        missing = [row for row in open_rows if not row[2] or row[2] <= 0]
        checks.append({"name": "Demo owner assignments", "ready": bool(open_rows) and not missing, "detail": f"{len(missing)} unassigned open task(s)" if missing else ("All open tasks have private chat IDs" if open_rows else "Need an open task for the follow-up demo")})
    except sqlite3.Error:
        checks.append({"name": "Database", "ready": False, "detail": "Database unavailable; seed a new board or check DATABASE_PATH"})
    finally:
        if conn:
            conn.close()
    passed = check_accuracy(report_path, model, provider)
    checks.append({"name": "Live accuracy report", "ready": passed, "detail": "Passing report matches current provider, prompt, dataset, and model" if passed else f"Run evaluate.py --live after configuring {label}"})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "data" / "evaluation-report.json")
    args = parser.parse_args()
    load_environment()
    checks = check_readiness(database_path(), args.report)
    print("Demo preflight (offline; credentials and delivery are not verified)")
    for check in checks:
        print(f"{'READY' if check['ready'] else 'TODO '} | {check['name']}: {check['detail']}")
    ready = all(check["ready"] for check in checks)
    print("Configuration checks passed; perform the live rehearsal." if ready else "Finish the TODO items, then perform the live rehearsal.")
    raise SystemExit(0 if ready else 1)


if __name__ == "__main__":
    main()
