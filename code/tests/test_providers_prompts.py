import io
import json
import os
import unittest
from unittest import mock
from urllib import error

from funpack.prompts import build_messages
from funpack.programs import extract_response
from funpack.providers import ApiProvider, BudgetExceeded, OfflineProvider, _NoRedirect, preflight


MESSAGES = [{"role": "user", "content": "Generate a bin-packing function."}]


def config():
    return {"enabled": True, "base_url": "https://example.invalid/v1", "model": "test-model",
            "api_key_env": "FUNPACK_TEST_KEY", "max_tokens": 100, "max_requests": 5,
            "timeout_seconds": 2, "temperature": 0.8,
            "extra_body": {"thinking": {"type": "disabled"}},
            "budget": {"currency": "USD", "max_cost": 1.0, "input_per_million": 1.0,
                       "output_per_million": 2.0, "pricing_source": "https://example.invalid/pricing"}}


def response(content="def priority(item, bins): return [0 for r in bins]", usage=None):
    return {"model": "test-model-version-1", "choices": [{"message": {"content": content},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}
            if usage is None else usage}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FUNPACK_TEST_KEY": "unit-test-secret-12345"})
        self.env.start()
        self.opener_patch = mock.patch("funpack.providers.request.build_opener")
        self.opener = self.opener_patch.start().return_value
        self.addCleanup(self.env.stop)
        self.addCleanup(self.opener_patch.stop)

    def set_response(self, data):
        self.opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(data).encode("utf-8")

    def test_preflight_has_no_network_side_effects_and_requires_both_switches(self):
        checked = preflight(config(), allow_paid=True)
        self.assertTrue(checked["ok"])
        self.assertNotIn("unit-test-secret", json.dumps(checked))
        self.opener.open.assert_not_called()
        self.assertFalse(preflight(config())["ok"])
        disabled = config()
        disabled["enabled"] = False
        with self.assertRaises(ValueError):
            ApiProvider(disabled, allow_paid=True)

    def test_unknown_price_cap_model_key_and_currency_refused(self):
        for key in ("max_cost", "input_per_million", "output_per_million", "currency"):
            cfg = config()
            cfg["budget"][key] = None
            self.assertFalse(preflight(cfg, True)["ok"], key)
        cfg = config()
        cfg["model"] = None
        self.assertFalse(preflight(cfg, True)["ok"])
        cfg = config()
        cfg["api_key_env"] = "FUNPACK_TEST_KEY_NOT_SET"
        self.assertFalse(preflight(cfg, True)["ok"])

    def test_success_usage_metadata_and_actual_token_estimate(self):
        self.set_response(response())
        provider = ApiProvider(config(), allow_paid=True)
        event = provider.generate(MESSAGES)
        self.assertEqual(provider.attempts, 1)
        self.assertTrue(event["request_attempted"])
        self.assertTrue(event["cost_known"])
        self.assertEqual(event["usage"]["input_tokens"], 10)
        self.assertEqual(event["usage"]["completion_tokens"], 20)
        self.assertEqual(event["model"], "test-model-version-1")
        self.assertAlmostEqual(provider.accounted_cost, 0.00005)
        req = self.opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertNotIn("unit-test-secret", json.dumps(event))

    def test_timeout_counts_once_and_keeps_full_reservation(self):
        self.opener.open.side_effect = TimeoutError("timeout unit-test-secret-12345")
        provider = ApiProvider(config(), allow_paid=True)
        amount = provider.estimate_reservation(MESSAGES)["cost"]
        event = provider.generate(MESSAGES)
        self.assertEqual(provider.attempts, 1)
        self.opener.open.assert_called_once()
        self.assertFalse(event["cost_known"])
        self.assertFalse(event["usage"]["input_tokens_known"])
        self.assertFalse(event["usage"]["output_tokens_known"])
        self.assertAlmostEqual(provider.accounted_cost, amount)
        self.assertNotIn("unit-test-secret", json.dumps(event))
        self.assertIn("[REDACTED]", event["error"]["message"])

    def test_incomplete_http_response_retains_reservation(self):
        import http.client
        self.opener.open.side_effect = http.client.IncompleteRead(b"partial")
        provider = ApiProvider(config(), allow_paid=True)
        expected = provider.estimate_reservation(MESSAGES)["cost"]
        event = provider.generate(MESSAGES)
        self.assertEqual(event["error"]["type"], "network_error")
        self.assertFalse(event["cost_known"])
        self.assertEqual(provider.attempts, 1)
        self.assertAlmostEqual(provider.accounted_cost, expected)

    def test_missing_returned_model_is_unknown_not_requested_alias(self):
        data = response()
        del data["model"]
        self.set_response(data)
        event = ApiProvider(config(), allow_paid=True).generate(MESSAGES)
        self.assertIsNone(event["model"])
        self.assertEqual(event["requested_model"], config()["model"])

    def test_partial_usage_keeps_full_reservation(self):
        self.set_response(response(usage={"prompt_tokens": 10}))
        provider = ApiProvider(config(), allow_paid=True)
        amount = provider.estimate_reservation(MESSAGES)["cost"]
        event = provider.generate(MESSAGES)
        self.assertTrue(event["usage"]["input_tokens_known"])
        self.assertFalse(event["usage"]["output_tokens_known"])
        self.assertIsNone(event["usage"]["output_tokens"])
        self.assertFalse(event["cost_known"])
        self.assertAlmostEqual(provider.accounted_cost, amount)

    def test_budget_and_attempt_caps_block_before_network(self):
        cfg = config()
        cfg["budget"]["max_cost"] = 0.000001
        provider = ApiProvider(cfg, allow_paid=True)
        with self.assertRaises(BudgetExceeded):
            provider.generate(MESSAGES)
        self.assertEqual(provider.attempts, 0)
        self.opener.open.assert_not_called()
        cfg = config()
        cfg["max_requests"] = 1
        self.opener.open.side_effect = error.URLError("network unavailable")
        provider = ApiProvider(cfg, allow_paid=True)
        provider.generate(MESSAGES)
        with self.assertRaises(BudgetExceeded):
            provider.generate(MESSAGES)
        self.assertEqual(provider.attempts, 1)
        self.opener.open.assert_called_once()

    def test_unknown_failure_reservation_can_exhaust_budget(self):
        cfg = config()
        provider = ApiProvider(cfg, allow_paid=True)
        amount = provider.estimate_reservation(MESSAGES)["cost"]
        cfg["budget"]["max_cost"] = amount * 1.5
        provider = ApiProvider(cfg, allow_paid=True)
        self.opener.open.side_effect = TimeoutError("timeout")
        provider.generate(MESSAGES)
        with self.assertRaises(BudgetExceeded):
            provider.generate(MESSAGES)
        self.opener.open.assert_called_once()

    def test_http_failure_body_redacted_and_no_retry(self):
        self.opener.open.side_effect = error.HTTPError(
            "https://example.invalid", 429, "limit", {}, io.BytesIO(b"echo unit-test-secret-12345"))
        provider = ApiProvider(config(), allow_paid=True)
        event = provider.generate(MESSAGES)
        self.assertEqual(event["error"]["type"], "http_error")
        self.assertNotIn("unit-test-secret", json.dumps(event))
        self.assertFalse(event["cost_known"])
        self.opener.open.assert_called_once()

    def test_raw_response_redacted_and_invalid_content_billed(self):
        data = response(content=None)
        data["debug"] = "unit-test-secret-12345"
        self.set_response(data)
        event = ApiProvider(config(), allow_paid=True).generate(MESSAGES)
        self.assertEqual(event["error"]["type"], "invalid_response")
        self.assertTrue(event["cost_known"])
        self.assertNotIn("unit-test-secret", json.dumps(event))

    def test_invalid_json_retains_redacted_raw_body_and_reservation(self):
        self.opener.open.return_value.__enter__.return_value.read.return_value = b"<html>unit-test-secret-12345</html>"
        event = ApiProvider(config(), allow_paid=True).generate(MESSAGES)
        self.assertEqual(event["error"]["type"], "invalid_response")
        self.assertFalse(event["cost_known"])
        self.assertEqual(event["raw_response"]["unparsed_body"], "<html>[REDACTED]</html>")
        self.opener.open.assert_called_once()

    def test_extra_body_cannot_override_budget_or_include_keys(self):
        for extra in ({"max_tokens": 1000000}, {"model": "other"}, {"api_key": "bad"},
                      {"nested": {"Authorization": "bad"}}):
            cfg = config()
            cfg["extra_body"] = extra
            self.assertFalse(preflight(cfg, True)["ok"])

    def test_url_credentials_not_exposed_in_preflight(self):
        cfg = config()
        cfg["base_url"] = "https://someone:private-password@example.invalid"
        checked = preflight(cfg, True)
        self.assertFalse(checked["ok"])
        self.assertNotIn("private-password", json.dumps(checked))

    def test_redirect_handler_refuses_all_destinations(self):
        handler = _NoRedirect()
        for url in ("https://example.invalid/new", "https://other.invalid/steal"):
            self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, url))

    def test_offline_stream_deterministic_prompt_independent_and_labelled(self):
        basic = OfflineProvider(seed=11)
        enhanced = OfflineProvider(seed=11)
        first_cycle = []
        for _ in range(16):
            left = basic.generate([{"role": "user", "content": "basic"}])
            right = enhanced.generate([{"role": "user", "content": "enhanced"}])
            self.assertEqual(left["text"], right["text"])
            self.assertEqual(left["provider"], "offline_fixture")
            self.assertFalse(left["request_attempted"])
            self.assertFalse(left["paid_api_request"])
            self.assertEqual(left["estimated_cost"], 0)
            first_cycle.append(left["text"])
        self.assertEqual(basic.generate(MESSAGES)["text"], first_cycle[0])
        self.assertLess(len(set(first_cycle)), 16)
        self.opener.open.assert_not_called()

    def test_offline_enhanced_format_changes_wrapper_only(self):
        basic = OfflineProvider(seed=11).generate(build_messages("basic", []))
        enhanced = OfflineProvider(seed=11).generate(build_messages("enhanced", []))
        basic_parsed = extract_response(basic["text"], "basic")
        enhanced_parsed = extract_response(enhanced["text"], "enhanced")
        self.assertTrue(basic_parsed["format_ok"])
        self.assertTrue(enhanced_parsed["format_ok"])
        self.assertEqual(basic_parsed["code"], enhanced_parsed["code"])
        self.assertTrue(enhanced["raw_response"]["candidate_generation_ignores_prompt"])


class PromptTests(unittest.TestCase):
    def test_modes_share_contract_and_parent_feedback(self):
        parents = [{"code": "def priority(item, bins): return [0 for r in bins]",
                    "counts": [5, 6], "mean_bins": 5.5,
                    "test_scores": "SECRET_TEST_SCORE", "probes": "SECRET_PROBE",
                    "audit": "SECRET_AUDIT"}]
        basic = build_messages("basic", parents)
        enhanced = build_messages("enhanced", parents)
        self.assertEqual(basic[0], enhanced[0])
        self.assertIn("80 English words", enhanced[1]["content"])
        self.assertNotIn("80 English words", basic[1]["content"])
        for messages in (basic, enhanced):
            serialized = json.dumps(messages)
            self.assertNotIn("SECRET_", serialized)
            self.assertIn("5.5", serialized)
            self.assertIn("priority", serialized)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            build_messages("wrong-mode", [])


if __name__ == "__main__":
    unittest.main()
