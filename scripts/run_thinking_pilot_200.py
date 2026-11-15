"""4 x 50 thinking-mode pilot. Use --low for the calibrated batch; add --run to start.

One seed, not a formal multi-seed result. Dedicated ledger; no automatic retry,
resume, formal activation or held-out evaluation. Enter the key locally only.
"""
import argparse
import copy
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing, contextmanager
from urllib import error as urlerror, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from funpack.data import load_instances, load_probes
from funpack.ledger import budget_signature
from funpack.programs import MAX_CODE_BYTES
from funpack.protocol import core_source_hashes, public_api_config
from funpack.storage import digest, read_json, write_json
from scripts.run_calibration_pilot import exclusive_json, inspect_ledger

ACTIVE = "configs/thinking200.local.json"
CLAIM = "configs/thinking200.claim.local.json"
LEDGER = "configs/thinking200_budget.sqlite3"
OUTPUT = "run_paid_thinking200_20261109"
PRIOR = (("configs/calibration.local.json", "configs/calibration128.claim.local.json", 144),
         ("configs/prompt64.local.json", "configs/prompt64.claim.local.json", 64))
CALLS = 50
SEED = 11


def artifact_paths(retry=False, *, low=False):
    if low:
        if retry:
            raise ValueError("the low-effort batch is separate from the high-effort network retry")
        return {"active": "configs/low200.local.json", "claim": "configs/low200.claim.local.json",
                "ledger": "configs/low200_budget.sqlite3", "output": "run_paid_low200_20261115"}
    if not retry:
        return {"active": ACTIVE, "claim": CLAIM, "ledger": LEDGER, "output": OUTPUT}
    return {"active": "configs/thinking200_retry1.local.json",
            "claim": "configs/thinking200_retry1.claim.local.json",
            "ledger": "configs/thinking200_retry1_budget.sqlite3",
            "output": "run_paid_thinking200_retry1_20261109"}


@contextmanager
def connection_route(direct):
    """Bypass proxies for this API host in this process only; retain TLS checks."""
    previous = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    if direct:
        for key, value in previous.items():
            os.environ[key] = (value + "," if value else "") + "api.deepseek.com"
    try:
        yield
    finally:
        if direct:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def check_connection():
    """Credential-free TLS preflight before reserving any candidate requests."""
    try:
        with request.urlopen("https://api.deepseek.com/models", timeout=12) as response:
            if response.status != 200:
                raise ValueError("API connectivity preflight returned HTTP " + str(response.status))
    except urlerror.HTTPError as error:
        if error.code != 401:
            raise ValueError("API connectivity preflight returned HTTP " + str(error.code)) from None
    except (urlerror.URLError, OSError) as error:
        raise ValueError("API connectivity preflight failed; no generation request sent: " + str(error)) from None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def calibrated_low(root, histories):
    """Reuse the completed low16 settings and recheck its gate against saved results."""
    from scripts.run_prompt_diagnostic import LOW_PATHS, summarize_low16
    config = read_json(root / LOW_PATHS["active"])
    claim = read_json(root / LOW_PATHS["claim"])
    scope = claim["authorized_scope"]
    out = root / LOW_PATHS["output"]
    summary = read_json(out / "diagnostic_summary.json")
    events_path = out / "events.jsonl"
    with events_path.open(encoding="utf-8") as stream:
        check = summarize_low16([json.loads(line) for line in stream if line.strip()], summary.get("stop_reason"))
    if (claim.get("status") != "completed" or claim.get("summary") != summary or not check["engineering_gate_passed"]
            or check["stop_reason"] is not None or any(summary.get(k) != v for k, v in check.items())):
        raise ValueError("low16 must be complete and pass its recorded generation gate")
    if (public_api_config(config) != scope["api_config"] or config["model"] != "deepseek-flash"
            or config["base_url"] != "https://api.deepseek.com" or config["ledger_path"] != LOW_PATHS["ledger"]
            or config["max_requests"] != 16 or config["extra_body"] != {
                "thinking": {"type": "enabled"}, "reasoning_effort": "low", "top_p": 1.0}):
        raise ValueError("low16 model configuration differs from the recorded calibration")
    # Keep every earlier ledger, including the interrupted high-effort request.
    # Only fixed project paths may enter the read-only history comparison.
    previous = set(histories) | {LEDGER, artifact_paths(retry=True)["ledger"]}
    if previous != set(scope["prior_ledgers"]):
        raise ValueError("low16 has a different ledger history")
    for name in sorted(previous):
        state = inspect_ledger(root / name)
        if state != scope["prior_ledgers"][name] or sha(root / name) != scope["prior_ledger_hashes"][name]:
            raise ValueError("prior ledger changed since low16: " + name)
        histories[name] = state
    state = inspect_ledger(root / LOW_PATHS["ledger"])
    if (state != claim["ledger_after"] or state["attempts"] != 16 or state["signature"] != budget_signature(config)
            or state["unknown_cost_requests"] or state["unresolved_reservations"]):
        raise ValueError("low16 ledger does not match the completed 16 requests")
    histories[LOW_PATHS["ledger"]] = state
    return config, {"run": LOW_PATHS["output"], "valid": sum(r["valid_candidates"] for r in check["rows"]),
                    "requests": 16, "events_sha256": sha(events_path), "search_hash": scope["search_hash"]}


