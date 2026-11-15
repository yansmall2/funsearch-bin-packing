"""Merge formal handshake part-runs into one study directory for sealing.

Each part is one `experiment.py --protocol` invocation for a subset of
(arm, seed) pairs, with its own out_dir. This script verifies every part
against the frozen protocol, copies the run directories verbatim, and builds
the merged study.json the seal step inspects. It never edits run evidence:
final_info, selection, events, provider responses and evaluated files are
copied byte-for-byte, and the seal step re-verifies every one of them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from funpack.protocol import load_protocol, validate_runtime
from funpack.storage import read_json, write_json

RUN_FILES = ("run_manifest.json", "final_info.json", "selection.json", "events.jsonl",
             "provider_responses.jsonl", "evaluated.jsonl")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _baselines_content(path):
    """Canonical baselines content ignoring wall-clock timing.

    Each part run writes its own baselines.json at its own start time, so
    environment.utc_time and the per-algorithm `seconds` timings differ
    between parts. Everything substantive (data hashes, instance ids,
    bin counts, means, trace hashes) must still match exactly.
    """
    data = json.loads(Path(path).read_text())
    data["environment"].pop("utc_time", None)
    for scope in data.get("results", {}).values():
        for algo in ("FF", "BF"):
            if algo in scope:
                scope[algo].pop("seconds", None)
    return json.dumps(data, sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--parts", nargs="+", required=True)
    parser.add_argument("--data", default="data")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    protocol = load_protocol(args.protocol, args.data)
    plan = protocol["plan"]
    if plan.get("provider") != "handshake":
        parser.error("this merge script is only for handshake protocols")
    expected = {(a, s) for s in plan["seeds"] for a in plan["arms"]}
    out_dir = Path(args.out)
    if out_dir.exists() and any(out_dir.iterdir()):
        parser.error("use a NEW output directory; existing evidence will not be overwritten")
    collected = {}
    first_study = None
    first_args = None
    for part in args.parts:
        part_dir = Path(part)
        study = read_json(part_dir / "study.json")
        if study.get("phase") != "formal" or study.get("protocol_id") != protocol["protocol_id"]:
            parser.error(f"{part}: not a formal study for this protocol")
        if study.get("stop_reason"):
            parser.error(f"{part}: study stopped early ({study['stop_reason']}); rerun it, do not merge")
        if study.get("core_source_hashes") != protocol["source_hashes"]:
            parser.error(f"{part}: study source differs from protocol")
        if study.get("provider_config") != protocol.get("provider_config"):
            parser.error(f"{part}: provider config differs from protocol")
        validate_runtime(protocol, study["arguments"], study.get("provider_config"))
        for key in ("search_hash", "validation_hash", "probe_hash"):
            if study.get(key) != protocol["data_hashes"][{"search_hash": "search", "validation_hash": "validation", "probe_hash": "probes"}[key]]:
                parser.error(f"{part}: study data hash differs: {key}")
        if first_study is None:
            first_study = study
            first_args = dict(study["arguments"])
        else:
            for key in ("core_source_hashes", "source_hashes", "search_hash", "validation_hash", "probe_hash"):
                if study.get(key) != first_study.get(key):
                    parser.error(f"{part}: {key} differs between parts; do not merge")
            if _baselines_content(part_dir / "baselines.json") != _baselines_content(Path(args.parts[0]) / "baselines.json"):
                parser.error(f"{part}: baselines differ between parts; do not merge")
        for entry in study.get("runs", []):
            key = (entry["arm"], entry["seed"])
            if key in collected:
                parser.error(f"duplicate run {key} in {part}")
            if entry.get("termination") != "request_budget_completed":
                parser.error(f"{part}: run {key} did not complete")
            run_dir = part_dir / entry["run"]
            if (run_dir / "pending_request.json").exists():
                parser.error(f"{part}: run {key} still has a pending request")
            for filename in RUN_FILES:
                if not (run_dir / filename).is_file():
                    parser.error(f"{part}: run {key} is missing {filename}")
            info = read_json(run_dir / "final_info.json")
            if entry.get("metrics") != info.get("metrics"):
                parser.error(f"{part}: run {key} metrics disagree with final_info")
            collected[key] = (part_dir, entry)
    if set(collected) != expected:
        parser.error(f"parts cover {sorted(collected)}; protocol needs {sorted(expected)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for (arm, seed), (part_dir, entry) in sorted(collected.items()):
        shutil.copytree(part_dir / entry["run"], out_dir / entry["run"])
    shutil.copy2(Path(args.parts[0]) / "baselines.json", out_dir / "baselines.json")
    arguments = dict(first_args)
    arguments.update({"arms": list(plan["arms"]), "seeds": list(plan["seeds"]),
                      "calls": plan["calls_per_run"], "out_dir": str(out_dir.resolve()),
                      "protocol": args.protocol})
    merged = {"phase": "formal", "protocol_id": protocol["protocol_id"],
              "arguments": arguments, "environment": first_study.get("environment"),
              "source_hashes": first_study.get("source_hashes"),
              "core_source_hashes": protocol["source_hashes"],
              "search_hash": protocol["data_hashes"]["search"],
              "validation_hash": protocol["data_hashes"]["validation"],
              "probe_hash": protocol["data_hashes"]["probes"],
              "provider_config": protocol.get("provider_config"),
              "heldout_evaluated": False,
              "runs": [entry for _, (_, entry) in sorted(collected.items(),
                                                     key=lambda kv: (kv[0][1], plan["arms"].index(kv[0][0])))],
              "merge_note": ("Merged from one experiment.py part-run per (arm, seed); "
                             "run directories copied verbatim; study.json rebuilt from the "
                             "frozen protocol and the parts' manifests."),
              "timing": {"merged_from_parts": [str(Path(p).resolve()) for p in args.parts],
                         "part_search_seconds_total": sum(
                             read_json(Path(p) / "study.json").get("timing", {}).get("study_elapsed_seconds", 0) or 0
                             for p in args.parts)}}
    write_json(out_dir / "study.json", merged)
    print(f"Merged {len(collected)} runs into {out_dir.resolve()}; ready for formal.py seal.")


if __name__ == "__main__":
    main()
