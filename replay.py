"""Offline 32-versus-128 probe diagnostic; never calls a model or reads test data."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from funpack.data import load_instances, load_probes
from funpack.packing import evaluate
from funpack.programs import Program, FF_CODE, BF_CODE
from funpack.screening import fingerprint
from funpack.storage import digest, read_json, source_hashes, write_json


def replay(run_dir, data_dir):
    run_dir, data_dir = Path(run_dir), Path(data_dir)
    manifest = read_json(run_dir / "run_manifest.json")
    if source_hashes(Path(__file__).parent / "funpack") != manifest["source_hashes"]:
        raise ValueError("core source changed since source run; replay requires the original source version")
    instances = load_instances(data_dir / "search.json")
    probes = load_probes(data_dir / "probes.json")
    if digest(probes) != manifest["probe_hash"]:
        raise ValueError("probe set changed since source run")
    source_summary = read_json(run_dir.parent / "study.json")
    if digest(instances) != source_summary["search_hash"]:
        raise ValueError("search data changed since source run")
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    codes = [FF_CODE, BF_CODE] + [event["parsed"]["code"] for event in events
                               if event.get("parsed", {}).get("code")]
    truth = {}
    start = time.perf_counter()
    for code in codes:
        if code in truth:
            continue
        try:
            program = Program(code)
            deadline = time.perf_counter() + 30
            def bounded(item, bins):
                if time.perf_counter() > deadline:
                    raise RuntimeError("replay evaluation time budget exceeded")
                return program(item, bins)
            truth[code] = {"result": evaluate(bounded, instances)}
        except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
            truth[code] = {"error": f"{type(error).__name__}: {error}"}
    reference_seconds = time.perf_counter() - start
    variants = []
    for count in (32, 128):
        representatives, seen_text, decisions = {}, set(), []
        started = time.perf_counter()
        for index, code in enumerate(codes):
            event = {"stream_index": index, "code_hash": digest(code), "seed": index < 2}
            if code in seen_text:
                event["status"] = "exact_text_repeat"
                decisions.append(event)
                continue
            seen_text.add(code)
            try:
                signature = fingerprint(Program(code), probes[:count])
            except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
                event.update(status="probe_error", error=str(error))
                decisions.append(event)
                continue
            if signature in representatives:
                old_code = representatives[signature]
                event.update(status="probe_match", representative_hash=digest(old_code))
                if "error" in truth[code]:
                    event.update(full_evaluation_error=truth[code]["error"], observed_false_positive=True)
                else:
                    left, right = truth[code]["result"], truth[old_code]["result"]
                    event.update(counts_differ=left["counts"] != right["counts"],
                                 traces_differ=left["trace_hashes"] != right["trace_hashes"])
                    event["observed_false_positive"] = event["counts_differ"] or event["traces_differ"]
            elif "result" in truth[code]:
                representatives[signature] = code
                event["status"] = "new_evaluated_representative"
            else:
                event.update(status="full_evaluation_error", error=truth[code]["error"])
            decisions.append(event)
        candidate_decisions = [row for row in decisions if not row["seed"]]
        variants.append({"probe_count": count, "probe_replay_seconds": time.perf_counter() - started,
                         "matches": sum(row["status"] == "probe_match" for row in candidate_decisions),
                         "observed_false_positives": sum(row.get("observed_false_positive", False) for row in candidate_decisions),
                         "decisions": decisions})
    result = {"purpose": "fixed-stream offline diagnostic; not a replacement for adaptive search",
              "source_run": str(run_dir), "evidence_scope": manifest["evidence_scope"],
              "search_data_hash": digest(instances), "probe_hash": digest(probes),
              "reference_evaluation_seconds": reference_seconds, "paid_api_requests": 0,
              "heldout_evaluated": False, "variants": variants,
              "limitation": "No observed difference is not proof of equivalence. Timings exclude LLM generation."}
    write_json(run_dir / "replay_32_128.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    result = replay(args.run, args.data)
    print(json.dumps({"paid_api_requests": 0, "variants": [
        {key: row[key] for key in ("probe_count", "matches", "observed_false_positives")}
        for row in result["variants"]]}, indent=2))
