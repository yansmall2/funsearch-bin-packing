"""Deterministic online bin packing with a small, explicit scorer contract.

Scores are maximized. Only already-open feasible bins reach the scorer, in
opening order; ties select the first. Future items are never passed to it.
"""

from __future__ import annotations

import hashlib
import json
import math
from numbers import Integral, Real
from typing import Callable, Iterable


def _positive_integer(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def validate_items(items: Iterable[int], capacity: int) -> tuple[list[int], int]:
    """Validate the whole input before scoring; empty instances are legal."""
    capacity = _positive_integer(capacity, "capacity")
    if isinstance(items, (str, bytes, dict)):
        raise ValueError("items must be an iterable of positive integers")
    try:
        values = list(items)
    except TypeError as exc:
        raise ValueError("items must be an iterable of positive integers") from exc
    result = []
    for index, value in enumerate(values):
        value = _positive_integer(value, f"items[{index}]")
        if value > capacity:
            raise ValueError(f"items[{index}] exceeds capacity")
        result.append(value)
    return result, capacity


def choose_index(scores, n: int) -> int:
    """Return the earliest maximum, rejecting wrong shapes and nonfinite values."""
    n = _positive_integer(n, "n")
    if isinstance(scores, (str, bytes, dict, set)):
        raise ValueError("scores must be a one-dimensional numeric sequence")
    if not hasattr(scores, "__len__") or not hasattr(scores, "__getitem__"):
        raise ValueError("scores must be a one-dimensional numeric sequence")
    if len(scores) != n:
        raise ValueError(f"expected {n} scores, got {len(scores)}")
    values = []
    for value in scores:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("each score must be a real number (not bool)")
        try:
            finite = math.isfinite(value)
        except (TypeError, OverflowError) as exc:
            raise ValueError("score is not representable as a finite number") from exc
        if not finite:
            raise ValueError("scores must be finite: NaN and infinity are invalid")
        values.append(value)
    return max(range(n), key=values.__getitem__)


def first_fit_scores(item: int, bins: list[int]) -> list[int]:
    """Equal scores plus earliest-tie rule implement First Fit (FF)."""
    return [0] * len(bins)


def best_fit_scores(item: int, bins: list[int]) -> list[int]:
    """Maximize negative post-placement free space: Best Fit (BF)."""
    return [-(remaining - item) for remaining in bins]


def worst_fit_scores(item: int, bins: list[int]) -> list[int]:
    """An auxiliary trajectory policy used to diversify development probes."""
    return [remaining - item for remaining in bins]


def pack(items, capacity: int, scorer: Callable, trace: bool = False,
         state_callback: Callable | None = None) -> dict:
    """Pack in arrival order, opening a bin only if no opened bin is feasible.

    Optional ``state_callback`` receives a fresh ``{item, bins, step}`` dict
    before scoring each feasible decision. ``bins`` contains feasible residual
    capacities in opening order; ``step`` is the zero-based item position.
    """
    items, capacity = validate_items(items, capacity)
    remaining: list[int] = []
    assignments: list[int] = []
    for step, item in enumerate(items):
        feasible_indices = [i for i, space in enumerate(remaining) if space >= item]
        if feasible_indices:
            feasible = [remaining[i] for i in feasible_indices]
            if state_callback is not None:
                state_callback({"item": item, "bins": feasible.copy(), "step": step})
            selected = choose_index(scorer(item, feasible.copy()), len(feasible))
            bin_index = feasible_indices[selected]
            remaining[bin_index] -= item
        else:
            bin_index = len(remaining)
            remaining.append(capacity - item)
        if trace:
            assignments.append(bin_index)
    result = {"bins_used": len(remaining), "remaining": remaining}
    if trace:
        result["assignments"] = assignments
    return result


def evaluate(scorer: Callable, instances: list[dict]) -> dict:
    """Evaluate one scorer without aggregating away per-instance observations."""
    if not instances:
        raise ValueError("evaluation needs at least one instance")
    counts, trace_hashes = [], []
    for instance in instances:
        result = pack(instance["items"], instance["capacity"], scorer, trace=True)
        counts.append(result["bins_used"])
        raw = json.dumps(result["assignments"], separators=(",", ":")).encode("utf-8")
        trace_hashes.append(hashlib.sha256(raw).hexdigest())
    return {"counts": counts, "mean_bins": sum(counts) / len(counts),
            "trace_hashes": trace_hashes}
