"""Reproduce local checks, baselines, or a logged four-arm pilot.

Default is offline. Paid requests require --provider api --allow-paid and a
complete enabled budget configuration, as well as an environment API key.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
from pathlib import Path

from funpack.data import _generate_probes, load_instances, load_probes
from funpack.packing import best_fit_scores, evaluate, first_fit_scores, pack
from funpack.programs import Program, BF_CODE, FF_CODE
from funpack.prompts import ENHANCED_VERSIONS
from funpack.screening import COLLISION_POLICIES
from funpack.providers import ApiProvider, OfflineProvider
from funpack.protocol import core_source_hashes, load_protocol, public_api_config, validate_runtime
from funpack.search import ARMS, run_search
from funpack.storage import digest, environment, read_json, source_hashes, write_json


def baselines(data_dir, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {"environment": environment(), "scope": "development_and_search_only",
               "heldout_evaluated": False, "results": {}}
    for split in ("development", "search"):
        instances = load_instances(Path(data_dir) / f"{split}.json")
        results["results"][split] = {"data_hash": digest(instances), "instance_ids": [i["id"] for i in instances]}
        for name, policy in (("FF", first_fit_scores), ("BF", best_fit_scores)):
            start = time.perf_counter()
            result = evaluate(policy, instances)
            result["seconds"] = time.perf_counter() - start
            results["results"][split][name] = result
    results["hand_example"] = {
        name: pack([6, 7, 3, 4], 10, policy, trace=True)
        for name, policy in (("FF", first_fit_scores), ("BF", best_fit_scores))}
    write_json(out_dir / "baselines.json", results)
    return results


def select_validation(run_dir, validation, timeout_seconds=30):
    run_dir = Path(run_dir)
    info = read_json(run_dir / "final_info.json")
    start = time.perf_counter()
    measured = []
    for record in info["top_search_candidates"]:
        candidate = {"id": record["id"], "code": record["code"], "code_hash": record["code_hash"],
                     "search_mean_bins": record["mean_bins"]}
        try:
            program = Program(record["code"])
            deadline = time.perf_counter() + timeout_seconds
            def bounded(item, bins):
                if time.perf_counter() > deadline:
                    raise RuntimeError("validation evaluation time budget exceeded")
                return program(item, bins)
            outcome = evaluate(bounded, validation)
            if time.perf_counter() > deadline:
                raise RuntimeError("validation evaluation time budget exceeded")
            candidate.update(validation=outcome, status="valid")
        except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
            candidate.update(status="validation_error", error=f"{type(error).__name__}: {error}")
        measured.append(candidate)
    valid = [record for record in measured if record["status"] == "valid"]
    fallback = not valid
    if fallback:
        for label, code in (("fallback_ff", FF_CODE), ("fallback_bf", BF_CODE)):
            record = {"id": label, "code": code, "code_hash": digest(code), "search_mean_bins": None,
                      "validation": evaluate(Program(code), validation), "status": "valid_seed_fallback"}
            measured.append(record)
            valid.append(record)
    valid.sort(key=lambda record: (record["validation"]["mean_bins"],
                                  record["search_mean_bins"] or 0, len(record["code"]), record["code_hash"]))
    selection = {"selection_rule": "search top5; validation mean; search mean; code length; hash",
                 "validation_hash": digest(validation), "candidates": measured,
                 "selected": valid[0], "used_seed_fallback": fallback, "heldout_evaluated": False,
                 "evidence_scope": info["evidence_scope"], "timeout_seconds": timeout_seconds,
                 "seconds": time.perf_counter() - start}
    write_json(run_dir / "selection.json", selection)
    return selection



def _execute_run(job, provider=None, *, api_config=None, stop_path=None):
    """Run one independent adaptive search; return only the small summary to the parent."""
    config, instances, probes, validation, out_dir, validation_limit = job
    arm, seed = config["arm"], config["seed"]
    name = f"{arm}_seed{seed}"
    started = time.perf_counter()
    try:
        if provider is None:
            provider = ApiProvider(api_config, allow_paid=True) if api_config else OfflineProvider(seed=seed)
        directory = out_dir / name
        if stop_path is None:
            info = run_search(instances, probes, provider, config, directory)
        else:
            info = run_search(instances, probes, provider, config, directory, stop_requested=stop_path.exists)
            if info["termination"] != "request_budget_completed":
                stop_path.touch(exist_ok=True)
        selection = select_validation(directory, validation, validation_limit)
        return {"run": name, "arm": arm, "seed": seed,
                "best_search_mean_bins": info["best_search_mean_bins"],
                "selected_validation_mean_bins": selection["selected"]["validation"]["mean_bins"],
                "metrics": info["metrics"], "termination": info["termination"],
                "pipeline_seconds": time.perf_counter() - started}
    except Exception:
        if stop_path is not None:
            stop_path.touch(exist_ok=True)
        raise

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["baselines", "search", "all"], default="all")
    parser.add_argument("--data", default="data")
    parser.add_argument("--out_dir", default="run_1")
    parser.add_argument("--provider", choices=["offline", "api"], default="offline")
    parser.add_argument("--api-config", default="configs/api.local.json")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--parallel-arms", action="store_true", help="Run independent arms in four processes; each arm stays adaptive")
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS))
    parser.add_argument("--seeds", nargs="+", type=int, default=[11, 22, 33])
    parser.add_argument("--calls", type=int, default=16)
    parser.add_argument("--islands", type=int, default=4)
    parser.add_argument("--reset-every", type=int, default=50)
    parser.add_argument("--audit-rate", type=float, default=0.1)
    parser.add_argument("--database-temperature", type=float, default=1.0)
    parser.add_argument("--enhanced-prompt-version", choices=ENHANCED_VERSIONS, default="enhanced_v1",
                        help="Explicitly select mechanism_v3 for the revised method; legacy default is retained")
    parser.add_argument("--collision-policy", choices=COLLISION_POLICIES, default="retain_representative")
    parser.add_argument("--evaluation-timeout-seconds", type=float, default=30)
    parser.add_argument("--validation-timeout-seconds", type=float, default=30)
    parser.add_argument("--protocol", help="Frozen formal protocol; prefer formal.py run")
    parser.add_argument("--time-probe-setup", action="store_true",
                        help="Rebuild and time the frozen development probes before a pilot, as in a formal run")
    args = parser.parse_args(argv)
    if args.calls < 1 or args.calls > 100000:
        parser.error("calls must be in [1,100000]")
    if len(set(args.arms)) != len(args.arms) or len(set(args.seeds)) != len(args.seeds):
        parser.error("duplicate arms or seeds are not permitted")
    import math
    if not all(math.isfinite(x) and x > 0 for x in (
            args.database_temperature, args.evaluation_timeout_seconds, args.validation_timeout_seconds)):
        parser.error("temperatures and timeouts must be finite and positive")
    if not 0 <= args.audit_rate <= 1 or args.islands < 1 or args.reset_every < 0:
        parser.error("invalid audit rate, island count or reset interval")
    out_dir = Path(args.out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        parser.error("use a NEW output directory; existing evidence will not be overwritten")
    # Preflight occurs before creating any experiment outputs. One API provider
    # spans every arm/seed so its cap is study-wide, not accidentally per run.
    study_started = time.perf_counter()
    api = protocol = None
    api_config = read_json(args.api_config) if args.provider == "api" else None
    data_dir = Path(args.data)
    if args.protocol:
        protocol = load_protocol(args.protocol, data_dir)
        validate_runtime(protocol, args, api_config)
    if args.provider == "api":
        if not api_config.get("ledger_path"):
            parser.error("paid API runs require a persistent ledger_path; see the CNY pilot example")
        api = ApiProvider(api_config, allow_paid=args.allow_paid)
        if protocol and api.config["max_requests"] - api.attempts < protocol["total_planned_requests"]:
            raise ValueError("ledger has insufficient remaining request slots for all formal runs; "
                             "do not reset the ledger or start an incomplete comparison")
    instances = load_instances(data_dir / "search.json")
    validation = load_instances(data_dir / "validation.json")
    probes = load_probes(data_dir / "probes.json")
    probe_construction_seconds = 0.0
    timed_setup = bool(protocol) or args.time_probe_setup
    if timed_setup:
        # Construct once from development, before any search. Charge this measured
        # cost to C/D; do not substitute a post-hoc timing for original setup.
        development = load_instances(data_dir / "development.json")
        started = time.perf_counter()
        rebuilt = _generate_probes(development)["states"]
        probe_construction_seconds = time.perf_counter() - started
        if digest(rebuilt) != digest(probes):
            raise ValueError("reconstructed probes differ from the frozen development probes")
    setup_seconds = time.perf_counter() - study_started
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"phase": "formal" if protocol else ("offline_integration" if args.provider == "offline" else "pilot"),
                "arguments": vars(args), "environment": environment(),
                "source_hashes": source_hashes(Path(__file__).parent),
                "search_hash": digest(instances), "validation_hash": digest(validation),
                "probe_hash": digest(probes), "heldout_evaluated": False,
                "runs": [], "note": "Offline fixtures cannot test the effect of prompts."}
    if protocol:
        manifest.update(protocol_id=protocol["protocol_id"],
                        core_source_hashes=core_source_hashes(Path(__file__).parent))
    manifest["timing"] = {
        "setup_seconds": setup_seconds,
        "probe_construction_seconds": probe_construction_seconds if timed_setup else None,
        "common_setup_seconds": setup_seconds - probe_construction_seconds,
        "allocation_rule": "common setup / planned runs; probe construction / planned screened runs",
        "scope": "per-run pipeline includes seeds, generation, screening, audits, validation and run logs; "
                 "study baseline report and top-level bookkeeping are measured separately",
    }
    if api is not None:
        manifest["api_config"] = {key: api.config[key] for key in (
            "base_url", "model", "expected_model_version", "api_key_env", "ledger_path", "temperature", "max_tokens", "timeout_seconds",
            "max_requests", "extra_body", "budget") if key in api.config}
        if protocol:
            manifest["api_config"] = public_api_config(api.config)
    write_json(out_dir / "study.json", manifest)
    if args.stage in ("baselines", "all"):
        started = time.perf_counter()
        result = baselines(data_dir, out_dir)
        manifest["timing"]["baseline_report_seconds"] = time.perf_counter() - started
        print("Baseline correctness and search-set comparison; held-out tests remain sealed.")
        for split, values in result["results"].items():
            print(f"{split}: FF={values['FF']['mean_bins']:.3f}, BF={values['BF']['mean_bins']:.3f} bins")
    if args.stage in ("search", "all"):
        common_share = manifest["timing"]["common_setup_seconds"] / (len(args.arms) * len(args.seeds))
        screened_runs = sum(ARMS[arm][1] for arm in args.arms) * len(args.seeds)
        pool = ProcessPoolExecutor(max_workers=len(args.arms)) if args.parallel_arms else None
        stop_path = out_dir / "STOP"
        try:
            for seed_index, seed in enumerate(args.seeds):
                order = args.arms[seed_index % len(args.arms):] + args.arms[:seed_index % len(args.arms)]
                jobs = []
                for arm in order:
                    config = {"arm": arm, "seed": seed, "calls": args.calls, "islands": args.islands,
                              "reset_every": args.reset_every, "audit_rate": args.audit_rate,
                              "temperature": args.database_temperature,
                              "enhanced_prompt_version": args.enhanced_prompt_version,
                              "collision_policy": args.collision_policy,
                              "evaluation_timeout_seconds": args.evaluation_timeout_seconds}
                    if protocol:
                        config["protocol_id"] = protocol["protocol_id"]
                    jobs.append((config, instances, probes, validation, out_dir, args.validation_timeout_seconds))
                print(f"Seed {seed}: {'parallel' if pool else 'sequential'} {', '.join(order)}; "
                      f"{args.calls} candidates per arm.", flush=True)
                if pool:
                    futures = [pool.submit(_execute_run, job, api_config=api_config, stop_path=stop_path) for job in jobs]
                    completed = (future.result() for future in as_completed(futures))
                else:
                    completed = (_execute_run(job, provider=api) for job in jobs)
                for entry in completed:
                    probe_share = (probe_construction_seconds / screened_runs if ARMS[entry["arm"]][1] and screened_runs else 0)
                    entry["timing"] = {"pipeline_seconds": entry.pop("pipeline_seconds"),
                                       "common_setup_share_seconds": common_share,
                                       "probe_construction_share_seconds": probe_share if timed_setup else None}
                    entry["timing"]["inclusive_search_seconds"] = (
                        entry["timing"]["pipeline_seconds"] + common_share + probe_share if timed_setup else None)
                    manifest["runs"].append(entry)
                    if entry["termination"] != "request_budget_completed":
                        manifest.setdefault("stop_reason", entry["termination"])
                    write_json(out_dir / "study.json", manifest)
                    print(f"{entry['run']}: {entry['termination']}; "
                          f"best search={entry['best_search_mean_bins']:.3f}, "
                          f"validation={entry['selected_validation_mean_bins']:.3f}", flush=True)
                    if not pool and manifest.get("stop_reason"):
                        break
                if manifest.get("stop_reason"):
                    break
        finally:
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
    manifest["timing"]["study_elapsed_seconds"] = time.perf_counter() - study_started
    write_json(out_dir / "study.json", manifest)
    print(f"Saved: {out_dir.resolve()}; no held-out test evaluation performed.")


if __name__ == "__main__":
    main()
