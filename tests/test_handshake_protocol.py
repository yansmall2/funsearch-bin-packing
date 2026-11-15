"""Handshake-provider formal protocol safeguards; no paid calls, no sockets.

Mirrors tests/test_protocol.py fixtures, but with provider="handshake".
All data are tiny test-owned fixtures. Nothing here reads or evaluates the
project's sealed benchmarks.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import experiment
from funpack import protocol
from funpack.data import load_instances, load_probes
from funpack.storage import digest, read_json, write_json
from tests.test_protocol import make_data, workspace_temp


class HandshakeProvider:
    """Test double: never opens a socket, charges money, or uses a key."""

    def __init__(self, model, code=None):
        self.model = model
        self.code = code or "def priority(item, bins):\n    return [-r for r in bins]"

    def generate(self, messages):
        return {"text": "```python\n" + self.code + "\n```", "usage": {},
                "raw_response": {"test_double": True}, "model": self.model,
                "requested_model": self.model, "offline_fixture": False,
                "paid_api_request": False, "request_attempted": True,
                "estimated_cost": 0, "cost_known": True}


def make_handshake_fixture(root):
    data = root / "data"
    make_data(data)
    source = root / "source"
    for name in protocol.CORE_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Isolated integrity-test source placeholder: " + name + "\n", encoding="utf-8")
    plan = protocol.draft_plan()
    plan["provider"] = "handshake"
    plan["calls_per_run"] = 1
    plan_path = root / "plan.json"
    write_json(plan_path, plan)
    handshake = {"provider": "handshake", "model": "muse-spark-1.3",
                 "timeout_seconds": 30, "max_requests": 12}
    handshake_path = root / "handshake.json"
    write_json(handshake_path, handshake)
    return SimpleNamespace(root=root, data=data, source=source, plan=plan,
                           plan_path=plan_path, handshake=handshake,
                           handshake_path=handshake_path, frozen_path=root / "frozen.json")


def freeze(f):
    return protocol.freeze_protocol(f.plan_path, f.handshake_path, f.data, f.frozen_path,
                                    source_root=f.source)


def runtime(f, arms=None, seeds=None):
    p = f.plan
    return SimpleNamespace(provider="handshake", stage="all",
                           arms=list(p["arms"] if arms is None else arms),
                           seeds=list(p["seeds"] if seeds is None else seeds),
                           calls=p["calls_per_run"], islands=p["islands"],
                           reset_every=p["reset_every"], audit_rate=p["audit_rate"],
                           database_temperature=p["database_temperature"],
                           enhanced_prompt_version=p["enhanced_prompt_version"],
                           collision_policy=p["collision_policy"],
                           evaluation_timeout_seconds=p["evaluation_timeout_seconds"],
                           validation_timeout_seconds=p["validation_timeout_seconds"])


def make_handshake_study(f, frozen):
    """Fabricate a complete 12-run handshake study the seal step can inspect."""
    from funpack.search import run_search
    study_dir = f.root / "simulated-study"
    study_dir.mkdir()
    search = load_instances(f.data / "search.json")
    validation = load_instances(f.data / "validation.json")
    probes = load_probes(f.data / "probes.json")
    cores = protocol.core_source_hashes(f.source)
    module_sources = {name.removeprefix("funpack/"): value for name, value in cores.items()
                      if name.startswith("funpack/")}
    study = {"phase": "formal", "protocol_id": frozen["protocol_id"],
             "core_source_hashes": cores, "provider_config": frozen["provider_config"],
             "arguments": vars(runtime(f)), "search_hash": digest(search),
             "validation_hash": digest(validation), "probe_hash": digest(probes),
             "heldout_evaluated": False, "runs": [], "test_fixture_only": True}
    for seed in f.plan["seeds"]:
        for arm in f.plan["arms"]:
            name = f"{arm}_seed{seed}"
            config = {"arm": arm, "seed": seed, "calls": f.plan["calls_per_run"],
                      "islands": f.plan["islands"], "reset_every": f.plan["reset_every"],
                      "audit_rate": f.plan["audit_rate"],
                      "temperature": f.plan["database_temperature"],
                      "enhanced_prompt_version": f.plan["enhanced_prompt_version"],
                      "collision_policy": f.plan["collision_policy"],
                      "evaluation_timeout_seconds": f.plan["evaluation_timeout_seconds"],
                      "protocol_id": frozen["protocol_id"]}
            with patch("funpack.search.source_hashes", return_value=module_sources):
                info = run_search(search, probes, HandshakeProvider(f.handshake["model"]),
                                  config, study_dir / name)
            info["evidence_scope"] = "formal_comparison"
            write_json(study_dir / name / "final_info.json", info)
            run_manifest = read_json(study_dir / name / "run_manifest.json")
            run_manifest["evidence_scope"] = "formal_comparison"
            write_json(study_dir / name / "run_manifest.json", run_manifest)
            selected = experiment.select_validation(study_dir / name, validation,
                                                    f.plan["validation_timeout_seconds"])
            study["runs"].append({"run": name, "arm": arm, "seed": seed,
                                  "termination": info["termination"], "metrics": info["metrics"],
                                  "best_search_mean_bins": info["best_search_mean_bins"],
                                  "selected_validation_mean_bins": selected["selected"]["validation"]["mean_bins"]})
    write_json(study_dir / "study.json", study)
    return study_dir


class HandshakeFreezeTests(unittest.TestCase):
    def test_handshake_plan_freezes_and_loads(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            self.assertEqual(frozen["plan"]["provider"], "handshake")
            self.assertEqual(frozen["provider_config"], f.handshake)
            self.assertIsNone(frozen["api_config"])
            self.assertEqual(frozen["total_planned_requests"], 12)
            reloaded = protocol.load_protocol(f.frozen_path, f.data, source_root=f.source)
            self.assertEqual(reloaded["protocol_id"], frozen["protocol_id"])

    def test_handshake_freeze_refuses_small_request_cap(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            f.handshake["max_requests"] = 11
            write_json(f.handshake_path, f.handshake)
            with self.assertRaises(ValueError):
                freeze(f)

    def test_handshake_freeze_refuses_secrets_and_unknown_fields(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            bad = dict(f.handshake)
            bad["api_key"] = "x"
            write_json(f.handshake_path, bad)
            with self.assertRaises(ValueError):
                freeze(f)
            bad = dict(f.handshake)
            bad["api_key"] = "x"
            bad.pop("api_key")
            bad["temperature"] = 0.8
            write_json(f.handshake_path, bad)
            with self.assertRaises(ValueError):
                freeze(f)

    def test_validate_runtime_accepts_full_and_subset_runs(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            protocol.validate_runtime(frozen, runtime(f), f.handshake)
            protocol.validate_runtime(frozen, runtime(f, arms=["A"], seeds=[11]), f.handshake)
            protocol.validate_runtime(frozen, runtime(f, arms=["B", "D"], seeds=[22, 33]), f.handshake)

    def test_validate_runtime_rejects_wrong_provider_config(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            wrong = dict(f.handshake)
            wrong["model"] = "other-model"
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, runtime(f), wrong)
            wrong = dict(f.handshake)
            wrong["timeout_seconds"] = 5
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, runtime(f), wrong)

    def test_validate_runtime_rejects_api_provider_and_changed_calls(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            args = runtime(f)
            args.provider = "api"
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, args, f.handshake)
            args = runtime(f)
            args.calls = 2
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, args, f.handshake)
            args = runtime(f, arms=["A", "A"], seeds=[11])
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, args, f.handshake)
            args = runtime(f, arms=["E"], seeds=[11])
            with self.assertRaises(ValueError):
                protocol.validate_runtime(frozen, args, f.handshake)


class HandshakeSealTests(unittest.TestCase):
    def test_handshake_study_can_be_sealed(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            study_dir = make_handshake_study(f, frozen)
            seal_path = root / "seal.json"
            seal = protocol.seal_candidates(f.frozen_path, study_dir, f.data, seal_path,
                                            source_root=f.source)
            self.assertEqual(len(seal["winners"]), 12)
            self.assertEqual(seal["protocol_id"], frozen["protocol_id"])

    def test_handshake_seal_rejects_paid_response(self):
        with workspace_temp() as root:
            f = make_handshake_fixture(root)
            frozen = freeze(f)
            study_dir = make_handshake_study(f, frozen)
            responses_path = study_dir / "A_seed11" / "provider_responses.jsonl"
            lines = responses_path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["response"]["paid_api_request"] = True
            lines[0] = json.dumps(record, ensure_ascii=False)
            responses_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                protocol.seal_candidates(f.frozen_path, study_dir, f.data, root / "seal.json",
                                         source_root=f.source)


if __name__ == "__main__":
    unittest.main()
