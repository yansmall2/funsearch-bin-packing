"""Matched prompt variants; only search-set parent scores may enter a prompt."""

import json

from .programs import language_prompt

ENHANCED_VERSIONS = ("enhanced_v1", "mechanism_v3", "mechanism_v4")


def prompt_version(mode, enhanced_version="enhanced_v1"):
    if mode not in ("basic", "enhanced") or enhanced_version not in ENHANCED_VERSIONS:
        raise ValueError("unknown prompt mode or enhanced version")
    return "basic_v1" if mode == "basic" else enhanced_version


def build_messages(mode, parents, *, enhanced_version="enhanced_v1", search_context=None):
    """Build the basic/enhanced prompt with an intentionally narrow parent schema.

    Unknown parent keys (test results, probe signatures, audit outcomes, etc.)
    are never serialized. Enhanced adds a brief strategy description, not an
    additional LLM call or a request for private step-by-step reasoning.

    mechanism_v4 is mechanism_v3 plus a compact search-memory block
    (see funpack.search_context): family summaries with a hard word budget,
    never raw candidate history. v4 requires search_context; passing it with
    any other version is a misuse error. The v3 text is byte-identical
    with or without this parameter.
    """
    # Keep the legacy default for historical callers. New experiment plans pin v3.
    prompt_version(mode, enhanced_version)
    if search_context is not None and enhanced_version != "mechanism_v4":
        raise ValueError("search_context is only supported by mechanism_v4")
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
    if mode == "enhanced" and enhanced_version in ("mechanism_v3", "mechanism_v4"):
        user = (
            "The following programs and measured search-set bin counts are reference "
            "strategies. Lower counts are better. Design one candidate with a coherent "
            "decision mechanism that can change which feasible bin is selected relative "
            "to the best supplied parent. You may redesign the scoring rule rather than "
            "edit its constants. The objective remains fewer bins; novelty alone is not success.\n"
            + json.dumps(clean_parents, ensure_ascii=False, allow_nan=False) + "\n\n"
            "Choose one mechanism to explore: a piecewise preference over post-placement "
            "residuals; a preference depending on the distribution of the currently feasible "
            "residual capacities; or a conditional tradeoff between filling tightly and "
            "preserving another useful residual size. These are suggestions, not guaranteed "
            "improvements. Use only information in item and bins; never assume knowledge of "
            "future items or access to unavailable bin state.\n\n"
            "For a fixed item, a score that is strictly decreasing in residual capacity will "
            "retain Best Fit's ranking. Adding the same item-dependent constant to every "
            "score also leaves the ranking unchanged. Avoid proposing such transformations "
            "as a new decision mechanism. A rule may agree with Best Fit in many states, "
            "but the intended change should have a clear condition under which the ranking "
            "can differ. Do not sacrifice feasibility or return an invalid score list.\n\n"
            "Start with 'Strategy:' and use at most 80 English words to identify the "
            "mechanism, the condition under which the preferred bin may change, and one "
            "reason it could help or fail. Treat quality claims as hypotheses. Do not output "
            "predicted witness indices or a step-by-step reasoning trace. Then provide one "
            "complete priority(item, bins) function in exactly one Python code block using "
            "the permitted language. Local execution will measure behavior and packing "
            "quality independently of your description."
        )
        if enhanced_version == "mechanism_v4":
            if search_context is None:
                raise ValueError("mechanism_v4 requires search_context")
            marker = "Choose one mechanism to explore:"
            user = user.replace(marker, search_context + "\n\n" + marker, 1)
    elif mode == "enhanced":
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
