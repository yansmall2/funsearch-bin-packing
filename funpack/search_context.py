"""Compact search memory for the generator: family summaries, not raw history.

Context engineering, not accumulation. The generator's prompt already carries
two parent programs; dumping every past candidate on top of that would bloat
the prompt, repeat saturated structures, and teach the generator to copy
instead of explore. This module keeps a small structured summary instead:

- Store: one row per mechanism FAMILY (structure signature), with tries and
  best search-set mean. Never store raw candidate code or full history.
- Classify: deterministic AST analysis of the candidate code. A family is
  defined by STRUCTURE -- the residual windows it conditions on plus
  structural flags (mod gate, count gate, triangular peak, item-scaled
  bonus, ratio gate, exact-fit handling, distribution use, worst-fit
  fallback).
  Bonus sizes and constants do NOT define a family; retuning a constant
  inside the same windows is the same family.
- Select: render() emits at most MAX_WORDS words, priority order:
  saturated families first (as an avoid-list), then the best family,
  then archetypes untried in this run. Lowest-priority lines are dropped
  first when the budget binds.
- Update: incremental, O(1) per observation; no re-scan of history.
- Scope: one run only; only search-set means enter. Validation, held-out
  and test results never enter, exactly like parent feedback.
"""

from __future__ import annotations

import ast

DEFAULT_MAX_WORDS = 120
SATURATED_TRIES = 3
MAX_SATURATED_LINES = 3
MAX_UNTRIED_LINES = 2

# Fixed archetype vocabulary, used only to name structures with zero tries
# in this run. These are suggestions for unexplored regions, not claims.
ARCHETYPES = (
    ("count-gated bands", "count_gate"),
    ("triangular peak", "triangular"),
    ("item-scaled bonus", "scaled"),
    ("ratio-gated bounds", "ratio"),
    ("distribution-relative windows", "dist"),
    ("exact-fit bonus only", "exact_fit"),
)


def _residual(node):
    """True if node is (r - item) for loop variable r and current item."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and isinstance(node.left, ast.Name)
        and node.left.id == "r"
        and isinstance(node.right, ast.Name)
        and node.right.id == "item"
    )


def _const(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    return None


def _contains_item(node):
    """True if an expression mentions the current item size."""
    return any(isinstance(sub, ast.Name) and sub.id == "item" for sub in ast.walk(node))


def _window_sort_key(window):
    lo, hi = window
    return (lo is None, lo if lo is not None else 0, hi is None, hi if hi is not None else 0)


def _intervals_and_flags(code):
    """Extract residual windows + structural flags from candidate code.

    Windows come from comparisons on (r - item): `lo <= r-item <= hi`,
    `r-item >= lo and r-item <= hi`, one-sided bounds included. A bound
    inside an `and` only counts as part of a closed pair, so the same band
    written two ways yields one signature.
    Returns (sorted tuple of (lo, hi) with None for open ends, flag set).
    Unparseable code yields no windows and no flags (family "plain").
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return (), frozenset()
    windows = set()
    flags = set()
    # Compare nodes inside an `and` contribute only closed pairs below.
    and_compares = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Compare):
                    and_compares.add(id(sub))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            flags.add("mod")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "len":
                flags.add("count_gate")
            elif node.func.id == "abs":
                flags.add("triangular")
            elif node.func.id in ("max", "min", "sum"):
                flags.add("dist")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if (isinstance(node.left, ast.Name) and node.left.id == "item") or (
                isinstance(node.right, ast.Name) and node.right.id == "item"
            ):
                flags.add("scaled")
        if isinstance(node, ast.Compare):
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq):
                    if (_residual(node.left) and _const(comp) == 0) or (
                        _residual(comp) and _const(node.left) == 0
                    ):
                        flags.add("exact_fit")
                # Residual gated by a ratio of the item (r-item vs item/3,
                # item, ...) is a different structure from constant windows.
                if (_residual(node.left) and _contains_item(comp)) or (
                    _residual(comp) and _contains_item(node.left)
                ):
                    flags.add("ratio")
            # Chained comparison: lo <= r-item <= hi.
            if len(node.ops) == 2 and _residual(node.comparators[0]):
                if isinstance(node.ops[0], (ast.LtE, ast.Lt)) and isinstance(
                    node.ops[1], (ast.LtE, ast.Lt)
                ):
                    lo, hi = _const(node.left), _const(node.comparators[1])
                    if lo is not None and hi is not None:
                        windows.add((lo, hi))
            # One-sided bounds, unless inside an `and` (paired below).
            if id(node) not in and_compares:
                for op, comp in zip(node.ops, node.comparators):
                    value = _const(comp)
                    if value is None:
                        continue
                    if _residual(node.left):
                        if isinstance(op, (ast.GtE, ast.Gt)):
                            windows.add((value, None))
                        elif isinstance(op, (ast.LtE, ast.Lt)):
                            windows.add((None, value))
                    elif _residual(comp):
                        if isinstance(op, (ast.GtE, ast.Gt)):
                            windows.add((None, value))
                        elif isinstance(op, (ast.LtE, ast.Lt)):
                            windows.add((value, None))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            los, his = [], []
            for value in node.values:
                if not isinstance(value, ast.Compare):
                    continue
                for op, comp in zip(value.ops, value.comparators):
                    bound = _const(comp)
                    if bound is None:
                        continue
                    if _residual(value.left):
                        if isinstance(op, (ast.GtE, ast.Gt)):
                            los.append(bound)
                        elif isinstance(op, (ast.LtE, ast.Lt)):
                            his.append(bound)
            for lo in los:
                for hi in his:
                    if lo <= hi:
                        windows.add((lo, hi))
    # A bare residual (or r) used as a score is a worst-fit fallback.
    for node in ast.walk(tree):
        if isinstance(node, ast.ListComp):
            if (isinstance(node.elt, ast.Name) and node.elt.id == "r") or _residual(node.elt):
                flags.add("worst_fit")
        if isinstance(node, ast.IfExp):
            for branch in (node.body, node.orelse):
                if (isinstance(branch, ast.Name) and branch.id == "r") or _residual(branch):
                    flags.add("worst_fit")
    return tuple(sorted(windows, key=_window_sort_key)), frozenset(sorted(flags))


