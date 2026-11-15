#!/usr/bin/env python3
"""Validate a generator response text, atomically write it, and append to generations.jsonl.
Usage: python3 submit.py <request_index> <response_text_file>
"""
import json, os, sys, time

IDX = int(sys.argv[1])
TEXT_PATH = sys.argv[2]
BASE = "/home/hatch/workspace/funsearch-cloud/handshake-ctxv4/D_seed11"

sys.path.insert(0, "/home/hatch/workspace/funsearch-cloud")
from funpack.programs import extract_response, Program

with open(TEXT_PATH) as f:
    text = f.read()

res = extract_response(text, "enhanced")
assert res["format_ok"], f"format errors: {res['format_errors']}"
code = res["code"]
assert code and code.strip(), "empty code"
prog = Program(code)
# small-example execution checks on feasible bins only
out1 = prog(50, [60, 80])
assert isinstance(out1, list) and len(out1) == 2, f"bad output {out1}"
out2 = prog(30, [30, 45, 100])
assert isinstance(out2, list) and len(out2) == 3, f"bad output {out2}"
out3 = prog(10, [10])
assert isinstance(out3, list) and len(out3) == 1
for o in (out1, out2, out3):
    for v in o:
        assert isinstance(v, (int, float)) and abs(v) <= 1e12, f"bad score {v}"
print(f"self-check OK idx={IDX} strategy={res['strategy']!r}")

resp_path = os.path.join(BASE, "responses", f"request_{IDX:06d}.json")
tmp_path = resp_path + ".tmp"
with open(tmp_path, "w") as f:
    json.dump({"text": text}, f)
os.replace(tmp_path, resp_path)

gen_path = os.path.join(BASE, "generations.jsonl")
with open(gen_path, "a") as f:
    f.write(json.dumps({
        "request_index": IDX,
        "strategy": res["strategy"],
        "code": code,
        "timestamp": time.time(),
    }) + "\n")
print(f"wrote {resp_path} and appended generations.jsonl")
