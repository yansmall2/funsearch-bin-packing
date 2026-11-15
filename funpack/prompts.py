"""Matched prompt variants; only search-set parent scores may enter a prompt."""

import json

from .programs import language_prompt

ENHANCED_VERSIONS = ("enhanced_v1", "mechanism_v3", "mechanism_v4", "mechanism_v3c",
                     "mechanism_v5", "mechanism_v5b")

# Prompt version -> search-memory semantics version (see search_context).
MEMORY_VERSIONS = {"mechanism_v4": "v1", "mechanism_v5": "v2", "mechanism_v5b": "v2"}

# Prompt version -> method-record label for the injected memory.
MEMORY_LABELS = {"mechanism_v4": "mechanism_v4_memory_v1",
                 "mechanism_v5": "mechanism_v5_memory_v1",
                 "mechanism_v5b": "mechanism_v5b_memory_v1"}


def search_context_label(enhanced_version):
    """Method-record label for a version's memory, or "off" when it has none."""
    return MEMORY_LABELS.get(enhanced_version, "off")


def memory_style(mode, enhanced_version):
    """How (if at all) memory is injected for this mode + version.

    "full" -- the multi-line memory block in the enhanced prompt
    (mechanism_v4/v5). "compact" -- one short memory line in the basic
    prompt (mechanism_v5b). None -- no memory is injected. This is the
    single source of truth shared by search.py (wiring) and protocol.py
    (expected method); basic + v4/v5 injects nothing, by design -- the
    v4 memory block only exists in the enhanced prompt.
    """
    if mode == "enhanced" and enhanced_version in ("mechanism_v4", "mechanism_v5"):
        return "full"
    if mode == "basic" and enhanced_version == "mechanism_v5b":
        return "compact"
    return None

# mechanism_v3c appends exactly this paragraph to the mechanism_v3 text.
# v3 itself is byte-identical with or without this parameter.
COMBINATION_PARAGRAPH = (
    "Combine the parents: you are shown two parent programs. You may take "
    "the residual preference of one parent and the conditional rule of the "
    "other, or merge their scoring terms into a single coherent priority "
    "function, as long as the result can change the ranking relative to the "
    "best parent. A combination must be one unified rule, not the two codes "
    "pasted together. If neither parent mechanism helps, you may still "
    "propose a fresh mechanism instead."
)


def prompt_version(mode, enhanced_version="enhanced_v1"):
    if mode not in ("basic", "enhanced") or enhanced_version not in ENHANCED_VERSIONS:
        raise ValueError("unknown prompt mode or enhanced version")
    if mode == "basic":
        # mechanism_v5b is the basic prompt with a compact memory line;
        # every other version leaves the basic prompt at basic_v1.
        return "basic_v2" if enhanced_version == "mechanism_v5b" else "basic_v1"
    if enhanced_version == "mechanism_v5b":
        raise ValueError("mechanism_v5b is a basic-only prompt version")
    return enhanced_version


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

    mechanism_v5 is mechanism_v4's text with the v2 memory semantics
    (see search_context.render): the best family is labelled refinable
    instead of avoid-listed, and only saturated families worse than the
    best are avoided. v5 requires search_context. A v5 message equals
    the v3 message with exactly the memory block inserted, same as v4.

    mechanism_v5b is the basic prompt with a compact one-line memory
    (search_context.render_compact), inserted before the return
    instruction; basic prompts have no Strategy text, so the memory must
    fit in one short instruction line. v5b requires search_context and
    is basic-only. A basic prompt with mechanism_v4/v5 and a search_context
    raises instead of silently dropping the memory (the v4 silent-drop
    trap: basic + v4 looked like a memory run but was byte-identical to
    basic_v1); use v5b to give basic arms memory.

    mechanism_v3c is mechanism_v3 plus the combination paragraph
    (COMBINATION_PARAGRAPH) appended after the v3 text. v3c requires no
    search_context. The v3 text is a strict prefix of the v3c text.
    """
    # Keep the legacy default for historical callers. New experiment plans pin v3.
    prompt_version(mode, enhanced_version)
    if search_context is not None and memory_style(mode, enhanced_version) is None:
        raise ValueError("search_context is only supported by mechanism_v4/mechanism_v5 "
                         "(enhanced) and mechanism_v5b (basic)")
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
    if mode == "enhanced" and enhanced_version in ("mechanism_v3", "mechanism_v4", "mechanism_v3c", "mechanism_v5"):
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
        if enhanced_version in ("mechanism_v4", "mechanism_v5"):
            if search_context is None:
                raise ValueError(f"{enhanced_version} requires search_context")
            marker = "Choose one mechanism to explore:"
            user = user.replace(marker, search_context + "\n\n" + marker, 1)
        elif enhanced_version == "mechanism_v3c":
            user = user + "\n\n" + COMBINATION_PARAGRAPH
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
        if enhanced_version == "mechanism_v5b":
            if search_context is None:
                raise ValueError("mechanism_v5b requires search_context")
            user += search_context + "\n\n"
        user += "Return only the Python function in one ```python code block."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
