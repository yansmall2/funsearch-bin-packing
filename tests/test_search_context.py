"""Search memory: family classification, word budget, v4 prompt wiring.

All offline; no network or LLM evidence. The v3 regression property is
structural: a v4 message must equal the v3 message with exactly the memory
block inserted, so v3 behavior cannot drift.
"""
import copy
import json
import unittest

from funpack.programs import BF_CODE, FF_CODE
from funpack.prompts import build_messages
from funpack.providers import OfflineProvider
from funpack.search import run_search
from funpack.search_context import DEFAULT_MAX_WORDS, SearchContext, classify
from funpack.storage import read_json
from test_search import INSTANCES, PROBES, _config, _events, workspace_temp

PARENTS = [{"code": FF_CODE, "counts": [3], "mean_bins": 3},
           {"code": BF_CODE, "counts": [2], "mean_bins": 2}]

B_WINNER = ("def priority(item, bins):\n    return [-(r - item) + (15 if r - item >= 28 "
            "and r - item <= 48 else 0) - (15 if r - item >= 8 and r - item <= 22 else 0) "
            "for r in bins]\n")
B_CONSTANTS = B_WINNER.replace("15 if", "20 if").replace("15 if r", "20 if r")
B_CHAINED = ("def priority(item, bins):\n    return [-(r - item) + (15 if 28 <= r - item <= 48 "
             "else 0) - (15 if 8 <= r - item <= 22 else 0) for r in bins]\n")
C_WINNER = ("def priority(item, bins):\n    return [-(r - item) + (20 if 30 <= r - item <= 45 "
            "and item % 3 == 1 else 0) - (20 if 8 <= r - item <= 22 else 0) for r in bins]\n")
D_TRIANGULAR = ("def priority(item, bins):\n    return [-(r - item) + (30 - abs((r - item) - 35) "
                "if 25 <= r - item <= 45 else 0) for r in bins]\n")
A_WORST_FIT = ("def priority(item, bins):\n    return [200 - 2 * (r - item) if r - item <= 10 "
              "else (r - item) for r in bins]\n")
RATIO_GATED = ("def priority(item, bins):\n    return [1000.0 if r - item == 0 else "
               "(-(r - item) - item / 2 if r - item > item / 3 and r - item <= item "
               "else -(r - item)) for r in bins]\n")


class ClassifyTests(unittest.TestCase):
    def test_constants_and_spelling_do_not_create_a_family(self):
        self.assertEqual(classify(B_WINNER), classify(B_CONSTANTS))
        self.assertEqual(classify(B_WINNER), classify(B_CHAINED))
        self.assertEqual(classify(B_WINNER), "windows[8,22];[28,48]")

    def test_distinct_structures_are_distinct_families(self):
        families = {classify(code) for code in (B_WINNER, C_WINNER, D_TRIANGULAR, A_WORST_FIT)}
        self.assertEqual(len(families), 4)
        self.assertEqual(classify(C_WINNER), "windows[8,22];[30,45]+mod")
        self.assertEqual(classify(D_TRIANGULAR), "windows[25,45]+triangular")
        self.assertEqual(classify(A_WORST_FIT), "windows[,10]+worst_fit")

    def test_seeds_and_unparseable_code_are_plain(self):
        self.assertEqual(classify(BF_CODE), "plain")
        self.assertEqual(classify(FF_CODE), "plain")
        self.assertEqual(classify("def priority(item, bins):\n    return [("), "plain")

    def test_ratio_gate_is_its_own_structure(self):
        self.assertEqual(classify(RATIO_GATED), "exact_fit+ratio+scaled")


class SearchContextTests(unittest.TestCase):
    def test_observe_counts_tries_and_tracks_best(self):
        context = SearchContext()
        context.observe(B_WINNER, 265.2)
        context.observe(B_CONSTANTS, 265.033)
        context.observe(C_WINNER, 265.067)
        row = context.families["windows[8,22];[28,48]"]
        self.assertEqual((row["tries"], row["best"]), (2, 265.033))
        self.assertEqual(context.total, 3)

    def test_render_lists_saturated_best_and_untried(self):
        context = SearchContext()
        for _ in range(3):
            context.observe(B_WINNER, 265.033)
        context.observe(C_WINNER, 265.067)
        text = context.render()
        self.assertIn("Avoid re-trying windows[8,22];[28,48]: tried 3x, best 265.033.", text)
        self.assertIn("Best so far: windows[8,22];[28,48] (265.033).", text)
        self.assertIn("Untried here:", text)
        self.assertLessEqual(len(text.split()), DEFAULT_MAX_WORDS)

    def test_render_enforces_hard_word_budget(self):
        context = SearchContext(max_words=20)
        for i in range(12):
            code = B_WINNER.replace("28", str(28 + i)).replace("48", str(48 + i))
            context.observe(code, 265.0 + i)
        text = context.render()
        self.assertLessEqual(len(text.split()), 20)
        # Header and best-so-far are never dropped.
        self.assertIn("Search memory", text)
        self.assertIn("Best so far:", text)

    def test_round_trip_and_max_words_validation(self):
        context = SearchContext()
        context.observe(B_WINNER, 265.033)
        self.assertEqual(SearchContext.from_dict(context.to_dict()).to_dict(), context.to_dict())
        with self.assertRaises(ValueError):
            SearchContext(max_words=19)


