"""Draft, freeze, run and evaluate a preregistered four-arm comparison.

Draft/freeze/seal require no API key and make no network request. Only `run`
with --allow-paid can use the API. Only `evaluate` with --allow-heldout can
load final test data. Real formal configuration has not yet been frozen.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from funpack.protocol import (draft_plan, evaluate_final, freeze_protocol,
                              load_protocol, public_api_config, seal_candidates)
from funpack.storage import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="Write unresolved, non-executable plan")
    draft.add_argument("--output", default="configs/formal-plan.draft.json")
    freeze = commands.add_parser("freeze", help="Freeze finalized plan, model configuration, code and data")
    freeze.add_argument("--plan", required=True)
    freeze.add_argument("--api-config", required=True)
    freeze.add_argument("--data", default="data")
    freeze.add_argument("--output", required=True)
    run = commands.add_parser("run", help="Run all frozen arms/seeds; new budget authorization is needed")
    run.add_argument("--protocol", required=True)
    run.add_argument("--api-config", required=True)
    run.add_argument("--data", default="data")
    run.add_argument("--out_dir", required=True)
    run.add_argument("--allow-paid", action="store_true")
    run.add_argument("--parallel-arms", action="store_true", help="One worker per independent condition")
    seal = commands.add_parser("seal", help="Lock all validation-selected candidates before final testing")
    seal.add_argument("--protocol", required=True)
    seal.add_argument("--study", required=True)
    seal.add_argument("--data", default="data")
    seal.add_argument("--output", required=True)
    final = commands.add_parser("evaluate", help="One final test of sealed candidates and FF/BF; no API")
    final.add_argument("--protocol", required=True)
    final.add_argument("--seal", required=True)
    final.add_argument("--study", required=True)
    final.add_argument("--data", default="data")
    final.add_argument("--out_dir", required=True)
    final.add_argument("--allow-heldout", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "draft":
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(draft_plan(), stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(f"Draft saved: {destination.resolve()}. No configuration frozen; no requests sent.")
    elif args.command == "freeze":
        result = freeze_protocol(args.plan, args.api_config, args.data, args.output)
        print(f"Protocol frozen: {result['protocol_id']}. No requests sent; held-out data not evaluated.")
    elif args.command == "seal":
        seal_candidates(args.protocol, args.study, args.data, args.output)
        print(f"Candidates sealed: {Path(args.output).resolve()}. Held-out data not evaluated.")
    elif args.command == "evaluate":
        evaluate_final(args.protocol, args.seal, args.study, args.data, args.out_dir,
                       allow_heldout=args.allow_heldout)
        print(f"Final evaluation saved: {Path(args.out_dir).resolve()}. Do not tune against these results.")
    else:
        if not args.allow_paid:
            parser.error("formal run requires --allow-paid and an authorized API budget")
        protocol = load_protocol(args.protocol, args.data)
        config = read_json(args.api_config)
        if config.get("enabled") is not True or public_api_config(config) != protocol["api_config"]:
            parser.error("API configuration differs from the frozen protocol")
        # Do not prompt for a secret until the immutable setup and destination pass.
        destination = Path(args.out_dir)
        if destination.exists() and any(destination.iterdir()):
            parser.error("use a NEW output directory")
        env_name = config["api_key_env"]
        entered = not os.environ.get(env_name)
        if entered:
            if not sys.stdin.isatty():
                parser.error("set the key environment variable locally, or run in an interactive terminal")
            key = getpass.getpass("输入 DeepSeek API 密钥（不会显示；不要发到聊天）：")
            if not key:
                parser.error("empty key; no request sent")
            os.environ[env_name] = key
            del key
        try:
            import experiment
            plan = protocol["plan"]
            experiment.main([
                *(["--parallel-arms"] if args.parallel_arms else []),
                "--stage", "all", "--provider", "api", "--allow-paid",
                "--protocol", args.protocol, "--api-config", args.api_config,
                "--data", args.data, "--out_dir", args.out_dir,
                "--arms", *plan["arms"], "--seeds", *(str(s) for s in plan["seeds"]),
                "--calls", str(plan["calls_per_run"]), "--islands", str(plan["islands"]),
                "--reset-every", str(plan["reset_every"]), "--audit-rate", str(plan["audit_rate"]),
                "--database-temperature", str(plan["database_temperature"]),
                "--enhanced-prompt-version", plan["enhanced_prompt_version"],
                "--collision-policy", plan["collision_policy"],
                "--evaluation-timeout-seconds", str(plan["evaluation_timeout_seconds"]),
                "--validation-timeout-seconds", str(plan["validation_timeout_seconds"]),
            ])
        finally:
            if entered:
                os.environ.pop(env_name, None)


if __name__ == "__main__":
    main()
