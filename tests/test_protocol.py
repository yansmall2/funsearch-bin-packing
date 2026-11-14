"""Protocol safeguards on isolated, explicitly simulated evidence; no API calls.

All data, including objects labelled test/OR3, are tiny test-owned fixtures.
Nothing in this module reads or evaluates the project's sealed benchmarks.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import experiment
from funpack import protocol
from funpack.data import content_hash, load_instances, load_probes
from funpack.programs import FF_CODE
from funpack.search import run_search
from funpack.storage import digest, read_json, write_json


@contextmanager
def workspace_temp():
    root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
    root.mkdir(exist_ok=True)
    directory = root / ("protocol-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent != root or directory.is_symlink():
            raise RuntimeError("refusing cleanup outside the test workspace")
        shutil.rmtree(directory)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_data(root):
    """Create complete miniature wrappers without opening production data."""
    root.mkdir()
    wrappers = {}
    examples = {"development": [6, 7, 3], "search": [6, 7, 2],
                "validation": [6, 7, 1], "test": [6, 7, 3, 4],
                "or3": [6, 6, 4, 4]}
    for split, items in examples.items():
        instance = {"id": "test-owned-" + split, "capacity": 10, "items": items}
        instance["content_sha256"] = content_hash({"capacity": 10, "items": items})
        relative = f"heldout/{split}.json" if split in ("test", "or3") else f"{split}.json"
        wrappers[relative] = {"schema_version": 1, "kind": "instances", "split": split,
                              "heldout": split in ("test", "or3"), "instances": [instance],
                              "instances_sha256": content_hash([instance])}
    states = [{"item": 1, "bins": [a, b],
               "source": {"split": "development", "instance": "test-owned-development"}}
              for a in range(2, 14) for b in range(2, 14)][:128]
    wrappers["probes.json"] = {"schema_version": 1, "kind": "probes", "states": states,
                                "states_sha256": content_hash(states), "stage_sizes": [32, 96]}
    for relative, value in wrappers.items():
        write_json(root / relative, value)
    manifest = {"schema_version": 1,
                "files": {name: {"sha256": file_sha(root / name),
                                  "bytes": (root / name).stat().st_size}
                          for name in wrappers}}
    write_json(root / "manifest.json", manifest)


def make_fixture(root):
    data = root / "data"
    make_data(data)
    source = root / "source"
    for name in protocol.CORE_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Isolated integrity-test source placeholder: " + name + "\n", encoding="utf-8")
    plan = protocol.draft_plan()
    plan["calls_per_run"] = 1
    plan_path = root / "plan.json"
    write_json(plan_path, plan)
    api = {"enabled": True, "base_url": "https://test.invalid", "model": "test-only-model",
           "expected_model_version": None, "api_key_env": "FUNPACK_TEST_KEY_NOT_NEEDED",
           "ledger_path": str(root / "unused-test-ledger.sqlite3"),
           "temperature": 0.8, "max_tokens": 64, "timeout_seconds": 1,
           "max_requests": 12, "extra_body": {},
           "budget": {"currency": "CNY", "max_cost": 1, "input_per_million": 1,
                      "output_per_million": 1, "pricing_source": "test-only simulated values"}}
    api_path = root / "api.json"
    write_json(api_path, api)
    return SimpleNamespace(root=root, data=data, source=source, plan=plan, plan_path=plan_path,
                           api=api, api_path=api_path, frozen_path=root / "frozen.json")


def freeze(f):
    return protocol.freeze_protocol(f.plan_path, f.api_path, f.data, f.frozen_path,
                                    source_root=f.source)


def runtime(f):
    p = f.plan
    return SimpleNamespace(provider="api", stage="all", arms=p["arms"], seeds=p["seeds"],
                           calls=p["calls_per_run"], islands=p["islands"],
                           reset_every=p["reset_every"], audit_rate=p["audit_rate"],
                           database_temperature=p["database_temperature"],
                           enhanced_prompt_version=p["enhanced_prompt_version"], collision_policy=p["collision_policy"],
                           evaluation_timeout_seconds=p["evaluation_timeout_seconds"],
                           validation_timeout_seconds=p["validation_timeout_seconds"])


class ApiProvider:
    """Test double: deliberately never opens a socket, charges money, or uses a key."""

    def __init__(self, model, code=None):
        self.model = model
        self.code = code or "def priority(item, bins):\n    return [-r for r in bins]"

    def generate(self, messages):
        return {"text": "```python\n" + self.code + "\n```", "usage": {},
                "raw_response": {"test_double": True}, "model": self.model,
                "requested_model": self.model, "offline_fixture": False,
                "paid_api_request": True, "request_attempted": True,
                "estimated_cost": 0, "cost_known": True}


def make_study(f, frozen):
    """Make removable test evidence using real search/selection and a fake API."""
    study_dir = f.root / "simulated-study"
    study_dir.mkdir()
    search = load_instances(f.data / "search.json")
    validation = load_instances(f.data / "validation.json")
    probes = load_probes(f.data / "probes.json")
    cores = protocol.core_source_hashes(f.source)
    module_sources = {name.removeprefix("funpack/"): value for name, value in cores.items()
                      if name.startswith("funpack/")}
    study = {"phase": "formal", "protocol_id": frozen["protocol_id"],
             "core_source_hashes": cores, "api_config": protocol.public_api_config(f.api),
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
                info = run_search(search, probes, ApiProvider(f.api["model"]), config, study_dir / name)
            # These are simulated formal records, explicitly labelled above;
            # this permits testing the gate without any real paid requests.
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


class FreezeTests(unittest.TestCase):
    def test_unresolved_call_budget_cannot_be_frozen(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            write_json(f.plan_path, protocol.draft_plan())
            with self.assertRaises(ValueError):
                freeze(f)
            self.assertFalse(f.frozen_path.exists())

    def test_runtime_cannot_change_factors_or_call_budget(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            args = runtime(f)
            protocol.validate_runtime(frozen, args, f.api)
            for field, value in (("provider", "offline"), ("stage", "search"),
                                 ("calls", 2), ("arms", ["A", "B"]),
                                 ("seeds", [11, 22]), ("audit_rate", 0.25),
                                 ("enhanced_prompt_version", "enhanced_v1"),
                                 ("collision_policy", "retain_representative"),
                                 ("database_temperature", 1.2)):
                with self.subTest(field=field):
                    altered = copy.deepcopy(args)
                    setattr(altered, field, value)
                    with self.assertRaises(ValueError):
                        protocol.validate_runtime(frozen, altered, f.api)

    def test_runtime_cannot_change_model_or_sampling(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            for field, value in (("model", "another-model"), ("temperature", 0.1),
                                 ("max_tokens", 256), ("max_requests", 24)):
                with self.subTest(field=field):
                    api = copy.deepcopy(f.api)
                    api[field] = value
                    with self.assertRaises(ValueError):
                        protocol.validate_runtime(frozen, runtime(f), api)

    def test_freeze_and_verification_never_open_heldout_files(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            # Remove them after recording their manifest hashes. A preparation
            # stage must work without reading any sealed benchmark bytes.
            for name in ("test.json", "or3.json"):
                (f.data / "heldout" / name).unlink()
            with patch.dict("os.environ", {}, clear=True):
                frozen = freeze(f)
                self.assertEqual(protocol.load_protocol(f.frozen_path, f.data, f.source), frozen)
            self.assertFalse((root / "unused-test-ledger.sqlite3").exists())

    def test_freeze_refuses_to_replace_existing_protocol(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            freeze(f)
            before = f.frozen_path.read_bytes()
            with self.assertRaises(FileExistsError):
                freeze(f)
            self.assertEqual(f.frozen_path.read_bytes(), before)

    def test_changed_core_source_is_detected(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            freeze(f)
            path = f.source / "funpack" / "packing.py"
            path.write_text("# Changed evaluator after freezing\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                protocol.load_protocol(f.frozen_path, f.data, source_root=f.source)

    def test_unrelated_user_file_does_not_invalidate_protocol(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            expected = freeze(f)
            (f.source / "funpack" / "test.py").write_text("# Student's separate work\n", encoding="utf-8")
            self.assertEqual(protocol.load_protocol(f.frozen_path, f.data, f.source), expected)

    def test_nonheldout_mutation_is_detected(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            freeze(f)
            path = f.data / "search.json"
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaises(ValueError):
                protocol.load_protocol(f.frozen_path, f.data, f.source)

    def test_frozen_payload_mutation_is_detected(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            freeze(f)
            obj = read_json(f.frozen_path)
            # Any unexpected payload alteration must invalidate its digest.
            obj["test_mutation"] = True
            write_json(f.frozen_path, obj)
            with self.assertRaises(ValueError):
                protocol.load_protocol(f.frozen_path, f.data, f.source)

    def test_plaintext_credentials_are_rejected_before_writing(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            f.api["api_key"] = "test-only-credential-placeholder"
            write_json(f.api_path, f.api)
            with self.assertRaises(ValueError):
                freeze(f)
            self.assertFalse(f.frozen_path.exists())


class CandidateGateTests(unittest.TestCase):
    def test_pilot_offline_and_missing_repetition_cannot_be_sealed(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            original = read_json(study_dir / "study.json")
            variants = []
            for phase in ("pilot", "offline_integration"):
                altered = copy.deepcopy(original)
                altered["phase"] = phase
                variants.append((phase, altered))
            altered = copy.deepcopy(original)
            altered["runs"].pop()
            variants.append(("missing_run", altered))
            for label, study in variants:
                with self.subTest(label=label):
                    write_json(study_dir / "study.json", study)
                    output = root / (label + ".json")
                    with self.assertRaises(ValueError):
                        protocol.seal_candidates(f.frozen_path, study_dir, f.data, output, f.source)
                    self.assertFalse(output.exists())

    def test_incomplete_request_count_and_offline_response_are_rejected(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            info_path = study_dir / "A_seed11" / "final_info.json"
            original = read_json(info_path)
            bad = copy.deepcopy(original)
            bad["metrics"]["generator_requests"] = 0
            write_json(info_path, bad)
            with self.assertRaises(ValueError):
                protocol.seal_candidates(f.frozen_path, study_dir, f.data, root / "seal1.json", f.source)
            write_json(info_path, original)
            path = study_dir / "A_seed11" / "provider_responses.jsonl"
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            events[0]["response"]["offline_fixture"] = True
            path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                protocol.seal_candidates(f.frozen_path, study_dir, f.data, root / "seal2.json", f.source)

    def test_selected_program_must_match_recorded_validation_winner(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            path = study_dir / "A_seed11" / "selection.json"
            selection = read_json(path)
            selection["selected"]["code"] = "def priority(item, bins):\n    return [r for r in bins]"
            selection["selected"]["code_hash"] = digest(selection["selected"]["code"])
            write_json(path, selection)
            with self.assertRaises(ValueError):
                protocol.seal_candidates(f.frozen_path, study_dir, f.data, root / "seal.json", f.source)

    def test_final_requires_explicit_flag_before_loading_data(self):
        with workspace_temp() as root:
            with patch.object(protocol, "load_instances", side_effect=AssertionError("unexpected load")):
                with self.assertRaises(PermissionError):
                    protocol.evaluate_final(root / "no-protocol", root / "no-seal", root / "no-study",
                                            root / "no-data", root / "output", allow_heldout=False)

    def test_changing_sealed_evidence_blocks_final_before_heldout_read(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            seal_path = root / "sealed.json"
            protocol.seal_candidates(f.frozen_path, study_dir, f.data, seal_path, f.source)
            evidence = study_dir / "A_seed11" / "events.jsonl"
            with evidence.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            real_load = protocol.load_instances
            visited = []

            def record_load(path, **kwargs):
                visited.append(Path(path).as_posix())
                if "heldout" in Path(path).parts:
                    raise AssertionError("held-out content loaded before integrity failure")
                return real_load(path, **kwargs)

            with patch.object(protocol, "load_instances", side_effect=record_load):
                with self.assertRaises(ValueError):
                    protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                            root / "final", allow_heldout=True, source_root=f.source)
            self.assertFalse(any("heldout/" in path for path in visited))

    def test_final_reports_every_frozen_winner_and_refuses_second_output(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            seal_path = root / "sealed.json"
            seal = protocol.seal_candidates(f.frozen_path, study_dir, f.data, seal_path, f.source)
            result = protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                             root / "final", allow_heldout=True, source_root=f.source)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["heldout_evaluated"])
            self.assertEqual(set(result["splits"]), {"test", "or3"})
            expected_methods = {"FF", "BF"} | {w["run"] for w in seal["winners"]}
            for split, data in result["splits"].items():
                self.assertEqual(set(data["methods"]), expected_methods)
                self.assertEqual(data["instance_ids"], ["test-owned-" + split])
                self.assertEqual(data["methods"]["BF"]["counts"], [2])
                self.assertEqual(data["methods"]["FF"]["counts"], [3 if split == "test" else 2])
                for winner in seal["winners"]:
                    observed = data["methods"][winner["run"]]
                    self.assertEqual(observed["code_hash"], winner["code_hash"])
                    self.assertEqual(observed["code"], winner["code"])
                    expected = 3 if split == "test" and winner["code"] == FF_CODE else 2
                    self.assertEqual(observed["counts"], [expected])
                    self.assertEqual(observed["delta_to_bf"], [expected - 2])
                    self.assertEqual(observed["instance_results"][0]["status"], "valid")
                    self.assertEqual(observed["mean_bins"], expected)
            snapshot = (root / "final" / "final_results.json").read_bytes()
            with self.assertRaises(ValueError):
                protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                        root / "different-output", allow_heldout=True, source_root=f.source)
            self.assertFalse((root / "different-output").exists())
            self.assertEqual((root / "final" / "final_results.json").read_bytes(), snapshot)

    def test_corrupt_heldout_leaves_claim_and_interrupted_record(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            seal_path = root / "sealed.json"
            protocol.seal_candidates(f.frozen_path, study_dir, f.data, seal_path, f.source)
            with (f.data / "heldout" / "test.json").open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaises(ValueError):
                protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                        root / "final", allow_heldout=True, source_root=f.source)
            self.assertTrue((study_dir / "final_evaluation_claim.json").exists())
            interrupted = read_json(root / "final" / "final_results.json")
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertFalse(interrupted["heldout_evaluated"])
            with self.assertRaises(ValueError):
                protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                        root / "retry", allow_heldout=True, source_root=f.source)

    def test_missing_benchmark_does_not_consume_final_evaluation_claim(self):
        with workspace_temp() as root:
            f = make_fixture(root)
            frozen = freeze(f)
            study_dir = make_study(f, frozen)
            seal_path = root / "sealed.json"
            protocol.seal_candidates(f.frozen_path, study_dir, f.data, seal_path, f.source)
            (f.data / "heldout" / "or3.json").unlink()
            with self.assertRaisesRegex(ValueError, "heldout file missing"):
                protocol.evaluate_final(f.frozen_path, seal_path, study_dir, f.data,
                                        root / "final", allow_heldout=True, source_root=f.source)
            self.assertFalse((study_dir / "final_evaluation_claim.json").exists())
            self.assertFalse((root / "final").exists())

    def test_failed_instances_are_retained_and_mean_is_not_biased(self):
        code = "def priority(item, bins):\n    return [1 / (item - 3) for r in bins]"
        instances = [{"id": "fails", "capacity": 10, "items": [6, 7, 3, 4]},
                     {"id": "works", "capacity": 10, "items": [6, 6, 4, 4]}]
        result = protocol._evaluate_frozen(code, instances, 30)
        self.assertEqual(result["status"], "evaluation_error")
        self.assertEqual(result["counts"], [None, 2])
        self.assertIsNone(result["mean_bins"])
        self.assertEqual(result["instance_results"][0]["status"], "evaluation_error")
        self.assertEqual(result["instance_results"][1]["status"], "valid")
        self.assertIn("error", result["instance_results"][0])


if __name__ == "__main__":
    unittest.main()
