"""Summarize audited matches and end-to-end cost without touching held-out data.

Original formal timing takes precedence over a legacy timing estimate.
Optional probe reconstruction is a fresh diagnostic, never an additional
charge on already measured setup time. No external API requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from pathlib import Path

from funpack.data import _generate_probes, content_hash, load_instances, load_probes
from funpack.storage import digest, environment, read_json, write_json


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def audit_events(events, evaluated, instance_ids):
    records = {r["id"]: r for r in evaluated}
    details = []
    for event in events:
        if not event.get("audited", False):
            continue
        row = {"request_index": event["request_index"], "representative": event.get("representative"),
               "status": event["status"]}
        if event["status"] == "audit_evaluation_error":
            row["error"] = event.get("error")
            details.append(row)
            continue
        representative = records.get(event.get("representative"))
        result = event.get("result")
        if representative is None or result is None:
            raise ValueError("audited record is missing its measured representative or result")
        before, after = representative["counts"], result["counts"]
        old_traces, new_traces = representative["trace_hashes"], result["trace_hashes"]
        if not (len(instance_ids) == len(before) == len(after) == len(old_traces) == len(new_traces)):
            raise ValueError("audit counts/trace lengths do not match search instances")
        differences = [{"instance_id": iid, "representative_bins": old, "candidate_bins": new,
                        "delta_bins_candidate_minus_representative": new - old,
                        "trace_differs": old_trace != new_trace}
                       for iid, old, new, old_trace, new_trace in
                       zip(instance_ids, before, after, old_traces, new_traces)]
        deltas = [d["delta_bins_candidate_minus_representative"] for d in differences]
        row.update(instances=differences, counts_differ=any(deltas),
                   traces_differ=any(d["trace_differs"] for d in differences),
                   mean_delta_bins=statistics.mean(deltas),
                   mean_absolute_delta_bins=statistics.mean(abs(d) for d in deltas),
                   mean_missed_improvement_bins=statistics.mean(max(-d, 0) for d in deltas),
                   max_missed_improvement_bins=max(max(-d, 0) for d in deltas),
                   candidate_better_instances=sum(d < 0 for d in deltas),
                   candidate_worse_instances=sum(d > 0 for d in deltas),
                   equal_bin_instances=sum(d == 0 for d in deltas))
        details.append(row)
    success = [r for r in details if "instances" in r]
    return {"audits_attempted": len(details), "audits_completed": len(success),
            "audit_errors": len(details) - len(success),
            "trace_disagreements": sum(r["traces_differ"] for r in success),
            "quality_disagreements": sum(r["counts_differ"] for r in success),
            "trace_disagreement_rate_completed": ratio(sum(r["traces_differ"] for r in success), len(success)),
            "quality_disagreement_rate_completed": ratio(sum(r["counts_differ"] for r in success), len(success)),
            "audit_error_rate_attempted": ratio(len(details) - len(success), len(details)),
            "details": details}


def measure_probe_setup(data_dir, repetitions=3):
    if not 1 <= repetitions <= 10:
        raise ValueError("repetitions must be between 1 and 10")
    development = load_instances(Path(data_dir) / "development.json")
    expected = load_probes(Path(data_dir) / "probes.json")
    times = []
    for _ in range(repetitions):
        start = time.perf_counter()
        payload = _generate_probes(development)
        times.append(time.perf_counter() - start)
        if content_hash(payload["states"]) != content_hash(expected):
            raise ValueError("reconstructed probes differ from the frozen probe data")
    return {"seconds_each": times, "median_seconds": statistics.median(times),
            "kind": "remeasurement_not_original_setup_time", "probe_hash": digest(expected),
            "environment": environment(), "heldout_loaded": False}


def analyze(study_dir, data_dir, setup_measurement=None):
    study_dir, data_dir = Path(study_dir), Path(data_dir)
    study = read_json(study_dir / "study.json")
    search_instances = load_instances(data_dir / "search.json")
    if digest(search_instances) != study["search_hash"]:
        raise ValueError("search data differs from the study manifest")
    ids = [x["id"] for x in search_instances]
    run_count = len(study["runs"])
    expected_count = len(study["arguments"]["arms"]) * len(study["arguments"]["seeds"])
    complete = (run_count == expected_count and all(r["termination"] == "request_budget_completed" for r in study["runs"]))
    n_screen = sum(r["arm"] in ("C", "D") for r in study["runs"])
    if setup_measurement and setup_measurement["probe_hash"] != study["probe_hash"]:
        raise ValueError("setup measurement uses a different probe set")
    runs = []
    for run in study["runs"]:
        directory = study_dir / run["run"]
        events = [json.loads(x) for x in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        evaluated = [json.loads(x) for x in (directory / "evaluated.jsonl").read_text(encoding="utf-8").splitlines()]
        audits = audit_events(events, evaluated, ids)
        metrics = run["metrics"]
        if audits["audits_attempted"] != metrics["audits"]:
            raise ValueError("audit event count differs from recorded metric")
        selection = read_json(directory / "selection.json")
        screen_enabled = run["arm"] in ("C", "D")
        setup_share = (setup_measurement["median_seconds"] / n_screen
                       if setup_measurement and screen_enabled and complete and n_screen else None)
        # For A/B the incremental screening setup is zero. Missing C/D setup is unknown, not zero.
        if not screen_enabled:
            setup_share = 0.0
        search_seconds = metrics["search_total_seconds"]
        selection_seconds = selection["seconds"]
        original_timing = run.get("timing", {})
        has_original_total = original_timing.get("inclusive_search_seconds") is not None
        cost = {"search_seconds_including_screening_and_audits": search_seconds,
                "collision_bypass_evaluation_seconds_subset": metrics.get("collision_bypass_evaluation_seconds", 0.0),
                "screening_seconds_in_search": metrics["screening_seconds"],
                "evaluation_seconds_in_search_including_audits": metrics["evaluation_seconds"],
                "generation_seconds_in_search": metrics["generation_seconds"],
                "validation_selection_seconds": selection_seconds,
                "probe_reconstruction_share_seconds": setup_share,
                "search_selection_setup_estimate_seconds": (search_seconds + selection_seconds + setup_share
                                                             if setup_share is not None and not has_original_total
                                                             else None),
                "pipeline_seconds": original_timing.get("pipeline_seconds"),
                "common_setup_share_seconds": original_timing.get("common_setup_share_seconds"),
                "probe_construction_share_seconds": original_timing.get("probe_construction_share_seconds"),
                "inclusive_search_seconds": original_timing.get("inclusive_search_seconds"),
                "timing_basis": ("original_in_run_measurements_with_allocated_setup" if has_original_total
                                 else "legacy_partial_estimate_original_setup_unknown"),
                "reported_cost_estimate": metrics["estimated_cost_reported"],
                "unknown_cost_requests": metrics["unknown_cost_requests"]}
        runs.append({"run": run["run"], "arm": run["arm"], "seed": run["seed"],
                     "collision_handling": {
                         "disabled_fingerprints": metrics.get("disabled_fingerprints", 0),
                         "bypass_full_evaluations": metrics.get("collision_bypass_full_evaluations", 0),
                         "bypass_evaluation_errors": metrics.get("collision_bypass_evaluation_errors", 0),
                         "time_is_subset_of_total_evaluation": True},
                     "termination": run["termination"], "audits": audits, "cost": cost})
    return {"source_study": str(study_dir), "phase": study["phase"], "complete_balanced_study": complete,
            "run_count": run_count, "expected_run_count": expected_count, "runs": runs,
            "probe_setup_measurement": setup_measurement,
            "original_study_timing": study.get("timing"),
            "scope": {"offline_integration": "Exploratory offline fixtures, not LLM evidence",
                      "formal": "Frozen formal search comparison; this report does not evaluate held-out data"
                      }.get(study["phase"], "Pilot only, unless separately frozen"),
            "accounting": "Search time already includes screening, audit evaluation, API waiting and run management. Original pipeline_seconds already includes validation selection; inclusive_search_seconds additionally includes the recorded common setup and screening-only probe construction shares. Do not add those components again. Original study timing and its scope/allocation rule are preserved separately. Optional fresh probe reconstruction is only a diagnostic when original timing exists. For legacy logs only, search_selection_setup_estimate_seconds adds search, selection and a fresh probe setup estimate apportioned across completed C/D runs; missing historical setup remains unknown. Neither a legacy estimate nor per-run allocated timing is the entire study wall-clock time or the final analysis cost.",
            "limitations": "Audit rates exclude errors in the completed denominator; small audit samples are uncertain. More/fewer bins is an observed search-set effect, not a claim on final test. Estimated token cost is not a provider invoice.",
            "heldout_loaded": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--data", default="data")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--measure-probe-setup", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    target = Path(args.out_dir)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("use a new output directory; no evidence will be overwritten")
    measurement = measure_probe_setup(args.data, args.repetitions) if args.measure_probe_setup else None
    report = analyze(args.study, args.data, measurement)
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "audit_and_cost.json", report)
    rows = []
    for run in report["runs"]:
        rows.append({"run": run["run"], "arm": run["arm"], "seed": run["seed"],
                     **{k: v for k, v in run["audits"].items() if k != "details"},
                     **{"collision_" + k: v for k, v in run["collision_handling"].items()}, **run["cost"]})
    if rows:
        with (target / "per_run.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({"run_count": report["run_count"], "complete": report["complete_balanced_study"],
                      "scope": report["scope"], "output": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
