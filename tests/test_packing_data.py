import hashlib
import json
from pathlib import Path
import random
from contextlib import contextmanager
import shutil
import uuid
import unittest

from funpack.data import (OR3_NOTEBOOK_SHA256, content_hash, generate_suite,
                          load_instances, load_probes)
from funpack.packing import (best_fit_scores, choose_index, evaluate,
                             first_fit_scores, pack)


@contextmanager
def local_temp_directory():
    """Use normal inherited ACLs: Python's Windows 0o700 temp ACL fails in this sandbox."""
    root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
    root.mkdir(exist_ok=True)
    directory = root / ("packing-data-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        # Confirm the exact recursive cleanup target remains in this test workspace.
        if directory.resolve().parent != root or directory.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(directory)


class PackingTests(unittest.TestCase):
    def test_hand_example_first_fit_and_best_fit(self):
        ff = pack([6, 7, 3, 4], 10, first_fit_scores, trace=True)
        bf = pack([6, 7, 3, 4], 10, best_fit_scores, trace=True)
        self.assertEqual(ff, {"bins_used": 3, "remaining": [1, 3, 6],
                              "assignments": [0, 1, 0, 2]})
        self.assertEqual(bf, {"bins_used": 2, "remaining": [0, 0],
                              "assignments": [0, 1, 1, 0]})

    def test_ties_and_existing_feasible_bins(self):
        seen = []
        result = pack([6, 6, 4], 10, lambda item, bins: seen.append(bins) or [1, 1], trace=True)
        self.assertEqual(result["assignments"], [0, 1, 0])
        self.assertEqual(seen, [[4, 4]])
        self.assertEqual(choose_index([-2, -1, -1], 3), 1)

    def test_capacity_conservation_and_prefix_online_behavior(self):
        rng = random.Random(879)
        items = [rng.randint(1, 100) for _ in range(300)]
        for scorer in (first_fit_scores, best_fit_scores):
            result = pack(items, 100, scorer, trace=True)
            self.assertEqual(sum(items) + sum(result["remaining"]), 100 * result["bins_used"])
            totals = [0] * result["bins_used"]
            for item, index in zip(items, result["assignments"]):
                totals[index] += item
            self.assertTrue(all(value <= 100 for value in totals))
            prefix = pack(items[:50], 100, scorer, trace=True)
            self.assertEqual(prefix["assignments"], result["assignments"][:50])

    def test_invalid_scorer_outputs(self):
        bad = [None, 3, "ab", {0: 0, 1: 0}, [0], [0, 0, 0],
               [[0], [1]], [True, 1], [float("nan"), 0], [0, float("inf")],
               [complex(1, 0), 0], [10 ** 1000, 0]]
        for output in bad:
            with self.subTest(output_type=type(output).__name__):
                with self.assertRaises(ValueError):
                    choose_index(output, 2)

    def test_input_validation_happens_before_scoring(self):
        calls = []
        scorer = lambda item, bins: calls.append(item) or [0] * len(bins)
        for items, capacity in (([6, 3, 11], 10), ([True], 10), ([1.5], 10),
                                ([0], 10), ([-1], 10), ([1], True), ([1], 0)):
            with self.assertRaises(ValueError):
                pack(items, capacity, scorer)
        self.assertEqual(calls, [])
        self.assertEqual(pack([], 10, scorer), {"bins_used": 0, "remaining": []})

    def test_scorer_and_callback_cannot_mutate_simulator_state(self):
        def destructive_scorer(item, bins):
            original_count = len(bins)
            bins[:] = [999]
            return [0] * original_count

        def destructive_callback(state):
            state["bins"][:] = [-100]
        actual = pack([6, 7, 3, 4], 10, destructive_scorer, True, destructive_callback)
        expected = pack([6, 7, 3, 4], 10, first_fit_scores, True)
        self.assertEqual(actual, expected)

    def test_evaluation_preserves_counts_and_auditable_trace(self):
        instances = [{"id": "example", "capacity": 10, "items": [6, 7, 3, 4]}]
        result = evaluate(best_fit_scores, instances)
        expected = hashlib.sha256(b"[0,1,1,0]").hexdigest()
        self.assertEqual(result, {"counts": [2], "mean_bins": 2.0, "trace_hashes": [expected]})
        with self.assertRaises(ValueError):
            evaluate(best_fit_scores, [])


class DataTests(unittest.TestCase):
    def test_frozen_splits_and_probes_reproduce(self):
        with local_temp_directory() as one, local_temp_directory() as two:
            manifest = generate_suite(one)
            second = generate_suite(two)
            self.assertEqual(manifest, second)
            self.assertEqual(manifest, generate_suite(one))
            observed_hashes, observed_seeds = set(), set()
            for split, count in (("development", 10), ("search", 30),
                                 ("validation", 10), ("test", 30)):
                info = manifest["splits"][split]
                path = Path(one) / info["path"]
                instances = load_instances(path, allow_heldout=split == "test")
                self.assertEqual(len(instances), count)
                self.assertEqual(path.read_bytes(), (Path(two) / info["path"]).read_bytes())
                for instance in instances:
                    self.assertEqual(len(instance["items"]), 500)
                    self.assertEqual(instance["capacity"], 100)
                    self.assertTrue(all(20 <= item <= 80 for item in instance["items"]))
                    self.assertNotIn(instance["content_sha256"], observed_hashes)
                    self.assertNotIn(instance["seed"], observed_seeds)
                    observed_hashes.add(instance["content_sha256"])
                    observed_seeds.add(instance["seed"])
            probes = load_probes(Path(one) / "probes.json")
            self.assertEqual(len(probes), 128)
            self.assertEqual(len({(p["item"], tuple(p["bins"])) for p in probes}), 128)
            self.assertTrue(all(len(p["bins"]) >= 2 and p["source"]["split"] == "development"
                                for p in probes))
            self.assertEqual(probes, load_probes(Path(two) / "probes.json"))
            for name, metadata in manifest["files"].items():
                self.assertEqual(hashlib.sha256((Path(one) / name).read_bytes()).hexdigest(),
                                 metadata["sha256"])

    def test_each_probe_is_from_recorded_legal_trajectory(self):
        with local_temp_directory() as directory:
            generate_suite(directory)
            development = load_instances(Path(directory) / "development.json")
            probes = load_probes(Path(directory) / "probes.json")
            recorded = {}
            from funpack.packing import worst_fit_scores
            for policy, scorer in (("first_fit", first_fit_scores), ("best_fit", best_fit_scores),
                                    ("worst_fit", worst_fit_scores)):
                for instance in development:
                    def save(state):
                        recorded[(instance["id"], policy, state["step"])] = (state["item"], state["bins"])
                    pack(instance["items"], 100, scorer, state_callback=save)
            for probe in probes:
                source = probe["source"]
                self.assertEqual(recorded[(source["instance"], source["policy"], source["step"])],
                                 (probe["item"], probe["bins"]))

    def test_sealed_and_corrupt_data_rejected_without_overwrite(self):
        with local_temp_directory() as directory:
            generate_suite(directory)
            with self.assertRaises(PermissionError):
                load_instances(Path(directory) / "heldout/test.json")
            path = Path(directory) / "search.json"
            corrupted = json.loads(path.read_text(encoding="utf-8"))
            corrupted["instances"][0]["items"][0] = 19
            raw = json.dumps(corrupted).encode()
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                load_instances(path)
            with self.assertRaises(FileExistsError):
                generate_suite(directory)
            self.assertEqual(path.read_bytes(), raw)

    def test_instance_hash_catches_collection_hash_only_rewrite(self):
        with local_temp_directory() as directory:
            generate_suite(directory)
            path = Path(directory) / "search.json"
            corrupted = json.loads(path.read_text(encoding="utf-8"))
            corrupted["instances"][0]["items"][0] = 19
            corrupted["instances_sha256"] = content_hash(corrupted["instances"])
            path.write_text(json.dumps(corrupted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "instance content hash"):
                load_instances(path)

    def test_or3_extraction_and_notebook_integrity(self):
        notebook = Path(__file__).resolve().parents[1] / "references/funsearch/bin_packing.ipynb"
        if not notebook.exists():
            self.skipTest("optional local source notebook unavailable")
        with local_temp_directory() as directory:
            manifest = generate_suite(directory, notebook)
            self.assertEqual(manifest["or3"]["status"], "prepared")
            self.assertEqual(manifest["or3"]["provenance"]["source_notebook_sha256"],
                             OR3_NOTEBOOK_SHA256)
            with self.assertRaises(PermissionError):
                load_instances(Path(directory) / "heldout/or3.json")
            # Loading checks content/structure only; no scorer sees these instances.
            instances = load_instances(Path(directory) / "heldout/or3.json", allow_heldout=True)
            self.assertEqual([instance["id"] for instance in instances],
                             [f"u500_{i:02d}" for i in range(20)])
            self.assertTrue(all(instance["capacity"] == 150 and len(instance["items"]) == 500
                                for instance in instances))


if __name__ == "__main__":
    unittest.main()