class SearchContextV5Tests(unittest.TestCase):
    def test_v5_refines_best_and_avoids_only_saturated_and_worse(self):
        context = SearchContext(memory_version="v2")
        for _ in range(3):
            context.observe(B_WINNER, 265.033)  # saturated AND best: refine, never avoid
        for _ in range(3):
            context.observe(D_TRIANGULAR, 280.0)  # saturated AND worse: avoid
        context.observe(C_WINNER, 265.067)  # unsaturated: neither
        text = context.render()
        self.assertIn("Refine the best family: windows[8,22];[28,48] (265.033)", text)
        self.assertNotIn("Avoid re-trying windows[8,22];[28,48]", text)
        self.assertIn("Avoid re-trying windows[25,45]+triangular: tried 3x, best 280.000.", text)
        self.assertNotIn("Avoid re-trying windows[8,22];[30,45]+mod", text)
        self.assertIn("Untried here:", text)
        self.assertLessEqual(len(text.split()), DEFAULT_MAX_WORDS)

    def test_v5_tied_saturated_family_is_not_avoided(self):
        context = SearchContext(memory_version="v2")
        for _ in range(3):
            context.observe(B_WINNER, 265.033)
        for _ in range(3):
            context.observe(C_WINNER, 265.033)  # saturated, tied best: not "worse"
        text = context.render()
        self.assertNotIn("Avoid re-trying", text)
        self.assertIn("Refine the best family:", text)

    def test_v5_unsaturated_mediocre_family_is_not_avoided(self):
        context = SearchContext(memory_version="v2")
        context.observe(B_WINNER, 265.033)
        context.observe(D_TRIANGULAR, 290.0)
        context.observe(D_TRIANGULAR, 290.0)  # 2 tries: not saturated
        text = context.render()
        self.assertNotIn("Avoid re-trying", text)

    def test_v5_render_enforces_hard_word_budget(self):
        context = SearchContext(max_words=30, memory_version="v2")
        for i in range(12):
            code = B_WINNER.replace("28", str(28 + i)).replace("48", str(48 + i))
            context.observe(code, 265.0 + i)
        text = context.render()
        self.assertLessEqual(len(text.split()), 30)
        # Header and refine line are never dropped.
        self.assertIn("Search memory", text)
        self.assertIn("Refine the best family:", text)

    def test_v5_round_trip_and_version_validation(self):
        context = SearchContext(memory_version="v2")
        context.observe(B_WINNER, 265.033)
        self.assertEqual(SearchContext.from_dict(context.to_dict()).to_dict(), context.to_dict())
        self.assertEqual(SearchContext().memory_version, "v1")
        with self.assertRaises(ValueError):
            SearchContext(memory_version="v3")
        # Dicts stored by v1 runs (no memory_version key) load as v1.
        old = {"max_words": 120, "total": 1,
               "families": {"plain": {"tries": 1, "best": 265.3}}}
        self.assertEqual(SearchContext.from_dict(old).memory_version, "v1")

    def test_render_compact_is_one_short_v2_line(self):
        context = SearchContext(memory_version="v2")
        for _ in range(3):
            context.observe(B_WINNER, 265.033)
        for _ in range(3):
            context.observe(D_TRIANGULAR, 280.0)
        text = context.render_compact()
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text.split()), 40)
        self.assertIn("refine windows[8,22];[28,48] (265.033)", text)
        self.assertIn("avoid windows[25,45]+triangular (3x, 280.000)", text)
        self.assertNotIn("Avoid re-trying windows[8,22];[28,48]", text)

    def test_render_compact_enforces_budget_and_handles_empty(self):
        self.assertIn("no observations yet",
                      SearchContext(memory_version="v2").render_compact())
        context = SearchContext(memory_version="v2")
        for i in range(12):
            code = B_WINNER.replace("28", str(28 + i)).replace("48", str(48 + i))
            for _ in range(3):
                context.observe(code, 265.0 + i)
        text = context.render_compact(max_words=20)
        self.assertLessEqual(len(text.split()), 20)
        self.assertIn("refine windows[8,22];[28,48] (265.000)", text)
        with self.assertRaises(ValueError):
            context.render_compact(max_words=19)