def amounts_by_status(root, names):
    """Separate token-based estimates from unresolved charges (ledger units = 1e-12 CNY)."""
    totals = Counter()
    for name in names:
        with closing(sqlite3.connect((root / name).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for status, units in db.execute("SELECT status, SUM(accounted_units) FROM funpack_requests GROUP BY status"):
                totals[status] += units
    return {status: totals[status] / 1e12 for status in ("known", "unknown", "reserved")}


def prepare(root, *, retry=False, direct=False, low=False):
    root = Path(root).resolve()
    paths = artifact_paths(retry, low=low)
    histories = {}
    for config_name, claim_name, count in PRIOR:
        prior_config = read_json(root / config_name)
        public_api_config(prior_config)
        ledger_name = prior_config["ledger_path"]
        expected = "configs/deepseek_pilot_ledger.sqlite3" if count == 144 else "configs/prompt64_budget.sqlite3"
        if ledger_name != expected or prior_config["budget"]["currency"] != "CNY":
            raise ValueError("unexpected prior ledger or currency")
        state = inspect_ledger(root / ledger_name)
        if (read_json(root / claim_name).get("status") != "completed" or state["attempts"] != count
                or state["signature"] != budget_signature(prior_config)
                or state["unresolved_reservations"] or state["unknown_cost_requests"]):
            raise ValueError("prior phases must be complete and accounted for")
        histories[ledger_name] = state
    if retry:
        old_config = read_json(root / ACTIVE)
        old_claim = read_json(root / CLAIM)
        old_state = inspect_ledger(root / LEDGER)
        if (old_config.get("ledger_path") != LEDGER or old_state["attempts"] != 200
                or old_state["signature"] != budget_signature(old_config)
                or old_state["unresolved_reservations"]
                or old_claim.get("status") not in ("completed_with_accounting_review", "finished_without_usable_generation")):
            raise ValueError("retry is only for the recorded 200-request network-failed batch")
        for arm in "ABCD":
            folder = root / OUTPUT / (arm + "_seed11")
            info = read_json(folder / "final_info.json")
            events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            if (len(events) != 50 or info["metrics"]["candidate_responses"] != 0
                    or any(e.get("response", {}).get("error", {}).get("type") != "network_error" for e in events)):
                raise ValueError("the original batch is not entirely network-failed; inspect before retrying")
        histories[LEDGER] = old_state
    calibration = None
    if low:
        config, calibration = calibrated_low(root, histories)
    else:
        config = copy.deepcopy(read_json(root / PRIOR[0][0]))
        config.update(enabled=True, model="deepseek-flash", base_url="https://api.deepseek.com",
                      expected_model_version="DeepSeek-V4.1-Flash-1017", max_tokens=32768, timeout_seconds=180,
                      temperature=0.8, extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high", "top_p": 1.0})
    config.update(max_requests=200, ledger_path=paths["ledger"])
    # The user removed the previous small monetary constraint. Keep a finite
    # engineering guard sized for the entire authorized request/token envelope,
    # not the old CNY2/5/10 ceiling. This is NOT an expected bill or billing guarantee.
    input_envelope = 2 * MAX_CODE_BYTES * 6 + 16384 + 4096
    guard = math.ceil(200 * (input_envelope * 2 + config["max_tokens"] * 8) / 1_000_000)
    config["budget"].update(currency="CNY", max_cost=guard, input_per_million=2, output_per_million=8,
                            connection_route="direct_for_api_host" if direct else "system_proxy_settings",
                            pricing_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                            pricing_checked_date="2026-11-15" if low else "2026-11-09")
    public_api_config(config)
    plan = read_json(root / "configs/formal-plan.proposed.json")
    if plan.get("enhanced_prompt_version") != "mechanism_v3" or plan.get("collision_policy") != "disable_on_mismatch_v1":
        raise ValueError("approved revised method is not installed")
    data = {name: digest(load_instances(root / "data" / (name + ".json")))
            for name in ("development", "search", "validation")}
    data["probes"] = digest(load_probes(root / "data/probes.json"))
    if low and calibration["search_hash"] != data["search"]:
        raise ValueError("search set differs from the low16 calibration")
    prior_amounts = amounts_by_status(root, histories)
    ledger_path = paths["ledger"]
    blockers = [name for name in (paths["active"], paths["claim"], ledger_path, ledger_path+"-wal", ledger_path+"-shm", ledger_path+"-journal", paths["output"])
                if (root / name).exists()]
    preview = {"kind": "thinking_four_arm_pilot", "ready_to_start": not blockers, "blockers": blockers,
               "artifacts": paths, "retry_of_network_failed_batch": retry, "direct_connection": direct,
               "arms": list("ABCD"), "calls_per_arm": CALLS, "total_requests": 200, "seeds": [SEED],
               "enhanced_prompt_version": "mechanism_v3", "collision_policy": "disable_on_mismatch_v1",
               "api_config": public_api_config(config), "data_hashes": data,
               "source_hashes": core_source_hashes(root), "runner_hash": sha(root / "scripts/run_thinking_pilot_200.py"),
               "prior_ledgers": histories, "prior_ledger_hashes": {n: sha(root/n) for n in histories},
               "prior_requests": sum(s["attempts"] for s in histories.values()),
               "prior_estimated_cost_cny": sum(s["accounted_cost_cny"] for s in histories.values()),
               "prior_unknown_cost_requests": sum(s["unknown_cost_requests"] for s in histories.values()),
               "prior_unknown_reservations_cny": prior_amounts["unknown"],
               "prior_amounts_cny_by_ledger_status": prior_amounts,
               "effective_temperature": None, "temperature_note": "ignored by DeepSeek in thinking mode",
               "reasoning_effort": config["extra_body"]["reasoning_effort"], "request_timeout_seconds": config["timeout_seconds"],
               "money_guard_note": "Token-envelope engineering guard, not a price forecast. The old small pilot cap does not apply.",
               "input_token_envelope_for_guard": input_envelope,
               "heldout_loaded": False, "automatic_formal_run": False,
               "authorization": "User requested thinking mode and 50 requests per arm; earlier 100-per-arm instruction superseded.",
               "limitations": "One seed and a changed model mode; cannot isolate a thinking-mode effect against older runs or claim formal multi-seed results."}
    if low:
        preview.update(calibration=calibration, authorization="Approved low-effort pilot: 50 requests per arm, seed 11.",
                       islands=4, reset_every=50, audit_rate=0.1, probe_stages=[32, 96], time_probe_setup=True)
    return config, preview


def summarize(root, preview):
    paths = preview.get("artifacts", artifact_paths())
    study = read_json(root / paths["output"] / "study.json")
    entries = []
    for run in study["runs"]:
        events = [json.loads(s) for s in (root/paths["output"]/run["run"]/"events.jsonl").read_text(encoding="utf-8").splitlines()]
        reasons = Counter(e["response"].get("finish_reason") or "unknown" for e in events if "response" in e)
        thinking_present = 0
        for event in events:
            raw = event.get("response", {}).get("raw_response") or {}
            raw = raw if isinstance(raw, dict) else {}
            choices = raw.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                thinking_present += isinstance(message, dict) and bool(message.get("reasoning_content"))
        entries.append({"arm": run["arm"], "seed": run["seed"], "metrics": run["metrics"],
                        "provider_errors": sum(bool(e.get("response", {}).get("error")) for e in events),
                        "best_search_mean_bins": run["best_search_mean_bins"],
                        "selected_validation_mean_bins": run["selected_validation_mean_bins"],
                        "finish_reasons": dict(reasons), "responses_with_reasoning_content": thinking_present,
                        "returned_models": sorted({e["response"].get("model") or "unknown" for e in events if "response" in e}),
                        "termination": run["termination"]})
        if preview.get("reasoning_effort") == "low":
            folder = root / paths["output"] / run["run"]
            entries[-1]["exploration"] = measured_exploration(folder, events)
            entries[-1]["inclusive_seconds"] = run["timing"]["inclusive_search_seconds"]
            entries[-1]["returned_system_fingerprints"] = sorted({
                raw["system_fingerprint"] for e in events
                if isinstance(raw := e.get("response", {}).get("raw_response"), dict)
                and isinstance(raw.get("system_fingerprint"), str)})
    ledger = inspect_ledger(root / paths["ledger"])
    complete = (len(entries) == 4 and {r["arm"] for r in entries} == set("ABCD")
                and all(r["termination"] == "request_budget_completed" and r["metrics"]["paid_api_requests"] == 50 for r in entries)
                and ledger["attempts"] == 200)
    return {"kind": "thinking_four_arm_pilot", "reasoning_effort": preview.get("reasoning_effort"), "complete": complete, "rows": entries, "ledger": ledger,
            "all_arms_have_usable_candidates": len(entries) == 4 and all(r["metrics"].get("valid_on_probes_or_search", 0) > 0 for r in entries),
            "accounting_complete": not ledger["unknown_cost_requests"] and not ledger["unresolved_reservations"],
            "new_requests": ledger["attempts"], "new_estimated_cost_cny": ledger["accounted_cost_cny"],
            "cumulative_requests": preview["prior_requests"] + ledger["attempts"],
            "cumulative_estimated_cost_cny": preview["prior_estimated_cost_cny"] + ledger["accounted_cost_cny"],
            "prior_unknown_cost_requests": preview.get("prior_unknown_cost_requests", 0),
            "prior_unknown_reservations_cny": preview.get("prior_unknown_reservations_cny", 0),
            "prior_amounts_cny_by_ledger_status": preview.get("prior_amounts_cny_by_ledger_status"),
            "new_amounts_cny_by_ledger_status": amounts_by_status(root, [paths["ledger"]]),
            "accounting_note": "Recorded totals include unknown and still-reserved amounts. Only the 'known' amount is based on token usage; none is a provider invoice.",
            "heldout_evaluated": False, "formal_comparison": False,
            "warning": "Completion means the requested calls finished, not that candidates are valid or the method improved. Inspect truncations, errors and per-arm results."}


def measured_exploration(folder, events):
    """Use only full scores or exact-text cache hits; a probe skip has no search score."""
    with (folder / "evaluated.jsonl").open(encoding="utf-8") as stream:
        records = {r["id"]: r for r in map(json.loads, filter(str.strip, stream))}
    bf = records["seed_bf"]
    measured, novel_no_worse, reused = [], 0, set()
    qualifying_ids = set()
    for event in events:
        if "response" not in event:
            continue
        if not event["response"].get("error"):
            reused.update(qualifying_ids.intersection(event.get("parents", ())))
        outcome = (records[event["representative"]] if event["status"] == "text_cache_hit"
                   else event.get("result"))
        if outcome is None:
            continue
        measured.append(outcome["mean_bins"])
        if outcome["trace_hashes"] != bf["trace_hashes"] and outcome["mean_bins"] <= bf["mean_bins"]:
            novel_no_worse += 1
            if not outcome["id"].startswith("seed_"):
                qualifying_ids.add(outcome["id"])
    return {"requests_with_known_search_score": len(measured),
            "best_known_generated_mean_bins": min(measured, default=None),
            "different_trace_and_no_worse_than_bf_requests": novel_no_worse,
            "different_trace_and_no_worse_nonseed_ids": sorted(qualifying_ids),
            "of_these_ids_used_later_as_parent": sorted(reused),
            "unscored_behavior_skips": sum(e["status"] == "behavior_skip" for e in events)}


def execute(args, *, root=ROOT):
    root = Path(root).resolve()
    opts = dict(retry=args.retry_network_failure, direct=args.direct, low=args.low)
    config, preview = prepare(root, **opts)
    paths = preview["artifacts"]
    if not args.run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return preview
    if not preview["ready_to_start"]:
        raise ValueError("batch already claimed or outputs exist; do not reset/delete evidence: " + ", ".join(preview["blockers"]))
    check_connection()
    if args.low:
        print(f"DeepSeek low | A/B/C/D x50 | seed 11 | output: {paths['output']}", flush=True)
    env_name = config["api_key_env"]
    entered = False
    if not os.environ.get(env_name):
        if not sys.stdin.isatty():
            raise ValueError("Run locally in an interactive terminal to enter the API key; never send it to chat")
        secret = getpass.getpass("输入 DeepSeek API 密钥（不会显示；不要发到聊天）：")
        if not secret:
            raise ValueError("empty key; nothing started")
        os.environ[env_name] = secret
        del secret
        entered = True
    old_cwd, claimed, start = Path.cwd(), False, time.perf_counter()
    claim = {"status": "starting", "authorized_scope": preview, "started_utc": datetime.now(timezone.utc).isoformat()}
    try:
        current, checked = prepare(root, **opts)
        if current != config or checked != preview:
            raise ValueError("source, data or prior evidence changed during key entry")
        exclusive_json(root / paths["claim"], claim)
        claimed = True
        exclusive_json(root / paths["active"], config)
        os.chdir(root)
        import experiment
        print(f"Thinking-mode pilot ({preview['reasoning_effort']}): A/B/C/D x50, seed11. Final tests remain sealed.", flush=True)
        experiment.main(["--provider", "api", "--allow-paid", "--api-config", paths["active"],
                         "--stage", "all", "--data", "data", "--out_dir", paths["output"],
                         "--arms", "A", "B", "C", "D", "--seeds", str(SEED), "--calls", str(CALLS),
                         "--islands", "4", "--reset-every", "50", "--audit-rate", "0.1",
                         "--enhanced-prompt-version", "mechanism_v3", "--collision-policy", "disable_on_mismatch_v1"]
                        + (["--time-probe-setup"] if args.low else []))
        report = summarize(root, preview)
        write_json(root / paths["output"] / "thinking_summary.json", report)
        import audit_and_cost
        write_json(root / paths["output"] / "audit_and_cost.json", audit_and_cost.analyze(root / paths["output"], root / "data"))
        claim.update(status="completed" if report["complete"] else "incomplete", summary=report)
        if report["complete"] and not report["accounting_complete"]:
            claim["status"] = "completed_with_accounting_review"
        if report["complete"] and not report["all_arms_have_usable_candidates"]:
            claim["status"] = "finished_without_usable_generation"
        claim["prior_ledgers_unchanged"] = all(sha(root/n) == h for n,h in preview["prior_ledger_hashes"].items())
        claim["workflow_seconds_excluding_key_entry"] = time.perf_counter()-start
        write_json(root / paths["claim"], claim)
        print(f"Saved: {root/paths['output']}. Requests={report['new_requests']}; estimated CNY={report['new_estimated_cost_cny']:.6f}; no final test.")
        return claim
    except BaseException as error:
        if claimed:
            claim.update(status="interrupted", error_type=type(error).__name__, elapsed_seconds=time.perf_counter()-start)
            if (root / paths["ledger"]).exists():
                claim["ledger_after"] = inspect_ledger(root / paths["ledger"])
            write_json(root / paths["claim"], claim)
        raise
    finally:
        os.chdir(old_cwd)
        if entered:
            os.environ.pop(env_name, None)


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start the authorized 4x50 batch")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--low", action="store_true", help="Use the passed low16 calibration and a separate 200-request batch")
    mode.add_argument("--retry-network-failure", action="store_true", help="Historical high-effort retry after the original200 SSL failures")
    parser.add_argument("--direct", action="store_true", help="Bypass proxies for api.deepseek.com in this process only")
    args = parser.parse_args(argv)
    with connection_route(args.direct):
        return execute(args, root=root)


if __name__ == "__main__":
    main()
