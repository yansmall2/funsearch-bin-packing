"""Preview prompt diagnostics; --low16 --run starts low-effort calibration.

Each of 32 saved parent contexts is used once with the original enhanced prompt
and once with the witness prompt. This is not an evolutionary/four-arm result.
Only existing search data are evaluated; no validation or held-out data loaded.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from collections import Counter
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from funpack.data import load_instances
from funpack.ledger import budget_signature
from funpack.packing import evaluate
from funpack.programs import BF_CODE, MAX_CODE_BYTES, Program, extract_response
from funpack.prompts import build_messages
from funpack.protocol import public_api_config, core_source_hashes
from funpack.providers import ApiProvider, BudgetExceeded
from funpack.storage import append_jsonl, digest, environment, read_json, write_json
from scripts.prompt_witness import (VARIANTS, check_witness, evaluate_candidate, messages_for,
                                    parse_response, reference_index, summarize)
from scripts.run_calibration_pilot import exclusive_json, inspect_ledger

PRIOR_RUN = "run_paid_calibration_20261106T175234Z"
PRIOR_LEDGER = "configs/deepseek_pilot_ledger.sqlite3"
ACTIVE_API = "configs/prompt64.local.json"
CLAIM = "configs/prompt64.claim.local.json"
LEDGER = "configs/prompt64_budget.sqlite3"
OUTPUT = "run_paid_prompt_diagnostic_20261107"
APPROVAL_FLAG = "--approve-64-calls-2-cny"
LOW_PATHS = {"active": "configs/low16.local.json", "claim": "configs/low16.claim.local.json",
             "ledger": "configs/low16_budget.sqlite3", "output": "run_paid_low16_20261109"}
LOW_VARIANTS = ("basic_v1", "mechanism_v3")


def prepare_low16(root, *, direct=False):
    """Predeclare four distinct saved contexts, without selecting by outcomes."""
    root = Path(root).resolve()
    config = copy.deepcopy(read_json(root / "configs/thinking200_retry1.local.json"))
    config.update(max_requests=16, ledger_path=LOW_PATHS["ledger"])
    config["extra_body"] = {"thinking": {"type": "enabled"}, "reasoning_effort": "low", "top_p": 1.0}
    envelope = 2 * MAX_CODE_BYTES * 6 + 16384 + 4096
    config["budget"]["max_cost"] = math.ceil(16 * (envelope * 2 + config["max_tokens"] * 8) / 1_000_000)
    config["budget"]["connection_route"] = "direct_for_api_host" if direct else "system_proxy_settings"
    if (config["model"] != "deepseek-flash" or config["base_url"] != "https://api.deepseek.com"
            or config["max_tokens"] != 32768 or config["budget"]["currency"] != "CNY"):
        raise ValueError("unexpected source model configuration; review before calibration")
    public_api_config(config)
    search = load_instances(root / "data/search.json")
    if read_json(root / PRIOR_RUN / "study.json")["search_hash"] != digest(search):
        raise ValueError("saved contexts belong to a different search set")
    unique, seen, evidence = [], set(), {}
    for arm in "ABCD":
        folder = root / PRIOR_RUN / (arm + "_seed11")
        measured = {r["id"]: r for r in jsonl(folder / "evaluated.jsonl")}
        for name in ("events.jsonl", "evaluated.jsonl"):
            path = folder / name
            evidence[path.relative_to(root).as_posix()] = sha(path)
        for event in sorted(jsonl(folder / "events.jsonl"), key=lambda e: e["request_index"]):
            parents = [measured[p] for p in event["parents"]]
            signature = digest([p["code"] for p in parents])
            if signature in seen:
                continue
            seen.add(signature)
            for parent in parents:
                if (digest(parent["code"]) != parent["code_hash"] or len(parent["counts"]) != len(search)
                        or sum(parent["counts"]) / len(search) != parent["mean_bins"]):
                    raise ValueError("inconsistent saved parent")
            unique.append({"source_arm": arm, "source_request": event["request_index"],
                           "parents": parents, "reference_index": reference_index(parents)})
            if len(unique) == 4:
                break
        if len(unique) == 4:
            break
    if len(unique) != 4:
        raise ValueError("four distinct measured contexts are required")
    contexts = [{**context, "context_index": 2 * i + repeat, "parent_context_index": i,
                 "repeat": repeat} for i, context in enumerate(unique) for repeat in range(2)]
    histories = {}
    for name in (PRIOR_LEDGER, "configs/prompt64_budget.sqlite3", "configs/thinking200_budget.sqlite3",
                 "configs/thinking200_retry1_budget.sqlite3"):
        histories[name] = inspect_ledger(root / name)
    names = list(LOW_PATHS.values()) + [LOW_PATHS["ledger"] + suffix for suffix in ("-wal", "-shm", "-journal")]
    blockers = [name for name in names if (root / name).exists()]
    preview = {"kind": "low_effort_fixed_parent_calibration", "ready_to_activate": not blockers,
               "blockers": blockers, "artifacts": LOW_PATHS, "total_requests": 16,
               "variants": list(LOW_VARIANTS), "requests_per_variant": 8, "unique_parent_contexts": 4,
               "context_selection": "First four distinct ordered parent-code contexts in saved A/B/C/D request order; two repeats each; no outcome-based selection.",
               "contexts_hash": digest(contexts), "search_hash": digest(search),
               "source_hashes": core_source_hashes(root), "runner_hash": sha(root / "scripts/run_prompt_diagnostic.py"),
               "saved_evidence_hashes": evidence, "prior_ledgers": histories,
               "prior_ledger_hashes": {name: sha(root / name) for name in histories},
               "api_config": public_api_config(config), "reasoning_effort": "low", "direct_connection": direct,
               "engineering_gate": {"valid_candidates_at_least": 15, "length_finishes_allowed": 0,
                                    "requires_all_16_requests": True},
               "gate_note": "Engineering triage only, not significance or a quality claim. No automatic four-arm run.",
               "screening_enabled": False, "validation_loaded": False, "heldout_loaded": False,
               "effective_temperature": None, "authorization": "User approved the proposed 16-request low-effort calibration.",
               "cost_note": "New independent token-envelope guard; prior unknown charges and pending reservations are retained, not cleared or treated as invoices."}
    return config, contexts, search, preview


def summarize_low16(events, stop=None):
    rows = []
    for variant in LOW_VARIANTS:
        samples = [e for e in events if e["variant"] == variant]
        valid = [e for e in samples if e["evaluation"].get("valid_search_evaluation")]
        rows.append({"variant": variant, "requests": len(samples), "valid_candidates": len(valid),
                     "format_ok": sum(e.get("parsed", {}).get("format_ok", False) for e in samples),
                     "provider_errors": sum(bool(e["response"].get("error")) for e in samples),
                     "finish_reasons": dict(Counter(e["response"].get("finish_reason") or "unknown" for e in samples)),
                     "search_novel_vs_bf": sum(e["evaluation"]["novel_vs_bf_on_search"] for e in valid),
                     "novel_and_no_worse_than_bf": sum(e["evaluation"]["novel_vs_bf_on_search"] and e["evaluation"]["mean_delta_vs_bf"] <= 0 for e in valid),
                     "best_generated_search_mean": min((e["evaluation"]["result"]["mean_bins"] for e in valid), default=None),
                     "generation_seconds": sum(e["generation_seconds"] for e in samples),
                     "median_generation_seconds": median(e["generation_seconds"] for e in samples) if samples else None,
                     "evaluation_seconds": sum(e["evaluation"].get("evaluation_seconds", 0) for e in samples),
                     "input_tokens": sum(e["response"].get("usage", {}).get("input_tokens") or 0 for e in samples),
                     "output_tokens": sum(e["response"].get("usage", {}).get("output_tokens") or 0 for e in samples),
                     "estimated_cost_cny": sum(e["response"].get("estimated_cost") or 0 for e in samples),
                     "unknown_cost_requests": sum(not e["response"].get("cost_known", False) for e in samples)})
    complete = (len(events) == 16 and all(row["requests"] == 8 for row in rows)
                and all({e["variant"] for e in events if e["context_index"] == i} == set(LOW_VARIANTS) for i in range(8)))
    return {"kind": "low_effort_fixed_parent_calibration", "complete": complete, "rows": rows,
            "engineering_gate_passed": complete and sum(r["valid_candidates"] for r in rows) >= 15
                and not any(e["response"].get("finish_reason") == "length" or e["response"].get("error") for e in events),
            "stop_reason": stop, "validation_used": False, "heldout_evaluated": False,
            "formal_comparison": False, "automatic_next_run": False}


def execute_low16(root, config, contexts, search, preview):
    if len(contexts) != 8 or sorted(c["context_index"] for c in contexts) != list(range(8)):
        raise ValueError("exactly eight paired contexts are required for the 16-request calibration")
    root = Path(root)
    out = root / LOW_PATHS["output"]
    out.mkdir()
    write_json(out / "diagnostic_plan.json", {"preview": preview, "contexts": contexts, "environment": environment()})
    tick = time.perf_counter()
    bf = evaluate(Program(BF_CODE), search)
    references = {}
    for context in contexts:
        for parent in context["parents"]:
            if parent["code_hash"] not in references:
                result = evaluate(Program(parent["code"]), search)
                if result["counts"] != parent["counts"] or result["trace_hashes"] != parent["trace_hashes"]:
                    raise ValueError("saved parent failed independent replay; no generation request sent")
                references[parent["code_hash"]] = result
    setup_seconds = time.perf_counter() - tick
    provider = ApiProvider(config, allow_paid=True)
    events, stop = [], None
    for context in contexts:
        index = context["context_index"]
        order = LOW_VARIANTS if index % 2 == 0 else tuple(reversed(LOW_VARIANTS))
        reference = context["parents"][context["reference_index"]]
        for variant in order:
            mode = "basic" if variant == "basic_v1" else "enhanced"
            event = {"context_index": index, "parent_context_index": context["parent_context_index"],
                     "repeat": context["repeat"], "variant": variant,
                     "messages": build_messages(mode, context["parents"], enhanced_version="mechanism_v3")}
            write_json(out / "pending_request.json", event)
            tick = time.perf_counter()
            try:
                response = provider.generate(event["messages"])
            except BudgetExceeded:
                stop = "cost_budget_guard_before_request"
                break
            event.update(response=response, generation_seconds=time.perf_counter() - tick)
            if response.get("error"):
                event["evaluation"] = {"status": "provider_error", "valid_search_evaluation": False}
                stop = "provider_error"
            else:
                event["parsed"] = extract_response(response.get("text", ""), mode)
                event["evaluation"] = evaluate_candidate(event["parsed"], references[reference["code_hash"]], bf, search)
            events.append(event)
            append_jsonl(out / "events.jsonl", event)
            write_json(out / "progress.json", summarize_low16(events, stop))
            (out / "pending_request.json").unlink(missing_ok=True)
            print(f"Low-effort {len(events)}/16 {variant}: {event['evaluation']['status']}; finish={response.get('finish_reason')}", flush=True)
            if stop:
                break
        if stop:
            break
    (out / "pending_request.json").unlink(missing_ok=True)
    report = {**summarize_low16(events, stop), "reference_setup_seconds": setup_seconds,
              "bf_search_mean": bf["mean_bins"]}
    write_json(out / "diagnostic_summary.json", report)
    return report


def main_low16(args, root):
    from scripts.run_thinking_pilot_200 import check_connection, connection_route
    root = Path(root).resolve()
    with connection_route(args.direct):
        config, contexts, search, preview = prepare_low16(root, direct=args.direct)
        if not args.run:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return preview
        if not preview["ready_to_activate"]:
            raise ValueError("calibration artifacts already exist; inspect rather than overwriting them")
        check_connection()
        env_name = config["api_key_env"]
        entered = not os.environ.get(env_name)
        claimed, old_cwd = False, Path.cwd()
        claim = {"status": "starting", "started_utc": datetime.now(timezone.utc).isoformat(), "authorized_scope": preview}
        try:
            if entered:
                if not sys.stdin.isatty():
                    raise ValueError("Run locally in an interactive terminal to enter the key; never send it to chat")
                key = getpass.getpass("DeepSeek API key (hidden; do not send to chat): ")
                if not key:
                    raise ValueError("empty key; nothing started")
                os.environ[env_name] = key
                del key
            if prepare_low16(root, direct=args.direct)[3] != preview:
                raise ValueError("source, configuration or evidence changed during key entry")
            exclusive_json(root / LOW_PATHS["claim"], claim)
            claimed = True
            exclusive_json(root / LOW_PATHS["active"], config)
            os.chdir(root)
            print("16-request low-effort calibration: basic vs mechanism_v3, fixed parents, full search evaluation. No final tests.", flush=True)
            report = execute_low16(root, config, contexts, search, preview)
            claim.update(status="completed" if report["complete"] else "stopped_incomplete", summary=report,
                         ledger_after=inspect_ledger(root / LOW_PATHS["ledger"]),
                         prior_ledgers_unchanged=all(sha(root / name) == value for name, value in preview["prior_ledger_hashes"].items()))
            write_json(root / LOW_PATHS["claim"], claim)
            print(f"Saved: {root / LOW_PATHS['output']}; gate_passed={report['engineering_gate_passed']}")
            return report
        except BaseException as error:
            if claimed:
                claim.update(status="interrupted", error_type=type(error).__name__)
                if (root / LOW_PATHS["ledger"]).is_file():
                    claim["ledger_after"] = inspect_ledger(root / LOW_PATHS["ledger"])
                write_json(root / LOW_PATHS["claim"], claim)
            raise
        finally:
            os.chdir(old_cwd)
            if entered:
                os.environ.pop(env_name, None)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def prepare(root):
    root = Path(root).resolve()
    old_config = read_json(root / "configs/calibration.local.json")
    public_api_config(old_config)
    expected = {"enabled": True, "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
                "max_requests": 144, "ledger_path": PRIOR_LEDGER, "temperature": 0.8,
                "max_tokens": 1024, "extra_body": {"thinking": {"type": "disabled"}}}
    if any(old_config.get(k) != v for k, v in expected.items()):
        raise ValueError("prior model/configuration changed; review before diagnostic")
    if any(old_config["budget"].get(k) != v for k, v in
           {"currency": "CNY", "max_cost": 10, "input_per_million": 2, "output_per_million": 8}.items()):
        raise ValueError("prior price assumptions differ; review before diagnostic")
    old_state = inspect_ledger(root / PRIOR_LEDGER)
    if (old_state["attempts"] != 144 or old_state["signature"] != budget_signature(old_config)
            or old_state["unknown_cost_requests"] or old_state["unresolved_reservations"]
            or read_json(root / "configs/calibration128.claim.local.json").get("status") != "completed"):
        raise ValueError("prior calibration must be complete and fully accounted for")
    config = copy.deepcopy(old_config)
    config.update(max_requests=64, ledger_path=LEDGER)
    config["budget"].update(currency="CNY", max_cost=2, input_per_million=2, output_per_million=8,
                            pricing_checked_date="2026-11-07")
    public_api_config(config)
    search = load_instances(root / "data/search.json")
    study = read_json(root / PRIOR_RUN / "study.json")
    if study["search_hash"] != digest(search):
        raise ValueError("search data changed since calibration")
    # All original core modules must still match. This diagnostic adds scripts,
    # without replacing the main search's prompts or screening logic.
    for name, actual in core_source_hashes(root).items():
        if actual != study["source_hashes"][name]:
            raise ValueError("original core changed; do not mix diagnostic contexts: " + name)
    contexts = []
    evidence_hashes = {}
    for arm in "ABCD":
        folder = root / PRIOR_RUN / (arm + "_seed11")
        events = jsonl(folder / "events.jsonl")
        measured = {r["id"]: r for r in jsonl(folder / "evaluated.jsonl")}
        for filename in ("events.jsonl", "evaluated.jsonl"):
            path = folder / filename
            evidence_hashes[path.relative_to(root).as_posix()] = sha(path)
        for request_index in range(1, 33, 4):
            event = next(e for e in events if e["request_index"] == request_index)
            parents = [measured[identifier] for identifier in event["parents"]]
            for parent in parents:
                if (digest(parent["code"]) != parent["code_hash"] or len(parent["counts"]) != len(search)
                        or sum(parent["counts"]) / len(search) != parent["mean_bins"]):
                    raise ValueError("inconsistent saved parent measurements")
            mode = "basic" if arm in "AC" else "enhanced"
            if build_messages(mode, parents) != event["messages"]:
                raise ValueError("saved parent messages differ from their reconstructed context")
            contexts.append({"context_index": len(contexts), "source_arm": arm,
                             "source_request": request_index, "parents": parents,
                             "reference_index": reference_index(parents)})
    assert len(contexts) == 32
    blockers = [name for name in (ACTIVE_API, CLAIM, LEDGER, LEDGER + "-wal", LEDGER + "-shm",
                                  LEDGER + "-journal", OUTPUT, "configs/formal4000.claim.local.json",
                                  "protocol-frozen-formal-20261107.json") if (root / name).exists()]
    preview = {"kind": "paired_fixed_parent_prompt_diagnostic", "ready_to_activate": not blockers,
               "blockers": blockers, "parent_contexts": len(contexts), "variants": list(VARIANTS),
               "new_request_limit": 64, "new_cost_limit_cny": 2, "prior_ledger": old_state,
               "combined_cost_limit_cny": old_state["accounted_cost_cny"] + 2,
               "context_selection": "Each arm's saved requests 1,5,9,13,17,21,25,29; no outcome-based selection.",
               "unique_parent_contexts": len({digest([p["code"] for p in c["parents"]]) for c in contexts}),
               "contexts_hash": digest(contexts), "search_hash": digest(search),
               "original_evidence_hashes": evidence_hashes, "api_config": public_api_config(config),
               "original_core_hashes": core_source_hashes(root),
               "diagnostic_source_hashes": {name: sha(root / name) for name in
                                            ("scripts/prompt_witness.py", "scripts/run_prompt_diagnostic.py")},
               "prior_ledger_sha256": sha(root / PRIOR_LEDGER),
               "validation_loaded": False, "heldout_loaded": False,
               "witness_is_filter": False, "automatic_formal_run": False,
               "limitations": "Fixed parent contexts may repeat; this checks response behavior, not evolution or final generalization."}
    return config, contexts, search, preview


def execute(root, config, contexts, search, preview):
    """Caller has authorized the batch and supplied a local key."""
    root = Path(root)
    out = root / OUTPUT
    out.mkdir()
    write_json(out / "diagnostic_plan.json", {"preview": preview, "contexts": contexts,
                                              "environment": environment()})
    # Compute reference behavior once per code, before requests. These costs are
    # shared setup, not hidden in a prompt variant's evaluation time.
    started = time.perf_counter()
    baseline = evaluate(Program(BF_CODE), search)
    references = {}
    for context in contexts:
        parent = context["parents"][context["reference_index"]]
        if parent["code_hash"] not in references:
            result = evaluate(Program(parent["code"]), search)
            if result["counts"] != parent["counts"] or result["trace_hashes"] != parent["trace_hashes"]:
                raise ValueError("saved parent does not replay on the search set")
            references[parent["code_hash"]] = result
    setup_seconds = time.perf_counter() - started
    write_json(out / "reference_replay.json", {"bf": baseline, "reference_count": len(references),
                                               "seconds": setup_seconds, "heldout_evaluated": False})
    provider = ApiProvider(config, allow_paid=True)
    events = []
    stop = None
    for context in contexts:
        index = context["context_index"]
        order = VARIANTS if index % 2 == 0 else tuple(reversed(VARIANTS))
        parent = context["parents"][context["reference_index"]]
        for variant in order:
            event = {"context_index": index, "variant": variant,
                     "messages": messages_for(variant, context["parents"]),
                     "reference_id": parent["id"], "reference_code_hash": parent["code_hash"]}
            write_json(out / "pending_request.json", event)
            tick = time.perf_counter()
            try:
                response = provider.generate(event["messages"])
            except BudgetExceeded:
                stop = "cost_budget_guard_before_request"
                write_json(out / "stopped_request.json", {**event, "request_sent": False, "reason": stop})
                (out / "pending_request.json").unlink(missing_ok=True)
                break
            event.update(response=response, generation_seconds=time.perf_counter() - tick)
            append_jsonl(out / "provider_responses.jsonl", event)
            if response.get("error"):
                event["evaluation"] = {"status": "provider_error", "valid_search_evaluation": False}
                raw = response.get("raw_response")
                if isinstance(raw, dict) and raw.get("http_status") in (401, 402, 403):
                    stop = "provider_auth_or_balance_error"
            else:
                parsed = parse_response(response.get("text", ""), variant)
                event["parsed"] = parsed
                tick = time.perf_counter()
                event["witness_check"] = (check_witness(parsed, parent["code"]) if variant == "witness_v2"
                                           else {"status": "not_requested", "verified_change": False})
                event["witness_check_seconds"] = time.perf_counter() - tick
                event["evaluation"] = evaluate_candidate(parsed, references[parent["code_hash"]], baseline, search)
            events.append(event)
            append_jsonl(out / "events.jsonl", event)
            write_json(out / "progress.json", {"completed_requests": len(events), "planned_requests": 64})
            (out / "pending_request.json").unlink(missing_ok=True)
            if stop:
                break
        print(f"Prompt context {index + 1}/{len(contexts)}; completed requests={len(events)}", flush=True)
        if stop:
            break
    report = summarize(events)
    report.update(stop_reason=stop, reference_setup_seconds=setup_seconds, bf_search_mean=baseline["mean_bins"])
    write_json(out / "diagnostic_summary.json", report)
    return report


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(APPROVAL_FLAG, action="store_true", help="Authorize at most64 new requests with a CNY2 diagnostic cap")
    parser.add_argument("--low16", action="store_true", help="Preview the approved 16-request low-effort calibration")
    parser.add_argument("--run", action="store_true", help="Start --low16 with hidden local key entry")
    parser.add_argument("--direct", action="store_true", help="Bypass proxies for the API host in this process only")
    args = parser.parse_args(argv)
    if args.low16:
        if args.approve_64_calls_2_cny:
            raise ValueError("choose either low16 or the historical64 diagnostic")
        return main_low16(args, root)
    if args.run or args.direct:
        raise ValueError("--run and --direct require --low16")
    root = Path(root).resolve()
    config, contexts, search, preview = prepare(root)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.approve_64_calls_2_cny:
        print("Preview only: no API calls, key prompt, active config, claim or ledger created.")
        return preview
    if not preview["ready_to_activate"]:
        raise ValueError("diagnostic/formal artifacts already exist; inspect rather than deleting them")
    env_name = config["api_key_env"]
    entered = not os.environ.get(env_name)
    if entered:
        if not sys.stdin.isatty():
            raise RuntimeError("run in an interactive terminal for hidden key entry")
        key = getpass.getpass("输入 DeepSeek API 密钥（不会显示；不要发到聊天）：")
        if not key:
            raise ValueError("empty key; no request or state change")
        os.environ[env_name] = key
        del key
    old_cwd, claimed = Path.cwd(), False
    started = time.perf_counter()
    claim = {"proposal": preview, "approval": "explicit " + APPROVAL_FLAG,
             "claimed_utc": datetime.now(timezone.utc).isoformat(), "status": "claimed"}
    try:
        os.chdir(root)
        current = prepare(root)
        if current[3] != preview:
            raise ValueError("configuration or evidence changed during key entry")
        exclusive_json(root / CLAIM, claim)
        claimed = True
        exclusive_json(root / ACTIVE_API, config)
        report = execute(root, config, contexts, search, preview)
        claim.update(status="completed" if report["complete"] else "stopped_incomplete",
                     ledger_after=inspect_ledger(root / LEDGER),
                     prior_ledger_unchanged=sha(root / PRIOR_LEDGER) == preview["prior_ledger_sha256"],
                     workflow_seconds=time.perf_counter() - started)
        write_json(root / CLAIM, claim)
        print(f"Diagnostic {claim['status']}: {root / OUTPUT}. No formal run or final testing started.")
        return report
    except BaseException as error:
        if claimed:
            claim.update(status="interrupted", error_type=type(error).__name__,
                         workflow_seconds=time.perf_counter() - started)
            if (root / LEDGER).is_file():
                try:
                    claim["ledger_after"] = inspect_ledger(root / LEDGER)
                except Exception:
                    claim["ledger_requires_inspection"] = True
            write_json(root / CLAIM, claim)
        raise
    finally:
        os.chdir(old_cwd)
        if entered:
            os.environ.pop(env_name, None)


if __name__ == "__main__":
    main()
