"""Replay six saved DeepSeek streams with no screen, 32 probes and 128 probes."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from collections import Counter
import json
from pathlib import Path
import random
import sys
import time

from funpack.data import _generate_probes, load_instances, load_probes
from funpack.packing import evaluate
from funpack.programs import BF_CODE, FF_CODE, Program
from funpack.screening import BF_PROTECTED, BehavioralScreen
from funpack.storage import digest, read_json, write_json

ROOT = Path(__file__).resolve().parent
RUNS = [(arm, seed) for arm in "CD" for seed in (101, 202, 303)]
CANDIDATE_ERRORS = (ValueError, ArithmeticError, TypeError, RuntimeError)
MATCHED_METRICS = (
    "candidate_full_evaluations", "successful_candidate_evaluations",
    "text_cache_hits", "behavior_skips", "audits", "audit_count_mismatches",
    "audit_trace_mismatches", "bf_protected_full_evaluations",
    "collision_bypass_full_evaluations", "disabled_fingerprints", "invalid_candidates",
)


class PrefixScreen(BehavioralScreen):
    """Use the published 32-state prefix as the complete fingerprint."""

    def __init__(self, probes):
        super().__init__(probes, collision_policy=BF_PROTECTED)
        self.states = probes[:32]

    def disable(self, prefix, tail):
        # This screen has an empty second stage.
        key = (prefix, tail)
        added = key not in self.disabled_fingerprints
        self.disabled_fingerprints.add(key)
        return added


def full_evaluation(program, instances, timeout):
    deadline = time.perf_counter() + timeout

    def bounded(item, bins):
        if time.perf_counter() > deadline:
            raise RuntimeError("full evaluation time budget exceeded")
        return program(item, bins)

    outcome = evaluate(bounded, instances)
    if time.perf_counter() > deadline:
        raise RuntimeError("full evaluation time budget exceeded")
    return outcome


def replay(sources, instances, probes, config, count):
    """Time each policy independently. The oracle never enters the screen's cache."""
    start = time.process_time()
    if count == 32:
        screen = PrefixScreen(probes)
    elif count == 128:
        screen = BehavioralScreen(probes, collision_policy=BF_PROTECTED)
    else:
        screen = None
    rng = random.Random(config["seed"] + 1_000_003)
    metrics = Counter(dict.fromkeys(MATCHED_METRICS, 0))
    cache, skipped, decisions = {}, [], []

    def record(code, program, label):
        outcome = {"id": label, **full_evaluation(
            program, instances, config["evaluation_timeout_seconds"])}
        cache[code] = outcome
        return outcome

    for label, code in (("seed_ff", FF_CODE), ("seed_bf", BF_CODE)):
        program = Program(code)
        outcome = record(code, program, label)
        if screen:
            screen.register(program, outcome)

    for index, code in enumerate(sources, 1):
        if code in cache:
            metrics["text_cache_hits"] += 1
            decisions.append("text_cache_hit")
            continue
        prefix = tail = representative = None
        audited = False
        try:
            program = Program(code)
            if screen:
                try:
                    representative, prefix, tail = screen.probe(program)
                except CANDIDATE_ERRORS:
                    representative, prefix, tail = None, None, None
                metrics["bf_protected_full_evaluations"] += int(screen.protects_bf(prefix, tail))
                metrics["collision_bypass_full_evaluations"] += int(screen.is_disabled(prefix, tail))
            if representative is not None:
                audited = rng.random() < config["audit_rate"]
                if not audited:
                    skipped.append((code, representative))
                    metrics["behavior_skips"] += 1
                    decisions.append("behavior_skip")
                    continue
            metrics["audits"] += int(audited)
            metrics["candidate_full_evaluations"] += 1
            outcome = record(code, program, f"candidate_{index:06d}")
            metrics["successful_candidate_evaluations"] += 1
            status = "audit_evaluated" if audited else "evaluated"
            if audited:
                count_difference = outcome["counts"] != representative["counts"]
                trace_difference = outcome["trace_hashes"] != representative["trace_hashes"]
                metrics["audit_count_mismatches"] += int(count_difference)
                metrics["audit_trace_mismatches"] += int(trace_difference)
                if count_difference or trace_difference:
                    metrics["disabled_fingerprints"] += int(screen.disable(prefix, tail))
            if screen:
                try:
                    screen.register(program, outcome, prefix, tail)
                except CANDIDATE_ERRORS:
                    status += "_probe_registration_error"
            decisions.append(status)
        except CANDIDATE_ERRORS:
            metrics["invalid_candidates"] += 1
            decisions.append("audit_evaluation_error" if audited else "candidate_error")
    elapsed = time.process_time() - start
    metrics.update({"best_search_mean_bins": min(row["mean_bins"] for row in cache.values())})
    return dict(metrics, policy_cpu_seconds=round(elapsed, 3)), cache, skipped, decisions


