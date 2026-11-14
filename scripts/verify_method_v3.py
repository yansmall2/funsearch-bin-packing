"""Replay saved D candidates to check collision handling; no API or held-out data.

Fixed candidate streams do NOT measure the generation quality of a new prompt.
The legacy replay is first required to reproduce original decisions and results.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from funpack.data import load_instances, load_probes
from funpack.programs import BF_CODE, FF_CODE
from funpack.prompts import build_messages
from funpack.providers import OfflineProvider
from funpack.search import run_search
from funpack.storage import digest, read_json, write_json


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(evidence_root, out):
    evidence_root, out = Path(evidence_root).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError("use a new output directory; existing evidence is never overwritten")
    source = evidence_root / "run_paid_calibration_20261106T175234Z/D_seed11"
    original = rows(source / "events.jsonl")
    original_info = read_json(source / "final_info.json")
    records = {r["id"]: r for r in rows(source / "evaluated.jsonl")}
    evidence_files = [source / name for name in ("events.jsonl", "evaluated.jsonl", "final_info.json")]
    evidence_files += [evidence_root / "data" / name for name in ("search.json", "probes.json")]
    hashes = {str(path): file_hash(path) for path in evidence_files}
    search = load_instances(evidence_root / "data/search.json")
    probes = load_probes(evidence_root / "data/probes.json")
    assert digest(search) == original_info["search_hash"]
    assert digest(probes) == original_info["probe_hash"]
    assert len(original) == 32
    fixtures = [e["parsed"]["code"].removesuffix("\n") for e in original]
    # Rebuild historical prompts from original measured parents, not new parents.
    for event in original:
        parents = [records[name] for name in event["parents"]]
        assert build_messages("enhanced", parents, enhanced_version="enhanced_v1") == event["messages"]
    out.mkdir(parents=True)
    print("Saved-candidate replay: checks control flow, not LLM prompt effectiveness.", flush=True)
    results = {}
    for label, version, policy in (("legacy", "enhanced_v1", "retain_representative"),
                                   ("revised", "mechanism_v3", "disable_on_mismatch_v1")):
        config = {**original_info["config"], "enhanced_prompt_version": version, "collision_policy": policy}
        provider = OfflineProvider(fixtures=fixtures, fixture_origin="saved_llm_response")
        info = run_search(search, probes, provider, config, out / label)
        events = rows(out / label / "events.jsonl")
        assert [e["parsed"]["code"] for e in events] == [e["parsed"]["code"] for e in original]
        assert info["metrics"]["paid_api_requests"] == 0 and info["metrics"]["estimated_cost_reported"] == 0
        if label == "legacy":
            for new, old in zip(events, original):
                for field in ("status", "parents", "island", "representative", "audited"):
                    assert new.get(field) == old.get(field), (new["request_index"], field)
                if "result" in old:
                    assert new["result"] == old["result"]
            assert info["best_search_mean_bins"] == original_info["best_search_mean_bins"]
        results[label] = {"method": info["method"], "metrics": info["metrics"],
                          "best_search_mean_bins": info["best_search_mean_bins"],
                          "selected_events": [{"request_index": e["request_index"], "status": e["status"],
                                               "representative": e.get("representative"),
                                               "fingerprint_disabled": e.get("fingerprint_disabled", False),
                                               "collision_bypass": e.get("collision_bypass", False),
                                               "mean_bins": e.get("result", {}).get("mean_bins")}
                                              for e in events if e["request_index"] in (9, 10, 15)]}
        print(label + ": " + json.dumps({"full_evaluations": info["metrics"]["candidate_full_evaluations"],
                                        "skips": info["metrics"]["behavior_skips"],
                                        "disabled": info["metrics"]["disabled_fingerprints"],
                                        "bypasses": info["metrics"]["collision_bypass_full_evaluations"]}), flush=True)
    new_events = {e["request_index"]: e for e in rows(out / "revised/events.jsonl")}
    assert new_events[9]["fingerprint_disabled"]
    for i in (10, 15):
        assert new_events[i]["collision_bypass"] and new_events[i]["status"] == "evaluated"
        assert new_events[i]["result"]["mean_bins"] < 265.3
    assert all(file_hash(Path(path)) == expected for path, expected in hashes.items())
    samples = {name: build_messages("enhanced", [records["seed_ff"], records["seed_bf"]], enhanced_version=name)
               for name in ("enhanced_v1", "mechanism_v3")}
    write_json(out / "example_prompts.json", samples)
    report = {"kind": "offline_fixed_saved_candidate_regression", "checks_passed": True,
              "source_run": str(source), "source_evidence_unchanged": True, "source_sha256": hashes,
              "new_api_requests": 0, "heldout_loaded": False, "validation_loaded": False,
              "legacy_reproduces_original": True, "runs": results,
              "limitation": "Identical SAVED candidates, not a new adaptive LLM search. Does not show v3 prompt quality, thinking-mode quality, generalization, or reliable end-to-end speedup. Bypass time is a subset of full evaluation time; do not add it again."}
    write_json(out / "verification.json", report)
    (out / "notes.txt").write_text(report["limitation"] + "\n" + json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    # Guard both the socket layer and the data loader against accidental expansion.
    original_open = Path.open
    def restricted_open(path, *a, **kw):
        if "heldout" in path.parts or path.name == "validation.json":
            raise AssertionError("validation and held-out data forbidden in replay")
        return original_open(path, *a, **kw)
    with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
         patch("urllib.request.build_opener", side_effect=AssertionError("API forbidden")), \
         patch.object(Path, "open", restricted_open):
        verify(args.evidence_root, args.out_dir)
    print("Verified legacy parity and revised handling of D9/D10/D15; zero new API calls.")


if __name__ == "__main__":
    main()
