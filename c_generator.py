#!/usr/bin/env python3
"""C-arm generator coordinator for the warmstart-150 FunSearch study.

Authored by the Muse coordinator (arm C). No paid API is used: every answer
is composed locally by this script from a hand-authored mechanism library,
combining the two parent mechanisms carried in each request (mechanism_v3c:
one parent's residual preference + the other's conditional rule, jittered,
plus an occasional fresh twist term), then validated with
funpack.programs.extract_response(..., "basic") + Program() before writing.

Only reads formal-handshake-warmstart/C_seed{11,22,33}/requests/.
Never touches other arms, data/, or any run_* directory.
"""
import json
import math
import os
import random
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
HS = os.path.join(BASE, "formal-handshake-warmstart")
SEEDS = (11, 22, 33)
TARGET_PER_SEED = 150
POLL_SECONDS = 5.0
LOG_PATH = os.path.join(BASE, "logs", "c_generator_C.log")

sys.path.insert(0, BASE)
from funpack.programs import extract_response, Program  # noqa: E402


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- parent feature detection ----------------

def find_windows(code):
    wins = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*<=\s*r\s*-\s*item\s*<=\s*(\d+(?:\.\d+)?)", code):
        wins.append((float(m.group(1)), float(m.group(2))))
    for m in re.finditer(r"r\s*-\s*item\s*>=\s*(\d+(?:\.\d+)?)[^;]*?r\s*-\s*item\s*<=\s*(\d+(?:\.\d+)?)", code):
        wins.append((float(m.group(1)), float(m.group(2))))
    out = []
    for lo, hi in wins:
        if lo < hi and (lo, hi) not in out:
            out.append((lo, hi))
    return out


