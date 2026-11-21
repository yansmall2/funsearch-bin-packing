"""Verify public numeric results and rebuild the prompts from measured, executable parents."""
from __future__ import annotations

import argparse
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from statistics import mean

from funpack.data import load_instances, load_probes
from funpack.packing import evaluate
from funpack.programs import BF_CODE, FF_CODE, Program
from funpack.prompts import build_messages
from funpack.screening import fingerprint
from funpack.storage import digest, read_json

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence"


@lru_cache(maxsize=None)
def instances(split):
    return load_instances(ROOT / "data" / (split + ".json"))


@lru_cache(maxsize=4096)
def pack(code, split):
    return evaluate(Program(code), instances(split))


def selected(records):
    shortlist=sorted(records.values(), key=lambda r:(r["mean_bins"],len(r["code"]),r["hash"]))[:5]
    return min(shortlist, key=lambda r:(pack(r["code"],"validation")["mean_bins"],r["mean_bins"],len(r["code"]),r["hash"]))


def verify_run(data, run, *, all_search=False):
    counts=data["search_count_vectors"]
    def record(index,code,vector):
        values=counts[vector]
        # The original evaluator serialized integer-valued means as floats in prompts.
        return {"index":index,"code":code,"hash":digest(code),"counts":values,"mean_bins":sum(values)/len(values)}
    records={i:record(i,code,run["seed_counts"][str(i)]) for i,code in ((-1,FF_CODE),(-2,BF_CODE))}
    for r in records.values():
        if pack(r["code"],"search")["counts"]!=r["counts"]:raise ValueError("baseline counts differ")
    audits={a[0]:a[1:] for a in run["audits"]}
    checked_audits=set()
    cutoff=None
    for index,(parents,prompt_hash,status_id,measurement,representative,protected,audited) in enumerate(run["events"],1):
        prompt=build_messages("enhanced" if run["arm"] in "BD" else "basic",
                              [records[i] for i in parents],enhanced_version="mechanism_v3")
        if digest(prompt)!=prompt_hash:raise ValueError("prompt mismatch at "+str(index))
        if measurement is not None:
            source,vector,trace_hash=measurement
            current=record(index,data["programs"][source],vector)
            records[index]=current
            if all_search:
                measured=pack(current["code"],"search")
                if measured["counts"]!=current["counts"] or digest(measured["trace_hashes"])!=trace_hash:
                    raise ValueError("measured search outcome differs")
        if audited:
            before,after=pack(records[representative]["code"],"search"),pack(records[index]["code"],"search")
            deltas=[new-old for old,new in zip(before["counts"],after["counts"])]
            if audits[index]!=[before["trace_hashes"]!=after["trace_hashes"],deltas]:raise ValueError("audit mismatch")
            checked_audits.add(index)
        if index==60:
            early=selected(records)
            cutoff=(min(r["mean_bins"] for r in records.values()),pack(early["code"],"validation")["mean_bins"],early["index"])
    if len(run["events"])!=data["budget"] or checked_audits!=set(audits):
        raise ValueError("incomplete request or audit series")
    best=min(r["mean_bins"] for r in records.values());winner=selected(records)
    validation=pack(winner["code"],"validation")["counts"]
    if (best!=run["best_search"] or winner["index"]!=run["winner"] or winner["hash"]!=run["winner_hash"]
            or validation!=run["validation_counts"]):
        raise ValueError("validation selection differs")
    top=sorted(records.values(),key=lambda r:(r["mean_bins"],len(r["code"]),r["hash"]))[:5]
    for r in top:
        if pack(r["code"],"search")["counts"]!=r["counts"]:raise ValueError("shortlist search count mismatch")
    q=sum(any(a[2]) for a in run["audits"]);delta=sum(sum(a[2]) for a in run["audits"])
    if q!=run["metrics"]["audit_count_mismatches"] or len(checked_audits)!=run["metrics"]["audits"]:
        raise ValueError("audit metrics differ")
    return {"arm":run["arm"],"seed":run["seed"],"search":best,"validation":mean(validation),
            "winner":winner,"prefix":cutoff,"deltas":[a-b for a,b in zip(validation,pack(BF_CODE,"validation")["counts"])],
            "audit_quality":q,"audit_delta":delta}


