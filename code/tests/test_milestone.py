"""Regressions for the Best Fit safeguard and the exact non-thinking request."""
import json
from pathlib import Path
import unittest

from funpack.programs import BF_CODE
from funpack.providers import ApiProvider, OfflineProvider
from funpack.screening import BF_PROTECTED
from funpack.search import run_search
from test_search import INSTANCES, PROBES, _config, _events, workspace_temp


class MilestoneTests(unittest.TestCase):
    def test_distinct_bf_match_is_measured_and_exact_repeat_still_hits_cache(self):
        change = "def priority(item, bins):\n    return [r if item == 3 else -r for r in bins]"
        cfg = {**_config("C", 4, 0.0), "collision_policy": BF_PROTECTED}
        with workspace_temp() as directory:
            info = run_search(INSTANCES, PROBES, OfflineProvider(fixtures=[
                change, change, BF_CODE.rstrip(), "def priority(item, bins):\n    return [1 for r in bins]"
            ]), cfg, directory)
            events = _events(directory)
        self.assertTrue(events[0]["bf_protected"])
        self.assertEqual(events[0]["result"]["counts"], [3])
        self.assertEqual([e["status"] for e in events[1:]], ["text_cache_hit", "text_cache_hit", "behavior_skip"])
        self.assertEqual(info["metrics"]["bf_protected_full_evaluations"], 1)
        self.assertEqual(info["metrics"]["audits"], 0)
        self.assertEqual(info["metrics"]["disabled_fingerprints"], 0)

    def test_payload_actually_transmitted_has_explicitly_disabled_thinking(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "configs/deepseek.json").read_text())
        # _body is exactly what generate hands to urllib. No provider initialization or network.
        client = object.__new__(ApiProvider)
        client.config = config
        body = json.loads(client._body([{"role": "user", "content": "generate one function"}]))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(config["max_requests"], 600)
