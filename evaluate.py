"""Evaluate labeled synthetic replies without touching the project database."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from classifier import ClaudeClassifier, GeminiClassifier, SYSTEM_PROMPT, validate_result
from config import ROOT, ai_provider, classification_settings, load_environment
from db import STATUSES

CASES_PATH = ROOT / "evaluation" / "replies.json"


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("Evaluation requires exactly 20 labeled cases")
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or case["id"] in ids or not isinstance(case.get("reply"), str)
                or not case["reply"].strip()
                or case.get("expected_status") not in (*STATUSES, None)
                or "expected_status" not in case
                or not isinstance(case.get("blocker_terms"), list)
                or not all(isinstance(term, str) and term.strip() for term in case["blocker_terms"])
                or case.get("task_status", "Pending") not in STATUSES
                or (case.get("task_status") == "Blocked" and not case.get("task_blocker"))):
            raise ValueError("Invalid or duplicate evaluation case")
        ids.add(case["id"])
    return cases


def score_case(case: dict, result) -> dict:
    # Score the same locally validated decision that production would apply.
    result = validate_result(result.to_json())
    actual = None if result.needs_clarification else result.status
    expected = case["expected_status"]
    reason_ok = True
    if expected == "Blocked":
        reason = (result.blocker or "").casefold()
        reason_ok = bool(reason) and (not case["blocker_terms"] or any(term.casefold() in reason for term in case["blocker_terms"]))
    return {
        "id": case["id"], "expected_status": expected, "actual_status": actual,
        "correct": actual == expected and reason_ok,
        "unsafe_completion": expected == "Blocked" and actual == "Complete",
        "blocker_reason_ok": reason_ok,
        "result": json.loads(result.to_json()),
    }


async def run_evaluation(classifier, cases: list[dict], delay: float = 0, progress=None) -> dict:
    rows = []
    for index, case in enumerate(cases):
        if index and delay:
            await asyncio.sleep(delay)
        task = {"title": "Connect the payment API", "status": case.get("task_status", "Pending"), "blocker": case.get("task_blocker")}
        try:
            result = await asyncio.wait_for(
                classifier.classify(task, [{"role": "owner", "text": case["reply"]}]), timeout=30,
            )
            rows.append(score_case(case, result))
        except Exception as exc:
            rows.append({"id": case["id"], "expected_status": case["expected_status"],
                         "actual_status": None, "correct": False, "unsafe_completion": False,
                         "error_type": type(exc).__name__})
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            if status in (400, 401, 402, 403, 404, 429):
                rows.extend({"id": remaining["id"], "expected_status": remaining["expected_status"],
                             "actual_status": None, "correct": False, "unsafe_completion": False,
                             "error_type": "NotRun", "blocked_by": type(exc).__name__}
                            for remaining in cases[index + 1:])
                if progress:
                    progress(index + 1, "Stopped after an API configuration or quota error")
                break
        if progress:
            progress(index + 1, "PASS" if rows[-1]["correct"] else "FAIL")
    correct = sum(row["correct"] for row in rows)
    unsafe = sum(row["unsafe_completion"] for row in rows)
    vague_ok = all(row["correct"] for row in rows if row["expected_status"] is None)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(), "model": classifier.model,
        "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "total": len(rows), "correct": correct, "accuracy": correct / len(rows) if rows else 0,
        "classified": sum("result" in row for row in rows),
        "unsafe_completions": unsafe, "all_vague_replies_clarified": vague_ok,
        "passed": len(rows) == 20 and correct >= 18 and unsafe == 0 and vague_ok,
        "cases": rows,
    }


async def live_evaluation(cases, delay=None):
    api_key, model = classification_settings()
    if not api_key:
        raise ValueError("Set the selected provider API key locally before using --live. No API calls were made.")
    provider = ai_provider()
    classifier = GeminiClassifier(api_key, model) if provider == "gemini" else ClaudeClassifier(api_key, model)
    try:
        report = await run_evaluation(
            classifier, cases, delay=(12 if provider == "gemini" else 0) if delay is None else delay,
            progress=lambda index, state: print(f"Case {index}/{len(cases)}: {state}", flush=True),
        )
        report["execution"] = f"live_{provider}"
        report["provider"] = provider
        return report
    finally:
        await classifier.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Evaluate synthetic replies with the selected provider (normal API quotas/charges apply)")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "evaluation-report.json")
    parser.add_argument("--delay", type=float, help="Seconds between requests (default: Gemini 12, Claude 0); respect your account quota")
    args = parser.parse_args()
    if args.delay is not None and (args.delay < 0 or args.delay != args.delay or args.delay == float("inf")):
        parser.error("Delay must be a finite nonnegative number.")
    try:
        cases = load_cases()
        if not args.live:
            print(f"Validated {len(cases)} labeled cases. No API calls made; model accuracy has not been measured.")
            print("Run evaluate.py --live to measure the 90% accuracy and zero unsafe completions gates.")
            return
        load_environment()
        report = asyncio.run(live_evaluation(cases, args.delay))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{report['correct']}/{report['total']} correct; {report['unsafe_completions']} unsafe completions; "
          f"{'PASS' if report['passed'] else 'FAIL'}. Report: {args.output}")
    if report["classified"] == 0:
        print("No replies were classified. This run does not measure model accuracy; inspect the report's error types.")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
