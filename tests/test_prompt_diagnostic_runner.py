"""Small isolated prompt-lab pipeline tests, never contacting a model."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from funpack.packing import evaluate
from funpack.programs import BF_CODE, Program
from funpack.providers import BudgetExceeded
from funpack.storage import digest, read_json
from scripts import run_prompt_diagnostic as runner
from test_prompt_witness import CHANGED, text


class FakeProvider:
    def __init__(self, config, allow_paid):
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        is_new = "Witness: " in messages[1]["content"]
        # Intentionally incorrect witness: all recoverable candidates must
        # nevertheless be evaluated by the real diagnostic implementation.
        content = text(witness={"wrong": True}) if is_new else "Strategy: Test only.\n```python\n" + BF_CODE + "```"
        return {"text": content, "usage": {"input_tokens": 0, "output_tokens": 0},
                "estimated_cost": 0, "cost_known": True, "offline_fixture": True}


class DiagnosticRunnerTests(unittest.TestCase):
    def setUp(self):
        self.base = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.base.mkdir(exist_ok=True)
        self.root = self.base / ("prompt-diagnostic-" + uuid.uuid4().hex)
        (self.root / "configs").mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch("sys.stdout", new=io.StringIO()))
        self.stack.enter_context(patch("urllib.request.build_opener", side_effect=AssertionError("network forbidden")))
        self.instances = [{"id": "test-owned", "capacity": 100, "items": [84, 80, 10]}]
        self.parent = {"id": "seed_bf", "code": BF_CODE, "code_hash": digest(BF_CODE),
                       **evaluate(Program(BF_CODE), self.instances)}
        self.contexts = [{"context_index": i, "parents": [self.parent], "reference_index": 0} for i in range(2)]
        self.config = {"api_key_env": "FUNPACK_PROMPT_TEST_KEY"}
        self.preview = {"ready_to_activate": True}

    def cleanup(self):
        if self.root.resolve().parent != self.base or self.root.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.root)

    def prepared(self):
        return patch.object(runner, "prepare", return_value=(self.config, self.contexts, self.instances, self.preview))

    def test_default_preview_cannot_prompt_write_or_generate(self):
        with (self.prepared(), patch.object(runner.getpass, "getpass") as hidden,
              patch.object(runner, "execute") as execute):
            runner.main([], root=self.root)
        hidden.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(list((self.root / "configs").iterdir()), [])

    def test_existing_artifacts_block_before_prompt(self):
        self.preview["ready_to_activate"] = False
        with self.prepared(), patch.object(runner.getpass, "getpass") as hidden:
            with self.assertRaises(ValueError):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        hidden.assert_not_called()

    def test_real_diagnostic_pipeline_keeps_failed_witness_and_counterbalances_order(self):
        with patch.object(runner, "ApiProvider", FakeProvider):
            report = runner.execute(self.root, self.config, self.contexts, self.instances, self.preview)
        events = runner.jsonl(self.root / runner.OUTPUT / "events.jsonl")
        self.assertEqual([e["variant"] for e in events], ["enhanced_v1", "witness_v2", "witness_v2", "enhanced_v1"])
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e["evaluation"]["valid_search_evaluation"] for e in events))
        self.assertTrue(all(not e["witness_check"]["verified_change"] for e in events))
        self.assertFalse(report["complete"])  # only two test contexts, not the paid32
        self.assertFalse((self.root / runner.OUTPUT / "pending_request.json").exists())

    def test_budget_guard_marks_unsent_request_and_stops(self):
        class Stops(FakeProvider):
            def generate(self, messages):
                if self.calls:
                    raise BudgetExceeded("test-only cap")
                return super().generate(messages)
        with patch.object(runner, "ApiProvider", Stops):
            report = runner.execute(self.root, self.config, self.contexts, self.instances, self.preview)
        self.assertFalse(report["complete"])
        self.assertEqual(report["stop_reason"], "cost_budget_guard_before_request")
        self.assertFalse(read_json(self.root / runner.OUTPUT / "stopped_request.json")["request_sent"])
        self.assertFalse((self.root / runner.OUTPUT / "pending_request.json").exists())

    def test_empty_key_creates_no_claim(self):
        with (self.prepared(), patch.dict(os.environ, {}, clear=True),
              patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="")):
            with self.assertRaisesRegex(ValueError, "empty key"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        self.assertFalse((self.root / runner.CLAIM).exists())

    def test_interruption_retains_claim_and_removes_entered_key(self):
        old_cwd = Path.cwd()
        with (self.prepared(), patch.dict(os.environ, {}, clear=True),
              patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="temporary-test-secret"),
              patch.object(runner, "execute", side_effect=RuntimeError("test-only interrupted"))):
            with self.assertRaisesRegex(RuntimeError, "test-only interrupted"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
            self.assertNotIn("FUNPACK_PROMPT_TEST_KEY", os.environ)
        self.assertEqual(Path.cwd(), old_cwd)
        claim = read_json(self.root / runner.CLAIM)
        self.assertEqual(claim["status"], "interrupted")
        self.assertNotIn("temporary-test-secret", json.dumps(claim))

    def low_contexts(self):
        return [{"context_index": i, "parent_context_index": i // 2, "repeat": i % 2,
                 "parents": [self.parent], "reference_index": 0} for i in range(8)]

    def test_low16_has_exact_limit_balanced_order_and_no_heldout_access(self):
        class LowFake(FakeProvider):
            def generate(self, messages):
                self.calls += 1
                enhanced = "Start with 'Strategy:'" in messages[1]["content"]
                content = ("Strategy: Test only.\n" if enhanced else "") + "```python\n" + BF_CODE + "```"
                return {"text": content, "finish_reason": "stop", "cost_known": True,
                        "estimated_cost": 0, "usage": {"input_tokens": 1, "output_tokens": 1}}
        with patch.object(runner, "ApiProvider", LowFake):
            report = runner.execute_low16(self.root, self.config, self.low_contexts(), self.instances, self.preview)
        events = runner.jsonl(self.root / runner.LOW_PATHS["output"] / "events.jsonl")
        self.assertEqual(len(events), 16)
        self.assertEqual([e["variant"] for e in events[:4]],
                         ["basic_v1", "mechanism_v3", "mechanism_v3", "basic_v1"])
        self.assertTrue(report["complete"])
        self.assertTrue(report["engineering_gate_passed"])
        self.assertEqual([r["valid_candidates"] for r in report["rows"]], [8, 8])
        self.assertFalse(report["validation_used"])
        self.assertFalse(report["heldout_evaluated"])
        self.assertFalse(report["automatic_next_run"])

    def test_low16_truncations_remain_in_denominator_and_fail_gate(self):
        class Truncated(FakeProvider):
            def generate(self, messages):
                return {"text": "", "finish_reason": "length", "cost_known": True}
        with patch.object(runner, "ApiProvider", Truncated):
            report = runner.execute_low16(self.root, self.config, self.low_contexts(), self.instances, self.preview)
        self.assertTrue(report["complete"])
        self.assertFalse(report["engineering_gate_passed"])
        self.assertEqual([r["requests"] for r in report["rows"]], [8, 8])
        self.assertEqual([r["valid_candidates"] for r in report["rows"]], [0, 0])

    def test_low16_network_error_stops_after_first_request(self):
        class Broken(FakeProvider):
            def generate(self, messages):
                return {"error": {"type": "network_error"}, "cost_known": False}
        with patch.object(runner, "ApiProvider", Broken):
            report = runner.execute_low16(self.root, self.config, self.low_contexts(), self.instances, self.preview)
        self.assertFalse(report["complete"])
        self.assertEqual(report["stop_reason"], "provider_error")
        self.assertEqual(sum(r["requests"] for r in report["rows"]), 1)
        self.assertEqual(sum(r["unknown_cost_requests"] for r in report["rows"]), 1)

    def test_low16_parent_replay_failure_prevents_generation(self):
        self.parent["trace_hashes"] = ["incorrect-saved-trace"]
        with patch.object(runner, "ApiProvider") as provider:
            with self.assertRaisesRegex(ValueError, "independent replay"):
                runner.execute_low16(self.root, self.config, self.low_contexts(), self.instances, self.preview)
        provider.assert_not_called()

    def test_low16_preview_does_not_prompt_or_create_outputs(self):
        with (patch.object(runner, "prepare_low16", return_value=(self.config, self.low_contexts(), self.instances, self.preview)),
              patch.object(runner.getpass, "getpass") as hidden,
              patch.object(runner, "execute_low16") as execute):
            runner.main(["--low16"], root=self.root)
        hidden.assert_not_called()
        execute.assert_not_called()
        self.assertFalse((self.root / runner.LOW_PATHS["output"]).exists())

    def test_low16_rejects_existing_artifacts_before_connecting(self):
        from scripts import run_thinking_pilot_200 as thinking
        self.preview["ready_to_activate"] = False
        with (patch.object(runner, "prepare_low16", return_value=(self.config, self.low_contexts(), self.instances, self.preview)),
              patch.object(thinking, "check_connection") as connection):
            with self.assertRaisesRegex(ValueError, "already exist"):
                runner.main(["--low16", "--run"], root=self.root)
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
