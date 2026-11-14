"""Formal activation safeguards with isolated fixtures and a blocked network."""
import copy
from contextlib import ExitStack
import io
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from funpack.ledger import BudgetLedger
from funpack.protocol import CORE_FILES
from funpack.storage import read_json, write_json
from scripts import run_formal_study as runner
from test_protocol import make_data


class FormalRunnerTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.workspace.mkdir(exist_ok=True)
        self.root = self.workspace / ("formal-runner-" + uuid.uuid4().hex)
        (self.root / "configs").mkdir(parents=True)
        self.addCleanup(self.cleanup)
        make_data(self.root / "data")
        for name in CORE_FILES:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("# Test-owned integrity fixture, never executed.\n", encoding="utf-8")
        for name in (runner.PLAN, runner.PROPOSED_API):
            shutil.copy2(runner.ROOT / name, self.root / name)
        self.prior = read_json(self.root / runner.PROPOSED_API)
        self.prior.update(enabled=True, max_requests=144, ledger_path=runner.PRIOR_LEDGER)
        self.prior["budget"]["max_cost"] = 10
        write_json(self.root / "configs/calibration.local.json", self.prior)
        ledger = BudgetLedger(self.root / runner.PRIOR_LEDGER, self.prior)
        for _ in range(144):
            reservation = ledger.reserve(0.001)
            ledger.settle(reservation["reservation_id"], 0.001, 2, 3)
        write_json(self.root / "configs/calibration128.claim.local.json", {"status": "completed"})
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch("sys.stdout", new=io.StringIO()))
        self.network = self.stack.enter_context(patch(
            "funpack.providers.request.build_opener", side_effect=AssertionError("No network in tests")))

    def cleanup(self):
        if self.root.resolve().parent != self.workspace or self.root.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.root)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def fake_run(self, argv):
        # Only create an EMPTY test ledger. Completeness and sealing are separate
        # mocked components; real 4000-request integrity is enforced by protocol.py.
        BudgetLedger(self.root / runner.FORMAL_LEDGER, read_json(self.root / runner.ACTIVE_API))
        (self.root / runner.STUDY).mkdir()

    def activation(self, complete=True, seal_error=None):
        execute = self.stack.enter_context(patch("formal.main", side_effect=self.fake_run))
        self.stack.enter_context(patch("audit_and_cost.analyze", return_value={
            "complete_balanced_study": complete, "simulated_test_only": True}))
        sealed = self.stack.enter_context(patch.object(runner, "seal_candidates", side_effect=seal_error))
        return execute, sealed

    def test_preview_does_not_write_prompt_open_heldout_or_call_network(self):
        before = self.snapshot()
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if "heldout" in path.parts:
                raise AssertionError("held-out contents opened in preview")
            return original_open(path, *args, **kwargs)

        with (patch.object(Path, "open", guarded_open),
              patch.object(runner.getpass, "getpass", side_effect=AssertionError("unexpected key prompt"))):
            preview = runner.main([], root=self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(preview["ready_to_activate"])
        self.assertEqual(preview["total_new_requests"], 4000)
        self.assertAlmostEqual(preview["project_cost_limit_including_prior_estimate_cny"], 20.144)
        self.network.assert_not_called()

    def test_changed_call_scope_and_money_cap_rejected_before_writes(self):
        plan = read_json(self.root / runner.PLAN)
        changed = copy.deepcopy(plan)
        changed["calls_per_run"] = 201
        write_json(self.root / runner.PLAN, changed)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "plan changed"):
            runner.prepare(self.root)
        self.assertEqual(self.snapshot(), before)
        write_json(self.root / runner.PLAN, plan)
        config = read_json(self.root / runner.PROPOSED_API)
        config["budget"]["max_cost"] = 21
        write_json(self.root / runner.PROPOSED_API, config)
        with self.assertRaisesRegex(ValueError, "budget changed"):
            runner.prepare(self.root)

    def add_prompt_ledger(self):
        config = copy.deepcopy(self.prior)
        config.update(max_requests=64, ledger_path=runner.PROMPT_LEDGER)
        config["budget"]["max_cost"] = 2
        write_json(self.root / runner.PROMPT_CONFIG, config)
        ledger = BudgetLedger(self.root / runner.PROMPT_LEDGER, config)
        for _ in range(64):
            reservation = ledger.reserve(.002)
            ledger.settle(reservation["reservation_id"], .002, 2, 3)
        write_json(self.root / runner.PROMPT_CLAIM, {"status": "completed"})

    def test_prompt_diagnostic_cost_is_included_without_mutating_ledger(self):
        self.add_prompt_ledger()
        before = self.snapshot()
        _, preview = runner.prepare(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(preview["prior_total_requests"], 208)
        self.assertAlmostEqual(preview["prior_total_estimated_cost_cny"], .272)
        self.assertAlmostEqual(preview["project_cost_limit_including_prior_estimate_cny"], 20.272)
        self.assertEqual(len(preview["prior_ledger_hashes"]), 2)

    def test_partial_prompt_ledger_history_blocks_preview(self):
        write_json(self.root / runner.PROMPT_CLAIM, {"status": "completed"})
        with self.assertRaisesRegex(ValueError, "accounting is incomplete"):
            runner.prepare(self.root)

    def test_thinking_pilot_charges_are_included_after_completion(self):
        self.add_prompt_ledger()
        config = copy.deepcopy(self.prior)
        config.update(max_requests=200, ledger_path=runner.THINKING_LEDGER)
        write_json(self.root / runner.THINKING_CONFIG, config)
        ledger = BudgetLedger(self.root / runner.THINKING_LEDGER, config)
        for _ in range(200):
            reservation = ledger.reserve(.001)
            ledger.settle(reservation["reservation_id"], .001, 2, 3)
        write_json(self.root / runner.THINKING_CLAIM, {"status": "completed"})
        _, preview = runner.prepare(self.root)
        self.assertEqual(preview["prior_total_requests"], 408)
        self.assertAlmostEqual(preview["prior_total_estimated_cost_cny"], .472)
        write_json(self.root / runner.THINKING_CLAIM, {"status": "interrupted"})
        with self.assertRaisesRegex(ValueError, "completed and accounted"):
            runner.prepare(self.root)

    def test_incomplete_prompt_claim_blocks_preview(self):
        self.add_prompt_ledger()
        write_json(self.root / runner.PROMPT_CLAIM, {"status": "interrupted"})
        with self.assertRaisesRegex(ValueError, "completed and accounted"):
            runner.prepare(self.root)

    def test_existing_ledger_blocks_activation_before_key_prompt(self):
        (self.root / runner.FORMAL_LEDGER).write_bytes(b"prior artifact")
        before = self.snapshot()
        with patch.object(runner.getpass, "getpass") as hidden:
            with self.assertRaisesRegex(ValueError, "formal activation blocked"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        hidden.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_empty_key_leaves_all_files_unchanged(self):
        before = self.snapshot()
        with (patch.dict(os.environ, {}, clear=True), patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="")):
            with self.assertRaisesRegex(ValueError, "empty key"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        self.assertEqual(before, self.snapshot())

    def test_pending_prompt_review_blocks_formal_activation(self):
        write_json(self.root / "configs/research-stage.json", {"formal_start_allowed": False})
        self.assertFalse(runner.prepare(self.root)[1]["ready_to_activate"])
        with patch.object(runner.getpass, "getpass") as hidden:
            with self.assertRaisesRegex(ValueError, "formal activation blocked"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        hidden.assert_not_called()

    def test_approved_simulated_run_freezes_then_seals_without_final_evaluation(self):
        execute, sealed = self.activation()
        before = (self.root / runner.PRIOR_LEDGER).read_bytes()
        with (patch.dict(os.environ, {"FUNPACK_API_KEY": "test-only-existing"}),
              patch("formal.evaluate_final", side_effect=AssertionError("heldout evaluation forbidden"))):
            result = runner.main([runner.APPROVAL_FLAG], root=self.root)
            self.assertEqual(os.environ["FUNPACK_API_KEY"], "test-only-existing")
        self.assertEqual(result["status"], "completed_and_sealed")
        self.assertEqual(read_json(self.root / runner.FROZEN)["total_planned_requests"], 4000)
        self.assertEqual(execute.call_args.args[0][0], "run")
        self.assertIn("--allow-paid", execute.call_args.args[0])
        sealed.assert_called_once()
        self.assertTrue(result["prior_ledger_unchanged"])
        self.assertEqual(before, (self.root / runner.PRIOR_LEDGER).read_bytes())
        self.assertFalse(runner.prepare(self.root)[1]["ready_to_activate"])
        self.network.assert_not_called()

    def test_incomplete_study_not_sealed_or_automatically_retried(self):
        execute, sealed = self.activation(complete=False)
        with patch.dict(os.environ, {"FUNPACK_API_KEY": "test-only"}):
            result = runner.main([runner.APPROVAL_FLAG], root=self.root)
        self.assertEqual(result["status"], "stopped_incomplete")
        sealed.assert_not_called()
        execute.assert_called_once()
        self.assertFalse(runner.prepare(self.root)[1]["ready_to_activate"])

    def test_sealing_failure_never_marked_completed(self):
        self.activation(seal_error=ValueError("incomplete frozen evidence"))
        with patch.dict(os.environ, {"FUNPACK_API_KEY": "test-only"}):
            with self.assertRaisesRegex(ValueError, "incomplete frozen evidence"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
        self.assertEqual(read_json(self.root / runner.CLAIM)["status"], "interrupted")

    def test_interruption_restores_cwd_and_removes_entered_secret(self):
        previous = Path.cwd()
        with (patch("formal.main", side_effect=RuntimeError("simulated failure")),
              patch.dict(os.environ, {}, clear=True), patch.object(runner.sys.stdin, "isatty", return_value=True),
              patch.object(runner.getpass, "getpass", return_value="temporary-test-secret")):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                runner.main([runner.APPROVAL_FLAG], root=self.root)
            self.assertNotIn("FUNPACK_API_KEY", os.environ)
        self.assertEqual(Path.cwd(), previous)
        claim = read_json(self.root / runner.CLAIM)
        self.assertEqual(claim["status"], "interrupted")
        self.assertEqual(claim["error_type"], "RuntimeError")
        self.assertNotIn("temporary-test-secret", str(claim))
        self.assertFalse(runner.prepare(self.root)[1]["ready_to_activate"])


if __name__ == "__main__":
    unittest.main()