class PromptV4Tests(unittest.TestCase):
    def test_v4_is_v3_plus_memory_block_only(self):
        memory = "Search memory (this run only):\nBest so far: plain (265.300)."
        v3 = build_messages("enhanced", copy.deepcopy(PARENTS), enhanced_version="mechanism_v3")
        v4 = build_messages("enhanced", copy.deepcopy(PARENTS), enhanced_version="mechanism_v4",
                            search_context=memory)
        self.assertEqual(v4[0], v3[0])
        self.assertEqual(v4[1]["content"].replace(memory + "\n\n", "", 1), v3[1]["content"])
        self.assertNotIn("Search memory", v3[1]["content"])
        self.assertIn(memory, v4[1]["content"])

    def test_v4_requires_context_and_other_versions_reject_it(self):
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v4")
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v3",
                           search_context="Search memory")
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v4",
                           search_context=None)


class PromptV5Tests(unittest.TestCase):
    def test_v5_is_v3_plus_memory_block_only(self):
        memory = ("Search memory (this run only):\n"
                  "Refine the best family: plain (265.300).")
        v3 = build_messages("enhanced", copy.deepcopy(PARENTS), enhanced_version="mechanism_v3")
        v5 = build_messages("enhanced", copy.deepcopy(PARENTS), enhanced_version="mechanism_v5",
                            search_context=memory)
        self.assertEqual(v5[0], v3[0])
        self.assertEqual(v5[1]["content"].replace(memory + "\n\n", "", 1), v3[1]["content"])
        self.assertNotIn("Search memory", v3[1]["content"])
        self.assertIn(memory, v5[1]["content"])
        self.assertIn("Refine the best family", v5[1]["content"])

    def test_v5_requires_context_and_other_versions_reject_it(self):
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v5")
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v5",
                           search_context=None)
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v3",
                           search_context="Search memory")
        # Basic + v4/v5 with a context is a loud error now, not the v4
        # silent drop (basic + v4 looked like a memory run but was v1).
        with self.assertRaises(ValueError):
            build_messages("basic", PARENTS, enhanced_version="mechanism_v4",
                           search_context="Search memory")
        with self.assertRaises(ValueError):
            build_messages("basic", PARENTS, enhanced_version="mechanism_v5",
                           search_context="Search memory")

    def test_v5b_basic_compact_line(self):
        from funpack.prompts import prompt_version
        compact = ("Search memory (this run only; search-set means, lower is better): "
                   "refine plain (265.300).")
        basic = build_messages("basic", copy.deepcopy(PARENTS))
        v5b = build_messages("basic", copy.deepcopy(PARENTS), enhanced_version="mechanism_v5b",
                             search_context=compact)
        self.assertEqual(v5b[0], basic[0])
        self.assertEqual(v5b[1]["content"].replace(compact + "\n\n", "", 1), basic[1]["content"])
        self.assertIn(compact, v5b[1]["content"])
        self.assertLess(v5b[1]["content"].index(compact),
                        v5b[1]["content"].index("Return only the Python function"))
        self.assertEqual(prompt_version("basic", "mechanism_v5b"), "basic_v2")
        self.assertEqual(prompt_version("basic", "mechanism_v5"), "basic_v1")
        with self.assertRaises(ValueError):
            build_messages("basic", PARENTS, enhanced_version="mechanism_v5b")
        with self.assertRaises(ValueError):
            build_messages("enhanced", PARENTS, enhanced_version="mechanism_v5b",
                           search_context=compact)
        with self.assertRaises(ValueError):
            prompt_version("enhanced", "mechanism_v5b")


