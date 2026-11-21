"""Regressions for the Best Fit safeguard and the exact non-thinking request."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from funpack.programs import BF_CODE
from funpack.providers import ApiProvider, OfflineProvider
from funpack.screening import BF_PROTECTED
from funpack.search import run_search
from scripts import run_study
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
        for budget in (60, 200):
            with self.subTest(budget=budget):
                _, protocol, config = run_study.checked_preset(budget)
                # _body is exactly what generate hands to urllib.
                client = object.__new__(ApiProvider)
                client.config = config
                body = json.loads(client._body([{"role": "user", "content": "generate one function"}]))
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", body)
                self.assertEqual(body["model"], "deepseek-flash")
                self.assertEqual(protocol["total_planned_requests"], budget * 12)

    def test_all_deepseek_configuration_files_use_the_project_key_name(self):
        configs = sorted((run_study.CODE / "configs").glob("deepseek*.json"))
        self.assertEqual({p.name for p in configs}, {"deepseek.json", "deepseek-60.json", "deepseek-200.json"})
        for path in configs:
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["api_key_env"],
                             "DEEPSEEK_API_KEY")

    def test_both_presets_use_the_same_project_key_and_separate_ledgers(self):
        with workspace_temp() as directory, patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-project-value",
            "ANTHROPIC_AUTH_TOKEN": "unrelated-value",
            "FUNPACK_API_KEY": "unrelated-value",
        }), patch.object(run_study.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launch:
            for budget in (60, 200):
                with self.subTest(budget=budget):
                    protocol, _, config = run_study.checked_preset(budget)
                    dest = (Path(directory) / f"run_{budget}").resolve()
                    self.assertEqual(run_study.run_replicate(protocol, config, dest), 0)
                    cmd = launch.call_args.args[0]
                    child = launch.call_args.kwargs
                    self.assertEqual(child["env"][config["api_key_env"]], "test-project-value")
                    self.assertEqual(child["cwd"], dest)
                    self.assertEqual(Path(cmd[cmd.index("--out_dir") + 1]), dest / "study")
                    saved = Path(cmd[cmd.index("--api-config") + 1]).read_text(encoding="utf-8")
                    self.assertEqual(json.loads(saved), config)
                    self.assertNotIn("test-project-value", saved)
                    with self.assertRaises(FileExistsError):
                        run_study.run_replicate(protocol, config, dest)
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(os.environ["FUNPACK_API_KEY"], "unrelated-value")
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "unrelated-value")

    def test_preflight_and_missing_paid_flag_do_not_launch_formal_runner(self):
        with patch.object(run_study.subprocess, "run") as launch, workspace_temp() as directory:
            dest = Path(directory) / "run_60"
            self.assertEqual(run_study.main(["check"]), 0)
            with self.assertRaises(SystemExit):
                run_study.main(["run", "--budget", "60", "--out-dir", str(dest)])
            self.assertFalse(dest.exists())
            launch.assert_not_called()

    def test_stale_historical_keys_cannot_start_a_run(self):
        with workspace_temp() as directory, patch.dict(os.environ, {
            "FUNPACK_API_KEY": "unrelated-value",
            "ANTHROPIC_AUTH_TOKEN": "unrelated-value",
        }, clear=True), patch.object(
            run_study.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)
        ) as launch:
            for budget in (60, 200):
                with self.subTest(budget=budget):
                    protocol, _, config = run_study.checked_preset(budget)
                    dest = Path(directory) / f"run_{budget}"
                    self.assertEqual(run_study.run_replicate(protocol, config, dest), 2)
                    self.assertNotIn(config["api_key_env"], launch.call_args.kwargs["env"])

    def test_early_provider_stop_does_not_report_a_complete_study(self):
        with workspace_temp() as directory:
            output = Path(directory) / "run_60"
            manifest = output / "study" / "study.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"stop_reason": "provider_auth_or_balance_error", "runs": []}))
            with patch.object(run_study, "run_replicate", return_value=0):
                status = run_study.main(["run", "--budget", "60", "--out-dir", str(output), "--allow-paid"])
            self.assertEqual(status, 1)
