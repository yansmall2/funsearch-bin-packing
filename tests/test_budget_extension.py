"""Explicit request-cap changes on isolated ledgers; no real API or budget.

The 16 -> 144 fixture mirrors the proposed calibration count, but these tests
neither authorize nor mutate the user's real ledger or configuration.
"""
from __future__ import annotations

import copy
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest.mock import patch
import uuid

from funpack.ledger import (BudgetLedger, LedgerBudgetExceeded, LedgerConfigMismatch,
                           LedgerStateError, budget_signature, extend_request_limit)
from funpack.providers import ApiProvider


def fixture_config(path):
    return {"enabled": True, "base_url": "https://extension-test.invalid", "model": "test-model",
            "expected_model_version": None, "api_key_env": "FUNPACK_EXTENSION_TEST_KEY",
            "ledger_path": str(path), "max_tokens": 100, "max_requests": 16,
            "timeout_seconds": 2, "temperature": 0.8, "extra_body": {},
            "budget": {"currency": "CNY", "max_cost": 10, "input_per_million": 2,
                       "output_per_million": 8, "pricing_source": "simulated-test-prices"}}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.root.mkdir(exist_ok=True)
        self.directory = self.root / ("extension-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(self.cleanup)
        self.path = self.directory / "test-budget.sqlite3"
        self.old = fixture_config(self.path)
        self.new = copy.deepcopy(self.old)
        self.new["max_requests"] = 144

    def cleanup(self):
        if self.directory.resolve().parent != self.root or self.directory.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.directory)

    def seed_ledger(self, count=16, charge=0.01, unknown_last=False):
        ledger = BudgetLedger(self.path, self.old)
        reservations = []
        for index in range(count):
            reservation = ledger.reserve(charge)
            reservations.append(reservation)
            ledger.settle(reservation["reservation_id"],
                          None if unknown_last and index == count - 1 else charge,
                          None if unknown_last and index == count - 1 else 5,
                          None if unknown_last and index == count - 1 else 3)
        return ledger, reservations

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT * FROM funpack_requests ORDER BY request_index").fetchall()

    def extend(self, **kwargs):
        return extend_request_limit(self.path, self.old, self.new,
                                    authorization_note="Explicit simulated test authorization", **kwargs)

    def test_extension_preserves_every_request_and_money_and_records_audit(self):
        ledger, _ = self.seed_ledger(unknown_last=True)
        before = ledger.summary()
        rows = self.rows()
        old_snapshot, new_snapshot = copy.deepcopy(self.old), copy.deepcopy(self.new)
        result = self.extend(expected_attempts=16)
        after = BudgetLedger(self.path, self.new).summary()
        self.assertEqual(self.rows(), rows)
        for field in ("attempts", "accounted_cost", "remaining_budget", "unknown_cost_requests",
                      "unresolved_reservations"):
            self.assertEqual(after[field], before[field])
            self.assertEqual(result[field], before[field])
        self.assertEqual(after["signature"], budget_signature(self.new))
        self.assertEqual((result["old_max_requests"], result["new_max_requests"]), (16, 144))
        self.assertEqual(self.old, old_snapshot)
        self.assertEqual(self.new, new_snapshot)
        with closing(sqlite3.connect(self.path)) as connection:
            audits = connection.execute("""SELECT old_max_requests,new_max_requests,
                old_signature,new_signature,attempts_at_extension,accounted_units_at_extension,
                created_utc,authorization_note FROM funpack_budget_extensions""").fetchall()
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertEqual(audit[:5], (16, 144, budget_signature(self.old), budget_signature(self.new), 16))
        self.assertEqual(audit[5], 160_000_000_000)
        self.assertTrue(audit[6])
        self.assertEqual(audit[7], "Explicit simulated test authorization")

    def test_only_128_additional_requests_are_possible(self):
        self.seed_ledger()
        self.extend(expected_attempts=16)
        ledger = BudgetLedger(self.path, self.new)
        for expected_index in range(17, 145):
            record = ledger.reserve(0.001)
            self.assertEqual(record["request_index"], expected_index)
            ledger.settle(record["reservation_id"], 0.001, 1, 1)
        self.assertEqual(ledger.summary()["attempts"], 144)
        with self.assertRaises(LedgerBudgetExceeded):
            ledger.reserve(0.001)
        self.assertEqual(BudgetLedger(self.path, self.new).summary()["attempts"], 144)

    def test_cost_cap_remains_ten_including_original_spend(self):
        self.seed_ledger(charge=0.6)
        self.extend(expected_attempts=16)
        ledger = BudgetLedger(self.path, self.new)
        self.assertAlmostEqual(ledger.summary()["accounted_cost"], 9.6)
        reservation = ledger.reserve(0.4)
        ledger.settle(reservation["reservation_id"], 0.4, 1, 1)
        self.assertEqual(ledger.summary()["remaining_budget"], 0)
        self.assertEqual(ledger.summary()["attempts"], 17)
        with self.assertRaises(LedgerBudgetExceeded):
            ledger.reserve(0.00001)

    def test_missing_ledger_is_not_created(self):
        self.assertFalse(self.path.exists())
        with self.assertRaises(FileNotFoundError):
            self.extend(expected_attempts=16)
        self.assertFalse(self.path.exists())

    def test_authorization_note_required_without_changes(self):
        self.seed_ledger()
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            extend_request_limit(self.path, self.old, self.new)
        for note in ("", "   ", None):
            with self.subTest(note=note), self.assertRaises(ValueError):
                extend_request_limit(self.path, self.old, self.new, authorization_note=note)
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_configuration_cannot_change_model_sampling_prices_or_total_money(self):
        ledger, _ = self.seed_ledger()
        variants = []
        for key, value in (("model", "other-model"), ("base_url", "https://other.invalid"),
                           ("temperature", 0.3), ("max_tokens", 200), ("timeout_seconds", 10),
                           ("expected_model_version", "unverified-version"),
                           ("extra_body", {"thinking": {"type": "enabled"}})):
            changed = copy.deepcopy(self.new)
            changed[key] = value
            variants.append((key, changed))
        for key, value in (("max_cost", 11), ("currency", "USD"), ("input_per_million", 1),
                           ("output_per_million", 1), ("pricing_source", "different-prices")):
            changed = copy.deepcopy(self.new)
            changed["budget"][key] = value
            variants.append(("budget." + key, changed))
        for label, changed in variants:
            with self.subTest(field=label), self.assertRaises(ValueError):
                extend_request_limit(self.path, self.old, changed, authorization_note="test")
        self.assertEqual(ledger.summary()["attempts"], 16)
        self.assertAlmostEqual(ledger.summary()["accounted_cost"], 0.16)

    def test_new_limit_must_increase_and_be_integer(self):
        ledger, _ = self.seed_ledger()
        for cap in (16, 15, 0, -1, True, 144.5, "144"):
            changed = copy.deepcopy(self.new)
            changed["max_requests"] = cap
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                extend_request_limit(self.path, self.old, changed, authorization_note="test")
        self.assertEqual(ledger.summary()["signature"], budget_signature(self.old))

    def test_unresolved_reservation_prevents_extension(self):
        ledger, _ = self.seed_ledger(count=15)
        pending = ledger.reserve(0.01)
        rows = self.rows()
        with self.assertRaises(LedgerStateError):
            self.extend(expected_attempts=16)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(ledger.summary()["unresolved_reservations"], 1)
        ledger.settle(pending["reservation_id"], None)
        self.extend(expected_attempts=16)
        self.assertEqual(BudgetLedger(self.path, self.new).summary()["unknown_cost_requests"], 1)

    def test_wrong_expected_attempts_prevents_extension(self):
        ledger, _ = self.seed_ledger()
        for expected in (0, 15, 17):
            with self.subTest(expected=expected), self.assertRaises(LedgerStateError):
                self.extend(expected_attempts=expected)
        self.assertEqual(ledger.summary()["signature"], budget_signature(self.old))
        self.assertEqual(ledger.summary()["attempts"], 16)

    def test_duplicate_extension_cannot_grant_new_allowance(self):
        self.seed_ledger()
        self.extend(expected_attempts=16)
        with self.assertRaises(LedgerConfigMismatch):
            self.extend(expected_attempts=16)
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM funpack_budget_extensions").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(BudgetLedger(self.path, self.new).summary()["attempts"], 16)

    def test_competing_extensions_only_commit_once(self):
        ledger, _ = self.seed_ledger()
        before = ledger.summary()
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=5)
            try:
                self.extend(expected_attempts=16)
                return "extended"
            except LedgerConfigMismatch:
                return "stale_configuration"

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [pool.submit(attempt) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in attempts]
        self.assertCountEqual(outcomes, ["extended", "stale_configuration"])
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM funpack_budget_extensions").fetchone()[0]
        self.assertEqual(count, 1)
        after = BudgetLedger(self.path, self.new).summary()
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["accounted_cost"], before["accounted_cost"])

    def test_old_live_ledger_rejects_reads_reservations_and_settlements(self):
        ledger, reservations = self.seed_ledger()
        self.extend(expected_attempts=16)
        with self.assertRaises(LedgerConfigMismatch):
            ledger.summary()
        with self.assertRaises(LedgerConfigMismatch):
            ledger.reserve(0.001)
        with self.assertRaises(LedgerConfigMismatch):
            ledger.settle(reservations[0]["reservation_id"], 0.01, 5, 3)
        with self.assertRaises(LedgerConfigMismatch):
            BudgetLedger(self.path, self.old)
        self.assertEqual(BudgetLedger(self.path, self.new).summary()["attempts"], 16)

    def test_old_provider_is_stopped_before_network_request(self):
        self.seed_ledger()
        with (patch.dict(os.environ, {"FUNPACK_EXTENSION_TEST_KEY": "not-a-real-api-key"}),
              patch("funpack.providers.request.build_opener") as opener):
            old_provider = ApiProvider(self.old, allow_paid=True)
            self.extend(expected_attempts=16)
            with self.assertRaises(LedgerConfigMismatch):
                old_provider.generate([{"role": "user", "content": "simulated prompt"}])
            opener.return_value.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
