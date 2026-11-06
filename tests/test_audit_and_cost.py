import unittest
from pathlib import Path
from unittest.mock import patch

from audit_and_cost import analyze, audit_events, ratio
from funpack.storage import digest


class AuditSummaryTests(unittest.TestCase):
    def test_no_audit_has_no_rate(self):
        summary = audit_events([], [], ["one"])
        self.assertEqual(summary["audits_attempted"], 0)
        self.assertIsNone(summary["quality_disagreement_rate_completed"])
        self.assertIsNone(ratio(0, 0))

    def test_quality_delta_trace_and_errors_are_separate(self):
        representative = {"id": "ref", "counts": [4, 5, 7], "trace_hashes": ["x", "y", "z"]}
        event = {"request_index": 1, "audited": True, "status": "audit_evaluated",
                 "representative": "ref", "result": {"counts": [3, 5, 8], "trace_hashes": ["a", "y", "b"]}}
        error = {"request_index": 2, "audited": True, "status": "audit_evaluation_error", "error": "timeout"}
        summary = audit_events([event, error], [representative], ["one", "two", "three"])
        self.assertEqual((summary["audits_attempted"], summary["audits_completed"], summary["audit_errors"]), (2, 1, 1))
        self.assertEqual(summary["quality_disagreement_rate_completed"], 1)
        self.assertEqual(summary["audit_error_rate_attempted"], .5)
        detail = summary["details"][0]
        self.assertEqual([r["delta_bins_candidate_minus_representative"] for r in detail["instances"]], [-1, 0, 1])
        self.assertEqual((detail["candidate_better_instances"], detail["equal_bin_instances"], detail["candidate_worse_instances"]), (1, 1, 1))

    def test_missing_representative_does_not_fabricate_comparison(self):
        event = {"request_index": 1, "audited": True, "status": "audit_evaluated", "representative": "missing", "result": {}}
        with self.assertRaises(ValueError):
            audit_events([event], [], ["one"])


class TimingSummaryTests(unittest.TestCase):
    """Tiny in-memory records check accounting without reading any data files."""

    def summarize(self, *, original=False, remeasurement=False, complete=True):
        instances = [{"id": "one", "capacity": 10, "items": [6, 4]}]
        study = {"phase": "formal" if original else "pilot", "search_hash": digest(instances),
                 "probe_hash": "frozen_probe", "arguments": {"arms": list("ABCD"), "seeds": [11]},
                 "runs": []}
        if original:
            study["timing"] = {"setup_seconds": 8, "probe_construction_seconds": 4,
                               "common_setup_seconds": 4, "allocation_rule": "common/4; probes/2",
                               "scope": "test fixture timing scope", "baseline_report_seconds": 3,
                               "study_elapsed_seconds": 51}
        for arm in "ABCD":
            row = {"run": arm + "_seed11", "arm": arm, "seed": 11,
                   "termination": "request_budget_completed",
                   "metrics": {"audits": 0, "search_total_seconds": 7, "screening_seconds": .1,
                               "evaluation_seconds": 2, "generation_seconds": 4,
                               "estimated_cost_reported": .01, "unknown_cost_requests": 0}}
            if original:
                probe_share = 2 if arm in "CD" else 0
                row["timing"] = {"pipeline_seconds": 9, "common_setup_share_seconds": 1,
                                 "probe_construction_share_seconds": probe_share,
                                 "inclusive_search_seconds": 10 + probe_share}
            study["runs"].append(row)
        if not complete:
            study["runs"].pop()
        measurement = ({"probe_hash": "frozen_probe", "median_seconds": 1000,
                        "kind": "remeasurement_not_original_setup_time"} if remeasurement else None)

        def fake_read(path):
            if Path(path).name == "study.json":
                return study
            if Path(path).name == "selection.json":
                return {"seconds": 1}
            raise AssertionError("Unexpected JSON read: " + str(path))

        with (patch("audit_and_cost.read_json", side_effect=fake_read),
              patch("audit_and_cost.load_instances", return_value=instances) as loader,
              patch("audit_and_cost.Path.read_text", return_value="")):
            report = analyze("synthetic-study", "synthetic-data", measurement)
        loader.assert_called_once_with(Path("synthetic-data") / "search.json")
        return report, study

    def test_original_timing_never_adds_remeasurement_or_selection_twice(self):
        report, study = self.summarize(original=True, remeasurement=True)
        self.assertEqual(report["original_study_timing"], study["timing"])
        self.assertIn("formal", report["scope"])
        costs = {r["arm"]: r["cost"] for r in report["runs"]}
        self.assertEqual(costs["C"]["pipeline_seconds"], 9)
        self.assertEqual(costs["C"]["inclusive_search_seconds"], 12)
        self.assertEqual(costs["A"]["inclusive_search_seconds"], 10)
        self.assertEqual(costs["C"]["probe_construction_share_seconds"], 2)
        self.assertEqual(costs["C"]["probe_reconstruction_share_seconds"], 500)
        self.assertTrue(all(c["search_selection_setup_estimate_seconds"] is None for c in costs.values()))
        self.assertFalse(report["heldout_loaded"])

    def test_legacy_missing_probe_setup_remains_unknown(self):
        report, _ = self.summarize()
        self.assertIsNone(report["original_study_timing"])
        costs = {r["arm"]: r["cost"] for r in report["runs"]}
        self.assertEqual(costs["A"]["search_selection_setup_estimate_seconds"], 8)
        self.assertIsNone(costs["C"]["search_selection_setup_estimate_seconds"])
        self.assertIsNone(costs["C"]["probe_construction_share_seconds"])
        self.assertTrue(all(c["inclusive_search_seconds"] is None for c in costs.values()))

    def test_legacy_remeasurement_is_estimate_not_original_measurement(self):
        report, _ = self.summarize(remeasurement=True)
        costs = {r["arm"]: r["cost"] for r in report["runs"]}
        self.assertEqual(costs["C"]["search_selection_setup_estimate_seconds"], 508)
        self.assertEqual(costs["A"]["search_selection_setup_estimate_seconds"], 8)
        self.assertIsNone(costs["C"]["inclusive_search_seconds"])
        self.assertIsNone(costs["C"]["probe_construction_share_seconds"])

    def test_incomplete_legacy_study_does_not_invent_setup_allocation(self):
        report, _ = self.summarize(remeasurement=True, complete=False)
        self.assertFalse(report["complete_balanced_study"])
        c_run = next(r for r in report["runs"] if r["arm"] == "C")
        self.assertIsNone(c_run["cost"]["probe_reconstruction_share_seconds"])
        self.assertIsNone(c_run["cost"]["search_selection_setup_estimate_seconds"])
