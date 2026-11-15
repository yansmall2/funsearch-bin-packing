"""Prospective configuration and one-use final evaluation guards.

These records catch accidental changes; they are not cryptographic proof that a
person with write access has never inspected or changed the data. Freezing and
sealing do not open held-out files. Only ``evaluate_final`` can opt in to them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

from .data import load_instances, load_probes
from .packing import evaluate
from .programs import BF_CODE, FF_CODE, Program
from .prompts import ENHANCED_VERSIONS, prompt_version
from .screening import COLLISION_POLICIES
from .providers import _contains_secret_field, preflight
from .storage import digest, environment, read_json, write_json


CORE_FILES = ("experiment.py", "formal.py") + tuple(
    "funpack/" + name + ".py" for name in (
        "__init__", "data", "database", "handshake_provider", "ledger", "packing", "programs", "prompts",
        "providers", "protocol", "screening", "search", "storage"))
NONHELDOUT_FILES = ("development.json", "search.json", "validation.json", "probes.json")
HELDOUT_FILES = ("heldout/test.json", "heldout/or3.json")
RUN_FILES = ("run_manifest.json", "final_info.json", "selection.json", "events.jsonl",
             "provider_responses.jsonl", "evaluated.jsonl")
SELECTION_RULE = "search top5; validation mean; search mean; code length; hash"
PUBLIC_API_FIELDS = ("base_url", "model", "expected_model_version", "api_key_env",
                     "ledger_path", "temperature", "max_tokens", "timeout_seconds",
                     "max_requests", "extra_body", "budget")
PUBLIC_HANDSHAKE_FIELDS = ("provider", "model", "timeout_seconds", "max_requests")


def normalize_handshake_config(config):
    """Normalize a Muse handshake provider config. No secrets, no payment."""
    _require(isinstance(config, dict), "handshake configuration must be an object")
    unknown = set(config) - set(PUBLIC_HANDSHAKE_FIELDS)
    _require(not unknown, "handshake configuration has unsupported fields")
    _require(config.get("provider") == "handshake", "handshake provider must be 'handshake'")
    model = config.get("model")
    _require(isinstance(model, str) and model.strip(), "handshake model must be explicitly configured")
    timeout = config.get("timeout_seconds")
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
             "handshake timeout_seconds must be positive")
    max_requests = config.get("max_requests")
    _require(type(max_requests) is int and max_requests > 0,
             "handshake max_requests must be a positive integer")
    _require(not _contains_secret_field(config), "handshake configuration must not contain credentials")
    return {"provider": "handshake", "model": model, "timeout_seconds": timeout,
            "max_requests": max_requests}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _exclusive_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize before reserving a filename so malformed input leaves no file.
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def _signed(payload, key):
    return {**payload, key: digest(payload)}


def _check_signed(value, key, kind):
    _require(isinstance(value, dict) and value.get("kind") == kind,
             "not a supported " + kind)
    payload = {k: v for k, v in value.items() if k != key}
    _require(value.get(key) == digest(payload), kind + " checksum mismatch")


def _root(source_root):
    return Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]


def core_source_hashes(source_root=None):
    """Pin imported project modules and entry points, excluding unrelated files."""
    root = _root(source_root)
    return {name: _sha(root / name) for name in CORE_FILES}


def public_api_config(config):
    """Normalize public API settings without retaining credentials or extra keys."""
    _require(isinstance(config, dict), "API configuration must be an object")
    unknown = set(config) - set(PUBLIC_API_FIELDS) - {"enabled"}
    _require(not unknown, "API configuration has unsupported fields; do not put secrets in JSON")
    normalized = copy.deepcopy({key: config[key] for key in PUBLIC_API_FIELDS if key in config})
    normalized.setdefault("expected_model_version", None)
    normalized.setdefault("api_key_env", "FUNPACK_API_KEY")
    normalized.setdefault("temperature", 0.8)
    normalized.setdefault("timeout_seconds", 60)
    normalized.setdefault("extra_body", {})
    _require(not _contains_secret_field(normalized.get("budget", {})),
             "budget metadata must not contain credentials")
    # preflight does not send requests. Missing the environment key is expected
    # during freezing; every other API validation remains mandatory.
    checked = preflight({**normalized, "enabled": config.get("enabled", True)}, allow_paid=True)
    errors = [item for item in checked["errors"]
              if item != "the configured API-key environment variable is empty"]
    _require(not errors, "Invalid public API configuration: " + "; ".join(errors))
    _require(bool(normalized.get("ledger_path")), "a persistent ledger_path is required")
    return normalized


def draft_plan():
    return {"schema_version": 2, "status": "draft", "provider": "api", "arms": ["A", "B", "C", "D"],
            "seeds": [11, 22, 33], "calls_per_run": None, "islands": 4,
            "reset_every": 50, "audit_rate": 0.1, "database_temperature": 1.0,
            "enhanced_prompt_version": "mechanism_v3", "collision_policy": "disable_on_mismatch_v1",
            "evaluation_timeout_seconds": 30, "validation_timeout_seconds": 30,
            "final_timeout_seconds": 30}


def _validate_plan(plan):
    _require(isinstance(plan, dict) and set(plan) == set(draft_plan()),
             "plan fields must match the draft schema")
    _require(plan["schema_version"] == 2 and plan["status"] == "draft", "invalid plan schema/status")
    _require(plan.get("provider") in ("api", "handshake"), "plan provider must be 'api' or 'handshake'")
    _require(plan["enhanced_prompt_version"] in ENHANCED_VERSIONS, "unknown enhanced prompt version")
    _require(plan["collision_policy"] in COLLISION_POLICIES, "unknown collision policy")
    _require(plan["arms"] == ["A", "B", "C", "D"], "formal comparison requires all four arms A-D")
    seeds = plan["seeds"]
    _require(isinstance(seeds, list) and len(seeds) >= 3 and all(type(x) is int for x in seeds)
             and len(seeds) == len(set(seeds)), "at least three distinct integer seeds are required")
    for name in ("calls_per_run", "islands", "reset_every"):
        low = 0 if name == "reset_every" else 1
        _require(type(plan[name]) is int and low <= plan[name] <= 100000,
                 name + " must be resolved to a valid integer before freezing")
    for name in ("database_temperature", "evaluation_timeout_seconds", "validation_timeout_seconds",
                 "final_timeout_seconds"):
        value = plan[name]
        _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                 name + " must be finite and positive")
    rate = plan["audit_rate"]
    _require(type(rate) in (int, float) and math.isfinite(rate) and 0 < rate <= 1,
             "audit_rate must be in (0,1]; random audits cannot be disabled")


def _data_snapshot(data_dir):
    data_dir = Path(data_dir)
    manifest = read_json(data_dir / "manifest.json")
    files = manifest.get("files", {})
    for name in NONHELDOUT_FILES + HELDOUT_FILES:
        expected = files.get(name, {}).get("sha256")
        _require(isinstance(expected, str) and len(expected) == 64
                 and all(c in "0123456789abcdef" for c in expected), "missing manifest checksum: " + name)
    actual = {name: _sha(data_dir / name) for name in NONHELDOUT_FILES}
    _require(all(actual[name] == files[name]["sha256"] for name in NONHELDOUT_FILES),
             "non-heldout data differs from its manifest")
    actual["manifest.json"] = _sha(data_dir / "manifest.json")
    # Validate wrappers too; no access to the heldout directory is made here.
    load_instances(data_dir / "development.json")
    loaded = {"search": load_instances(data_dir / "search.json"),
              "validation": load_instances(data_dir / "validation.json"),
              "probes": load_probes(data_dir / "probes.json")}
    semantic = {name: digest(value) for name, value in loaded.items()}
    return {"data_files": actual, "data_hashes": semantic,
            "data_counts": {name: len(value) for name, value in loaded.items()},
            "heldout_files": {name: files[name]["sha256"] for name in HELDOUT_FILES}}


def freeze_protocol(plan_path, config_path, data_dir, output_path, source_root=None):
    """Write a prospective protocol; never enables/calls a provider or reads tests.

    config_path is the API configuration for provider="api" or the handshake
    configuration for provider="handshake" (no key, no payment, no ledger).
    """
    plan = read_json(plan_path)
    _validate_plan(plan)
    config = read_json(config_path)
    requests = len(plan["arms"]) * len(plan["seeds"]) * plan["calls_per_run"]
    if plan.get("provider") == "handshake":
        provider_config = normalize_handshake_config(config)
        _require(provider_config["max_requests"] >= requests,
                 "handshake request cap cannot cover the complete formal comparison")
        api_public = None
    else:
        _require(config.get("enabled") is True, "resolve the API configuration before freezing")
        provider_config = None
        api_public = public_api_config(config)
        _require(api_public["max_requests"] >= requests, "API request cap cannot cover the complete formal comparison")
    payload = {"schema_version": 2, "kind": "formal_protocol", "frozen_at_utc": _now(),
               "plan": plan, "api_config": api_public, "provider_config": provider_config,
               "total_planned_requests": requests,
               "source_hashes": core_source_hashes(source_root), **_data_snapshot(data_dir),
               "python_version": platform.python_version(),
               "heldout_evaluated": False,
               "notes": "File hashes are integrity checks, not proof against deliberate editing. "
                        "API model aliases do not verify an immutable backend version."}
    frozen = _signed(payload, "protocol_id")
    _exclusive_json(output_path, frozen)
    return frozen


def load_protocol(path, data_dir, source_root=None):
    frozen = read_json(path)
    _check_signed(frozen, "protocol_id", "formal_protocol")
    _require(frozen.get("schema_version") == 2, "unsupported protocol schema; use archived code for historical evidence")
    _validate_plan(frozen["plan"])
    _require(frozen["source_hashes"] == core_source_hashes(source_root),
             "core source changed after freezing; do not relabel existing results")
    _require(frozen["python_version"] == platform.python_version(), "Python version changed after freezing")
    current = _data_snapshot(data_dir)
    for name in ("data_files", "data_hashes", "data_counts", "heldout_files"):
        _require(frozen[name] == current[name], "data changed after freezing: " + name)
    return frozen


def validate_runtime(protocol, args, provider_config):
    """Reject runtime overrides before a provider is created.

    For provider="api", provider_config is the API configuration and arms/seeds
    must match the plan exactly. For provider="handshake", provider_config is the
    handshake configuration and arms/seeds may be a nonempty subset of the plan,
    so one (arm, seed) part can run per process; the seal step still requires
    every arm and seed exactly once in the merged study.
    """
    args = vars(args) if not isinstance(args, dict) else args
    plan = protocol["plan"]
    provider = plan.get("provider", "api")
    _require(args.get("stage") == "all", "formal runs require stage=all")
    _require(args.get("provider") == provider,
             "formal runs require provider=" + provider)
    mapping = {"calls": "calls_per_run", "islands": "islands",
               "reset_every": "reset_every", "audit_rate": "audit_rate",
               "database_temperature": "database_temperature",
               "enhanced_prompt_version": "enhanced_prompt_version", "collision_policy": "collision_policy",
               "evaluation_timeout_seconds": "evaluation_timeout_seconds",
               "validation_timeout_seconds": "validation_timeout_seconds"}
    for argument, field in mapping.items():
        _require(args.get(argument) == plan[field], "runtime setting differs from protocol: " + argument)
    if provider == "handshake":
        _require(isinstance(args.get("arms"), list) and args["arms"]
                 and set(args["arms"]) <= set(plan["arms"]) and len(args["arms"]) == len(set(args["arms"])),
                 "formal handshake arms must be a nonempty subset of the frozen plan")
        _require(isinstance(args.get("seeds"), list) and args["seeds"]
                 and set(args["seeds"]) <= set(plan["seeds"]) and len(args["seeds"]) == len(set(args["seeds"])),
                 "formal handshake seeds must be a nonempty subset of the frozen plan")
        _require(provider_config == protocol.get("provider_config"),
                 "provider configuration differs from frozen protocol")
        return
    _require(args.get("arms") == plan["arms"] and args.get("seeds") == plan["seeds"],
             "formal runs require the frozen arms and seeds")
    _require(provider_config.get("enabled") is True, "formal API configuration must be enabled")
    _require(public_api_config(provider_config) == protocol["api_config"],
             "API configuration differs from frozen protocol")


def _jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _selection_winner(selection, info, validation_hash, validation_count, timeout):
    _require(selection.get("heldout_evaluated") is False and selection.get("validation_hash") == validation_hash,
             "candidate selection is not from the frozen validation data")
    _require(selection.get("selection_rule") == SELECTION_RULE and selection.get("timeout_seconds") == timeout,
             "candidate selection settings differ from the protocol")
    candidates = selection["candidates"]
    top = info["top_search_candidates"]
    _require(len(candidates) >= len(top) and bool(top), "missing validation candidates")
    for measured, searched in zip(candidates, top):
        _require(all(measured.get(key) == searched.get(key) for key in ("id", "code", "code_hash")),
                 "validation candidate was not a top search candidate")
        _require(measured.get("search_mean_bins") == searched.get("mean_bins"), "search score mismatch")
        _require(measured.get("status") in ("valid", "validation_error"), "unknown validation candidate status")
    for candidate in candidates:
        _require(digest(candidate["code"]) == candidate["code_hash"], "candidate code checksum mismatch")
    valid_top = [c for c in candidates[:len(top)] if c.get("status") == "valid"]
    if valid_top:
        _require(len(candidates) == len(top) and selection.get("used_seed_fallback") is False,
                 "unexpected fallback or validation candidate")
        valid = valid_top
    else:
        fallback = candidates[len(top):]
        _require(selection.get("used_seed_fallback") is True and len(fallback) == 2,
                 "invalid seed fallback")
        _require([c.get("code") for c in fallback] == [FF_CODE, BF_CODE]
                 and all(c.get("status") == "valid_seed_fallback" for c in fallback), "fallback is not FF/BF")
        valid = fallback
    for record in valid:
        counts = record.get("validation", {}).get("counts", [])
        _require(len(counts) == validation_count and all(type(x) is int and x >= 0 for x in counts)
                 and record["validation"]["mean_bins"] == sum(counts) / len(counts),
                 "invalid validation measurements")
    selected = min(valid, key=lambda c: (c["validation"]["mean_bins"], c["search_mean_bins"] or 0,
                                         len(c["code"]), c["code_hash"]))
    _require(selection.get("selected") == selected, "selected program violates the fixed selection rule")
    Program(selected["code"])
    return selected


def _inspect_study(protocol, study_dir):
    study_dir = Path(study_dir)
    study = read_json(study_dir / "study.json")
    _require(study.get("phase") == "formal" and study.get("protocol_id") == protocol["protocol_id"],
             "only a prospectively frozen formal study can be sealed")
    _require(study.get("heldout_evaluated") is False and not study.get("stop_reason"), "study is incomplete or already tested")
    _require(study.get("core_source_hashes") == protocol["source_hashes"], "study source differs from protocol")
    handshake = protocol["plan"].get("provider") == "handshake"
    if handshake:
        validate_runtime(protocol, study["arguments"], study.get("provider_config"))
    else:
        validate_runtime(protocol, study["arguments"], {**study["api_config"], "enabled": True})
    for field, role in (("search_hash", "search"), ("validation_hash", "validation"), ("probe_hash", "probes")):
        _require(study.get(field) == protocol["data_hashes"][role], "study data hash differs: " + field)
    plan = protocol["plan"]
    expected = {(a, s): f"{a}_seed{s}" for s in plan["seeds"] for a in plan["arms"]}
    entries = study.get("runs", [])
    actual = [(entry.get("arm"), entry.get("seed")) for entry in entries]
    _require(len(actual) == len(expected) and set(actual) == set(expected), "all arms and seeds must finish exactly once")
    evidence = {"study.json": _sha(study_dir / "study.json")}
    winners = []
    for entry in entries:
        arm, seed = entry["arm"], entry["seed"]
        name = expected[(arm, seed)]
        _require(entry.get("run") == name, "unexpected run path")
        directory = study_dir / name
        _require(not (directory / "pending_request.json").exists(), "run still has a pending request")
        for filename in RUN_FILES:
            evidence[name + "/" + filename] = _sha(directory / filename)
        info = read_json(directory / "final_info.json")
        manifest = read_json(directory / "run_manifest.json")
        config = {"arm": arm, "seed": seed, "calls": plan["calls_per_run"], "islands": plan["islands"],
                  "reset_every": plan["reset_every"], "audit_rate": plan["audit_rate"],
                  "temperature": plan["database_temperature"],
                  "enhanced_prompt_version": plan["enhanced_prompt_version"],
                  "collision_policy": plan["collision_policy"],
                  "evaluation_timeout_seconds": plan["evaluation_timeout_seconds"],
                  "protocol_id": protocol["protocol_id"]}
        module_hashes = {key.removeprefix("funpack/"): value for key, value in protocol["source_hashes"].items()
                         if key.startswith("funpack/")}
        for record in (info, manifest):
            expected_method = {"prompt_version": prompt_version("enhanced" if arm in "BD" else "basic", plan["enhanced_prompt_version"]),
                               "enhanced_prompt_version": plan["enhanced_prompt_version"],
                               "screening_enabled": arm in "CD",
                               "collision_policy": plan["collision_policy"] if arm in "CD" else "off",
                               "search_context": ("mechanism_v4_memory_v1"
                                                  if plan["enhanced_prompt_version"] == "mechanism_v4" else "off")}
            _require(record.get("method") == expected_method, "run method versions differ from protocol")
            _require(record.get("config") == config
                     and record.get("provider_class") == ("HandshakeProvider" if handshake else "ApiProvider")
                     and record.get("heldout_evaluated") is False, "run configuration/provider differs from protocol")
            _require(record.get("evidence_scope") == "formal_comparison", "run is not formal LLM evidence")
            _require(all(record.get("source_hashes", {}).get(k) == v for k, v in module_hashes.items()),
                     "run module hashes differ from protocol")
            _require(record.get("probe_hash") == protocol["data_hashes"]["probes"], "run probe hash mismatch")
            _require(record.get("search_hash") == protocol["data_hashes"]["search"], "run search hash mismatch")
        _require(info.get("termination") == entry.get("termination") == "request_budget_completed",
                 "incomplete search cannot enter final testing")
        for metric in ("generator_requests", "requests_attempted"):
            _require(info["metrics"].get(metric) == plan["calls_per_run"], "formal request count mismatch")
        if handshake:
            _require(info["metrics"].get("paid_api_requests") == 0,
                     "handshake runs must not record paid requests")
        else:
            _require(info["metrics"].get("paid_api_requests") == plan["calls_per_run"],
                     "formal request count mismatch")
        _require(entry.get("metrics") == info["metrics"], "study and run metrics disagree")
        events = _jsonl(directory / "events.jsonl")
        responses = _jsonl(directory / "provider_responses.jsonl")
        indices = list(range(1, plan["calls_per_run"] + 1))
        _require([e.get("request_index") for e in events] == indices
                 and [r.get("request_index") for r in responses] == indices,
                 "missing, duplicate or unordered candidate requests")
        for event, saved in zip(events, responses):
            _require(event.get("method") == saved.get("method") == expected_method,
                     "event method versions differ from protocol")
            _require(event.get("messages_hash") == saved.get("messages_hash") == digest(event.get("messages")),
                     "prompt checksum mismatch")
            response = saved.get("response", {})
            _require(event.get("response") == response and event.get("messages") == saved.get("messages")
                     and bool(event.get("messages")), "prompt/response logs disagree")
            if handshake:
                _require(response.get("offline_fixture") is False and response.get("paid_api_request") is False
                         and response.get("request_attempted") is True and "model" in response
                         and response.get("requested_model") == protocol["provider_config"]["model"],
                         "fixture, unsent, or incorrectly identified model response")
            else:
                _require(response.get("offline_fixture") is False and response.get("paid_api_request") is True
                         and response.get("request_attempted") is True and "model" in response
                         and response.get("requested_model") == protocol["api_config"]["model"],
                         "fixture, unsent, or incorrectly identified model response")
        measured = _jsonl(directory / "evaluated.jsonl")
        _require(bool(measured) and all(digest(r["code"]) == r["code_hash"] for r in measured),
                 "invalid measured candidate evidence")
        for record in measured:
            counts = record.get("counts", [])
            _require(len(counts) == protocol["data_counts"]["search"]
                     and all(type(x) is int and x >= 0 for x in counts)
                     and record.get("mean_bins") == sum(counts) / len(counts),
                     "search result has missing or inconsistent instance counts")
        best = sorted(measured, key=lambda r: (r["mean_bins"], len(r["code"]), r["code_hash"]))[:5]
        _require(info.get("top_search_candidates") == best, "search shortlist differs from measured results")
        selected = _selection_winner(read_json(directory / "selection.json"), info,
                                     protocol["data_hashes"]["validation"], protocol["data_counts"]["validation"],
                                     plan["validation_timeout_seconds"])
        winners.append({"run": name, "arm": arm, "seed": seed, "selected_id": selected["id"],
                        "code": selected["code"], "code_hash": selected["code_hash"],
                        "validation_mean_bins": selected["validation"]["mean_bins"]})
    return winners, evidence


def seal_candidates(protocol_path, study_dir, data_dir, output_path, source_root=None):
    protocol = load_protocol(protocol_path, data_dir, source_root)
    _require(not (Path(study_dir) / "final_evaluation_claim.json").exists(), "final testing has already been claimed")
    winners, evidence = _inspect_study(protocol, study_dir)
    payload = {"schema_version": 1, "kind": "candidate_seal", "sealed_at_utc": _now(),
               "protocol_id": protocol["protocol_id"], "winners": winners,
               "evidence_hashes": evidence, "heldout_evaluated": False}
    seal = _signed(payload, "seal_id")
    _exclusive_json(output_path, seal)
    return seal


def _evaluate_frozen(code, instances, timeout):
    """Keep every instance, including failed ones; never average failures away."""
    start = time.perf_counter()
    deadline = start + timeout
    records = []
    program = Program(code)

    def bounded(item, bins):
        if time.perf_counter() > deadline:
            raise RuntimeError("final evaluation time budget exceeded")
        return program(item, bins)

    for instance in instances:
        row = {"instance_id": instance["id"]}
        try:
            if time.perf_counter() > deadline:
                raise RuntimeError("final evaluation time budget exceeded")
            outcome = evaluate(bounded, [instance])
            if time.perf_counter() > deadline:
                raise RuntimeError("final evaluation time budget exceeded")
            row.update(status="valid", bins_used=outcome["counts"][0], trace_hash=outcome["trace_hashes"][0])
        except (ValueError, ArithmeticError, TypeError, RuntimeError) as error:
            row.update(status="evaluation_error", bins_used=None, trace_hash=None,
                       error=f"{type(error).__name__}: {error}")
        records.append(row)
    counts = [record["bins_used"] for record in records]
    complete = all(value is not None for value in counts)
    return {"status": "valid" if complete else "evaluation_error", "instance_results": records,
            "counts": counts, "mean_bins": sum(counts) / len(counts) if complete else None,
            "seconds": time.perf_counter() - start}


def evaluate_final(protocol_path, seal_path, study_dir, data_dir, out_dir,
                   allow_heldout=False, source_root=None):
    """One final evaluation for a sealed study; no network, reselection or tuning."""
    if allow_heldout is not True:
        raise PermissionError("final evaluation requires explicit --allow-heldout")
    protocol = load_protocol(protocol_path, data_dir, source_root)
    seal = read_json(seal_path)
    _check_signed(seal, "seal_id", "candidate_seal")
    _require(seal.get("protocol_id") == protocol["protocol_id"] and seal.get("heldout_evaluated") is False,
             "seal does not belong to this untested protocol")
    winners, evidence = _inspect_study(protocol, study_dir)
    _require(seal.get("winners") == winners and seal.get("evidence_hashes") == evidence,
             "study evidence or selected candidates changed after sealing")
    study_dir, out_dir, data_dir = Path(study_dir), Path(out_dir), Path(data_dir)
    claim_path = study_dir / "final_evaluation_claim.json"
    _require(not claim_path.exists(), "final evaluation already claimed; preserve partial results and investigate interruption")
    # Check presence without reading benchmarks. Missing optional downloads can
    # be restored before claiming the one final evaluation.
    _require(all((data_dir / name).is_file() for name in HELDOUT_FILES),
             "heldout file missing; restore the pinned datasets before final evaluation")
    out_dir.mkdir(parents=True, exist_ok=False)
    # The study-level exclusive claim is reserved BEFORE any heldout file read,
    # and survives failure. A different output path does not bypass this guard.
    _exclusive_json(claim_path, {"protocol_id": protocol["protocol_id"], "seal_id": seal["seal_id"],
                               "claimed_at_utc": _now(), "output_path": str(out_dir.resolve())})
    started = time.perf_counter()
    result = {"schema_version": 1, "kind": "final_evaluation", "protocol_id": protocol["protocol_id"],
              "seal_id": seal["seal_id"], "environment": environment(), "status": "started",
              "heldout_evaluated": False, "splits": {},
              "interpretation": "Each run winner was selected on validation. Negative delta_to_bf means fewer bins. "
                                "Any failed instance makes that method's split mean undefined; no replacement winner."}
    write_json(out_dir / "final_results.json", result)
    try:
        for name in HELDOUT_FILES:
            _require(_sha(data_dir / name) == protocol["heldout_files"][name], "heldout checksum mismatch: " + name)
            instances = load_instances(data_dir / name, allow_heldout=True)
            split = Path(name).stem
            result["heldout_evaluated"] = True
            data = {"data_sha256": protocol["heldout_files"][name],
                    "instance_ids": [i["id"] for i in instances], "methods": {}}
            result["splits"][split] = data
            methods = [("FF", {"code": FF_CODE, "code_hash": digest(FF_CODE)}),
                       ("BF", {"code": BF_CODE, "code_hash": digest(BF_CODE)})]
            methods += [(winner["run"], winner) for winner in winners]
            for label, method in methods:
                data["methods"][label] = {**method, **_evaluate_frozen(
                    method["code"], instances, protocol["plan"]["final_timeout_seconds"])}
                if "BF" in data["methods"]:
                    baseline = data["methods"]["BF"]["counts"]
                    for measured in data["methods"].values():
                        measured["delta_to_bf"] = [a - b if a is not None and b is not None else None
                                                    for a, b in zip(measured["counts"], baseline)]
                write_json(out_dir / "final_results.json", result)
        result["status"] = "complete"
    except BaseException as error:
        result.update(status="interrupted", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        result["total_seconds"] = time.perf_counter() - started
        write_json(out_dir / "final_results.json", result)
    return result
