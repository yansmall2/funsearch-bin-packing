"""Preview a 128-call calibration; activate only with --approve-128-calls.

The same ledger grows from 16 to 144 total request slots; its original CNY 10
money cap and all previous charges remain. No automatic resume or retries.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from funpack.data import load_instances, load_probes
from funpack.ledger import budget_signature, extend_request_limit
from funpack.protocol import public_api_config
from funpack.storage import read_json, write_json

ADDITIONAL_CALLS = 128
TOTAL_CALLS = 144
CLAIM_NAME = "configs/calibration128.claim.local.json"
CONFIG_NAME = "configs/calibration.local.json"
BACKUP_NAME = "configs/deepseek_pilot_before_calibration.sqlite3"


def inspect_ledger(path):
    """Read-only; a wrong path must never create a new zero-cost ledger."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("original pilot ledger is missing; do not create a replacement")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        meta = db.execute("SELECT schema_version,signature FROM funpack_budget_meta WHERE singleton=1").fetchone()
        row = db.execute("SELECT COUNT(*),COALESCE(SUM(accounted_units),0),"
                         "COALESCE(SUM(status='reserved'),0),COALESCE(SUM(status='unknown'),0) "
                         "FROM funpack_requests").fetchone()
    if meta is None or meta[0] != 1:
        raise ValueError("original ledger schema is not supported")
    return {"signature": meta[1], "attempts": row[0], "accounted_cost_cny": row[1] / 1e12,
            "unresolved_reservations": row[2], "unknown_cost_requests": row[3]}


def prepare_plan(root):
    root = Path(root).resolve()
    old = read_json(root / "configs/api.local.json")
    public_api_config(old)  # validates settings without requiring a live key
    budget = old["budget"]
    if (old.get("enabled") is not True or old["base_url"] != "https://api.deepseek.com"
            or old["model"] != "deepseek-flash" or old["max_requests"] != 16
            or old.get("temperature") != 0.8 or old["max_tokens"] != 1024
            or old.get("extra_body") != {"thinking": {"type": "disabled"}}
            or budget["currency"] != "CNY" or budget["max_cost"] != 10
            or budget["input_per_million"] != 2 or budget["output_per_million"] != 8):
        raise ValueError("original configuration differs from the reviewed pilot; inspect it before extending")
    ledger = Path(old["ledger_path"])
    ledger = (root / ledger).resolve() if not ledger.is_absolute() else ledger.resolve()
    new = copy.deepcopy(old)
    new["max_requests"] = TOTAL_CALLS
    state = inspect_ledger(ledger)
    old_signature, new_signature = budget_signature(old), budget_signature(new)
    if state["signature"] not in (old_signature, new_signature):
        raise ValueError("ledger configuration signature differs from the original and planned configuration")
    ready = (state["signature"] == old_signature and state["attempts"] == 16
             and state["unresolved_reservations"] == 0 and state["accounted_cost_cny"] < 10
             and not any((root / name).exists() for name in (CLAIM_NAME, CONFIG_NAME, BACKUP_NAME)))
    plan = {"kind": "supplementary_pilot_not_formal_comparison", "ready_to_activate": ready,
            "arms": ["A", "B", "C", "D"], "seeds": [11], "calls_per_arm": 32,
            "additional_request_limit": ADDITIONAL_CALLS, "cumulative_request_limit": TOTAL_CALLS,
            "cumulative_cost_limit_cny": 10, "prior_ledger": state,
            "remaining_budget_cny": max(0, 10 - state["accounted_cost_cny"]),
            "model": new["model"], "temperature": new["temperature"], "max_tokens": new["max_tokens"],
            "thinking": "disabled", "audit_rate": 0.1, "probe_sizes": [32, 96],
            "prompt_change": False, "heldout_evaluated": False,
            "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "pricing_checked_on": "2026-11-06",
            "cost_note": "Uncached peak token-price estimate; not a platform invoice.",
            "interpretation": "32 calls/arm calibrate validity and novelty; reset at50 is not reached. "
                              "Preserve zero audits as NA; this is not evidence of final improvement."}
    return old, new, ledger, plan


