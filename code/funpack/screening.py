"""Finite action fingerprints. Matching probes never implies equivalence."""
from __future__ import annotations

from .packing import choose_index

BF_PROTECTED = "protect_bf_disable_on_mismatch_v2"
COLLISION_POLICIES = ("retain_representative", "disable_on_mismatch_v1", BF_PROTECTED)


def fingerprint(program, states):
    return tuple(choose_index(program(state["item"], list(state["bins"])), len(state["bins"]))
                 for state in states)


class BehavioralScreen:
    def __init__(self, states, first_stage=32, *, collision_policy="retain_representative"):
        if len(states) != 128 or first_stage != 32:
            raise ValueError("the planned online screen requires exactly 32 + 96 states")
        if collision_policy not in COLLISION_POLICIES:
            raise ValueError("unknown collision policy")
        self.states = states
        self.first_stage = first_stage
        self.representatives = {}
        self.collision_policy = collision_policy
        self.disabled_fingerprints = set()
        self.bf_fingerprint = None

    def protects_bf(self, prefix, tail):
        return self.bf_fingerprint is not None and (prefix, tail) == self.bf_fingerprint

    def is_disabled(self, prefix, tail):
        return tail is not None and (prefix, tail) in self.disabled_fingerprints

    def disable(self, prefix, tail):
        """Disable one COMPLETE fingerprint after a successful audit disagrees.

        It stays disabled for the remainder of this run, including island resets.
        Retain prefix representatives to preserve two-stage lookup. No probe,
        representative score or exact-text cache is altered.
        """
        if self.collision_policy == "retain_representative":
            return False
        if prefix is None or tail is None or len(prefix) != 32 or len(tail) != 96:
            raise ValueError("disabling requires the complete 32+96 fingerprint")
        key = (tuple(prefix), tuple(tail))
        added = key not in self.disabled_fingerprints
        self.disabled_fingerprints.add(key)
        return added

    def probe(self, program):
        prefix = fingerprint(program, self.states[:self.first_stage])
        matches = self.representatives.get(prefix, [])
        if not matches:
            return None, prefix, None
        tail = fingerprint(program, self.states[self.first_stage:])
        if self.is_disabled(prefix, tail) or self.protects_bf(prefix, tail):
            return None, prefix, tail
        for old_tail, record in matches:
            if old_tail == tail:
                return record, prefix, tail
        return None, prefix, tail

    def register(self, program, record, prefix=None, tail=None):
        # Only call this with a successfully FULLY evaluated representative.
        prefix = fingerprint(program, self.states[:self.first_stage]) if prefix is None else prefix
        tail = fingerprint(program, self.states[self.first_stage:]) if tail is None else tail
        if self.collision_policy == BF_PROTECTED and record["id"] == "seed_bf":
            # Preserve the prefix entry so every later match reaches the 96-state tail.
            self.bf_fingerprint = (prefix, tail)
        elif self.protects_bf(prefix, tail):
            return
        if self.is_disabled(prefix, tail):
            return
        self.representatives.setdefault(prefix, []).append((tail, record))
