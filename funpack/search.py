"""One logged search run. Search data only; held-out data never loaded here."""
from __future__ import annotations

import random
import time
from pathlib import Path

from .database import Database
from .packing import evaluate
from .programs import BF_CODE, FF_CODE, CODE_LANGUAGE_VERSION, Program, extract_response
from .prompts import build_messages
from .providers import BudgetExceeded
from .screening import BehavioralScreen
from .storage import append_jsonl, digest, environment, source_hashes, write_json

ARMS = {"A": ("basic", False), "B": ("enhanced", False),
        "C": ("basic", True), "D": ("enhanced", True)}
EVALUATOR_VERSION = "feasible-opened-only_max-first_v1"


def run_search(instances, probes, provider, config, out_dir):
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"Refusing to overwrite run directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    arm = config["arm"]
    mode, screening = ARMS[arm]
    calls, seed = config["calls"], config["seed"]
    audit_rate = config.get("audit_rate", 0.1)
    if type(calls) is not int or calls < 1 or not 0 <= audit_rate <= 1:
        raise ValueError("calls must be positive and audit_rate in [0,1]")
    timer = time.perf_counter()
    rng_audit = random.Random(seed + 1_000_003)
    db = Database(seed, config.get("islands", 4), config.get("temperature", 1.0))
    screen = BehavioralScreen(probes) if screening else None
    source_snapshot = source_hashes(Path(__file__).parent)
    context = digest({"search_data": instances, "language": CODE_LANGUAGE_VERSION,
                      "evaluator": EVALUATOR_VERSION, "source_hashes": source_snapshot,
                      "evaluation_timeout_seconds": config.get("evaluation_timeout_seconds", 30)})
    provenance = {"config": config, "environment": environment(), "search_context_hash": context,
                  "search_hash": digest(instances),
                  "probe_hash": digest(probes), "source_hashes": source_snapshot,
                  "dataset_role": "search", "heldout_evaluated": False,
                  "provider_class": type(provider).__name__,
                  "evidence_scope": ("offline_fixture_plumbing_only" if type(provider).__name__ == "OfflineProvider"
                                     else "formal_comparison" if config.get("protocol_id")
                                     else "llm_pilot_unless_frozen")}
    write_json(out_dir / "run_manifest.json", provenance)
    records, cache = [], {}
    metrics = {"generator_requests": 0, "requests_attempted": 0, "paid_api_requests": 0,
               "candidate_responses": 0, "code_extracted": 0,
               "valid_on_probes_or_search": 0, "format_violations": 0, "text_cache_hits": 0,
               "behavior_matches": 0, "behavior_skips": 0, "audits": 0,
               "audit_count_mismatches": 0, "audit_trace_mismatches": 0,
               "audit_evaluation_errors": 0, "probe_registration_errors": 0, "probe_errors": 0,
               "seed_full_evaluations": 0, "candidate_full_evaluations": 0,
               "successful_candidate_evaluations": 0, "invalid_candidates": 0,
               "generation_seconds": 0.0, "screening_seconds": 0.0, "evaluation_seconds": 0.0,
               "input_tokens_reported": 0, "output_tokens_reported": 0,
               "input_tokens_unknown_requests": 0, "output_tokens_unknown_requests": 0,
               "estimated_cost_reported": 0.0, "unknown_cost_requests": 0}

    def full_record(code, program, label):
        started = time.perf_counter()
        deadline = started + config.get("evaluation_timeout_seconds", 30)
        def bounded(item, bins):
            if time.perf_counter() > deadline:
                raise RuntimeError("full evaluation time budget exceeded")
            return program(item, bins)
        try:
            outcome = evaluate(bounded, instances)
            if time.perf_counter() > deadline:
                raise RuntimeError("full evaluation time budget exceeded")
        finally:
            metrics["evaluation_seconds"] += time.perf_counter() - started
        record = {"id": label, "code": code, "code_hash": digest(code), **outcome}
        records.append(record)
        cache[(context, code)] = record
        append_jsonl(out_dir / "evaluated.jsonl", record)
        return record

    for name, code in (("seed_ff", FF_CODE), ("seed_bf", BF_CODE)):
        program = Program(code)
        metrics["seed_full_evaluations"] += 1
        record = full_record(code, program, name)
        db.add(record)
        if screen:
            started = time.perf_counter()
            screen.register(program, record)
            metrics["screening_seconds"] += time.perf_counter() - started

    termination = "request_budget_completed"
    for request_index in range(1, calls + 1):
        island, parents = db.parents()
        messages = build_messages(mode, parents)
        event = {"request_index": request_index, "arm": arm, "island": island,
                 "parents": [p["id"] for p in parents], "messages": messages}
        # A pending record survives interruption without silently losing an attempted request.
        write_json(out_dir / "pending_request.json", event)
        started = time.perf_counter()
        try:
            response = provider.generate(messages)
        except BudgetExceeded as error:
            termination = "cost_budget_guard"
            event.update(status="not_sent_budget_guard", error=str(error))
            append_jsonl(out_dir / "events.jsonl", event)
            (out_dir / "pending_request.json").unlink(missing_ok=True)
            break
        metrics["generation_seconds"] += time.perf_counter() - started
        metrics["generator_requests"] += 1
        metrics["requests_attempted"] += int(response.get("request_attempted", True))
        metrics["paid_api_requests"] += int(response.get("paid_api_request", False))
        event["response"] = response
        # Preserve a paid response before any candidate parsing/evaluation can fail.
        append_jsonl(out_dir / "provider_responses.jsonl", event)
        usage = response.get("usage") or {}
        metrics["input_tokens_reported"] += usage.get("prompt_tokens", 0) or 0
        metrics["output_tokens_reported"] += usage.get("completion_tokens", 0) or 0
        metrics["input_tokens_unknown_requests"] += int(not usage.get("input_tokens_known", True))
        metrics["output_tokens_unknown_requests"] += int(not usage.get("output_tokens_known", True))
        metrics["estimated_cost_reported"] += response.get("estimated_cost", 0) or 0
        metrics["unknown_cost_requests"] += int(not response.get("cost_known", False))
        prefix = tail = representative = None
        audit = False
        if response.get("error"):
            event["status"] = "provider_error"
            raw_response = response.get("raw_response")
            status = raw_response.get("http_status") if isinstance(raw_response, dict) else None
            if status in (401, 402, 403):
                termination = "provider_auth_or_balance_error"
        else:
            metrics["candidate_responses"] += 1
            parsed = extract_response(response.get("text", ""), mode)
            event["parsed"] = parsed
            metrics["format_violations"] += int(not parsed["format_ok"])
            code = parsed.get("code")
            if not code:
                event["status"] = "extraction_failed"
                metrics["invalid_candidates"] += 1
            else:
                metrics["code_extracted"] += 1
                try:
                    key = (context, code)
                    if key in cache:
                        metrics["text_cache_hits"] += 1
                        metrics["valid_on_probes_or_search"] += 1
                        event.update(status="text_cache_hit", representative=cache[key]["id"])
                        # Exact measured result can enter this island without re-evaluation.
                        db.add(cache[key], island)
                    else:
                        program = Program(code)
                        if screen:
                            started = time.perf_counter()
                            try:
                                representative, prefix, tail = screen.probe(program)
                            except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
                                # A probe failure does not establish invalidity on the search set.
                                # Evaluate rather than silently filtering it from screened arms.
                                metrics["probe_errors"] += 1
                                event["probe_error"] = f"{type(error).__name__}: {error}"
                                representative, prefix, tail = None, None, None
                            finally:
                                metrics["screening_seconds"] += time.perf_counter() - started
                        audit = False
                        if representative:
                            metrics["behavior_matches"] += 1
                            audit = rng_audit.random() < audit_rate
                            event.update(representative=representative["id"], audited=audit)
                        if representative and not audit:
                            metrics["valid_on_probes_or_search"] += 1
                            metrics["behavior_skips"] += 1
                            # Never invent a full score or insert this unmeasured program.
                            event["status"] = "behavior_skip"
                        else:
                            metrics["candidate_full_evaluations"] += 1
                            metrics["audits"] += int(audit)
                            record = full_record(code, program, f"candidate_{request_index:06d}")
                            metrics["valid_on_probes_or_search"] += 1
                            metrics["successful_candidate_evaluations"] += 1
                            db.add(record, island)
                            event.update(status="audit_evaluated" if audit else "evaluated",
                                         result={k: v for k, v in record.items() if k != "code"})
                            if audit:
                                count_mismatch = record["counts"] != representative["counts"]
                                trace_mismatch = record["trace_hashes"] != representative["trace_hashes"]
                                metrics["audit_count_mismatches"] += int(count_mismatch)
                                metrics["audit_trace_mismatches"] += int(trace_mismatch)
                                event.update(audit_count_mismatch=count_mismatch, audit_trace_mismatch=trace_mismatch)
                            if screen:
                                started = time.perf_counter()
                                try:
                                    screen.register(program, record, prefix, tail)
                                except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
                                    metrics["probe_registration_errors"] += 1
                                    event["probe_registration_error"] = f"{type(error).__name__}: {error}"
                                    event["status"] += "_probe_registration_error"
                                finally:
                                    metrics["screening_seconds"] += time.perf_counter() - started
                except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
                    metrics["invalid_candidates"] += 1
                    if audit:
                        metrics["audit_evaluation_errors"] += 1
                    event.update(status="audit_evaluation_error" if audit else "candidate_error",
                                 error=f"{type(error).__name__}: {error}")
        interval = config.get("reset_every", 50)
        if interval and request_index % interval == 0:
            event["reset_islands"] = db.reset()
        event["best_search_mean_bins"] = min(record["mean_bins"] for record in records)
        append_jsonl(out_dir / "events.jsonl", event)
        write_json(out_dir / "progress.json", {"request_index": request_index, "metrics": metrics,
                                               "best_search_mean_bins": event["best_search_mean_bins"]})
        (out_dir / "pending_request.json").unlink(missing_ok=True)
        if termination != "request_budget_completed":
            break

    best = sorted(records, key=lambda r: (r["mean_bins"], len(r["code"]), r["code_hash"]))[:5]
    metrics["search_total_seconds"] = time.perf_counter() - timer
    metrics["full_evaluations_including_seeds"] = metrics["seed_full_evaluations"] + metrics["candidate_full_evaluations"]
    summary = {**provenance, "termination": termination, "metrics": metrics,
               "top_search_candidates": best, "best_search_mean_bins": best[0]["mean_bins"],
               "not_a_test_result": True}
    write_json(out_dir / "final_info.json", summary)
    return summary
