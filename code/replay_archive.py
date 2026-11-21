"""Paired, fixed-stream re-evaluation of the archived 1,200 Muse proposals. No API."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import subprocess
from functools import lru_cache
import json
from pathlib import Path
import random
import time

from funpack.data import _generate_probes, load_instances, load_probes
from funpack.packing import evaluate
from funpack.programs import BF_CODE, FF_CODE, Program
from funpack.screening import BF_PROTECTED, BehavioralScreen
from funpack.storage import digest, read_json, write_json

ROOT = Path(__file__).resolve().parent
LEGACY = "disable_on_mismatch_v1"


@lru_cache(maxsize=1)
def load_archive(checkout):
    """Read authenticated historical prompts from the separately published pinned source checkout."""
    summary = read_json(ROOT / "evidence/historical-summary.json")
    commit = subprocess.check_output(["git","-C",str(checkout),"rev-parse","HEAD"],text=True).strip()
    if commit != summary["archive_commit"]:
        raise ValueError("use the published archived revision: " + summary["archive_commit"])
    groups, sources = {}, []
    for row in summary["historical_selections"]:
        name = row["run"]
        path = checkout / ("run_formal_part_" + name) / name / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
        if [e["request_index"] for e in events] != list(range(1,101)):
            raise ValueError("incomplete stream: " + name)
        entries = []
        for e in events:
            if (digest(e["messages"]) != e["messages_hash"] or e["response"]["provider"] != "muse_handshake"
                    or e["response"]["model"] != "muse-spark-1.3"):
                raise ValueError("historical prompt or metadata mismatch: " + name)
            entries.append([e["request_index"],len(sources)])
            sources.append(e["parsed"]["code"])
        groups[name] = entries
    return {"history":summary["historical_selections"],"origin":{"commit":commit},
            "groups":groups,"sources":sources}

def measure(code, instances):
    program = Program(code)
    deadline = time.perf_counter() + 30
    def bounded(item, bins):
        if time.perf_counter() > deadline:
            raise RuntimeError("candidate exceeded its 30-second evaluation budget")
        return program(item, bins)
    outcome = evaluate(bounded, instances)
    if time.perf_counter() > deadline:
        raise RuntimeError("candidate exceeded its 30-second evaluation budget")
    return outcome


def replay(job):
    """Retain source order. Only fully measured candidates can enter the shortlist."""
    name, policy, checkout = job
    archive = load_archive(checkout)
    search = load_instances(ROOT / "data/search.json")
    validation = load_instances(ROOT / "data/validation.json")
    probes = load_probes(ROOT / "data/probes.json")
    seed = int(name.split("seed")[1])
    rng = random.Random(seed + 1_000_003)
    screen = BehavioralScreen(probes, collision_policy=policy) if name[0] in "CD" else None
    start = time.perf_counter()
    records, cache = [], {}
    audit_rows, errors = [], []
    hits = skipped = protected = attempted = 0

    def add(code, label):
        record = {"id": label, "code": code, "code_hash": digest(code), **measure(code, search)}
        records.append(record)
        cache[code] = record
        return record

    for label, source in (("seed_ff", FF_CODE), ("seed_bf", BF_CODE)):
        record = add(source, label)
        if screen:
            screen.register(Program(source), record)

    for row in archive["groups"][name]:
        i, code = row[0], archive["sources"][row[1]]
        if code in cache:
            hits += 1
            continue
        prefix = tail = representative = audit = None
        try:
            program = Program(code)
            if screen:
                try:
                    representative, prefix, tail = screen.probe(program)
                except (ValueError, ArithmeticError, RuntimeError, TypeError):
                    prefix = tail = representative = None
                protected += int(screen.protects_bf(prefix, tail))
            if representative:
                if rng.random() >= 0.1:
                    skipped += 1
                    continue
                audit = {"index": i, "representative": representative["id"]}
                audit_rows.append(audit)
            attempted += 1
            record = add(code, f"candidate_{i:06d}")
            if audit is not None:
                deltas = [new - old for old, new in zip(representative["counts"], record["counts"])]
                trace_differs = representative["trace_hashes"] != record["trace_hashes"]
                audit.update(quality=any(deltas), traces=trace_differs, deltas=deltas,
                             better=sum(d < 0 for d in deltas), worse=sum(d > 0 for d in deltas))
                if trace_differs or any(deltas):
                    screen.disable(prefix, tail)
            if screen:
                try:
                    screen.register(program, record, prefix, tail)
                except (ValueError, ArithmeticError, RuntimeError, TypeError):
                    pass
        except (ValueError, ArithmeticError, RuntimeError, TypeError) as exc:
            errors.append({"index": i, "type": type(exc).__name__})
            if audit is not None:
                audit["error"] = str(exc)
    ranked = sorted(records, key=lambda r: (r["mean_bins"], len(r["code"]), r["code_hash"]))[:5]
    selected = min(({"record": r, "valid": measure(r["code"], validation)} for r in ranked),
                   key=lambda x: (x["valid"]["mean_bins"], x["record"]["mean_bins"],
                                  len(x["record"]["code"]), x["record"]["code_hash"]))
    return {"run": name, "arm": name[0], "seed": seed, "policy": policy if screen else "off",
            "candidate_calls_replayed": 100, "new_api_calls": 0, "full_attempts": attempted,
            "skips": skipped, "text_hits": hits, "bf_protected": protected, "errors": errors,
            "audits": audit_rows, "search": ranked[0]["mean_bins"],
            "validation": selected["valid"]["mean_bins"], "selected_id": selected["record"]["id"],
            "selected_code_hash": selected["record"]["code_hash"], "elapsed_seconds": time.perf_counter() - start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1,5), default=4)
    parser.add_argument("--archive-checkout", type=Path, required=True, help="local checkout of the pinned historical branch")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("choose a new output file")
    wall = time.perf_counter()
    archive = load_archive(args.archive_checkout)
    development = load_instances(ROOT / "data/development.json")
    probe_start = time.perf_counter()
    rebuilt = _generate_probes(development)["states"]
    probe_seconds = time.perf_counter() - probe_start
    if digest(rebuilt) != digest(load_probes(ROOT / "data/probes.json")):
        raise ValueError("probe reconstruction mismatch")
    jobs = [(row["run"], policy, args.archive_checkout) for row in archive["history"]
            for policy in ([LEGACY, BF_PROTECTED] if row["arm"] in "CD" else [LEGACY])]
    if args.workers == 1:
        rows = list(map(replay, jobs))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(replay, jobs))
    history = {r["run"]: r for r in archive["history"]}
    differences = []
    for row in rows:
        if row["policy"] == BF_PROTECTED:
            continue
        original = history[row["run"]]
        audits = [a for a in row["audits"] if "error" not in a]
        fields = {"search": (row["search"],original["search"]), "validation": (row["validation"],original["validation"]),
                  "skips": (row["skips"],original["metrics"]["skips"]), "full_attempts": (row["full_attempts"],original["metrics"]["full"]),
                  "random_audits": (len(audits),original["audit"]["complete"]),
                  "audited_quality_disagreements": (sum(a["quality"] for a in audits),original["audit"]["quality"])}
        for field, (new, old) in fields.items():
            if new != old:
                differences.append({"run":row["run"], "field":field,"replayed":new,"archived":old})
    result = {"origin":archive["origin"],"scope":"fixed historical proposals; no model regenerated; no held-out evaluation",
              "probe_construction_s":probe_seconds,"wall_seconds":time.perf_counter()-wall,
              "workers":args.workers,"elapsed_scope":"Per-row time includes packing seeds, screening, audits and validation. Parallel rows overlap. Wall time includes loading, prompt checks and probe construction.",
              "archived_rule_check":differences,"rows":rows}
    write_json(args.out, result)
    print(f"Replayed {len(archive['groups'])} streams, {len(rows)} condition/policy runs, {len(differences)} archive mismatches, {result['wall_seconds']:.1f} seconds.")


if __name__ == "__main__":
    main()
