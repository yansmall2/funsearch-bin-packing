"""Warm-start seeds + mechanism_v3c combination prompt. No network, no heldout."""
import copy
import json
from pathlib import Path
import unittest

from funpack.data import load_instances
from funpack.packing import evaluate
from funpack.programs import BF_CODE, FF_CODE, WARMSTART_SEEDS, Program
from funpack.prompts import COMBINATION_PARAGRAPH, build_messages
from funpack.providers import OfflineProvider
from funpack.search import run_search
from funpack.storage import digest, read_json
from test_search import INSTANCES, PROBES, _config, workspace_temp

ROOT = Path(__file__).resolve().parents[1]

# Search-set means (3 decimals) of the sealed selected winners, from
# run_formal/<run>/selection.json. The seed codes are read verbatim from the
# sealed candidates file, so these numbers guard against transcription errors.
EXPECTED_SEARCH_MEANS = {
    "A_seed11": 265.167, "B_seed11": 265.033, "C_seed11": 265.067,
    "D_seed11": 265.067, "A_seed22": 265.167, "D_seed22": 265.133,
    "A_seed33": 265.200, "C_seed33": 265.067, "D_seed33": 265.200,
}

PARENTS = [{"code": FF_CODE, "counts": [3], "mean_bins": 3},
           {"code": BF_CODE, "counts": [2], "mean_bins": 2}]


class WarmstartSeedTests(unittest.TestCase):
    def test_nine_distinct_valid_seeds_none_equal_to_ff_or_bf(self):
        self.assertEqual(len(WARMSTART_SEEDS), 9)
        codes = [code for _, code in WARMSTART_SEEDS]
        self.assertEqual(len(set(codes)), 9)
        for name, code in WARMSTART_SEEDS:
            Program(code)  # raises unless valid DSL
            self.assertNotIn(code, (FF_CODE, BF_CODE))
        self.assertEqual([name for name, _ in WARMSTART_SEEDS],
                         ["A_seed11", "B_seed11", "C_seed11", "D_seed11",
                          "A_seed22", "D_seed22", "A_seed33", "C_seed33", "D_seed33"])

    def test_seed_search_means_match_sealed_winners(self):
        instances = load_instances(ROOT / "data" / "search.json")
        for name, code in WARMSTART_SEEDS:
            mean = evaluate(Program(code), instances)["mean_bins"]
            self.assertEqual(round(mean, 3), EXPECTED_SEARCH_MEANS[name])
            self.assertLessEqual(mean, 265.3)  # every warm-start seed beats or ties BF


class CombinationPromptTests(unittest.TestCase):
    def test_v3c_is_v3_plus_combination_paragraph_only(self):
        parents = copy.deepcopy(PARENTS)
        v3 = build_messages("enhanced", parents, enhanced_version="mechanism_v3")
        v3c = build_messages("enhanced", parents, enhanced_version="mechanism_v3c")
        self.assertEqual(v3[0], v3c[0])
        self.assertEqual(v3c[1]["content"],
                         v3[1]["content"] + "\n\n" + COMBINATION_PARAGRAPH)
        self.assertIn("Combine the parents", v3c[1]["content"])
        self.assertIn("one unified rule", v3c[1]["content"])

    def test_v3c_keeps_basic_control_and_rejects_unknown_versions(self):
        self.assertEqual(build_messages("basic", PARENTS, enhanced_version="mechanism_v3c"),
                         build_messages("basic", PARENTS))
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v3x")

    def test_v3c_run_uses_eleven_seeds_and_records_v3c_method(self):
        config = {**_config(arm="B", calls=1), "enhanced_prompt_version": "mechanism_v3c"}
        with workspace_temp() as directory:
            summary = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[BF_CODE.rstrip()]),
                                 config, directory / "run")
            manifest = read_json(directory / "run" / "run_manifest.json")
            seed_ids = [json.loads(line)["id"] for line in
                        (directory / "run" / "evaluated.jsonl").read_text(encoding="utf-8").splitlines()[:11]]
        self.assertEqual(summary["metrics"]["seed_full_evaluations"], 11)
        self.assertEqual(summary["method"]["prompt_version"], "mechanism_v3c")
        self.assertEqual(summary["method"]["enhanced_prompt_version"], "mechanism_v3c")
        self.assertEqual(summary["method"]["search_context"], "off")
        self.assertEqual(manifest["method"], summary["method"])
        self.assertEqual(seed_ids[:2], ["seed_ff", "seed_bf"])
        self.assertTrue(all(s.startswith("seed_warmstart_") for s in seed_ids[2:]))
