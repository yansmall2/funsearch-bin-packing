"""Check saved integration evidence without evaluating any held-out algorithm."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from funpack.storage import read_json, source_hashes, write_json


def verify(run_dir, data_dir):
    run_dir, data_dir = Path(run_dir), Path(data_dir)
    study = read_json(run_dir / "study.json")
    assert study["phase"] == "offline_integration", "this verifier is for integration evidence only"
    expected = {(arm, seed) for arm in study["arguments"]["arms"] for seed in study["arguments"]["seeds"]}
    assert {(r["arm"], r["seed"]) for r in study["runs"]} == expected
    core = source_hashes(ROOT / "funpack")
    totals = {"generator_requests": 0, "paid_api_requests": 0, "estimated_cost_reported": 0.0}
    for run in study["runs"]:
        directory = run_dir / run["run"]
        result = read_json(directory / "final_info.json")
        assert result["source_hashes"] == core, f"core changed during/after run {run['run']}"
        assert result["termination"] == "request_budget_completed"
        assert result["heldout_evaluated"] is False
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(events) == study["arguments"]["calls"]
        assert len({event["request_index"] for event in events}) == len(events)
        assert not (directory / "pending_request.json").exists()
        for event in events:
            assert event["response"]["offline_fixture"] is True
            if event["status"] == "behavior_skip":
                assert "result" not in event
        selection = read_json(directory / "selection.json")
        assert selection["heldout_evaluated"] is False
        assert selection["selected"]["validation"]["mean_bins"] > 0
        for key in totals:
            totals[key] += result["metrics"][key]
    assert totals["paid_api_requests"] == 0 and totals["estimated_cost_reported"] == 0
    # Hashing held-out files verifies integrity, not algorithm performance.
    manifest = read_json(data_dir / "manifest.json")
    for relative, details in manifest["files"].items():
        expected_sha = details if isinstance(details, str) else details["sha256"]
        assert hashlib.sha256((data_dir / relative).read_bytes()).hexdigest() == expected_sha
    result = {"checks_passed": True, "run_count": len(expected), **totals,
              "core_source_hashes_match_every_run": True, "data_file_hashes_match": True,
              "heldout_algorithm_evaluated": False,
              "scope": "offline integration evidence only; not an LLM performance claim"}
    write_json(run_dir / "evidence_verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    print(json.dumps(verify(args.run, args.data), indent=2))