class IntegrationTests(unittest.TestCase):
    def test_v4_run_records_context_and_v3_run_records_none(self):
        fixtures = [B_WINNER, C_WINNER]
        for version, expect_file in (("mechanism_v4", True), ("mechanism_v3", False)):
            with self.subTest(version=version), workspace_temp() as directory:
                config = {**_config(arm="B", calls=2), "enhanced_prompt_version": version}
                provider = OfflineProvider(seed=11, fixtures=fixtures)
                summary = run_search(INSTANCES, PROBES, provider, config, directory / "run")
                self.assertEqual(summary["termination"], "request_budget_completed")
                self.assertEqual((directory / "run" / "search_context.json").exists(), expect_file)
                method = read_json(directory / "run" / "run_manifest.json")["method"]
                words = summary["metrics"]["search_context_max_words"]
                if expect_file:
                    self.assertEqual(method["search_context"], "mechanism_v4_memory_v1")
                    self.assertGreater(words, 0)
                    self.assertLessEqual(words, DEFAULT_MAX_WORDS)
                    stored = read_json(directory / "run" / "search_context.json")
                    self.assertEqual(stored["total"], 4)  # 2 seeds + 2 candidates
                    self.assertIn("windows[8,22];[28,48]", stored["families"])
                    self.assertTrue(all(e["search_context_words"] > 0 for e in _events(directory / "run")))
                else:
                    self.assertEqual(method["search_context"], "off")
                    self.assertEqual(words, 0)
                    self.assertIsNone(summary["search_context"])


class IntegrationV5Tests(unittest.TestCase):
    def test_v5_run_records_v5_method(self):
        fixtures = [B_WINNER, C_WINNER]
        with workspace_temp() as directory:
            config = {**_config(arm="B", calls=2), "enhanced_prompt_version": "mechanism_v5"}
            provider = OfflineProvider(seed=11, fixtures=fixtures)
            summary = run_search(INSTANCES, PROBES, provider, config, directory / "run")
            self.assertEqual(summary["termination"], "request_budget_completed")
            self.assertTrue((directory / "run" / "search_context.json").exists())
            method = read_json(directory / "run" / "run_manifest.json")["method"]
            self.assertEqual(method, summary["method"])
            self.assertEqual(method["prompt_version"], "mechanism_v5")
            self.assertEqual(method["search_context"], "mechanism_v5_memory_v1")
            words = summary["metrics"]["search_context_max_words"]
            self.assertGreater(words, 0)
            self.assertLessEqual(words, DEFAULT_MAX_WORDS)
            stored = read_json(directory / "run" / "search_context.json")
            self.assertEqual(stored["memory_version"], "v2")
            self.assertEqual(stored["total"], 4)  # 2 seeds + 2 candidates
            self.assertIn("windows[8,22];[28,48]", stored["families"])
            self.assertTrue(all(e["search_context_words"] > 0 for e in _events(directory / "run")))

    def test_v5b_run_records_compact_method(self):
        fixtures = [B_WINNER, C_WINNER]
        with workspace_temp() as directory:
            config = {**_config(arm="A", calls=2), "enhanced_prompt_version": "mechanism_v5b"}
            provider = OfflineProvider(seed=11, fixtures=fixtures)
            summary = run_search(INSTANCES, PROBES, provider, config, directory / "run")
            self.assertEqual(summary["termination"], "request_budget_completed")
            self.assertTrue((directory / "run" / "search_context.json").exists())
            method = read_json(directory / "run" / "run_manifest.json")["method"]
            self.assertEqual(method, summary["method"])
            self.assertEqual(method["prompt_version"], "basic_v2")
            self.assertEqual(method["search_context"], "mechanism_v5b_memory_v1")
            words = summary["metrics"]["search_context_max_words"]
            self.assertGreater(words, 0)
            self.assertLessEqual(words, 40)
            stored = read_json(directory / "run" / "search_context.json")
            self.assertEqual(stored["memory_version"], "v2")
            self.assertTrue(all(e["search_context_words"] > 0 for e in _events(directory / "run")))

    def test_basic_v5_records_off(self):
        # The fixed v4 boundary: basic + v5 injects no memory at all.
        fixtures = [B_WINNER, C_WINNER]
        with workspace_temp() as directory:
            config = {**_config(arm="A", calls=2), "enhanced_prompt_version": "mechanism_v5"}
            provider = OfflineProvider(seed=11, fixtures=fixtures)
            summary = run_search(INSTANCES, PROBES, provider, config, directory / "run")
            self.assertEqual(summary["termination"], "request_budget_completed")
            self.assertFalse((directory / "run" / "search_context.json").exists())
            method = read_json(directory / "run" / "run_manifest.json")["method"]
            self.assertEqual(method["prompt_version"], "basic_v1")
            self.assertEqual(method["search_context"], "off")
            self.assertEqual(summary["metrics"]["search_context_max_words"], 0)
            self.assertIsNone(summary["search_context"])


if __name__ == "__main__":
    unittest.main()