def exclusive_json(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(text)


def backup_ledger(source, destination):
    # Exclusive placeholder prevents an existing backup from being overwritten.
    with Path(destination).open("xb") as stream:
        stream.flush()
    with (closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as source_db,
          closing(sqlite3.connect(str(destination))) as target_db):
        source_db.backup(target_db)


def summarize(study_dir):
    study_dir = Path(study_dir)
    study = read_json(study_dir / "study.json")
    rows = []
    for run in study["runs"]:
        m = run["metrics"]
        attempts = m["generator_requests"]
        rows.append({"arm": run["arm"], "seed": run["seed"], "requests": attempts,
                     "code_extracted": m["code_extracted"], "invalid_candidates": m["invalid_candidates"],
                     "format_violations": m["format_violations"], "text_hits": m["text_cache_hits"],
                     "behavior_skips": m["behavior_skips"], "random_audits": m["audits"],
                     "audit_count_mismatches": m["audit_count_mismatches"],
                     "audit_errors": m["audit_evaluation_errors"],
                     "full_candidate_evaluations": m["candidate_full_evaluations"],
                     "best_search_bins_including_seed_bf": run["best_search_mean_bins"],
                     "selected_validation_bins": run["selected_validation_mean_bins"],
                     "input_tokens": m["input_tokens_reported"], "output_tokens": m["output_tokens_reported"],
                     "cost_estimate_cny": m["estimated_cost_reported"],
                     "unknown_cost_requests": m["unknown_cost_requests"],
                     "generation_seconds": m["generation_seconds"],
                     "evaluation_seconds": m["evaluation_seconds"],
                     "termination": run["termination"]})
    complete = (len(rows) == 4 and {r["arm"] for r in rows} == set("ABCD")
                and all(r["requests"] == 32 and r["termination"] == "request_budget_completed" for r in rows))
    result = {"phase": "supplementary_pilot", "complete": complete, "runs": rows,
              "heldout_evaluated": False, "note": "No final performance conclusion from this calibration."}
    write_json(study_dir / "calibration_summary.json", result)
    return result


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-128-calls", action="store_true",
                        help="Explicitly authorize at most128 NEW requests, preserving the total CNY10 cap")
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    old, new, ledger, plan = prepare_plan(root)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.approve_128_calls:
        print("Preview only: no files changed, no key requested, no API requests sent.")
        return plan
    if not plan["ready_to_activate"]:
        raise ValueError("batch cannot start: inspect prior claim/config/backup and ledger; do not reset them")
    # Confirm ordinary data is usable before any budget mutation; never load heldout.
    for split in ("development", "search", "validation"):
        load_instances(root / "data" / (split + ".json"))
    load_probes(root / "data/probes.json")
    env_name = old["api_key_env"]
    entered = not os.environ.get(env_name)
    if entered:
        if not sys.stdin.isatty():
            raise RuntimeError("Run in an interactive terminal for hidden key entry; never send the key to chat")
        key = getpass.getpass("输入 DeepSeek API 密钥（不会显示；不要发到聊天）：")
        if not key:
            raise ValueError("empty key; ledger unchanged and no request sent")
        os.environ[env_name] = key
        del key
    previous_dir = Path.cwd()
    claimed = False
    try:
        os.chdir(root)  # relative ledger path must retain its original meaning
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = root / ("run_paid_calibration_" + stamp)
        if output.exists():
            raise FileExistsError("output exists; no existing evidence will be overwritten")
        claim = {"plan": plan, "approval": "explicit --approve-128-calls CLI flag",
                 "claimed_utc": datetime.now(timezone.utc).isoformat(), "output": output.name,
                 "status": "claimed_before_extension"}
        exclusive_json(root / CLAIM_NAME, claim)
        claimed = True
        backup_ledger(ledger, root / BACKUP_NAME)
        extension = extend_request_limit(ledger, old, new, expected_attempts=16,
                                        authorization_note="Explicit --approve-128-calls; add128 requests; cumulative CNY10 unchanged")
        exclusive_json(root / CONFIG_NAME, new)
        claim.update(status="extended_not_yet_run", extension=extension)
        write_json(root / CLAIM_NAME, claim)
        import experiment
        claim["status"] = "running"
        write_json(root / CLAIM_NAME, claim)
        experiment.main(["--stage", "all", "--provider", "api", "--allow-paid", "--api-config", CONFIG_NAME,
                         "--data", "data", "--out_dir", str(output), "--arms", "A", "B", "C", "D",
                         "--calls", "32", "--seeds", "11", "--audit-rate", "0.1"])
        report = summarize(output)
        import audit_and_cost
        write_json(output / "audit_and_cost.json", audit_and_cost.analyze(output, root / "data"))
        claim.update(status="completed" if report["complete"] else "stopped_incomplete",
                     ledger_after=inspect_ledger(ledger))
        write_json(root / CLAIM_NAME, claim)
        print("Calibration saved: " + str(output))
        print("Send calibration_summary.json or the terminal summary; never the key/config/ledger.")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    except BaseException as error:
        if claimed:
            claim.update(status="interrupted", error_type=type(error).__name__)
            try:
                claim["ledger_after"] = inspect_ledger(ledger)
            except (OSError, sqlite3.Error, ValueError) as snapshot_error:
                claim["ledger_snapshot_error_type"] = type(snapshot_error).__name__
            try:
                write_json(root / CLAIM_NAME, claim)
            except OSError:
                print("Could not update the claim; preserve all existing files for investigation.", file=sys.stderr)
        raise
    finally:
        os.chdir(previous_dir)
        if entered:
            os.environ.pop(env_name, None)


if __name__ == "__main__":
    main()
