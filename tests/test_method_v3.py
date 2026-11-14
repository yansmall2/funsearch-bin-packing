"""Behavioral regressions for the approved method; all providers are offline."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import experiment
from funpack.programs import BF_CODE, FF_CODE
from funpack.prompts import build_messages
from funpack.providers import OfflineProvider
from funpack.screening import BehavioralScreen
from funpack.search import run_search
from funpack.storage import digest, read_json
from test_search import INSTANCES, PROBES, _config, _events, workspace_temp

PARENTS = [{"code": FF_CODE, "counts": [3], "mean_bins": 3},
           {"code": BF_CODE, "counts": [2], "mean_bins": 2}]
COLLISION = "def priority(item, bins):\n    return [r if item == 3 else -r for r in bins]"
ALTERNATIVE = "def priority(item, bins):\n    return [r+1 if item == 3 else -r for r in bins]"


def revised_config(calls=1):
    return {**_config(arm="D", calls=calls, audit_rate=1.0),
            "enhanced_prompt_version": "mechanism_v3", "collision_policy": "disable_on_mismatch_v1"}


class PromptVersionTests(unittest.TestCase):
    def test_historical_prompt_bytes_preserved(self):
        # These hashes were computed with the actual unmodified implementation.
        self.assertEqual(digest(build_messages("basic", PARENTS)),
                         "98267cb50f2ee6c0a7300262a1fc2eb8cf76a9d7a287bdc5eb5f195d976ecaa4")
        self.assertEqual(digest(build_messages("enhanced", PARENTS)),
                         "ee2b494ad71fe1baab835e9b700c1674de71c833ae140f9e79a3ecd69123783b")

    def test_v3_keeps_contract_parent_feedback_and_basic_control(self):
        parents = copy.deepcopy(PARENTS)
        parents[0].update(validation="DO_NOT_LEAK", probes="DO_NOT_LEAK", audit="DO_NOT_LEAK")
        new = build_messages("enhanced", parents, enhanced_version="mechanism_v3")
        old = build_messages("enhanced", parents)
        self.assertEqual(new[0], old[0])
        self.assertIn(json.dumps(PARENTS), new[1]["content"])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(new))
        self.assertNotIn("Propose one useful variation", new[1]["content"])
        self.assertIn("novelty alone is not success", new[1]["content"])
        self.assertEqual(build_messages("basic", parents, enhanced_version="mechanism_v3"),
                         build_messages("basic", parents))

    def test_unknown_method_rejected_before_output_or_generation(self):
        for field in ("enhanced_prompt_version", "collision_policy"):
            with self.subTest(field=field), workspace_temp() as directory:
                config = revised_config()
                config[field] = "typo"
                provider = OfflineProvider(fixtures=[COLLISION])
                with self.assertRaises(ValueError):
                    run_search(INSTANCES, PROBES, provider, config, directory / "new")
                self.assertEqual(provider.attempts, 0)
                self.assertFalse((directory / "new").exists())


class CollisionPolicyTests(unittest.TestCase):
    def test_disabling_is_full_fingerprint_scoped_persistent_and_idempotent(self):
        states = [{"item": 2, "bins": [4, 3]} for _ in range(32)] + [
            {"item": 3, "bins": [4, 3]} for _ in range(96)]
        screen = BehavioralScreen(states, collision_policy="disable_on_mismatch_v1")
        zero = lambda item, bins: [0, 0]
        tail_one = lambda item, bins: [0, item - 2]
        first, other = {"id": "first"}, {"id": "other"}
        screen.register(zero, first)
        screen.register(tail_one, other)
        _, prefix, tail = screen.probe(zero)
        self.assertTrue(screen.disable(prefix, tail))
        self.assertFalse(screen.disable(prefix, tail))
        self.assertIsNone(screen.probe(zero)[0])
        self.assertIs(screen.probe(tail_one)[0], other)
        screen.register(zero, {"id": "cannot_reenable"})
        self.assertIsNone(screen.probe(zero)[0])
        self.assertEqual(len(screen.disabled_fingerprints), 1)
        with self.assertRaises(ValueError):
            screen.disable(prefix, None)

    def test_audited_count_collision_bypasses_next_source_but_keeps_exact_cache(self):
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES,
                                OfflineProvider(fixtures=[COLLISION, ALTERNATIVE, ALTERNATIVE, BF_CODE.rstrip()]),
                                revised_config(4), directory)
            metrics, events = result["metrics"], _events(directory)
            self.assertEqual(metrics["disabled_fingerprints"], 1)
            self.assertEqual(metrics["audits"], 1)
            self.assertEqual(metrics["candidate_full_evaluations"], 2)
            self.assertEqual(metrics["collision_bypass_full_evaluations"], 1)
            self.assertEqual(metrics["text_cache_hits"], 2)
            self.assertEqual(metrics["behavior_skips"], 0)
            self.assertTrue(events[0]["fingerprint_disabled"])
            self.assertTrue(events[1]["collision_bypass"])
            self.assertEqual(events[0]["fingerprint_hash"], events[1]["fingerprint_hash"])
            self.assertNotIn("audited", events[1])
            self.assertEqual(events[1]["result"]["counts"], [3])
            self.assertGreater(metrics["collision_bypass_evaluation_seconds"], 0)
            self.assertLessEqual(metrics["collision_bypass_evaluation_seconds"], metrics["evaluation_seconds"])
            self.assertEqual(metrics["paid_api_requests"], 0)
            self.assertEqual(events[1]["messages_hash"], digest(events[1]["messages"]))

    def test_equal_bin_counts_but_different_trace_also_disables(self):
        instances = [{"id": "trace_only", "capacity": 10, "items": [6, 7, 3]}]
        with workspace_temp() as directory:
            result = run_search(instances, PROBES, OfflineProvider(fixtures=[COLLISION, ALTERNATIVE]),
                                revised_config(2), directory)
            self.assertEqual(result["metrics"]["audit_count_mismatches"], 0)
            self.assertEqual(result["metrics"]["audit_trace_mismatches"], 1)
            self.assertEqual(result["metrics"]["disabled_fingerprints"], 1)
            self.assertTrue(_events(directory)[1]["collision_bypass"])

    def test_matching_audit_does_not_disable(self):
        equivalent = "def priority(item, bins):\n    return [-r for r in bins]"
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[equivalent]),
                                revised_config(), directory)
            self.assertEqual(result["metrics"]["audits"], 1)
            self.assertEqual(result["metrics"]["disabled_fingerprints"], 0)

    def test_failed_audit_is_logged_but_does_not_claim_a_completed_mismatch(self):
        error = "def priority(item, bins):\n    return [r/(item-3) for r in bins]"
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[error]),
                                revised_config(), directory)
            self.assertEqual(result["metrics"]["audit_evaluation_errors"], 1)
            self.assertEqual(result["metrics"]["disabled_fingerprints"], 0)

    def test_error_after_disabled_match_is_charged_to_full_evaluation(self):
        error = "def priority(item, bins):\n    return [r/(item-3) for r in bins]"
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[COLLISION, error]),
                                revised_config(2), directory)
            self.assertEqual(result["metrics"]["collision_bypass_full_evaluations"], 1)
            self.assertEqual(result["metrics"]["collision_bypass_evaluation_errors"], 1)
            self.assertEqual(result["metrics"]["audits"], 1)
            self.assertNotIn("result", _events(directory)[1])

    def test_legacy_policy_keeps_old_audit_behavior(self):
        config = revised_config(2)
        config["collision_policy"] = "retain_representative"
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[COLLISION, ALTERNATIVE]),
                                config, directory)
            self.assertEqual(result["metrics"]["audits"], 2)
            self.assertEqual(result["metrics"]["disabled_fingerprints"], 0)
            self.assertEqual(result["metrics"]["collision_bypass_full_evaluations"], 0)

    def test_disable_survives_island_reset(self):
        config = revised_config(2)
        config["reset_every"] = 1
        with workspace_temp() as directory:
            result = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[COLLISION, ALTERNATIVE]),
                                config, directory)
            self.assertEqual(result["metrics"]["collision_bypass_full_evaluations"], 1)
            self.assertTrue(_events(directory)[0]["reset_islands"])

    def test_cli_routes_versions_to_all_four_arms(self):
        with workspace_temp() as directory:
            with (patch.object(experiment, "load_instances", return_value=INSTANCES),
                  patch.object(experiment, "load_probes", return_value=PROBES),
                  patch("builtins.print")):
                experiment.main(["--stage", "search", "--out_dir", str(directory), "--calls", "1",
                                 "--seeds", "11", "--enhanced-prompt-version", "mechanism_v3",
                                 "--collision-policy", "disable_on_mismatch_v1"])
            for arm in "ABCD":
                info = read_json(directory / (arm + "_seed11") / "final_info.json")
                method = info["method"]
                self.assertEqual(method["prompt_version"], "mechanism_v3" if arm in "BD" else "basic_v1")
                self.assertEqual(method["collision_policy"], "disable_on_mismatch_v1" if arm in "CD" else "off")


if __name__ == "__main__":
    unittest.main()
