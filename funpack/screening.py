"""Finite action fingerprints. Matching probes never implies equivalence."""
from __future__ import annotations

from .packing import choose_index


def fingerprint(program, states):
    return tuple(choose_index(program(state["item"], list(state["bins"])), len(state["bins"]))
                 for state in states)


class BehavioralScreen:
    def __init__(self, states, first_stage=32):
        if len(states) != 128 or first_stage != 32:
            raise ValueError("the planned online screen requires exactly 32 + 96 states")
        self.states = states
        self.first_stage = first_stage
        self.representatives = {}

    def probe(self, program):
        prefix = fingerprint(program, self.states[:self.first_stage])
        matches = self.representatives.get(prefix, [])
        if not matches:
            return None, prefix, None
        tail = fingerprint(program, self.states[self.first_stage:])
        for old_tail, record in matches:
            if old_tail == tail:
                return record, prefix, tail
        return None, prefix, tail

    def register(self, program, record, prefix=None, tail=None):
        # Only call this with a successfully FULLY evaluated representative.
        prefix = fingerprint(program, self.states[:self.first_stage]) if prefix is None else prefix
        tail = fingerprint(program, self.states[self.first_stage:]) if tail is None else tail
        self.representatives.setdefault(prefix, []).append((tail, record))
