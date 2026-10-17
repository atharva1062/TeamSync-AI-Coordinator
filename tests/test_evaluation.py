import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from classifier import validate_result
from evaluate import live_evaluation, load_cases, run_evaluation, score_case
from test_classifier import output


def expected_result(case):
    status = case["expected_status"]
    return validate_result(output(
        status=status, blocker=(case["blocker_terms"][0] + " pending") if status == "Blocked" else None,
        dependency_open=status == "Blocked", confidence=0.9 if status else 0.2,
    ))


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_run_requires_key_before_constructing_client(self):
        with patch("evaluate.classification_settings", return_value=("", "mock")), patch("evaluate.ClaudeClassifier") as client:
            with self.assertRaises(ValueError):
                await live_evaluation(load_cases())
            client.assert_not_called()

    def test_dataset_has_twenty_unique_cases_and_required_edges(self):
        cases = load_cases()
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({case["id"] for case in cases}), 20)
        self.assertTrue(any(case["id"] == "injection-dependency" for case in cases))
        self.assertTrue(any(case["task_status"] == "Blocked" for case in cases if "task_status" in case))

    async def test_scoring_passes_labeled_synthetic_results(self):
        cases = load_cases()
        classifier = SimpleNamespace(model="MOCK-not-live", classify=AsyncMock(side_effect=[expected_result(case) for case in cases]))
        report = await run_evaluation(classifier, cases)
        self.assertEqual(report["correct"], 20)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["prompt_sha256"]), 64)

    async def test_single_unsafe_completion_fails_even_above_90_percent(self):
        cases = load_cases()
        results = [expected_result(case) for case in cases]
        results[2] = validate_result(output(status="Complete"))
        report = await run_evaluation(SimpleNamespace(model="mock", classify=AsyncMock(side_effect=results)), cases)
        self.assertEqual(report["correct"], 19)
        self.assertEqual(report["unsafe_completions"], 1)
        self.assertFalse(report["passed"])

    async def test_vague_reply_mutation_fails_gate(self):
        cases = load_cases()
        results = [expected_result(case) for case in cases]
        results[4] = validate_result(output(status="In Progress"))
        report = await run_evaluation(SimpleNamespace(model="mock", classify=AsyncMock(side_effect=results)), cases)
        self.assertFalse(report["all_vague_replies_clarified"])
        self.assertFalse(report["passed"])

    async def test_api_errors_count_as_incorrect_not_correct_no_change(self):
        cases = load_cases()
        classifier = SimpleNamespace(model="mock", classify=AsyncMock(side_effect=TimeoutError("secret")))
        report = await run_evaluation(classifier, cases)
        self.assertEqual(report["correct"], 0)
        self.assertFalse(report["passed"])
        self.assertNotIn("secret", json.dumps(report))

    def test_blocker_reason_checked(self):
        case = load_cases()[2]
        result = validate_result(output(status="Blocked", blocker="Unrelated reason", dependency_open=True))
        self.assertFalse(score_case(case, result)["correct"])
