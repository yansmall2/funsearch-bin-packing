"""An isolated prompt variant and diagnostic; it never screens candidates out."""
import copy
import json
import re
import time

from funpack.packing import choose_index, evaluate
from funpack.programs import MAX_RESPONSE_CHARS, Program, extract_response
from funpack.prompts import build_messages
from funpack.storage import digest

VARIANTS = ("enhanced_v1", "witness_v2")


def reference_index(parents):
    if not parents:
        raise ValueError("a measured reference parent is required")
    return min(range(len(parents)), key=lambda i: (
        parents[i]["mean_bins"], len(parents[i]["code"]), digest(parents[i]["code"])))


def messages_for(variant, parents):
    messages = copy.deepcopy(build_messages("enhanced", parents))
    if variant == "enhanced_v1":
        return messages
    if variant != "witness_v2":
        raise ValueError("unknown diagnostic prompt variant")
    prefix, marker, _ = messages[1]["content"].rpartition("Start with 'Strategy:'")
    if not marker:
        raise ValueError("original prompt changed; review the diagnostic contrast")
    reference = reference_index(parents)
    messages[1]["content"] = prefix + (
        "Start with 'Strategy:' and at most 80 English words describing one intended "
        "change in bin-choice behavior and why it might help packing. Performance "
        "claims are hypotheses to evaluate. Renaming variables or applying an "
        "order-preserving score transformation does not constitute a decision change. "
        f"Use parent index {reference} (zero-based, in the list above) as the reference; "
        "it is the best supplied parent under the fixed mean/length/hash rule. "
        "Construct one feasible decision state where your new function chooses a "
        "different bin from this reference, not merely from a weaker parent. "
        "After the strategy, write exactly one line starting 'Witness: ' followed by "
        "a JSON object with exactly four keys: item, bins, parent_choice, candidate_choice. "
        "item must be an integer from 1 to 100. bins must contain 2 to 8 integer residual "
        "capacities, each between item and 100. Both choices are zero-based indices in "
        "that list, calculated from the actual functions: highest score wins, earliest "
        "index breaks ties. The two choices must differ. The example is checked by "
        "executing the functions; a valid example alone does not establish packing quality. "
        "Then provide exactly one Python function in one ```python code block. "
        "Give the concise strategy and example only, without a step-by-step reasoning trace."
    )
    return messages


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate witness JSON key")
        result[key] = value
    return result


def parse_response(text, variant):
    if variant not in VARIANTS:
        raise ValueError("unknown diagnostic prompt variant")
    if variant == "enhanced_v1":
        return {**extract_response(text, "enhanced"), "witness": None, "witness_parse_error": None}
    if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
        return {**extract_response(text, "enhanced"), "witness": None,
                "witness_parse_error": "invalid response length/type"}
    # Remove only a separate witness line BEFORE the first fence. Candidate
    # source is never transformed, and no JSON/Python is evaluated as code.
    header, separator, rest = text.partition("```")
    found = re.findall(r"(?m)^Witness:[ \t]*(.*)$", header)
    cleaned = re.sub(r"(?m)^Witness:[^\r\n]*(?:\r?\n|$)", "", header) + separator + rest
    parsed = extract_response(cleaned, "enhanced")
    witness = None
    issue = None
    try:
        if len(found) != 1 or len(found[0]) > 2048:
            raise ValueError("expected one short Witness JSON line before code")
        witness = json.loads(found[0], object_pairs_hook=_unique_json_object)
        if not isinstance(witness, dict) or set(witness) != {"item", "bins", "parent_choice", "candidate_choice"}:
            raise ValueError("wrong witness keys")
        item, bins = witness["item"], witness["bins"]
        if type(item) is not int or not 1 <= item <= 100:
            raise ValueError("witness item must be an integer in [1,100]")
        if (type(bins) is not list or not 2 <= len(bins) <= 8
                or any(type(r) is not int or not item <= r <= 100 for r in bins)):
            raise ValueError("witness bins must be 2-8 feasible integer residuals <=100")
        if any(type(witness[k]) is not int or not 0 <= witness[k] < len(bins)
               for k in ("parent_choice", "candidate_choice")):
            raise ValueError("witness choices must be valid zero-based integer indices")
    except (ValueError, TypeError, RecursionError) as error:
        issue = str(error)
        witness = None
    if issue:
        parsed["format_errors"].append("witness: " + issue)
        parsed["format_ok"] = False
    return {**parsed, "witness": witness, "witness_parse_error": issue}


