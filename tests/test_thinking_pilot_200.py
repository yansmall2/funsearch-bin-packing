"""Read-only preview, bounded activation and summaries with fake API results."""
from contextlib import ExitStack
import copy
import io
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from funpack.ledger import BudgetLedger
from funpack.protocol import CORE_FILES
from funpack.storage import read_json, write_json
from scripts import run_thinking_pilot_200 as runner
from test_protocol import make_data
from test_search import workspace_temp


class ThinkingPilotTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(workspace_temp())
        (self.root / "configs").mkdir()
        (self.root / "scripts").mkdir()
        make_data(self.root / "data")
        for name in CORE_FILES:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("# Integrity fixture only\n", encoding="utf-8")
        (self.root / "scripts/run_thinking_pilot_200.py").write_text("# Runner integrity fixture\n")
        shutil.copy2(runner.ROOT / "configs/formal-plan.proposed.json", self.root / "configs/formal-plan.proposed.json")
        template = read_json(runner.ROOT / "configs/formal-api.proposed.json")
        for config_name, claim_name, count in runner.PRIOR:
            config = copy.deepcopy(template)
            ledger_name = "configs/deepseek_pilot_ledger.sqlite3" if count == 144 else "configs/prompt64_budget.sqlite3"
            config.update(enabled=True, max_requests=count, ledger_path=ledger_name, api_key_env="THINKING_TEST_KEY")
            config["budget"]["max_cost"] = 10
            write_json(self.root / config_name, config)
            ledger = BudgetLedger(self.root / ledger_name, config)
            for _ in range(count):
                reservation = ledger.reserve(.001)
                ledger.settle(reservation["reservation_id"], .001, 1, 1)
            write_json(self.root / claim_name, {"status": "completed"})
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.network = self.enterContext(patch("urllib.request.build_opener", side_effect=AssertionError("no network")))
        self.enterContext(patch.object(runner, "check_connection", return_value=None))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def fake_run(self, argv):
        self.assertEqual(argv[argv.index("--calls")+1], "50")
        self.assertEqual(argv[argv.index("--enhanced-prompt-version")+1], "mechanism_v3")
        self.assertEqual(argv[argv.index("--collision-policy")+1], "disable_on_mismatch_v1")
        self.assertEqual(argv[argv.index("--seeds")+1], "11")
        config = read_json(self.root / runner.ACTIVE)
        ledger = BudgetLedger(self.root / runner.LEDGER, config)
        entries = []
        for arm in "ABCD":
            run_name = arm+"_seed11"
            directory = self.root / runner.OUTPUT / run_name
            directory.mkdir(parents=True)
            events = []
            for i in range(50):
                reservation = ledger.reserve(.001)
                ledger.settle(reservation["reservation_id"], .001, 1, 1)
                events.append({"response": {"model": "test-only-simulated-model", "finish_reason": "length" if i == 0 else "stop",
                                              "raw_response": {"choices": [{"message": {"reasoning_content": "test fixture"}}]}}})
            import json
            (directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events)+"\n", encoding="utf-8")
            entries.append({"run": run_name, "arm": arm, "seed": 11, "termination": "request_budget_completed",
                            "metrics": {"paid_api_requests": 50, "valid_on_probes_or_search": 50}, "best_search_mean_bins": 2,
                            "selected_validation_mean_bins": 2})
        write_json(self.root / runner.OUTPUT / "study.json", {"runs": entries, "test_fixture_only": True})

    def test_preview_is_read_only_no_secret_prompt_or_heldout(self):
        before = self.snapshot()
        old_open = Path.open
        def guarded(path, *a, **kw):
            if "heldout" in path.parts:
                raise AssertionError("held-out read")
            return old_open(path, *a, **kw)
        with patch.object(Path, "open", guarded), patch.object(runner.getpass, "getpass", side_effect=AssertionError("secret prompt")):
            preview = runner.main([], root=self.root)
        self.assertTrue(preview["ready_to_start"])
        self.assertEqual(preview["total_requests"], 200)
        self.assertEqual(preview["prior_requests"], 208)
        self.assertIsNone(preview["effective_temperature"])
        self.assertEqual(preview["api_config"]["extra_body"]["thinking"], {"type": "enabled"})
        self.assertEqual(preview["api_config"]["max_tokens"], 32768)
        self.assertEqual(self.snapshot(), before)
        self.network.assert_not_called()

    def test_existing_claim_blocks_before_key_entry(self):
        write_json(self.root / runner.CLAIM, {"status": "interrupted"})
        before = self.snapshot()
        with patch.object(runner.getpass, "getpass") as hidden:
            with self.assertRaisesRegex(ValueError, "already claimed"):
                runner.main(["--run"], root=self.root)
        hidden.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_empty_key_leaves_no_claim_or_ledger(self):
        before = self.snapshot()
        with patch.dict(os.environ, {}, clear=True), patch.object(runner.sys.stdin, "isatty", return_value=True), patch.object(runner.getpass, "getpass", return_value=""):
            with self.assertRaisesRegex(ValueError, "empty key"):
                runner.main(["--run"], root=self.root)
        self.assertEqual(self.snapshot(), before)

    def test_noninteractive_without_key_cannot_start(self):
        before = self.snapshot()
        with patch.dict(os.environ, {}, clear=True), patch.object(runner.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "interactive terminal"):
                runner.main(["--run"], root=self.root)
        self.assertEqual(self.snapshot(), before)

    def test_authorized_fake_batch_records_cost_reasoning_and_truncation(self):
        with patch.dict(os.environ, {"THINKING_TEST_KEY": "test-only"}), patch("experiment.main", side_effect=self.fake_run), patch("audit_and_cost.analyze", return_value={"test_fixture_only": True}):
            result = runner.main(["--run"], root=self.root)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["new_requests"], 200)
        self.assertEqual(result["summary"]["cumulative_requests"], 408)
        self.assertAlmostEqual(result["summary"]["cumulative_estimated_cost_cny"], .408)
        self.assertTrue(result["prior_ledgers_unchanged"])
        for row in result["summary"]["rows"]:
            self.assertEqual(row["finish_reasons"], {"length": 1, "stop": 49})
            self.assertEqual(row["responses_with_reasoning_content"], 50)
        self.assertFalse(runner.prepare(self.root)[1]["ready_to_start"])
        self.network.assert_not_called()

    def test_interruption_keeps_claim_and_restores_entered_secret_and_cwd(self):
        original_cwd = Path.cwd()
        with patch.dict(os.environ, {}, clear=True), patch.object(runner.sys.stdin, "isatty", return_value=True), patch.object(runner.getpass, "getpass", return_value="test-only"), patch("experiment.main", side_effect=RuntimeError("test interruption")):
            with self.assertRaises(RuntimeError):
                runner.main(["--run"], root=self.root)
            self.assertNotIn("THINKING_TEST_KEY", os.environ)
        self.assertEqual(Path.cwd(), original_cwd)
        self.assertEqual(read_json(self.root / runner.CLAIM)["status"], "interrupted")

    def test_pending_prior_phase_blocks_new_run(self):
        write_json(self.root / runner.PRIOR[1][1], {"status": "interrupted"})
        with self.assertRaisesRegex(ValueError, "complete and accounted"):
            runner.prepare(self.root)

    def test_connection_failure_is_detected_before_key_prompt_or_claim(self):
        before = self.snapshot()
        with patch.object(runner, "check_connection", side_effect=ValueError("TLS preflight failed")), patch.object(runner.getpass, "getpass") as secret:
            with self.assertRaisesRegex(ValueError, "TLS preflight failed"):
                runner.main(["--run"], root=self.root)
        secret.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_retry_keeps_failed_ledger_and_uses_new_paths(self):
        config, _ = runner.prepare(self.root)
        write_json(self.root / runner.ACTIVE, config)
        failed = BudgetLedger(self.root / runner.LEDGER, config)
        for _ in range(200):
            reservation = failed.reserve(.002)
            failed.settle(reservation["reservation_id"], None, None, None)
        write_json(self.root / runner.CLAIM, {"status": "completed_with_accounting_review"})
        import json
        for arm in "ABCD":
            folder = self.root / runner.OUTPUT / (arm+"_seed11")
            folder.mkdir(parents=True)
            write_json(folder / "final_info.json", {"metrics": {"candidate_responses": 0}})
            (folder / "events.jsonl").write_text("\n".join(json.dumps({"response": {"error": {"type": "network_error"}}}) for _ in range(50))+"\n", encoding="utf-8")
        before = self.snapshot()
        retry_config, preview = runner.prepare(self.root, retry=True, direct=True)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(preview["ready_to_start"])
        self.assertEqual(preview["prior_requests"], 408)
        self.assertEqual(preview["prior_unknown_cost_requests"], 200)
        self.assertAlmostEqual(preview["prior_unknown_reservations_cny"], .4)
        self.assertNotEqual(retry_config["ledger_path"], runner.LEDGER)
        self.assertNotEqual(preview["artifacts"]["output"], runner.OUTPUT)
        write_json(self.root / runner.OUTPUT / "A_seed11/final_info.json", {"metrics": {"candidate_responses": 1}})
        with self.assertRaisesRegex(ValueError, "not entirely network-failed"):
            runner.prepare(self.root, retry=True, direct=True)

    def test_finished_requests_without_candidates_are_not_reported_as_success(self):
        def failed_batch(argv):
            self.fake_run(argv)
            study = read_json(self.root / runner.OUTPUT / "study.json")
            for run in study["runs"]:
                run["metrics"]["valid_on_probes_or_search"] = 0
            write_json(self.root / runner.OUTPUT / "study.json", study)
        with patch.dict(os.environ, {"THINKING_TEST_KEY": "test-only"}), patch("experiment.main", side_effect=failed_batch), patch("audit_and_cost.analyze", return_value={"test_fixture_only": True}):
            result = runner.main(["--run"], root=self.root)
        self.assertEqual(result["status"], "finished_without_usable_generation")
        self.assertFalse(result["summary"]["all_arms_have_usable_candidates"])


class DirectConnectionTests(unittest.TestCase):
    def test_direct_route_bypasses_only_api_host_and_restores_environment(self):
        import urllib.request
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "example.test", "no_proxy": "example.test"}, clear=True):
            before = dict(os.environ)
            with runner.connection_route(True):
                self.assertTrue(urllib.request.proxy_bypass("api.deepseek.com"))
                self.assertFalse(urllib.request.proxy_bypass("other.test"))
            self.assertEqual(dict(os.environ), before)

    def test_system_route_does_not_change_environment(self):
        before = dict(os.environ)
        with runner.connection_route(False):
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
