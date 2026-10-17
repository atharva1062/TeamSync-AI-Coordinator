import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from classifier import ClassificationError, GeminiClassifier
from config import ai_provider, classification_settings
from evaluate import load_cases, run_evaluation
from test_classifier import output


class GeminiConfigTests(unittest.TestCase):
    def test_provider_selects_gemini_key_and_model_without_using_claude_key(self):
        with patch.dict(os.environ, {"AI_PROVIDER":"gemini", "GEMINI_API_KEY":"test-google-key", "GEMINI_MODEL":"custom-model", "ANTHROPIC_API_KEY":"test-claude-key"}):
            self.assertEqual(classification_settings(), ("test-google-key", "custom-model"))

    def test_missing_gemini_key_does_not_fall_back_to_claude(self):
        with patch.dict(os.environ, {"AI_PROVIDER":"gemini", "GEMINI_API_KEY":"", "GOOGLE_API_KEY":"", "ANTHROPIC_API_KEY":"test-claude-key"}):
            self.assertEqual(classification_settings()[0], "")

    def test_invalid_provider_is_rejected(self):
        with patch.dict(os.environ, {"AI_PROVIDER":"invalid"}), self.assertRaises(ValueError):
            ai_provider()


class GeminiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_request_uses_header_and_shared_blocker_validation(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"candidates":[{"finishReason":"STOP", "content":{"parts":[{"text":output(status="Complete", blocker="Review pending")}]} }]})
        classifier = GeminiClassifier("test-secret", "test-model", transport=httpx.MockTransport(respond))
        try:
            result = await classifier.classify({"title":"API", "status":"Pending", "blocker":None, "telegram_chat_id":987654321}, [{"role":"owner","text":"Review pending"}])
            self.assertEqual(result.status, "Blocked")
            self.assertNotIn("test-secret", str(requests[0].url))
            self.assertEqual(requests[0].headers["x-goog-api-key"], "test-secret")
            payload = json.loads(requests[0].content)
            self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
            self.assertNotIn("987654321", json.dumps(payload))
        finally:
            await classifier.close()

    async def test_blocked_truncated_and_malformed_responses_fail_safely(self):
        for payload in ({"candidates":[]}, {"candidates":[{"finishReason":"MAX_TOKENS"}]}, {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"broken"}]}}]}):
            classifier = GeminiClassifier("key", "test", transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload)))
            try:
                with self.assertRaises(ClassificationError):
                    await classifier.classify({"title":"API","status":"Pending","blocker":None}, [])
            finally:
                await classifier.close()

    async def test_quota_error_stops_evaluation_instead_of_repeating_requests(self):
        request = httpx.Request("POST", "https://example.test/generate")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError("quota", request=request, response=response)
        classifier = SimpleNamespace(model="mock", classify=AsyncMock(side_effect=error))
        report = await run_evaluation(classifier, load_cases())
        classifier.classify.assert_awaited_once()
        self.assertEqual(report["classified"], 0)
        self.assertEqual(sum(row.get("error_type") == "NotRun" for row in report["cases"]), 19)
        self.assertFalse(report["passed"])
