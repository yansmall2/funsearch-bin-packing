"""One-call DeepSeek smoke test for the funsearch-bin-packing project.

Purpose: find out, for at most ~0.50 CNY, whether the 2026-11-09
SSL failure (200/200 requests, SSL UNEXPECTED_EOF_WHILE_READING,
0 candidates) is fixed, before spending the user's 20 CNY envelope
on a 128-call calibration or a 200-call thinking pilot.

Rules carried over from the rest of this repo:
- Preview by default: no key, no request, no ledger change.
- --run requires --allow-paid and an interactive terminal for the key
  (getpass, never sent to chat, never written to a log).
- One attempt only, no automatic retry. A network failure is recorded,
  not retried, and its reservation stays in the dedicated ledger.
- Dedicated ledger configs/smoke1_budget.sqlite3, max_requests=1,
  max_cost=0.50 CNY. It does not touch the pilot / calibration /
  thinking-200 ledgers, the formal protocol, or test/OR3 data.
- The prompt is a minimal bin-packing priority-function request, not
  a full A/B/C/D arm. A pass here proves transport + generation only;
  it does not prove prompt enhancement or screening works.

Run on the user's Windows machine, in the repo root:
  python scripts/check_deepseek_connection.py
  python scripts/run_smoke_test_1.py
  python scripts/run_smoke_test_1.py --run --allow-paid
  python scripts/run_smoke_test_1.py --run --allow-paid --direct

--direct bypasses proxies for api.deepseek.com in this process only
(NO_PROXY), retaining TLS verification. Compare the two routes:
if --direct passes and the default route fails, the local proxy is
the suspect; if both fail with the same SSL error, the fault is
further upstream (network / DeepSeek / TLS middlebox).
"""

import argparse
import copy
import getpass
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from funpack.providers import ApiProvider, preflight
from funpack.storage import read_json, write_json

ACTIVE = "configs/smoke1.local.json"
LEDGER = "configs/smoke1_budget.sqlite3"
OUTPUT = "run_smoke_20261114"
MAX_COST_CNY = 0.50


@contextmanager
def connection_route(direct):
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
    """Credential-free TLS preflight. 401 means TLS and HTTP succeeded."""
    results = []
    for route, opener in (
        ("default", request.build_opener()),
        ("direct_diagnostic_only", request.build_opener(request.ProxyHandler({}))),
    ):
        try:
            with opener.open("https://api.deepseek.com/models", timeout=12) as response:
                results.append({"route": route, "http_status": response.status, "tls_connected": True})
        except urlerror.HTTPError as error:
            results.append({"route": route, "http_status": error.code, "tls_connected": True})
        except Exception as error:  # noqa: BLE001 - record the exact failure
            results.append(
                {"route": route, "tls_connected": False, "error_type": type(error).__name__,
                 "message": str(error)[:300]}
            )
    return results


def build_config():
    template = read_json(ROOT / "configs/deepseek_pilot_cny.example.json")
    config = copy.deepcopy(template)
    config.update(
        enabled=True,
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        max_requests=1,
        max_tokens=1024,
        timeout_seconds=60,
        temperature=0.8,
        ledger_path=LEDGER,
        extra_body={"thinking": {"type": "disabled"}},
    )
    config["budget"].update(
        currency="CNY", max_cost=MAX_COST_CNY,
        input_per_million=2.0, output_per_million=8.0,
        pricing_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        pricing_checked_date="2026-11-14",
    )
    return config


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Send the one authorised call")
    parser.add_argument("--allow-paid", action="store_true", help="Explicit paid-call approval")
    parser.add_argument("--direct", action="store_true",
                        help="Bypass proxies for api.deepseek.com in this process only")
    args = parser.parse_args(argv)
    with connection_route(args.direct):
        return execute(args, root=root)


def execute(args, *, root=ROOT):
    root = Path(root).resolve()
    config = build_config()
    checked = preflight(config, allow_paid=True)
    connection = check_connection()
    blockers = []
    if (root / ACTIVE).exists() or (root / LEDGER).exists() or (root / OUTPUT).exists():
        blockers.append("smoke artifacts already exist; do not delete evidence to rerun")
    if not args.run and args.allow_paid:
        blockers.append("--allow-paid without --run does nothing")
    preview = {
        "kind": "smoke_test_1_call",
        "date": "2026-11-14",
        "budget_envelope_cny": 20.0,
        "smoke_max_cost_cny": MAX_COST_CNY,
        "max_requests": 1,
        "model": config["model"],
        "direct_connection": bool(args.direct),
        "preflight_ok": checked["ok"],
        "preflight_errors": checked["errors"],
        "connection_check": connection,
        "ready_to_start": not blockers and checked["ok"],
        "blockers": blockers,
        "artifacts": {"config": ACTIVE, "ledger": LEDGER, "output": OUTPUT},
        "note": "A pass proves transport + one generation only, not A/B/C/D effects.",
    }
    if not args.run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return preview
    if not args.allow_paid:
        raise ValueError("paid smoke test requires --allow-paid")
    if blockers:
        raise ValueError("cannot start smoke test: " + "; ".join(blockers))
    if not checked["ok"]:
        raise ValueError("preflight failed: " + "; ".join(checked["errors"]))
    if not any(r.get("tls_connected") for r in connection):
        raise ValueError("TLS preflight failed on every route; no generation request sent")

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
    try:
        write_json(root / ACTIVE, config)
        provider = ApiProvider(config, allow_paid=True)
        messages = [
            {"role": "system", "content": "Return only a Python function named priority(item, bins)."},
            {"role": "user", "content": "Write priority(item, bins) returning one score per bin for online bin packing. Reply with a single ```python block and no other text."},
        ]
        started = time.perf_counter()
        result = provider.generate(messages)
        summary = {
            "kind": "smoke_test_1_call",
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "direct_connection": bool(args.direct),
            "error": result.get("error"),
            "model_returned": result.get("model"),
            "finish_reason": result.get("finish_reason"),
            "usage": result.get("usage"),
            "estimated_cost_cny": result.get("estimated_cost"),
            "cost_known": result.get("cost_known"),
            "elapsed_seconds": time.perf_counter() - started,
            "text_head": (result.get("text") or "")[:500],
            "connection_check": connection,
        }
        (root / OUTPUT).mkdir(exist_ok=True)
        write_json(root / OUTPUT / "smoke_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    finally:
        if entered:
            os.environ.pop(env_name, None)


if __name__ == "__main__":
    main()