def study(budget, all_search):
    data=read_json(EVIDENCE/f"search-{budget}.json")
    protocol=read_json(EVIDENCE/f"protocol-{budget}.json")
    if len(data["runs"])!=12 or data["protocol"]!=protocol["protocol_id"] or data["thinking"]!="disabled":
        raise ValueError("study does not match the published freeze")
    rows=[]
    for run in data["runs"]:
        row=verify_run(data,run,all_search=all_search);rows.append(row)
        m=run["metrics"]
        print(f"{row['arm']}_{row['seed']} search {row['search']:.3f} validation {row['validation']:.3f} "
              f"full {m['candidate_full_evaluations']} skips {m['behavior_skips']} protected {m['bf_protected_full_evaluations']} "
              f"audits {m['audits']} trace {m['audit_trace_mismatches']} counts {row['audit_quality']} net bins {row['audit_delta']:+d} "
              f"time {run['inclusive_seconds']:.1f}s CNY {m['estimated_cost_reported']:.4f}")
    for arm in "ABCD":
        runs=[r for r in rows if r["arm"]==arm]
        print(f"{arm} mean search {mean(r['search'] for r in runs):.4f} validation {mean(r['validation'] for r in runs):.4f}")
    print(f"{len(rows)*budget} reconstructed prompts matched their original message hashes; 12 selections verified.")
    print("Wall seconds",round(data["wall_seconds"],2),"token cost estimate CNY",round(sum(r["metrics"]["estimated_cost_reported"] for r in data["runs"]),6))
    if budget==200:
        print("Same-run improvements after step 60: search",sum(r["search"]<r["prefix"][0] for r in rows),
              "validation",sum(r["validation"]<r["prefix"][1] for r in rows),"of 12")
        probes=load_probes(ROOT/"data/probes.json");bf=fingerprint(Program(BF_CODE),probes)
        for r in rows:
            if any(r["deltas"]):
                states=[(i["id"],x) for i,x in zip(instances("validation"),r["deltas"]) if x]
                print("validation difference",r["arm"],r["seed"],"step",r["winner"]["index"],states,
                      "same BF fingerprint",fingerprint(Program(r["winner"]["code"]),probes)==bf)
    return rows


def final60(data_dir):
    final=read_json(EVIDENCE/"final-60.json");summary=read_json(EVIDENCE/"search-60.json")
    protocol=read_json(EVIDENCE/"protocol-60.json");codes={"BF":BF_CODE,"FF":FF_CODE}
    if (final["seal_id"],final["protocol_id"])!=(summary["seal"],protocol["protocol_id"]):
        raise ValueError("final records belong to a different seal")
    for run in summary["runs"]:
        i=run["winner"]
        code=({-1:FF_CODE,-2:BF_CODE}[i] if i<0 else summary["programs"][run["events"][i-1][3][0]])
        codes[f"{run['arm']}_seed{run['seed']}"]=code
    for split,methods in final["splits"].items():
        cases=None
        if data_dir:
            name=f"heldout/{split}.json";path=data_dir/name
            if sha256(path.read_bytes()).hexdigest()!=protocol["heldout_files"][name]:raise ValueError("benchmark checksum mismatch")
            cases=load_instances(path,allow_heldout=True)
        for name,r in sorted(methods.items()):
            if digest(codes[name])!=r["code_hash"]:raise ValueError("winner checksum differs")
            if cases is not None and evaluate(Program(codes[name]),cases)["counts"]!=r["counts"]:raise ValueError("final counts differ")
            print(split,name,"total",sum(r["counts"]),"delta from BF",sum(r["delta_to_bf"]))


def history():
    summary=read_json(EVIDENCE/"historical-summary.json");probes=load_probes(ROOT/"data/probes.json")
    bf=fingerprint(Program(BF_CODE),probes);total=sum(pack(BF_CODE,"search")["counts"])
    for x in summary["pilot_witnesses"]:
        print("Low200",x["arm"],x["index"],"BF fingerprint match",fingerprint(Program(x["code"]),probes)==bf,
              "bins",sum(pack(x["code"],"search")["counts"]),"vs BF",total)
    replay=read_json(EVIDENCE/"replay.json"); screened=[r for r in replay["rows"] if r["arm"] in "CD"]
    for policy in sorted({r["policy"] for r in screened}):
        subset=[r for r in screened if r["policy"]==policy]
        print(policy,"full scored attempts",sum(r["full_attempts"] for r in subset))
    print("Historical replay mismatches",replay["archived_rule_check"])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--budget",type=int,choices=[60,200],help="default verifies both formal studies")
    p.add_argument("--all-search",action="store_true",help="also repack every measured candidate")
    p.add_argument("--final60",action="store_true",help="report existing final counts for the sealed 60-step study")
    p.add_argument("--verify-final60-data",type=Path,help="independently repack the sealed 60-step winners")
    p.add_argument("--history",action="store_true",help="reproduce the historical pilot examples and replay summary")
    args=p.parse_args()
    if args.final60 or args.verify_final60_data:final60(args.verify_final60_data)
    elif args.history:history()
    else:
        for b in ([args.budget] if args.budget else [60,200]):study(b,args.all_search)


if __name__=="__main__":
    main()
