"""Check or start a new replicate of the frozen 60- or 200-step DeepSeek study."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from funpack.protocol import load_protocol, public_api_config
from funpack.storage import read_json, write_json

KEY_ENV = "DEEPSEEK_API_KEY"
BUDGETS = (60, 200)


def checked_preset(budget):
    """Pair a study with its original API settings and verify both before launch."""
    data = CODE / "data"
    protocol_path = CODE / "evidence" / f"protocol-{budget}.json"
    config_path = CODE / "configs" / f"deepseek-{budget}.json"
    protocol = load_protocol(protocol_path, data)
    config = read_json(config_path)
    if config.get("api_key_env") != KEY_ENV:
        raise ValueError(f"the API config must use {KEY_ENV} for the DeepSeek key")
    # The environment-variable name is part of the old signed snapshot. Adapt
    # only that field when passing settings to the frozen search runner.
    runtime_config = {**config, "api_key_env": protocol["api_config"]["api_key_env"]}
    if config.get("enabled") is not True or public_api_config(runtime_config) != protocol["api_config"]:
        raise ValueError("API settings do not match the frozen study")
    if protocol["plan"]["calls_per_run"] != budget:
        raise ValueError("the requested budget does not match the frozen study")
    if (config["base_url"] != "https://api.deepseek.com" or config["model"] != "deepseek-flash"
            or config["extra_body"] != {"thinking": {"type": "disabled"}}):
        raise ValueError("this launcher requires official DeepSeek Flash with thinking disabled")
    return protocol_path, protocol, runtime_config


def run_replicate(protocol_path, config, directory):
    """Give the frozen runner its expected key name and a separate local ledger."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    config_path = directory / "frozen-api-config.json"
    write_json(config_path, config)
    env = os.environ.copy()
    key = env.pop(KEY_ENV, None)
    recorded_name = config["api_key_env"]
    # Ignore stale variables under the historical name. Only the explicit
    # project key is translated, and only in the child process.
    env.pop(recorded_name, None)
    if key:
        env[recorded_name] = key

    return subprocess.run(
        [sys.executable, "-B", str(CODE / "formal.py"), "run",
         "--protocol", str(protocol_path), "--api-config", str(config_path),
         "--data", str(CODE / "data"), "--out_dir", str(directory / "study"),
         "--parallel-arms", "--allow-paid"],
        cwd=directory, env=env,
    ).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    check = modes.add_parser("check", help="inspect the frozen settings offline")
    check.add_argument("--budget", type=int, choices=BUDGETS)
    run = modes.add_parser("run", help="start a new paid replicate")
    run.add_argument("--budget", type=int, choices=BUDGETS, required=True)
    run.add_argument("--out-dir", type=Path, required=True, help="new directory for the study and its ledger")
    run.add_argument("--allow-paid", action="store_true", help="authorize this new run")
    args = parser.parse_args(argv)
    if args.action == "run" and not args.allow_paid:
        parser.error("run requires --allow-paid")

    budgets = (args.budget,) if args.budget is not None else BUDGETS
    for budget in budgets:
        try:
            protocol_path, protocol, config = checked_preset(budget)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"{budget} steps per arm and seed, {protocol['total_planned_requests']} planned requests. "
              f"{config['model']} at {config['base_url']}, thinking disabled. "
              f"Budget ceiling {config['budget']['max_cost']} {config['budget']['currency']}. "
              f"Key environment variable: {KEY_ENV}.", flush=True)

    if args.action == "check":
        return 0
    try:
        status = run_replicate(protocol_path, config, args.out_dir)
        if status:
            return status
        study = read_json(args.out_dir / "study" / "study.json")
    except FileExistsError:
        parser.error("--out-dir must name a new directory")
    except (OSError, ValueError) as error:
        print(f"Could not verify study completion: {error}", file=sys.stderr)
        return 1

    plan = protocol["plan"]
    stop = study.get("stop_reason")
    if stop or len(study.get("runs", [])) != len(plan["arms"]) * len(plan["seeds"]):
        print(f"Study incomplete: {stop or 'missing runs'}. See the study log in the output directory.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