def check_witness(parsed, reference_code):
    witness = parsed.get("witness")
    if witness is None:
        return {"status": "missing_or_invalid", "verified_change": False}
    try:
        item, bins = witness["item"], witness["bins"]
        parent_choice = choose_index(Program(reference_code)(item, list(bins)), len(bins))
        candidate_choice = choose_index(Program(parsed["code"])(item, list(bins)), len(bins))
        accurate = (parent_choice == witness["parent_choice"]
                    and candidate_choice == witness["candidate_choice"])
        changed = parent_choice != candidate_choice
        return {"status": ("verified_change" if accurate and changed else
                           "incorrect_claim" if not accurate else "same_choice"),
                "verified_change": accurate and changed, "actual_parent_choice": parent_choice,
                "actual_candidate_choice": candidate_choice, "claimed_choices_correct": accurate}
    except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
        return {"status": "execution_error", "verified_change": False, "error_type": type(error).__name__}


def evaluate_candidate(parsed, reference, bf, instances, timeout=30):
    """Always attempt full search evaluation if source is recoverable, regardless
    of witness status. Observational diagnostics cannot change selection here.
    """
    start = time.perf_counter()
    try:
        if not parsed.get("code"):
            return {"status": "extraction_failed", "valid_search_evaluation": False}
        program = Program(parsed["code"])
        deadline = time.perf_counter() + timeout

        def bounded(item, bins):
            if time.perf_counter() > deadline:
                raise RuntimeError("diagnostic evaluation deadline exceeded")
            return program(item, bins)

        result = evaluate(bounded, instances)
        if time.perf_counter() > deadline:
            raise RuntimeError("diagnostic evaluation deadline exceeded")
        return {"status": "evaluated", "valid_search_evaluation": True, "result": result,
                "novel_vs_reference_on_search": result["trace_hashes"] != reference["trace_hashes"],
                "novel_vs_bf_on_search": result["trace_hashes"] != bf["trace_hashes"],
                "mean_delta_vs_bf": result["mean_bins"] - bf["mean_bins"],
                "evaluation_seconds": time.perf_counter() - start}
    except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
        return {"status": "candidate_error", "valid_search_evaluation": False,
                "error_type": type(error).__name__, "evaluation_seconds": time.perf_counter() - start}


def summarize(events, expected_pairs=32):
    rows = []
    for variant in VARIANTS:
        samples = [e for e in events if e.get("variant") == variant and "response" in e]
        valid = [e for e in samples if e.get("evaluation", {}).get("valid_search_evaluation")]
        rows.append({"variant": variant, "requests": len(samples),
                     "valid_search_evaluations": len(valid),
                     "format_ok": sum(e.get("parsed", {}).get("format_ok", False) for e in samples),
                     "verified_witness_changes": sum(e.get("witness_check", {}).get("verified_change", False) for e in samples),
                     "search_novel_vs_reference": sum(e["evaluation"]["novel_vs_reference_on_search"] for e in valid),
                     "search_novel_vs_bf": sum(e["evaluation"]["novel_vs_bf_on_search"] for e in valid),
                     "novel_and_no_worse_than_bf": sum(e["evaluation"]["novel_vs_bf_on_search"] and e["evaluation"]["mean_delta_vs_bf"] <= 0 for e in valid),
                     "best_generated_search_mean": min((e["evaluation"]["result"]["mean_bins"] for e in valid), default=None),
                     "input_tokens": sum(e["response"].get("usage", {}).get("input_tokens", 0) or 0 for e in samples),
                     "output_tokens": sum(e["response"].get("usage", {}).get("output_tokens", 0) or 0 for e in samples),
                     "unknown_cost_requests": sum(not e["response"].get("cost_known", False) for e in samples),
                     "generation_seconds": sum(e.get("generation_seconds", 0) for e in samples),
                     "witness_check_seconds": sum(e.get("witness_check_seconds", 0) for e in samples),
                     "evaluation_seconds": sum(e.get("evaluation", {}).get("evaluation_seconds", 0) for e in samples),
                     "estimated_cost_cny": sum(e["response"].get("estimated_cost", 0) or 0 for e in samples)})
    complete = (len(events) == 2 * expected_pairs and all(r["requests"] == expected_pairs for r in rows)
                and all({e["variant"] for e in events if e["context_index"] == i} == set(VARIANTS)
                        for i in range(expected_pairs)))
    old, new = rows
    checks = {"complete": complete, "v2_valid_at_least_90_percent": new["valid_search_evaluations"] >= .9 * expected_pairs,
              "v2_verified_witness_at_least_75_percent": new["verified_witness_changes"] >= .75 * expected_pairs,
              "v2_search_novelty_gain_at_least_4_candidates": new["search_novel_vs_reference"] >= old["search_novel_vs_reference"] + 4,
              "v2_has_novel_candidate_no_worse_than_bf": new["novel_and_no_worse_than_bf"] >= 1}
    return {"kind": "fixed_parent_prompt_diagnostic_not_evolution", "complete": complete,
            "rows": rows, "operational_review_checks": checks,
            "next_step": "review_v2_for_formal_plan" if all(checks.values()) else "review_limitations_without_automatic_retries",
            "rule_note": "Prospective engineering triage, not statistical significance or proof of quality. No automatic adoption or formal run.",
            "heldout_evaluated": False, "validation_used": False}
