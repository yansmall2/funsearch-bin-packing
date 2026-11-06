"""Score-vector clusters and request-clock island resets.

Inspired by FunSearch; this is an independently written small implementation,
not a reproduction of DeepMind's full distributed search infrastructure.
"""
from __future__ import annotations

import math
import random


class Database:
    def __init__(self, seed=0, islands=4, temperature=1.0, cluster_cap=20):
        if islands < 1 or temperature <= 0 or cluster_cap < 1:
            raise ValueError("invalid database settings")
        self.rng = random.Random(seed)
        self.temperature = temperature
        self.cluster_cap = cluster_cap
        self.islands = [{} for _ in range(islands)]

    def add(self, record, island=None):
        targets = range(len(self.islands)) if island is None else [island]
        signature = tuple(record["counts"])
        for index in targets:
            members = self.islands[index].setdefault(signature, [])
            if not any(member["code_hash"] == record["code_hash"] for member in members):
                members.append(record)
                # Bound memory while retaining short alternative implementations.
                members.sort(key=lambda member: (len(member["code"]), member["code_hash"]))
                del members[self.cluster_cap:]

    def parents(self):
        island_id = self.rng.randrange(len(self.islands))
        clusters = list(self.islands[island_id].values())
        if not clusters:
            raise RuntimeError("island has no evaluated program")
        chosen = []
        for _ in range(min(2, len(clusters))):
            means = [group[0]["mean_bins"] for group in clusters]
            best = min(means)
            weights = [math.exp(max(-700.0, -(mean - best) / self.temperature)) for mean in means]
            position = self.rng.choices(range(len(clusters)), weights=weights)[0]
            group = clusters.pop(position)
            lengths = [len(record["code"]) for record in group]
            spread = max(lengths) - min(lengths) or 1
            weights = [math.exp(-(size - min(lengths)) / spread) for size in lengths]
            chosen.append(self.rng.choices(group, weights=weights)[0])
        # Weaker parent first; actual search scores accompany both parents.
        return island_id, sorted(chosen, key=lambda record: -record["mean_bins"])

    def reset(self):
        if len(self.islands) < 2:
            return []
        ranking = sorted(range(len(self.islands)), key=lambda i: min(
            group[0]["mean_bins"] for group in self.islands[i].values()))
        survivors = ranking[:(len(ranking) + 1) // 2]
        reset_ids = ranking[(len(ranking) + 1) // 2:]
        for target in reset_ids:
            donor = self.rng.choice(survivors)
            best = min((record for group in self.islands[donor].values() for record in group),
                       key=lambda record: (record["mean_bins"], len(record["code"])))
            self.islands[target] = {}
            self.add(best, target)
        return reset_ids
