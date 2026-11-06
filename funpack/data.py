"""Frozen, checksummed experiment data and development-only behavioral probes."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import random

from .packing import (best_fit_scores, first_fit_scores, pack, validate_items,
                      worst_fit_scores)


SCHEMA_VERSION = 1
SPLITS = {"development": (10, 2026101800), "search": (30, 2026102800),
          "validation": (10, 2026103800), "test": (30, 2026104800)}
PROBE_SEED = 2026105800
OR3_NOTEBOOK_SHA256 = "eec45846a8661722addbb856179327cf2ede0bc502e32be4ad58a6ac54e0aca6"


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def _instance_hash(instance: dict) -> str:
    # IDs and seeds cannot hide repeated capacity/item contents across splits.
    return content_hash({"capacity": instance["capacity"], "items": instance["items"]})


def _validate_instances(instances: list[dict]) -> None:
    if not isinstance(instances, list) or not instances:
        raise ValueError("instances must be a nonempty list")
    ids = set()
    for instance in instances:
        if not isinstance(instance, dict) or not isinstance(instance.get("id"), str):
            raise ValueError("each instance requires a string id")
        if instance["id"] in ids:
            raise ValueError("duplicate instance id")
        ids.add(instance["id"])
        validate_items(instance["items"], instance["capacity"])
        if instance.get("content_sha256") != _instance_hash(instance):
            raise ValueError(f"instance content hash mismatch: {instance['id']}")


def _wrapper(split: str, instances: list[dict], **extra) -> dict:
    return {"schema_version": SCHEMA_VERSION, "kind": "instances", "split": split,
            "heldout": split in ("test", "or3"), "instances": instances,
            "instances_sha256": content_hash(instances), **extra}


def load_instances(path, *, allow_heldout: bool = False) -> list[dict]:
    """Load and validate a data wrapper. Held-out data requires explicit opt-in."""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if obj.get("schema_version") != SCHEMA_VERSION or obj.get("kind") != "instances":
        raise ValueError("not a supported instance data wrapper")
    if (obj.get("heldout") or obj.get("split") in ("test", "or3")) and not allow_heldout:
        raise PermissionError("held-out split is sealed; final evaluation requires allow_heldout=True")
    instances = obj["instances"]
    if obj.get("instances_sha256") != content_hash(instances):
        raise ValueError("instance collection checksum mismatch")
    _validate_instances(instances)
    return instances


def load_probes(path) -> list[dict]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if obj.get("schema_version") != SCHEMA_VERSION or obj.get("kind") != "probes":
        raise ValueError("not a supported probe wrapper")
    states = obj["states"]
    if len(states) != 128 or obj.get("states_sha256") != content_hash(states):
        raise ValueError("probe count or checksum mismatch")
    unique = set()
    for state in states:
        item = state["item"]
        bins = state["bins"]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 < item <= 100:
            raise ValueError("invalid probe item")
        if not isinstance(bins, list) or len(bins) < 2:
            raise ValueError("each probe must have at least two feasible bins")
        if any(isinstance(b, bool) or not isinstance(b, int) or not item <= b <= 100
               for b in bins):
            raise ValueError("invalid feasible residual capacity in probe")
        if state.get("source", {}).get("split") != "development":
            raise ValueError("probes must come exclusively from development")
        key = (item, tuple(bins))
        if key in unique:
            raise ValueError("duplicate behavioral probe state")
        unique.add(key)
    return states


def _generate_probes(instances: list[dict]) -> dict:
    unique = {}
    used_policies = []
    # Only add worst-fit trajectories if FF/BF do not supply 128 unique states.
    for policy_name, scorer in (("first_fit", first_fit_scores),
                                ("best_fit", best_fit_scores),
                                ("worst_fit", worst_fit_scores)):
        if policy_name == "worst_fit" and len(unique) >= 128:
            break
        used_policies.append(policy_name)
        for instance in instances:
            def collect(state, instance_id=instance["id"], policy=policy_name):
                if len(state["bins"]) < 2:
                    return
                key = (state["item"], tuple(state["bins"]))
                if key not in unique:
                    unique[key] = {"item": state["item"], "bins": state["bins"],
                                   "source": {"split": "development",
                                              "instance": instance_id,
                                              "step": state["step"], "policy": policy}}
            pack(instance["items"], instance["capacity"], scorer, state_callback=collect)
    if len(unique) < 128:
        raise ValueError(f"only {len(unique)} unique multi-choice development states; need 128")
    states = random.Random(PROBE_SEED).sample(list(unique.values()), 128)
    return {"schema_version": SCHEMA_VERSION, "kind": "probes", "seed": PROBE_SEED,
            "source_split": "development", "policies": used_policies,
            "pool_size": len(unique), "stage_sizes": [32, 96], "states": states,
            "states_sha256": content_hash(states),
            "interpretation": "Finite choices are screening evidence, not program equivalence."}


def _extract_or3(notebook_path: Path) -> tuple[list[dict], dict]:
    """Read a literal OR3 assignment through AST; never execute notebook code."""
    source_manifest_path = notebook_path.parent / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    entries = [entry for entry in source_manifest.get("sources", [])
               if entry.get("file") == notebook_path.name]
    if len(entries) != 1:
        raise ValueError("source manifest must uniquely identify the notebook")
    entry = entries[0]
    raw = notebook_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != entry["sha256"] or actual != OR3_NOTEBOOK_SHA256:
        raise ValueError("OR3 notebook checksum does not match pinned source and manifest")
    notebook = json.loads(raw)
    found = []
    for cell in notebook["cells"]:
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        if "OR3" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # Cells with notebook magics are not executed or transformed.
            continue
        for statement in tree.body:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == "datasets" and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "OR3"):
                    found.append(ast.literal_eval(statement.value))
    if len(found) != 1 or not isinstance(found[0], dict):
        raise ValueError("expected exactly one literal datasets['OR3'] dictionary")
    dataset = found[0]
    expected_ids = [f"u500_{i:02d}" for i in range(20)]
    if list(dataset) != expected_ids:
        raise ValueError("unexpected OR3 instance order or IDs")
    instances = []
    for instance_id, item in dataset.items():
        if item["capacity"] != 150 or item["num_items"] != 500 or len(item["items"]) != 500:
            raise ValueError("unexpected OR3 capacity or item count")
        instance = {"id": instance_id, "capacity": 150, "items": item["items"]}
        instance["content_sha256"] = _instance_hash(instance)
        instances.append(instance)
    _validate_instances(instances)
    provenance = {
        "source_url": entry["url"], "source_notebook_sha256": actual,
        "source_manifest_sha256": hashlib.sha256(source_manifest_path.read_bytes()).hexdigest(),
        "source_verified_on": source_manifest.get("verified_on"),
        "upstream_dataset": "OR-Library bin packing, OR3 (u500_00 through u500_19)",
        "upstream_url": "https://people.brunel.ac.uk/~mastjjb/jeb/orlib/binpackinfo.html",
        "extraction": "ast.literal_eval of literal datasets['OR3']; no notebook execution",
        "order": "Original notebook instance and item order preserved; never sorted/shuffled",
        "license": "DeepMind repository states software Apache-2.0; other materials CC-BY-4.0."
                   " This data export is attributed under that statement; no separate OR-Library"
                   " dataset license was independently verified.",
        "attribution": "Copyright 2023 DeepMind Technologies Limited. Romera-Paredes et al.,"
                       " Mathematical discoveries from program search with large language models,"
                       " Nature (2023), DOI: 10.1038/s41586-023-06924-6; OR-Library, J. E. Beasley.",
        "license_urls": ["https://creativecommons.org/licenses/by/4.0/legalcode",
                         "https://www.apache.org/licenses/LICENSE-2.0"],
    }
    return instances, provenance


def generate_suite(out_dir, notebook_path=None) -> dict:
    """Create proposal data once; identical reruns verify, changed files fail.

    Files: development.json, search.json, validation.json, probes.json,
    heldout/test.json, optional heldout/or3.json, manifest.json. Sealing is a
    workflow safeguard, not encryption or an access-control boundary.
    """
    out_dir = Path(out_dir)
    payloads = {}
    split_instances = {}
    seen_hashes = set()
    split_metadata = {}
    for split, (count, seed_start) in SPLITS.items():
        instances = []
        for i in range(count):
            seed = seed_start + i
            rng = random.Random(seed)
            instance = {"id": f"{split}_{i:03d}", "seed": seed, "capacity": 100,
                        "items": [rng.randint(20, 80) for _ in range(500)]}
            digest = _instance_hash(instance)
            if digest in seen_hashes:
                raise ValueError("duplicate capacity/item contents across synthetic splits")
            seen_hashes.add(digest)
            instance["content_sha256"] = digest
            instances.append(instance)
        split_instances[split] = instances
        relative = f"heldout/{split}.json" if split == "test" else f"{split}.json"
        payloads[relative] = _wrapper(split, instances,
                                     generation={"distribution": "iid discrete uniform integers [20,80]",
                                                 "rng": "Python random.Random(seed).randint(20,80)",
                                                 "items_per_instance": 500, "capacity": 100})
        split_metadata[split] = {"path": relative, "instances": count,
                                 "seed_start": seed_start,
                                 "seed_end": seed_start + count - 1,
                                 "instances_sha256": content_hash(instances),
                                 "heldout": split == "test"}
    payloads["probes.json"] = _generate_probes(split_instances["development"])
    or3_provenance = None
    if notebook_path is not None:
        instances, or3_provenance = _extract_or3(Path(notebook_path))
        payloads["heldout/or3.json"] = _wrapper("or3", instances, provenance=or3_provenance)
        split_metadata["or3"] = {"path": "heldout/or3.json", "instances": 20,
                                 "instances_sha256": content_hash(instances), "heldout": True}
    raw_files = {name: _file_bytes(value) for name, value in payloads.items()}
    manifest = {"schema_version": SCHEMA_VERSION, "generator_version": "funpack-data-v1",
                "synthetic_split_overlap_checked": True,
                "split_overlap_criterion": "SHA256 of capacity plus ordered items, excluding ID/seed",
                "splits": split_metadata,
                "probes": {"path": "probes.json", "count": 128, "stage_sizes": [32, 96],
                           "seed": PROBE_SEED, "source_split": "development",
                           "pool_size": payloads["probes.json"]["pool_size"],
                           "states_sha256": payloads["probes.json"]["states_sha256"]},
                "or3": {"status": "prepared" if or3_provenance else "not_requested",
                        "provenance": or3_provenance},
                "files": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                          for name, raw in raw_files.items()},
                "heldout_policy": "test and OR3 are never loaded by default. Freeze candidate"
                                  " selection before explicit final evaluation; do not tune afterward."}
    raw_files["manifest.json"] = _file_bytes(manifest)
    # Check every destination before changing anything, so conflicts cannot be overwritten.
    for name, raw in raw_files.items():
        destination = out_dir / name
        if destination.exists() and destination.read_bytes() != raw:
            raise FileExistsError(f"refusing to overwrite changed/frozen data: {destination}")
    for name, raw in raw_files.items():
        destination = out_dir / name
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation also prevents accidental overwrite in a concurrent run.
            with destination.open("xb") as stream:
                stream.write(raw)
    return manifest
