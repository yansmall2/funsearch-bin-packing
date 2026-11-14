"""Preview the proposed formal study; only explicit approval can activate it.

4000 new API requests, at most CNY20 in the formal ledger. Prior pilot charges
are preserved separately and reported cumulatively. No retries, resume or final
test evaluation. Default invocation does not create config, ledger or claim.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from funpack.ledger import budget_signature
from funpack.protocol import (_data_snapshot, _validate_plan, core_source_hashes,
                              freeze_protocol, public_api_config, seal_candidates)
from funpack.storage import read_json, write_json
from scripts.run_calibration_pilot import exclusive_json, inspect_ledger

PLAN = "configs/formal-plan.proposed.json"
PROPOSED_API = "configs/formal-api.proposed.json"
ACTIVE_API = "configs/formal4000.local.json"
CLAIM = "configs/formal4000.claim.local.json"
FROZEN = "protocol-frozen-formal-20261107.json"
STUDY = "run_formal_20261107"
SEALED = "candidates-sealed-formal-20261107.json"
FORMAL_LEDGER = "configs/formal_4000_budget.sqlite3"
PRIOR_LEDGER = "configs/deepseek_pilot_ledger.sqlite3"
PROMPT_LEDGER = "configs/prompt64_budget.sqlite3"
PROMPT_CONFIG = "configs/prompt64.local.json"
PROMPT_CLAIM = "configs/prompt64.claim.local.json"
THINKING_LEDGER = "configs/thinking200_budget.sqlite3"
THINKING_CONFIG = "configs/thinking200.local.json"
THINKING_CLAIM = "configs/thinking200.claim.local.json"
APPROVAL_FLAG = "--approve-4000-calls-20-cny"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(root):
    """Read-only preflight. Validation never loads held-out instance contents."""
    root = Path(root).resolve()
    plan = read_json(root / PLAN)
    _validate_plan(plan)
    expected = {"seeds": [101, 202, 303, 404, 505], "calls_per_run": 200,
                "islands": 4, "reset_every": 50, "audit_rate": 0.1,
                "database_temperature": 1.0, "evaluation_timeout_seconds": 30,
                "enhanced_prompt_version": "mechanism_v3", "collision_policy": "disable_on_mismatch_v1",
                "validation_timeout_seconds": 30, "final_timeout_seconds": 30}
    if any(plan[k] != v for k, v in expected.items()):
        raise ValueError("proposed plan changed; review scope before activation")
    config = read_json(root / PROPOSED_API)
    if config.get("enabled") is not False:
        raise ValueError("proposed API config must remain disabled")
    active = copy.deepcopy(config)
    active["enabled"] = True
    public_api_config(active)
    reviewed = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                "expected_model_version": "DeepSeek-V4.1-Flash-1017",
                "api_key_env": "FUNPACK_API_KEY", "ledger_path": FORMAL_LEDGER,
                "temperature": 0.8, "max_tokens": 1024, "timeout_seconds": 60,
                "max_requests": 4000, "extra_body": {"thinking": {"type": "disabled"}}}
    budget = {"currency": "CNY", "max_cost": 20, "input_per_million": 2, "output_per_million": 8}
    if (any(active.get(k) != v for k, v in reviewed.items())
            or any(active["budget"].get(k) != v for k, v in budget.items())):
        raise ValueError("proposed model, prices or budget changed; review before activation")
    prior_config = read_json(root / "configs/calibration.local.json")
    public_api_config(prior_config)
    if (prior_config.get("ledger_path") != PRIOR_LEDGER
            or prior_config.get("max_requests") != 144
            or prior_config["budget"].get("currency") != "CNY"):
        raise ValueError("prior calibration ledger configuration differs")
    prior = inspect_ledger(root / PRIOR_LEDGER)
    claim = read_json(root / "configs/calibration128.claim.local.json")
    if (claim.get("status") != "completed" or prior["attempts"] != 144
            or prior["signature"] != budget_signature(prior_config)
            or prior["unresolved_reservations"] or prior["unknown_cost_requests"]):
        raise ValueError("prior calibration must be completed and accounted for")
    prior_ledgers = {PRIOR_LEDGER: prior}
    # An optional prior diagnostic must be either entirely absent or complete.
    # Never silently omit known charges from a partially written phase.
    for ledger_name, config_name, claim_name, expected_count, label in (
            (PROMPT_LEDGER, PROMPT_CONFIG, PROMPT_CLAIM, 64, "prompt diagnostic"),
            (THINKING_LEDGER, THINKING_CONFIG, THINKING_CLAIM, 200, "thinking pilot")):
        paths = [root / name for name in (ledger_name, config_name, claim_name)]
        if not any(path.exists() for path in paths):
            continue
        if not all(path.is_file() for path in paths):
            raise ValueError("prior " + label + " accounting is incomplete")
        previous_config = read_json(root / config_name)
        public_api_config(previous_config)
        state = inspect_ledger(root / ledger_name)
        previous_claim = read_json(root / claim_name)
        if (previous_config.get("ledger_path") != ledger_name
                or previous_config.get("max_requests") != expected_count
                or previous_config["budget"].get("currency") != "CNY"
                or previous_claim.get("status") != "completed"
                or state["attempts"] != expected_count
                or state["signature"] != budget_signature(previous_config)
                or state["unresolved_reservations"] or state["unknown_cost_requests"]):
            raise ValueError("prior " + label + " must be completed and accounted for")
        prior_ledgers[ledger_name] = state
    prior_total = sum(state["accounted_cost_cny"] for state in prior_ledgers.values())
    snapshot = _data_snapshot(root / "data")
    missing = [name for name in snapshot["heldout_files"] if not (root / "data" / name).is_file()]
    if missing:
        raise FileNotFoundError("missing final data files (existence check only): " + ", ".join(missing))
    blockers = [name for name in (ACTIVE_API, CLAIM, FROZEN, STUDY, SEALED, FORMAL_LEDGER,
                                  FORMAL_LEDGER + "-wal", FORMAL_LEDGER + "-shm", FORMAL_LEDGER + "-journal")
                if (root / name).exists()]
    stage_path = root / "configs/research-stage.json"
    if stage_path.exists() and read_json(stage_path).get("formal_start_allowed") is not True:
        blockers.append("research-stage.json: formal start not authorized; revised method/model calibration pending")
    preview = {"kind": "proposed_formal_study", "ready_to_activate": not blockers,
               "blockers": blockers, "arms": plan["arms"], "seeds": plan["seeds"],
               "calls_per_run": 200, "total_new_requests": 4000,
               "formal_cost_limit_cny": 20, "prior_ledger": prior,
               "prior_ledgers": prior_ledgers,
               "prior_total_requests": sum(state["attempts"] for state in prior_ledgers.values()),
               "prior_total_estimated_cost_cny": prior_total,
               "prior_ledger_hashes": {name: file_hash(root / name) for name in prior_ledgers},
               "project_cost_limit_including_prior_estimate_cny": 20 + prior_total,
               "api_config": public_api_config(active), "data_counts": snapshot["data_counts"],
               "plan_sha256": file_hash(root / PLAN), "api_proposal_sha256": file_hash(root / PROPOSED_API),
               "core_source_hashes": core_source_hashes(root),
               "prior_ledger_sha256": file_hash(root / PRIOR_LEDGER),
               "method": "basic_v1 vs mechanism_v3; 32+96 probes and 10% audits; disable a full fingerprint after audited count or trace disagreement. Calibration and final model/budget approval remain pending.",
               "heldout_loaded": False, "automatic_final_evaluation": False,
               "cost_note": "CNY20 applies to NEW formal requests; estimated token costs, not an invoice. Previous ledger is preserved."}
    return active, preview


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(APPROVAL_FLAG, action="store_true",
                        help="Authorize this one formal study: 4000 NEW requests, formal cap CNY20")
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    config, preview = prepare(root)
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.approve_4000_calls_20_cny:
        print("Preview only: no config/ledger/claim written, no key requested, no API calls.")
        return preview
    if not preview["ready_to_activate"]:
        raise ValueError("formal activation blocked: " + "; ".join(preview["blockers"]) +
                         "; inspect the research stage and existing evidence before proceeding")
    env_name = config["api_key_env"]
    entered = not os.environ.get(env_name)
    if entered:
        if not sys.stdin.isatty():
            raise RuntimeError("Use an interactive terminal for hidden key entry; never send the key to chat")
        key = getpass.getpass("输入 DeepSeek API 密钥（不会显示；不要发到聊天）：")
        if not key:
            raise ValueError("empty key; no files changed or requests sent")
        os.environ[env_name] = key
        del key
    old_cwd = Path.cwd()
    claimed = False
    started = time.perf_counter()
    claim = {"proposal": preview, "approval": "explicit " + APPROVAL_FLAG,
             "claimed_utc": datetime.now(timezone.utc).isoformat(), "status": "claimed",
             "study": STUDY, "final_evaluation_performed": False}
    try:
        os.chdir(root)
        # Recheck after interactive input; never silently use settings changed while waiting.
        current_config, current_preview = prepare(root)
        if current_config != config or current_preview != preview:
            raise ValueError("proposal or prior state changed during key entry")
        exclusive_json(root / CLAIM, claim)
        claimed = True
        exclusive_json(root / ACTIVE_API, config)
        frozen = freeze_protocol(root / PLAN, root / ACTIVE_API, root / "data", root / FROZEN, source_root=root)
        claim.update(status="running", protocol_id=frozen["protocol_id"])
        write_json(root / CLAIM, claim)
        import formal
        formal.main(["run", "--protocol", FROZEN, "--api-config", ACTIVE_API,
                     "--data", "data", "--out_dir", STUDY, "--allow-paid"])
        import audit_and_cost
        report = audit_and_cost.analyze(root / STUDY, root / "data")
        write_json(root / STUDY / "audit_and_cost.json", report)
        if report["complete_balanced_study"]:
            # seal_candidates also checks every frozen run, request count and source/data hash.
            seal_candidates(root / FROZEN, root / STUDY, root / "data", root / SEALED, source_root=root)
            claim["status"] = "completed_and_sealed"
        else:
            claim["status"] = "stopped_incomplete"
        claim["formal_ledger_after"] = inspect_ledger(root / FORMAL_LEDGER)
        claim["project_cost_estimate_cny"] = (preview["prior_total_estimated_cost_cny"]
                                              + claim["formal_ledger_after"]["accounted_cost_cny"])
        claim["prior_ledger_unchanged"] = file_hash(root / PRIOR_LEDGER) == preview["prior_ledger_sha256"]
        claim["all_prior_ledgers_unchanged"] = all(file_hash(root / name) == value
                                                  for name, value in preview["prior_ledger_hashes"].items())
        claim["workflow_seconds_excluding_secret_entry"] = time.perf_counter() - started
        write_json(root / CLAIM, claim)
        print(f"Formal study: {claim['status']}. Output: {root / STUDY}. Final tests remain sealed.")
        return claim
    except BaseException as error:
        if claimed:
            claim.update(status="interrupted", error_type=type(error).__name__,
                         workflow_seconds_excluding_secret_entry=time.perf_counter() - started)
            if (root / FORMAL_LEDGER).is_file():
                try:
                    claim["formal_ledger_after"] = inspect_ledger(root / FORMAL_LEDGER)
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
