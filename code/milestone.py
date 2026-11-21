"""Run the frozen four-condition milestone and seal its validation selections."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import formal
from audit_and_cost import analyze
from funpack.protocol import load_protocol
from funpack.storage import read_json, write_json

ROOT = Path(__file__).resolve().parent
PROTOCOL = "evidence/protocol.json"
API = "configs/deepseek.json"
STUDY = Path("run_live")


def finish():
    """Verify all 12 source logs, lock winners and calculate audit and cost data."""
    from funpack.protocol import seal_candidates
    study = read_json(STUDY / "study.json")
    if study.get("stop_reason") or len(study["runs"]) != 12:
        raise RuntimeError("incomplete paid study; inspect study.json and per-run events before continuing")
    seal = STUDY / "candidates-sealed.json"
    if not seal.exists():
        seal_candidates(PROTOCOL, STUDY, "data", seal)
    analysis = analyze(STUDY, "data")
    if not analysis["complete_balanced_study"]:
        raise RuntimeError("unbalanced study")
    write_json(STUDY / "audit_and_cost.json", analysis)
    rows = sorted(study["runs"], key=lambda r: (r["arm"], r["seed"]))
    for row in rows:
        print(f"{row['run']:12s} search {row['best_search_mean_bins']:.3f}  "
              f"validation {row['selected_validation_mean_bins']:.3f}")
    print(f"Verified {len(rows)} selections. Search, validation and audits saved in {STUDY.resolve()}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "run", "finish"])
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    protocol = load_protocol(PROTOCOL, "data")
    public = protocol["api_config"]
    plan = protocol["plan"]
    if public["extra_body"] != {"thinking": {"type": "disabled"}} or public["max_requests"] != 600:
        raise ValueError("milestone requires 600 slots and explicit non-thinking generation")
    print(f"DeepSeek {public['model']} | thinking disabled | four parallel arms | "
          f"seeds {plan['seeds']} x {plan['calls_per_run']} | CNY {public['budget']['max_cost']} ceiling", flush=True)
    if args.action == "check":
        print("Frozen protocol verified:", protocol["protocol_id"])
        return
    if args.action == "run":
        # Match the working pilot route only for the API host; preserve TLS verification.
        for name in ("NO_PROXY", "no_proxy"):
            os.environ[name] = ",".join(filter(None, (os.environ.get(name, ""), "api.deepseek.com")))
        formal.main(["run", "--protocol", PROTOCOL, "--api-config", API,
                     "--data", "data", "--out_dir", str(STUDY), "--parallel-arms", "--allow-paid"])
    finish()


if __name__ == "__main__":
    main()