def classify(code):
    """Structure signature of a candidate: windows + flags as a string.

    Same windows + same flags = same family, whatever the constants.
    """
    windows, flags = _intervals_and_flags(code)
    parts = []
    if windows:
        rendered = ";".join(
            f"[{'' if lo is None else lo},{'' if hi is None else hi}]" for lo, hi in windows
        )
        parts.append(f"windows{rendered}")
    parts.extend(sorted(flags))
    return "+".join(parts) if parts else "plain"


class SearchContext:
    """Per-run family table with a hard word budget on rendering."""

    def __init__(self, max_words=DEFAULT_MAX_WORDS):
        if type(max_words) is not int or max_words < 20:
            raise ValueError("max_words must be an int >= 20")
        self.max_words = max_words
        self.families = {}
        self.total = 0

    def observe(self, code, mean_bins):
        signature = classify(code)
        row = self.families.setdefault(signature, {"tries": 0, "best": None})
        row["tries"] += 1
        row["best"] = mean_bins if row["best"] is None else min(row["best"], mean_bins)
        self.total += 1
        return signature

    def _word_count(self, text):
        return len(text.split())

    def render(self):
        lines = ["Search memory (this run only; search-set means, lower is better):"]
        saturated = sorted(
            ((sig, row) for sig, row in self.families.items() if row["tries"] >= SATURATED_TRIES),
            key=lambda kv: (-kv[1]["tries"], kv[1]["best"]),
        )[:MAX_SATURATED_LINES]
        for sig, row in saturated:
            lines.append(f"Avoid re-trying {sig}: tried {row['tries']}x, best {row['best']:.3f}.")
        if self.families:
            best_sig, best_row = min(
                self.families.items(), key=lambda kv: (kv[1]["best"], -kv[1]["tries"])
            )
            lines.append(f"Best so far: {best_sig} ({best_row['best']:.3f}).")
        tried_flags = set()
        for sig in self.families:
            for flag in sig.split("+"):
                if flag.startswith("windows"):
                    continue
                tried_flags.add(flag)
        untried = [label for label, flag in ARCHETYPES if flag not in tried_flags]
        for label in untried[:MAX_UNTRIED_LINES]:
            lines.append(f"Untried here: {label}.")
        # Enforce the hard budget by dropping lowest-priority lines first
        # (untried hints, then saturated lines beyond the first). Header and
        # best-so-far are never dropped. Recompute each round: indices shift.
        text = "\n".join(lines)
        while self._word_count(text) > self.max_words:
            untried_idx = [i for i, line in enumerate(lines) if line.startswith("Untried here:")]
            if untried_idx:
                lines.pop(untried_idx[-1])
            else:
                sat_idx = [i for i, line in enumerate(lines) if line.startswith("Avoid re-trying")]
                if len(sat_idx) <= 1:
                    break
                lines.pop(sat_idx[-1])
            text = "\n".join(lines)
        return text

    def to_dict(self):
        return {
            "max_words": self.max_words,
            "total": self.total,
            "families": {
                sig: {"tries": row["tries"], "best": row["best"]}
                for sig, row in sorted(self.families.items())
            },
        }

    @classmethod
    def from_dict(cls, data):
        context = cls(max_words=data["max_words"])
        context.total = data["total"]
        context.families = {
            sig: {"tries": row["tries"], "best": row["best"]}
            for sig, row in data["families"].items()
        }
        return context
