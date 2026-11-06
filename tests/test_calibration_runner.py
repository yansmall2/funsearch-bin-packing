"""Calibration activation tests using temporary ledgers and no network."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from funpack.ledger import BudgetLedger, budget_signature, extend_request_limit
from funpack.storage import read_json, write_json
from scripts import run_calibration_pilot as runner


class CalibrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.workspace.mkdir(exist_ok=True)
        self.root = self.workspace / ("calibration-" + uuid.uuid4().hex)
        (self.root / "configs").mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.path = self.root / "configs/test-pilot.sqlite3"
        self.config_path = self.root / "configs/api.local.json"
        self.config = {
            "enabled": True, "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "expected_model_version": None, "api_key_env": "FUNPACK_CALIBRATION_TEST_KEY",
            "ledger_path": "configs/test-pilot.sqlite3", "temperature": 0.8,
            "max_tokens": 1024, "timeout_seconds": 60, "max_requests": 16,
            "extra_body": {"thinking": {"type": "disabled"}},
            "budget": {"currency": "CNY", "max_cost": 10, "input_per_million": 2,
                       "output_per_million": 8, "pricing_source": "test-only-prices"},
        }
        write_json(self.config_path, self.config)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch("sys.stdout", new=io.StringIO()))
        self.network = self.stack.enter_context(patch(
            "funpack.providers.request.build_opener", side_effect=AssertionError("network not allowed in tests")))

    def cleanup(self):
        if self.root.resolve().parent != self.workspace or self.root.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.root)

    def seed(self, count=16):
        ledger = BudgetLedger(self.path, self.config)
        for _ in range(count):
            reservation = ledger.reserve(0.01)
            ledger.settle(reservation["reservation_id"], 0.01, 5, 3)
        return ledger

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def activation_mocks(self, *, complete=True, error=None):
        self.stack.enter_context(patch.object(runner, "load_instances", return_value=[]))
        self.stack.enter_context(patch.object(runner, "load_probes", return_value=[]))
        execute = self.stack.enter_context(patch("experiment.main", side_effect=error))
        self.stack.enter_context(patch.object(runner, "summarize", return_value={"complete": complete, "runs": []}))
        self.stack.enter_context(patch("audit_and_cost.analyze", return_value={"simulated_test_only": True}))
        return execute

    def test_prepare_and_preview_do_not_write_prompt_or_call_network(self):
        self.seed()
        before = self.snapshot()
        with patch.object(runner.getpass, "getpass", side_effect=AssertionError("unexpected key prompt")):
            old, new, ledger, plan = runner.prepare_plan(self.root)
            preview = runner.main([], root=self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(preview["ready_to_activate"])
        self.assertEqual((old["max_requests"], new["max_requests"]), (16, 144))
        self.assertEqual(ledger, self.path)
        self.assertEqual(plan["prior_ledger"]["attempts"], 16)
        self.assertEqual(plan["additional_request_limit"], 128)
        self.network.assert_not_called()

    def test_missing_ledger_is_not_created(self):
        before = self.snapshot()
        with self.assertRaises(FileNotFoundError):
            runner.prepare_plan(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_incomplete_old_request_count_is_not_ready(self):
        self.seed(count=15)
        _, _, _, plan = runner.prepare_plan(self.root)
        self.assertFalse(plan["ready_to_activate"])
        with patch.object(runner.getpass, "getpass") as hidden:
            with self.assertRaises(ValueError):
                runner.main(["--approve-128-calls"], root=self.root)
            hidden.assert_not_called()

    def test_existing_claim_blocks_before_input_and_data_loading(self):
        self.seed()
        write_json(self.root / runner.CLAIM_NAME, {"status": "previous_attempt"})
        before = self.snapshot()
        with (patch.object(runner.getpass, "getpass") as hidden,
              patch.object(runner, "load_instances") as load):
            with self.assertRaises(ValueError):
                runner.main(["--approve-128-calls"], root=self.root)
            hidden.assert_not_called()
            load.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_reviewed_settings_cannot_be_changed(self):
        self.seed()
        mutations = []
        for name, value in (("model", "different-model"), ("temperature", 0.5),
                            ("max_tokens", 2048), ("extra_body", {"thinking": {"type": "enabled"}})):
            cfg = copy.deepcopy(self.config)
            cfg[name] = value
            mutations.append((name, cfg))
        for name, value in (("max_cost", 20), ("input_per_million", 1), ("output_per_million", 2)):
            cfg = copy.deepcopy(self.config)
            cfg["budget"][name] = value
            mutations.append((name, cfg))
        ledger_before = self.path.read_bytes()
        for name, cfg in mutations:
            with self.subTest(setting=name):
                write_json(self.config_path, cfg)
                with self.assertRaises(ValueError):
                    runner.prepare_plan(self.root)
        self.assertEqual(self.path.read_bytes(), ledger_before)

    def test_prior_extension_does_not_become_ready_for_another_batch(self):
        self.seed()
        new = copy.deepcopy(self.config)
        new["max_requests"] = 144
        extend_request_limit(self.path, self.config, new, authorization_note="test-only", expected_attempts=16)
        _, _, _, plan = runner.prepare_plan(self.root)
        self.assertFalse(plan["ready_to_activate"])
        self.assertEqual(plan["prior_ledger"]["attempts"], 16)

    def test_empty_hidden_key_keeps_config_ledger_and_claim_unchanged(self):
        self.seed()
        before = self.snapshot()
        self.activation_mocks()
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="")):
            with self.assertRaisesRegex(ValueError, "empty key"):
                runner.main(["--approve-128-calls"], root=self.root)
            self.assertNotIn(self.config["api_key_env"], os.environ)
        self.assertEqual(self.snapshot(), before)
        self.network.assert_not_called()

    def test_approved_activation_preserves_original_and_records_completed_batch(self):
        self.seed()
        original_config = self.config_path.read_bytes()
        execute = self.activation_mocks()
        previous_dir = Path.cwd()
        env_name = self.config["api_key_env"]
        with patch.dict(os.environ, {env_name: "test-only-existing-key"}):
            report = runner.main(["--approve-128-calls"], root=self.root)
            self.assertEqual(os.environ[env_name], "test-only-existing-key")
        self.assertEqual(Path.cwd(), previous_dir)
        self.assertTrue(report["complete"])
        self.assertEqual(self.config_path.read_bytes(), original_config)
        new = read_json(self.root / runner.CONFIG_NAME)
        self.assertEqual(new["max_requests"], 144)
        self.assertEqual(new["budget"]["max_cost"], 10)
        self.assertEqual(new["ledger_path"], self.config["ledger_path"])
        live = runner.inspect_ledger(self.path)
        backup = runner.inspect_ledger(self.root / runner.BACKUP_NAME)
        self.assertEqual((live["attempts"], backup["attempts"]), (16, 16))
        self.assertEqual(live["accounted_cost_cny"], backup["accounted_cost_cny"])
        self.assertEqual(backup["signature"], budget_signature(self.config))
        self.assertEqual(live["signature"], budget_signature(new))
        claim = read_json(self.root / runner.CLAIM_NAME)
        self.assertEqual(claim["status"], "completed")
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index("--calls") + 1], "32")
        self.assertEqual(argv[argv.index("--seeds") + 1], "11")
        self.assertEqual(argv[argv.index("--api-config") + 1], runner.CONFIG_NAME)
        self.network.assert_not_called()

    def test_incomplete_simulated_run_is_marked_incomplete(self):
        self.seed()
        self.activation_mocks(complete=False)
        with patch.dict(os.environ, {self.config["api_key_env"]: "test-only-key"}):
            runner.main(["--approve-128-calls"], root=self.root)
        self.assertEqual(read_json(self.root / runner.CLAIM_NAME)["status"], "stopped_incomplete")

    def test_exception_restores_cwd_and_removes_only_entered_key(self):
        self.seed()
        self.activation_mocks(error=RuntimeError("simulated search interruption"))
        previous_dir = Path.cwd()
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="temporary-test-key")):
            with self.assertRaisesRegex(RuntimeError, "simulated search interruption"):
                runner.main(["--approve-128-calls"], root=self.root)
            self.assertNotIn(self.config["api_key_env"], os.environ)
        self.assertEqual(Path.cwd(), previous_dir)
        claim = read_json(self.root / runner.CLAIM_NAME)
        self.assertEqual(claim["status"], "interrupted")
        self.assertEqual(claim["error_type"], "RuntimeError")
        self.assertNotIn("temporary-test-key", json.dumps(claim))
        self.assertEqual(runner.inspect_ledger(self.path)["attempts"], 16)
        self.assertFalse(runner.prepare_plan(self.root)[3]["ready_to_activate"])


if __name__ == "__main__":
    unittest.main()