def parent_features(code):
    feats = {}
    wins = find_windows(code)
    if wins:
        feats["windows"] = wins
    m = re.search(r"item\s*%\s*(\d+)\s*==\s*(\d+)", code)
    if m:
        feats["mod"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\(\(r\s*-\s*item\)\s*//\s*(\d+)\)\s*%\s*2\s*==\s*0", code)
    if m:
        feats["band"] = int(m.group(1))
    m = re.search(r"abs\(\(r\s*-\s*item\)\s*-\s*(\d+(?:\.\d+)?)\)", code)
    if m:
        feats["tricenter"] = float(m.group(1))
    if "item / 2" in code:
        feats["half"] = True
    if "r == item" in code or "r - item == 0" in code:
        feats["exact"] = True
    m = re.search(r"r\s*-\s*item\s*<=\s*(\d+(?:\.\d+)?)", code)
    if m:
        feats["threshold"] = float(m.group(1))
    if "sqrt" in code:
        feats["sqrt"] = True
    if re.search(r"-\s*\(r\s*-\s*item\)\s*\+\s*\(r\s*-\s*item\)", code):
        feats["flat"] = True
    return feats


# ---------------- term builders (expressions in r, item) ----------------

def _n(x):
    if float(x) == int(x):
        return str(int(x))
    return str(x)


def t_window(rng, lo, hi, bonus, sign=1):
    return f"({sign * bonus} if {_n(lo)} <= r - item <= {_n(hi)} else 0)", f"win[{_n(lo)},{_n(hi)}]{'+' if sign > 0 else '-'}{bonus}"


def t_tri(rng, lo, hi, c, bonus):
    return (f"(max(0, {bonus} - abs((r - item) - {_n(c)})) if {_n(lo)} <= r - item <= {_n(hi)} else 0)",
            f"tri[{_n(lo)},{_n(hi)}]@{_n(c)}")


def t_mod(rng, m, v, lo, hi, bonus):
    return (f"({bonus} if item % {m} == {v} and {_n(lo)} <= r - item <= {_n(hi)} else 0)",
            f"mod{m}={v}&win[{_n(lo)},{_n(hi)}]")


def t_band(rng, w, bonus, sign=1):
    return (f"({sign * bonus} if ((r - item) // {w}) % 2 == 0 else 0)",
            f"band//{w}{'+' if sign > 0 else '-'}")


def t_ratio(rng, t, bonus):
    return f"({bonus} if item / r >= {t:.2f} else 0)", f"ratio>={t:.2f}"


def t_frac(rng, a, b, bonus):
    return (f"({bonus} if item / {a} < r - item <= item / {b} else 0)",
            f"frac(item/{a},item/{b})")


def t_tight(rng, thr, bonus):
    return f"({bonus} if r - item <= {_n(thr)} else 0)", f"tight<={_n(thr)}"


def t_loose(rng, thr, bonus):
    return f"({bonus} if r - item >= {_n(thr)} else 0)", f"loose>={_n(thr)}"


def t_exact(rng, bonus):
    return f"({bonus} if r == item else 0)", "exact"


def t_half(rng, lo, hi):
    return (f"(item / 2 if {_n(lo)} <= r - item <= {_n(hi)} else 0)",
            f"half&win[{_n(lo)},{_n(hi)}]")


def t_modres(rng, m, bonus):
    return f"({bonus} if (r - item) % {m} == 0 else 0)", f"modres%{m}"


def t_gapmod(rng, m):
    return f"(-((r - item) % {m}))", f"gapmod%{m}"


def jitter_window(rng, lo, hi):
    lo2 = max(0, lo + rng.choice([-4, -2, 0, 0, 2, 4]))
    hi2 = max(lo2 + 4, hi + rng.choice([-4, -2, 0, 0, 2, 4, 6]))
    return lo2, hi2


def parent_signature_term(rng, feats):
    """Turn one parent's detected signature into a jittered term."""
    cands = []
    if feats.get("windows"):
        cands.append("window")
    if feats.get("tricenter") is not None:
        cands.append("tri")
    if feats.get("mod"):
        cands.append("mod")
    if feats.get("band"):
        cands.append("band")
    if feats.get("half"):
        cands.append("half")
    if feats.get("exact"):
        cands.append("exact")
    if not cands:
        return None
    kind = rng.choice(cands)
    bonus = rng.choice([10, 12, 15, 18, 20, 25, 30])
    if kind == "window":
        lo, hi = rng.choice(feats["windows"])
        lo, hi = jitter_window(rng, lo, hi)
        sign = rng.choice([1, 1, 1, -1])
        return t_window(rng, lo, hi, bonus, sign)
    if kind == "tri":
        c = feats["tricenter"] + rng.choice([-5, -2, 0, 2, 5])
        lo, hi = (max(0, c - 10), c + 10)
        if feats.get("windows"):
            lo, hi = jitter_window(rng, *rng.choice(feats["windows"]))
        return t_tri(rng, lo, hi, c, bonus)
    if kind == "mod":
        m, v = feats["mod"]
        lo, hi = jitter_window(rng, *rng.choice(feats["windows"])) if feats.get("windows") else (25, 45)
        return t_mod(rng, m, v, lo, hi, bonus)
    if kind == "band":
        w = feats["band"] + rng.choice([-2, 0, 0, 2])
        w = max(2, w)
        return t_band(rng, w, bonus, rng.choice([1, 1, -1]))
    if kind == "half":
        lo, hi = jitter_window(rng, *rng.choice(feats["windows"])) if feats.get("windows") else (28, 42)
        return t_half(rng, lo, hi)
    if kind == "exact":
        return t_exact(rng, bonus)
    return None


def fresh_term(rng, used_fams):
    fams = ["window", "tri", "mod", "band", "ratio", "frac", "tight",
            "loose", "exact", "half", "modres", "gapmod"]
    avail = [f for f in fams if f not in used_fams] or fams
    kind = rng.choice(avail)
    bonus = rng.choice([8, 10, 12, 15, 18, 20, 25, 30])
    if kind == "window":
        lo = rng.choice([4, 6, 8, 10, 12, 20, 25, 28, 30])
        hi = lo + rng.choice([8, 10, 12, 15, 18, 20])
        return t_window(rng, lo, hi, bonus, rng.choice([1, 1, 1, -1]))
    if kind == "tri":
        c = rng.choice([15, 20, 25, 30, 35, 40])
        return t_tri(rng, max(0, c - 10), c + 10, c, bonus)
    if kind == "mod":
        m = rng.choice([3, 4, 5])
        v = rng.randrange(m)
        lo = rng.choice([20, 25, 28, 30])
        return t_mod(rng, m, v, lo, lo + rng.choice([12, 15, 18]), bonus)
    if kind == "band":
        return t_band(rng, rng.choice([5, 8, 10, 12]), bonus, rng.choice([1, 1, -1]))
    if kind == "ratio":
        return t_ratio(rng, rng.choice([0.5, 0.6, 0.7, 0.75, 0.8]), bonus)
    if kind == "frac":
        a, b = rng.choice([(3, 2), (4, 2), (4, 3), (5, 2), (3, 1)])
        return t_frac(rng, a, b, bonus)
    if kind == "tight":
        return t_tight(rng, rng.choice([5, 8, 10, 12, 15]), bonus)
    if kind == "loose":
        return t_loose(rng, rng.choice([40, 45, 50, 60]), bonus)
    if kind == "exact":
        return t_exact(rng, bonus)
    if kind == "half":
        lo = rng.choice([25, 28, 30])
        return t_half(rng, lo, lo + rng.choice([12, 14, 15]))
    if kind == "modres":
        return t_modres(rng, rng.choice([5, 10]), bonus)
    if kind == "gapmod":
        return t_gapmod(rng, rng.choice([5, 7, 10]))
    raise AssertionError(kind)


def pick_base(rng, parent_feats_list):
    r = rng.random()
    if r < 0.34:
        return "-(r - item)", "BF"
    if r < 0.44:
        k = rng.choice([2, 3])
        return f"-{k} * (r - item)", f"BFx{k}"
    if r < 0.54:
        return "-sqrt(r - item)", "sqrt"
    if r < 0.62:
        return "-log(1 + (r - item))", "log"
    if r < 0.70:
        s = rng.choice([50, 100, 200])
        return f"-((r - item) ** 2) / {s}", f"quad/{s}"
    if r < 0.78:
        k = rng.choice([2, 3, 4])
        return f"-(r - item) + (r - item) / {k}", f"flat/{k}"
    if r < 0.86:
        thr = None
        for f in parent_feats_list:
            if f.get("threshold") is not None:
                thr = f["threshold"]
                break
        if thr is None:
            thr = rng.choice([8, 10, 12])
        else:
            thr = max(4, thr + rng.choice([-3, -2, 0, 0, 2, 3]))
        c = rng.choice([100, 150, 200])
        k = rng.choice([2, 3])
        return (f"({_n(c)} - {k} * (r - item) if r - item <= {_n(thr)} else (r - item))",
                f"flip<={_n(thr)}")
    return "(r - item)", "worst"


# ---------------- validation ----------------

PROBES = [
    (30, [30, 35, 40, 50, 70, 100]),
    (10, [10, 12, 20, 33, 47, 80]),
    (25, [25, 28, 31, 45, 60, 90]),
    (40, [40, 44, 55, 61, 78, 99]),
    (60, [60, 65, 72, 88, 95, 100]),
    (17, [17, 19, 24, 30, 41, 53, 70]),
]


def ranking(scores):
    return tuple(sorted(range(len(scores)), key=lambda i: (-scores[i], i)))


def validate_code(code, parent_codes, seen_codes):
    """Returns (ok, reason)."""
    if code in parent_codes or code in seen_codes:
        return False, "duplicate-of-parent-or-emitted"
    text = "```python\n" + code + "```\n"
    try:
        p = extract_response(text, "basic")
    except Exception as exc:  # noqa: BLE001
        return False, f"extract-error:{exc}"
    if not p["format_ok"] or not p.get("code"):
        return False, f"format:{p.get('format_errors')}"
    try:
        prog = Program(p["code"])
    except Exception as exc:  # noqa: BLE001
        return False, f"program:{exc}"
    try:
        bf = Program("def priority(item, bins):\n    return [-(r - item) for r in bins]\n")
        diff_rank = False
        for item, bins in PROBES:
            out = prog(item, list(bins))
            if len(out) != len(bins) or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in out):
                return False, "bad-probe-output"
            if ranking(out) != ranking(bf(item, list(bins))):
                diff_rank = True
        if not diff_rank:
            return False, "order-equivalent-to-BF"
    except Exception as exc:  # noqa: BLE001
        return False, f"probe:{exc}"
    return True, ""


# ---------------- composition ----------------

def compose(rng, parent_codes):
    feats_list = [parent_features(c) for c in parent_codes]
    base_expr, base_fam = pick_base(rng, feats_list)
    terms, fams = [], set()
    for feats in feats_list:
        t = parent_signature_term(rng, feats)
        if t is not None:
            expr, fam = t
            key = fam.split("[")[0].split("&")[0]
            if key not in fams:
                terms.append(expr)
                fams.add(key)
                fams.add(fam)
    while len(terms) < 2:
        expr, fam = fresh_term(rng, fams)
        terms.append(expr)
        fams.add(fam.split("[")[0].split("&")[0])
        fams.add(fam)
    if rng.random() < 0.25:
        expr, fam = fresh_term(rng, fams)
        terms.append(expr)
        fams.add(fam)
    expr = base_expr + "".join(" + " + t for t in terms)
    code = "def priority(item, bins):\n    return [" + expr + " for r in bins]\n"
    sig = base_fam + "+" + "+".join(sorted(fams))
    return code, sig


def extract_parent_codes(messages):
    codes = []
    for m in messages or []:
        content = m.get("content", "") if isinstance(m, dict) else ""
        for blk in re.finditer(r"```python\r?\n([\s\S]*?)```", content):
            code = blk.group(1).strip()
            if code.startswith("def priority(") and code not in codes:
                codes.append(code)
    return codes


# ---------------- IO ----------------

def scan_seed(seed):
    d = os.path.join(HS, f"C_seed{seed}")
    req_dir = os.path.join(d, "requests")
    resp_dir = os.path.join(d, "responses")
    os.makedirs(resp_dir, exist_ok=True)
    pending = []
    if os.path.isdir(req_dir):
        for name in os.listdir(req_dir):
            if not name.endswith(".json"):
                continue
            resp = os.path.join(resp_dir, name)
            if os.path.exists(resp):
                continue
            try:
                with open(os.path.join(req_dir, name), encoding="utf-8") as f:
                    req = json.load(f)
                pending.append((int(req.get("request_index", 0)), name, req))
            except (ValueError, OSError, KeyError):
                continue
    pending.sort(key=lambda x: x[0])
    return d, pending


def count_responses(seed):
    resp_dir = os.path.join(HS, f"C_seed{seed}", "responses")
    if not os.path.isdir(resp_dir):
        return 0
    return sum(1 for n in os.listdir(resp_dir) if n.endswith(".json"))


def reconcile_generations(seed):
    """Ensure every response file has a generations.jsonl entry (crash safety)."""
    d = os.path.join(HS, f"C_seed{seed}")
    gen_path = os.path.join(d, "generations.jsonl")
    have = set()
    if os.path.exists(gen_path):
        with open(gen_path, encoding="utf-8") as f:
            for line in f:
                try:
                    have.add(json.loads(line)["request_id"])
                except (ValueError, KeyError):
                    continue
    resp_dir = os.path.join(d, "responses")
    req_dir = os.path.join(d, "requests")
    added = 0
    if os.path.isdir(resp_dir):
        with open(gen_path, "a", encoding="utf-8") as f:
            for name in sorted(os.listdir(resp_dir)):
                if not name.endswith(".json"):
                    continue
                rid = name[:-5]
                if rid in have:
                    continue
                try:
                    with open(os.path.join(resp_dir, name), encoding="utf-8") as rf:
                        text = json.load(rf)["text"]
                    idx = None
                    rp = os.path.join(req_dir, name)
                    if os.path.exists(rp):
                        with open(rp, encoding="utf-8") as qf:
                            idx = json.load(qf).get("request_index")
                    f.write(json.dumps({"arm": "C", "run": f"C_seed{seed}",
                                        "request_index": idx, "request_id": rid,
                                        "text": text}, ensure_ascii=False) + "\n")
                    added += 1
                except (ValueError, OSError, KeyError):
                    continue
    if added:
        log(f"C_seed{seed}: reconciled {added} missing generations.jsonl entries")


def answer_one(seed, req, seen_codes, stats):
    d = os.path.join(HS, f"C_seed{seed}")
    rid = req["request_id"]
    idx = int(req.get("request_index", 0))
    parent_codes = extract_parent_codes(req.get("messages"))
    attempts = 0
    while attempts < 40:
        attempts += 1
        rng = random.Random(f"C-{seed}-{idx}-{attempts}")
        code, sig = compose(rng, parent_codes)
        ok, reason = validate_code(code, parent_codes, seen_codes)
        if ok:
            break
        stats["rewrites"] += 1
        stats["rewrite_reasons"][reason] = stats["rewrite_reasons"].get(reason, 0) + 1
    else:
        log(f"C_seed{seed} {rid} idx={idx}: FAILED after 40 attempts")
        stats["failed"] += 1
        return False
    text = "```python\n" + code + "```\n"
    resp_dir = os.path.join(d, "responses")
    tmp = os.path.join(resp_dir, f".tmp-{rid}.json")
    final = os.path.join(resp_dir, f"{rid}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text}, f, ensure_ascii=False)
    os.replace(tmp, final)
    gen_path = os.path.join(d, "generations.jsonl")
    with open(gen_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"arm": "C", "run": f"C_seed{seed}", "request_index": idx,
                            "request_id": rid, "text": text}, ensure_ascii=False) + "\n")
    seen_codes.add(code)
    stats["answered"] += 1
    stats["families"][sig] = stats["families"].get(sig, 0) + 1
    if attempts > 1:
        log(f"C_seed{seed} {rid} idx={idx} answered after {attempts} attempts [{sig}]")
    return True


