"""Evaluate literature-inspired seed candidates on the search set (selection)
and validation set (descriptive only). Seeds must be valid DSL programs.

Selection discipline: which seeds enter the new study is decided by
literature + DSL-expressibility + diversity + SEARCH-set performance only.
Validation numbers below are descriptive context, not selection criteria.
"""
import json
import sys

sys.path.insert(0, ".")
from funpack.packing import evaluate
from funpack.programs import Program

SEEDS = {
    "seed_ff (existing)": "def priority(item, bins):\n    return [0.0 for r in bins]\n",
    "seed_bf (existing)": "def priority(item, bins):\n    return [-(r - item) for r in bins]\n",
    # Worst Fit: prefer the emptiest bin. Genuinely different mechanism.
    "seed_wf": "def priority(item, bins):\n    return [r for r in bins]\n",
    # Size-gated hybrid: large items -> Best Fit, small items -> Worst Fit.
    "seed_hybrid_bf_wf": (
        "def priority(item, bins):\n"
        "    return [(-(r - item) if item > 50 else r) for r in bins]\n"
    ),
    # Harmonic-Match-inspired (NOT HM itself: HM needs per-bin class memory,
    # which the stateless priority(item, bins) DSL cannot express).
    # Small items prefer bins already holding a large item (residual <= 50),
    # tight fit among those; large items -> Best Fit.
    "seed_hm_inspired": (
        "def priority(item, bins):\n"
        "    return [((-(r - item)) if r <= 50 else (-1000.0 - (r - item)))"
        " if item <= 50 else (-(r - item)) for r in bins]\n"
    ),
}

def main():
    search = json.load(open("data/search.json"))["instances"]
    validation = json.load(open("data/validation.json"))["instances"]
    print(f"search instances: {len(search)}, validation instances: {len(validation)}")
    for name, code in SEEDS.items():
        program = Program(code)  # raises ProgramError if not valid DSL
        s = evaluate(program, search)
        v = evaluate(program, validation)
        print(f"{name}: search mean {s['mean_bins']:.3f}  validation mean {v['mean_bins']:.3f}")
        print(f"    search counts: {s['counts']}")
    print("Baselines for reference: search BF 265.3 / FF 269.5; validation BF 267.7 / FF 272.4")

if __name__ == "__main__":
    main()
