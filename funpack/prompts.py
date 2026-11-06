"""Matched prompt variants; only search-set parent scores may enter a prompt."""

import json

from .programs import language_prompt


def build_messages(mode, parents):
    """Build the basic/enhanced prompt with an intentionally narrow parent schema.

    Unknown parent keys (test results, probe signatures, audit outcomes, etc.)
    are never serialized. Enhanced adds a brief strategy description, not an
    additional LLM call or a request for private step-by-step reasoning.
    """
    if mode not in ("basic", "enhanced"):
        raise ValueError("mode must be 'basic' or 'enhanced'")
    clean_parents = []
    for parent in parents:
        code = parent.get("code")
        if not isinstance(code, str):
            raise ValueError("each parent must supply code as a string")
        entry = {"code": code}
        for key in ("counts", "mean_bins"):
            if key in parent:
                entry[key] = parent[key]
        clean_parents.append(entry)
    system = (
        "Design a deterministic online bin-packing heuristic. You are given "
        "the current item's size and only feasible existing bins' remaining "
        "capacities. Return one score per feasible bin; higher wins and ties "
        "choose the first bin. The evaluator opens a new bin only when no "
        "existing bin fits. Minimize the number of bins used across the search "
        "instances. You cannot see future items.\n\n" + language_prompt()
    )
    user = (
        "Below are previously evaluated candidate functions and their "
        "search-set bin counts or mean bin count (lower is better). These are "
        "search feedback, not test-set results. Propose one useful variation.\n"
        + json.dumps(clean_parents, ensure_ascii=False, allow_nan=False)
        + "\n\n"
    )
    if mode == "enhanced":
        user += (
            "Start with 'Strategy:' and at most 80 English words describing "
            "the intended change in bin-choice behavior and a situation where "
            "that change occurs. Renaming variables or applying an order-preserving "
            "transformation to every score does not constitute a decision change. "
            "Performance claims are hypotheses to evaluate. Then provide the Python function in one "
            "```python code block in this same response. Give a concise "
            "strategy summary only, not a step-by-step reasoning trace."
        )
    else:
        user += "Return only the Python function in one ```python code block."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