def main():
    stats = {"answered": 0, "rewrites": 0, "failed": 0,
             "rewrite_reasons": {}, "families": {}}
    seen_codes = set()
    for seed in SEEDS:
        reconcile_generations(seed)
        d = os.path.join(HS, f"C_seed{seed}")
        gen_path = os.path.join(d, "generations.jsonl")
        if os.path.exists(gen_path):
            with open(gen_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        code = extract_response(entry.get("text", ""), "basic").get("code", "")
                        if code:
                            seen_codes.add(code)
                    except (ValueError, KeyError):
                        continue
    log(f"coordinator start: existing responses " +
        ", ".join(f"seed{s}={count_responses(s)}" for s in SEEDS))
    while True:
        done = all(count_responses(s) >= TARGET_PER_SEED for s in SEEDS)
        if done:
            break
        progressed = False
        for seed in SEEDS:
            if count_responses(seed) >= TARGET_PER_SEED:
                continue
            _, pending = scan_seed(seed)
            if not pending:
                continue
            _, _, req = pending[0]
            if answer_one(seed, req, seen_codes, stats):
                progressed = True
                n = count_responses(seed)
                if n % 25 == 0 or n == TARGET_PER_SEED:
                    log(f"progress seed{seed}: {n}/{TARGET_PER_SEED} responses "
                        f"(total answered this run: {stats['answered']}, "
                        f"rewrites: {stats['rewrites']}, distinct families: {len(stats['families'])})")
        if not progressed:
            time.sleep(POLL_SECONDS)
    log("ALL DONE: " + ", ".join(f"seed{s}={count_responses(s)}" for s in SEEDS))
    log(f"stats: answered={stats['answered']} rewrites={stats['rewrites']} "
        f"failed={stats['failed']} distinct-families={len(stats['families'])}")
    log(f"rewrite reasons: {stats['rewrite_reasons']}")
    top = sorted(stats["families"].items(), key=lambda kv: -kv[1])[:15]
    log(f"top families: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
