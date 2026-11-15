"""Unit tests for the Muse handshake provider (0 CNY, no paid API)."""
import json
import tempfile
import unittest
from pathlib import Path

from funpack.handshake_provider import HandshakeProvider, MODEL_NAME, PROVIDER_NAME


def _write_response(provider, request_index, text):
    path = provider.responses_dir / f"request_{request_index:06d}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"text": text, "generator_note": "unit test"}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class HandshakeProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = HandshakeProvider(self.tmp.name, "A_seed11", timeout_seconds=30)

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_file_written_then_response_returned(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Return only the Python function."}]
        _write_response(self.provider, 1, "```python\ndef priority(item, bins):\n    return [-(r - item) for r in bins]\n```")
        result = self.provider.generate(messages)
        self.assertEqual(result["provider"], PROVIDER_NAME)
        self.assertEqual(result["model"], MODEL_NAME)
        self.assertFalse(result["paid_api_request"])
        self.assertTrue(result["request_attempted"])
        self.assertEqual(result["estimated_cost"], 0.0)
        self.assertTrue(result["cost_known"])
        self.assertIn("def priority", result["text"])
        request = json.loads((self.provider.requests_dir / "request_000001.json").read_text(encoding="utf-8"))
        self.assertEqual(request["messages"], messages)
        self.assertEqual(request["generator"], MODEL_NAME)

    def test_invalid_response_file_is_error_not_exception(self):
        path = self.provider.responses_dir / "request_000001.json"
        path.write_text(json.dumps({"no_text": True}), encoding="utf-8")
        result = self.provider.generate([{"role": "user", "content": "x"}])
        self.assertEqual(result["error"]["type"], "handshake_invalid_response")
        self.assertEqual(result["text"], "")

    def test_timeout_is_error_not_exception(self):
        provider = HandshakeProvider(self.tmp.name, "B_seed11", timeout_seconds=0.05, poll_seconds=0.01)
        result = provider.generate([{"role": "user", "content": "x"}])
        self.assertEqual(result["error"]["type"], "handshake_timeout")

    def test_bad_run_name_rejected(self):
        with self.assertRaises(ValueError):
            HandshakeProvider(self.tmp.name, "../escape", timeout_seconds=30)


if __name__ == "__main__":
    unittest.main()