def compare_skips(skipped, oracle):
    counts = Counter(dict.fromkeys(("better", "tied", "worse", "unscorable", "count_mismatch",
                                    "trace_mismatch"), 0))
    for source, representative in skipped:
        if source not in oracle:
            counts["unscorable"] += 1
            continue
        outcome = oracle[source]
        delta = sum(outcome["counts"]) - sum(representative["counts"])
        counts["better" if delta < 0 else "worse" if delta > 0 else "tied"] += 1
        counts["count_mismatch"] += int(outcome["counts"] != representative["counts"])
        counts["trace_mismatch"] += int(outcome["trace_hashes"] != representative["trace_hashes"])
    return dict(counts)


def run_pair(job):
    arm, seed, study_dir = job
    name = f"{arm}_seed{seed}"
    folder = study_dir / name
    original = read_json(folder / "final_info.json")
    config = original["config"]
    instances = load_instances(ROOT / "data/search.json")
    probes = load_probes(ROOT / "data/probes.json")
    with (folder / "events.jsonl").open(encoding="utf-8-sig") as stream:
        events = [json.loads(line) for line in stream]
    if (config["calls"] != 200 or config["seed"] != seed or config["arm"] != arm
            or config["collision_policy"] != BF_PROTECTED or config["audit_rate"] != 0.1
            or original["search_hash"] != digest(instances) or original["probe_hash"] != digest(probes)
            or [event["request_index"] for event in events] != list(range(1, 201))):
        raise ValueError(f"{name} does not match the frozen 200-step run")
    if any(event["response"]["model"] != "deepseek-flash"
           or digest(event["messages"]) != event["messages_hash"] for event in events):
        raise ValueError(f"{name} has inconsistent generator or prompt metadata")
    sources = [event["parsed"]["code"] for event in events]
    if any(not source for source in sources):
        raise ValueError(f"{name} is missing an extracted program")

    off, oracle, _, _ = replay(sources, instances, probes, config, 0)
    modes = {"off": off}
    for count in (32, 128):
        metrics, _, skipped, decisions = replay(sources, instances, probes, config, count)
        if count == 128:
            if (decisions != [event["status"] for event in events]
                    or any(metrics[key] != original["metrics"][key] for key in MATCHED_METRICS)
                    or metrics["best_search_mean_bins"] != original["best_search_mean_bins"]):
                raise ValueError(f"{name} 128-state replay differs from the recorded decisions")
        metrics["skip_outcomes"] = compare_skips(skipped, oracle)
        modes[str(count)] = metrics
    return {"arm": arm, "seed": seed, "requests": len(events), "stream_sha256": digest(sources),
            "modes": modes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", type=Path, required=True, help="local directory with run_200 logs")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 7), default=4)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("choose a new output file")
    print("Comparing compute and skipped decisions on 1,200 saved proposals. No API or final-test data.", flush=True)
    development = load_instances(ROOT / "data/development.json")
    start = time.process_time()
    probe_hash = digest(_generate_probes(development)["states"])
    probe_setup = round(time.process_time() - start, 3)
    if probe_hash != digest(load_probes(ROOT / "data/probes.json")):
        raise ValueError("probe reconstruction mismatch")
    jobs = [(arm, seed, args.study_dir) for arm, seed in RUNS]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        runs = []
        for row in pool.map(run_pair, jobs):
            runs.append(row)
            counts = [row["modes"][mode]["candidate_full_evaluations"] for mode in ("off", "32", "128")]
            print(f"{row['arm']}/{row['seed']} verified. Full evaluations (off / 32 / 128): {counts}",
                  flush=True)
    result = {
        "scope": "Fixed proposal streams from C and D. Search sequences only.",
        "api_requests": 0,
        "search_sha256": digest(load_instances(ROOT / "data/search.json")),
        "probe_sha256": probe_hash,
        "shared_128_probe_setup_cpu_seconds": probe_setup,
        "timing": "CPU time per replay, including baseline fits, screening and audits. "
                  "Probe construction (shown separately), model generation, post-hoc oracle comparison, "
                  "and validation are outside this timing.",
        "skips": "Event counts, including repeated skips of the same program. "
                 "Better means fewer total search bins than the representative.",
        "reproduced_128_statuses": True,
        "python": sys.version.split()[0],
        "runs": runs,
    }
    write_json(args.out, result)


if __name__ == "__main__":
    main()
