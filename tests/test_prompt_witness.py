"""Witness correctness and non-filtering diagnostics; no API calls."""
import json
import unittest

from funpack.packing import evaluate
from funpack.programs import BF_CODE, FF_CODE, Program
from funpack.prompts import build_messages
from scripts.prompt_witness import (check_witness, evaluate_candidate, messages_for,
                                    parse_response, reference_index, summarize)

CHANGED = "def priority(item, bins):\n    return [-(r-item)+(0.5*item if r-item>=item else 0) for r in bins]\n"
SQUARED = "def priority(item, bins):\n    return [-(r-item)**2 for r in bins]\n"
WITNESS = {"item": 10, "bins": [16, 20], "parent_choice": 0, "candidate_choice": 1}
PARENTS = [{"code": FF_CODE, "mean_bins": 269.5, "counts": [269, 270]},
           {"code": BF_CODE, "mean_bins": 265.3, "counts": [265, 266], "test_scores": "SECRET_TEST_SCORE"}]


def text(code=CHANGED, witness=WITNESS):
    return "Strategy: Preserve a usable residual.\nWitness: " + json.dumps(witness) + "\n```python\n" + code + "```"


class WitnessTests(unittest.TestCase):
    def test_original_prompt_is_identical_and_new_targets_best_parent(self):
        self.assertEqual(messages_for("enhanced_v1", PARENTS), build_messages("enhanced", PARENTS))
        self.assertEqual(reference_index(PARENTS), 1)
        new = messages_for("witness_v2", PARENTS)
        self.assertEqual(new[0], build_messages("enhanced", PARENTS)[0])
        self.assertIn("parent index 1", new[1]["content"])
        self.assertNotIn("SECRET_TEST_SCORE", json.dumps(new))
        self.assertIn("zero-based", new[1]["content"])

    def test_parse_retains_code_and_separates_strategy_from_witness(self):
        parsed = parse_response(text(), "witness_v2")
        self.assertTrue(parsed["format_ok"])
        self.assertEqual(parsed["code"], CHANGED)
        self.assertEqual(parsed["strategy"], "Preserve a usable residual.")
        self.assertEqual(parsed["witness"], WITNESS)

    def test_true_decision_change_is_verified(self):
        check = check_witness(parse_response(text(), "witness_v2"), BF_CODE)
        self.assertEqual(check["status"], "verified_change")
        self.assertEqual((check["actual_parent_choice"], check["actual_candidate_choice"]), (0, 1))

    def test_monotonic_rewrite_cannot_claim_change(self):
        check = check_witness(parse_response(text(SQUARED), "witness_v2"), BF_CODE)
        self.assertEqual(check["status"], "incorrect_claim")
        self.assertFalse(check["verified_change"])

    def test_honest_same_choice_is_not_novel(self):
        w = {**WITNESS, "candidate_choice": 0}
        check = check_witness(parse_response(text(SQUARED, w), "witness_v2"), BF_CODE)
        self.assertEqual(check["status"], "same_choice")

    def test_tie_breaking_uses_earliest_bin(self):
        w = {"item": 10, "bins": [20, 20], "parent_choice": 0, "candidate_choice": 1}
        check = check_witness(parse_response(text(CHANGED, w), "witness_v2"), BF_CODE)
        self.assertEqual(check["actual_candidate_choice"], 0)
        self.assertFalse(check["verified_change"])

    def test_malformed_witness_does_not_prevent_code_extraction(self):
        for w in ([], {**WITNESS, "item": True}, {**WITNESS, "item": 10.0},
                  {**WITNESS, "bins": [9, 20]}, {**WITNESS, "bins": [20] * 9},
                  {**WITNESS, "candidate_choice": 2}, {**WITNESS, "parent_choice": False},
                  {**WITNESS, "extra": "unexpected"}):
            with self.subTest(w=w):
                parsed = parse_response(text(witness=w), "witness_v2")
                self.assertFalse(parsed["format_ok"])
                self.assertEqual(parsed["code"], CHANGED)

    def test_duplicate_json_keys_and_multiple_witness_lines_rejected(self):
        duplicate = text().replace('"item": 10', '"item": 10, "item": 11')
        self.assertIsNone(parse_response(duplicate, "witness_v2")["witness"])
        multiple = text().replace("```python", 'Witness: {}\n```python')
        self.assertIsNone(parse_response(multiple, "witness_v2")["witness"])

    def test_bad_witness_still_gets_full_search_evaluation(self):
        instances = [{"id": "test-owned", "capacity": 100, "items": [84, 80, 10]}]
        bf = evaluate(Program(BF_CODE), instances)
        parsed = parse_response(text(witness={"wrong": True}), "witness_v2")
        self.assertFalse(check_witness(parsed, BF_CODE)["verified_change"])
        result = evaluate_candidate(parsed, bf, bf, instances)
        self.assertTrue(result["valid_search_evaluation"])
        self.assertTrue(result["novel_vs_bf_on_search"])

    def test_missing_witness_still_gets_code_evaluated(self):
        parsed = parse_response("Strategy: Changed.\n```python\n" + CHANGED + "```", "witness_v2")
        self.assertEqual(parsed["code"], CHANGED)
        self.assertFalse(parsed["format_ok"])

    def test_execution_error_is_recorded_without_running_arbitrary_python(self):
        parsed = parse_response(text("def priority(item, bins):\n    import os\n    return [0 for r in bins]\n"), "witness_v2")
        self.assertEqual(check_witness(parsed, BF_CODE)["status"], "execution_error")

    def test_errors_stay_in_request_denominator(self):
        events = [{"context_index": i, "variant": variant, "response": {"cost_known": True},
                   "evaluation": {"valid_search_evaluation": False}}
                  for i in range(2) for variant in ("enhanced_v1", "witness_v2")]
        report = summarize(events, expected_pairs=2)
        self.assertTrue(report["complete"])
        self.assertEqual(report["rows"][1]["requests"], 2)
        self.assertEqual(report["rows"][1]["valid_search_evaluations"], 0)
        self.assertEqual(report["next_step"], "review_limitations_without_automatic_retries")


if __name__ == "__main__":
    unittest.main()
