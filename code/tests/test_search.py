"""Integration checks with handwritten fixtures; no network or LLM evidence."""

from contextlib import contextmanager
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import experiment
from funpack.database import Database
from funpack.programs import BF_CODE, FF_CODE, Program
from funpack.providers import OfflineProvider
from funpack.screening import BehavioralScreen
from funpack.search import run_search
from funpack.storage import digest, write_json


@contextmanager
def workspace_temp():
    root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
    root.mkdir(exist_ok=True)
    directory = root / ("search-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent != root or directory.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(directory)


INSTANCES = [{"id": "hand", "capacity": 10, "items": [6, 7, 3, 4]}]
# Handconstructed test states deliberately repeat to control signature collisions.
# Production data independently requires 128 unique development trajectory states.
PROBES = [{"item": 2, "bins": [4, 3]} for _ in range(128)]


def _config(arm="C", calls=1, audit_rate=0.0):
    return {"arm": arm, "calls": calls, "seed": 37, "islands": 2,
            "temperature": 1.0, "reset_every": 0, "audit_rate": audit_rate}


def _events(directory):
    return [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _record(name, counts, code):
    return {"id": name, "counts": counts, "mean_bins": sum(counts) / len(counts),
            "code": code, "code_hash": digest(code), "trace_hashes": ["trace"] * len(counts)}


class ScreenTests(unittest.TestCase):
    def test_two_stage_short_circuit_and_full_match(self):
        states = ([{"item": 1, "bins": [4, 3]} for _ in range(32)]
                  + [{"item": 2, "bins": [4, 3]} for _ in range(96)])
        screen = BehavioralScreen(states)
        representative = {"id": "fully_evaluated"}
        screen.register(lambda item, bins: [0, 0], representative)
        calls = []

        def new_prefix(item, bins):
            calls.append(item)
            return [0, 1]
        match, prefix, tail = screen.probe(new_prefix)
        self.assertIsNone(match)
        self.assertIsNone(tail)
        self.assertEqual(len(calls), 32)
        self.assertEqual(prefix, (1,) * 32)
        calls.clear()

        def new_tail(item, bins):
            calls.append(item)
            return [0, item - 1]
        match, prefix, tail = screen.probe(new_tail)
        self.assertIsNone(match)
        self.assertEqual(len(calls), 128)
        self.assertEqual(prefix, (0,) * 32)
        self.assertEqual(tail, (1,) * 96)
        self.assertIs(screen.probe(lambda item, bins: [0, 0])[0], representative)


class SearchTests(unittest.TestCase):
    def test_exact_code_cache_reuses_only_measured_results(self):
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES,
                                 OfflineProvider(fixtures=[BF_CODE.rstrip()]),
                                 _config(arm="A", calls=2), directory)
            self.assertEqual(summary["metrics"]["text_cache_hits"], 2)
            self.assertEqual(summary["metrics"]["candidate_full_evaluations"], 0)
            self.assertEqual(summary["metrics"]["requests_attempted"], 0)
            self.assertEqual(summary["metrics"]["estimated_cost_reported"], 0)
            self.assertEqual(summary["evidence_scope"], "offline_fixture_plumbing_only")
            self.assertTrue(all(e["representative"] == "seed_bf" for e in _events(directory)))

    def test_behavior_skip_does_not_invent_a_result_or_exact_cache_entry(self):
        equivalent = "def priority(item, bins):\n    return [2 * (-(r - item)) + 3 for r in bins]"
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[equivalent]),
                                 _config(calls=2), directory)
            self.assertEqual(summary["metrics"]["behavior_skips"], 2)
            self.assertEqual(summary["metrics"]["text_cache_hits"], 0)
            self.assertEqual(summary["metrics"]["candidate_full_evaluations"], 0)
            self.assertEqual(len(summary["top_search_candidates"]), 2)
            for event in _events(directory):
                self.assertEqual(event["status"], "behavior_skip")
                self.assertNotIn("result", event)

    def test_forced_audit_records_real_finite_probe_false_positive(self):
        collision = "def priority(item, bins):\n    return [r if item == 3 else -r for r in bins]"
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[collision]),
                                 _config(audit_rate=1.0), directory)
            metrics = summary["metrics"]
            self.assertEqual(metrics["audits"], 1)
            self.assertEqual(metrics["behavior_skips"], 0)
            self.assertEqual(metrics["audit_count_mismatches"], 1)
            self.assertEqual(metrics["audit_trace_mismatches"], 1)
            event = _events(directory)[0]
            self.assertEqual(event["representative"], "seed_bf")
            self.assertEqual(event["result"]["counts"], [3])
            self.assertTrue(event["audited"])

    def test_forced_audit_runtime_failure_is_false_positive_evidence(self):
        collision = "def priority(item, bins):\n    return [r / (item - 3) for r in bins]"
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[collision]),
                                 _config(audit_rate=1.0), directory)
            self.assertEqual(summary["metrics"]["audits"], 1)
            self.assertEqual(summary["metrics"]["audit_evaluation_errors"], 1)
            event = _events(directory)[0]
            self.assertEqual(event["status"], "audit_evaluation_error")
            self.assertTrue(event["audited"])
            self.assertNotIn("result", event)

    def test_probe_tail_registration_error_keeps_real_search_evidence(self):
        states = ([{"item": 3, "bins": [3, 5, 4]} for _ in range(32)]
                  + [{"item": 2, "bins": [3, 5, 4]} for _ in range(96)])
        candidate = "def priority(item, bins):\n    return [r / (item - 2) for r in bins]"
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, states, OfflineProvider(fixtures=[candidate]),
                                 _config(), directory)
            self.assertEqual(summary["metrics"]["probe_registration_errors"], 1)
            self.assertEqual(summary["metrics"]["invalid_candidates"], 0)
            self.assertEqual(summary["metrics"]["successful_candidate_evaluations"], 1)
            event = _events(directory)[0]
            self.assertEqual(event["status"], "evaluated_probe_registration_error")
            self.assertEqual(event["result"]["counts"], [3])
            self.assertIn("probe_registration_error", event)

    def test_probe_error_falls_back_to_real_search_evaluation(self):
        candidate = "def priority(item, bins):\n    return [r / (item - 2) for r in bins]"
        states = [{"item": 2, "bins": [3, 5]} for _ in range(128)]
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, states, OfflineProvider(fixtures=[candidate]),
                                 _config(), directory)
            event = _events(directory)[0]
            self.assertEqual(summary["metrics"]["probe_errors"], 1)
            self.assertEqual(summary["metrics"]["successful_candidate_evaluations"], 1)
            self.assertEqual(event["result"]["counts"], [3])
            self.assertIn("probe_error", event)

    def test_unauthorized_response_is_saved_and_stops_study_run(self):
        class Unauthorized:
            def generate(self, messages):
                return {"text": "", "raw_response": {"http_status": 401},
                        "error": {"type": "http_error"}, "request_attempted": True,
                        "paid_api_request": True, "cost_known": False, "usage": {}}
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES, Unauthorized(), _config(arm="A", calls=5), directory)
            self.assertEqual(summary["termination"], "provider_auth_or_balance_error")
            self.assertEqual(summary["metrics"]["requests_attempted"], 1)
            saved = (directory / "provider_responses.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0])["response"]["raw_response"]["http_status"], 401)

    def test_failed_validation_candidate_uses_a_measured_seed_fallback(self):
        code = "def priority(item, bins):\n    return [r / (item - 3) for r in bins]\n"
        with workspace_temp() as directory:
            write_json(directory / "final_info.json", {
                "evidence_scope": "offline_fixture_plumbing_only",
                "top_search_candidates": [_record("fails_on_validation", [2], code)]})
            result = experiment.select_validation(directory, INSTANCES)
            self.assertEqual(result["selected"]["validation"]["counts"], [2])
            self.assertIn(result["selected"]["code"], (FF_CODE, BF_CODE))
            self.assertFalse(result["heldout_evaluated"])

    def test_main_loads_no_heldout_split(self):
        visited = []

        def restricted_load(path):
            path = Path(path)
            visited.append(path.name)
            if path.name not in {"development.json", "search.json", "validation.json"}:
                raise AssertionError("Unexpected held-out read: " + str(path))
            return INSTANCES
        with workspace_temp() as directory:
            with (patch.object(experiment, "load_instances", side_effect=restricted_load),
                  patch.object(experiment, "load_probes", return_value=PROBES),
                  patch("sys.argv", ["experiment.py", "--out_dir", str(directory),
                                     "--calls", "1", "--arms", "A", "--seeds", "11"]),
                  patch("sys.stdout", new=io.StringIO())):
                experiment.main()
        self.assertEqual(set(visited), {"development.json", "search.json", "validation.json"})


class DatabaseTests(unittest.TestCase):
    def test_clusters_are_score_vectors_not_mean_only(self):
        database = Database(seed=1, islands=2, cluster_cap=2)
        database.add(_record("one", [2, 4], "code1"))
        database.add(_record("two", [4, 2], "code2"))
        database.add(_record("same_text", [2, 4], "code1"))
        self.assertEqual(len(database.islands[0]), 2)
        self.assertEqual(len(database.islands[0][(2, 4)]), 1)
        _, parents = database.parents()
        self.assertEqual({tuple(parent["counts"]) for parent in parents}, {(2, 4), (4, 2)})

    def test_reset_retains_best_half_and_seeds_worst_half_from_survivors(self):
        database = Database(seed=1, islands=4)
        database.add(_record("seed", [5], "seed"))
        database.add(_record("good", [2], "good"), 0)
        database.add(_record("okay", [3], "okay"), 1)
        reset = database.reset()
        self.assertEqual(reset, [2, 3])
        for index in reset:
            members = [m for group in database.islands[index].values() for m in group]
            self.assertEqual(len(members), 1)
            self.assertIn(members[0]["id"], {"good", "okay"})
        self.assertEqual(min(g[0]["mean_bins"] for g in database.islands[0].values()), 2)


if __name__ == "__main__":
    unittest.main()
