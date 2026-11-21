import copy
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import shutil
import uuid
import unittest
from unittest import mock

from funpack.ledger import BudgetLedger, LedgerBudgetExceeded, LedgerConfigMismatch, LedgerStateError
from funpack.providers import ApiProvider, BudgetExceeded


class WorkspaceTemporaryDirectory:
    """Use inherited Windows ACLs, like the other project integration tests."""
    def __init__(self, prefix):
        self.root = (Path(__file__).resolve().parents[1] / ".test-tmp").resolve()
        self.root.mkdir(exist_ok=True)
        self.path = self.root / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        if self.path.resolve().parent != self.root or self.path.is_symlink():
            raise RuntimeError("refusing cleanup outside test workspace")
        shutil.rmtree(self.path)


def config():
    return {"enabled": True, "base_url": "https://unused.invalid/v1", "model": "test-model",
            "api_key_env": "FUNPACK_LEDGER_TEST_KEY", "max_tokens": 100, "max_requests": 3,
            "timeout_seconds": 2, "temperature": 0.8, "extra_body": {},
            "budget": {"currency": "USD", "max_cost": 1.0, "input_per_million": 1.0,
                       "output_per_million": 2.0, "pricing_source": "test-price-source"}}


def _reserve_in_process(path, cfg, result_queue):
    try:
        ledger = BudgetLedger(path, cfg)
        result_queue.put(("reserved", ledger.reserve(0.4)["request_index"]))
    except LedgerBudgetExceeded:
        result_queue.put(("blocked", None))
    except Exception as exc:
        result_queue.put(("unexpected", type(exc).__name__))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = WorkspaceTemporaryDirectory(prefix="ledger-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "budget.sqlite3"
        self.cfg = config()

    def test_restart_preserves_unresolved_reservation_and_request_limit(self):
        first = BudgetLedger(self.path, self.cfg)
        reservation = first.reserve(0.4)
        restarted = BudgetLedger(self.path, self.cfg)
        self.assertEqual(restarted.summary()["attempts"], 1)
        self.assertEqual(restarted.summary()["unresolved_reservations"], 1)
        self.assertAlmostEqual(restarted.summary()["accounted_cost"], 0.4)
        restarted.reserve(0.4)
        with self.assertRaises(LedgerBudgetExceeded):
            restarted.reserve(0.3)
        self.assertEqual(restarted.summary()["attempts"], 2)
        restarted.settle(reservation["reservation_id"], 0.1, 1, 2)
        self.assertAlmostEqual(first.summary()["accounted_cost"], 0.5)

    def test_unknown_settlement_retains_reservation(self):
        ledger = BudgetLedger(self.path, self.cfg)
        reservation = ledger.reserve(0.4)
        result = ledger.settle(reservation["reservation_id"], input_tokens=10)
        self.assertAlmostEqual(result["accounted_cost"], 0.4)
        self.assertEqual(result["unknown_cost_requests"], 1)
        self.assertEqual(result["unresolved_reservations"], 0)
        reopened = BudgetLedger(self.path, self.cfg)
        self.assertAlmostEqual(reopened.summary()["accounted_cost"], 0.4)

    def test_request_count_never_refunded_and_settlement_idempotent(self):
        ledger = BudgetLedger(self.path, self.cfg)
        for _ in range(3):
            reservation = ledger.reserve(0.1)
            ledger.settle(reservation["reservation_id"], 0.001, 1, 2)
            ledger.settle(reservation["reservation_id"], 0.001, 1, 2)
        self.assertEqual(ledger.summary()["attempts"], 3)
        self.assertAlmostEqual(ledger.summary()["accounted_cost"], 0.003)
        with self.assertRaises(LedgerBudgetExceeded):
            BudgetLedger(self.path, self.cfg).reserve(0.1)
        with self.assertRaises(LedgerStateError):
            ledger.settle(reservation["reservation_id"], 0.0, 1, 2)

    def test_known_overrun_is_recorded_and_stops_further_requests(self):
        ledger = BudgetLedger(self.path, self.cfg)
        reservation = ledger.reserve(0.2)
        ledger.settle(reservation["reservation_id"], 1.1, 1, 2)
        self.assertAlmostEqual(ledger.summary()["accounted_cost"], 1.1)
        self.assertEqual(ledger.summary()["remaining_budget"], 0)
        with self.assertRaises(LedgerBudgetExceeded):
            ledger.reserve(0.01)

    def test_parameter_and_budget_changes_rejected_without_resetting_ledger(self):
        ledger = BudgetLedger(self.path, self.cfg)
        ledger.reserve(0.1)
        changes = [dict(model="changed"), dict(base_url="https://other.invalid"),
                   dict(max_tokens=200), dict(temperature=0.2), dict(max_requests=4),
                   dict(expected_model_version="v2"), dict(extra_body={"thinking": {"type": "disabled"}})]
        for change in changes:
            modified = copy.deepcopy(self.cfg)
            modified.update(change)
            with self.assertRaises(LedgerConfigMismatch):
                BudgetLedger(self.path, modified)
        for name, value in (("max_cost", 2.0), ("input_per_million", 3), ("currency", "CNY")):
            modified = copy.deepcopy(self.cfg)
            modified["budget"][name] = value
            with self.assertRaises(LedgerConfigMismatch):
                BudgetLedger(self.path, modified)
        self.assertEqual(ledger.summary()["attempts"], 1)

    def test_atomic_reservations_across_processes(self):
        BudgetLedger(self.path, self.cfg)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=_reserve_in_process, args=(str(self.path), self.cfg, queue))
                     for _ in range(4)]
        try:
            for process in processes:
                process.start()
            results = [queue.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(result[0] == "reserved" for result in results), 2)
            self.assertEqual(sum(result[0] == "blocked" for result in results), 2)
            summary = BudgetLedger(self.path, self.cfg).summary()
            self.assertEqual(summary["attempts"], 2)
            self.assertAlmostEqual(summary["accounted_cost"], 0.8)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            queue.close()

    def test_database_stores_no_credentials_prompt_or_raw_config(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["api_key"] = "not-a-real-secret-test-string"
        ledger = BudgetLedger(self.path, cfg)
        ledger.reserve(0.2)
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        for forbidden in ("not-a-real-secret", "FUNPACK_LEDGER_TEST_KEY", "test-model", "test-price-source"):
            self.assertNotIn(forbidden, dump)


class ProviderLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = WorkspaceTemporaryDirectory(prefix="provider-ledger-test-")
        self.addCleanup(self.temporary.cleanup)
        self.cfg = config()
        self.cfg["ledger_path"] = str(Path(self.temporary.name) / "api-budget.sqlite3")
        self.env = mock.patch.dict(os.environ, {"FUNPACK_LEDGER_TEST_KEY": "mock-ledger-key-not-real"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.patch = mock.patch("funpack.providers.request.build_opener")
        self.opener = self.patch.start().return_value
        self.addCleanup(self.patch.stop)
        self.messages = [{"role": "user", "content": "Generate a function."}]

    def set_response(self):
        value = {"model": "test-version", "choices": [{"message": {"content": "code"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        self.opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(value).encode()

    def test_two_providers_and_restart_share_attempt_and_cost_caps(self):
        self.set_response()
        first = ApiProvider(self.cfg, allow_paid=True)
        second = ApiProvider(self.cfg, allow_paid=True)
        self.assertEqual(first.generate(self.messages)["request_index"], 1)
        self.assertEqual(second.generate(self.messages)["request_index"], 2)
        third = ApiProvider(self.cfg, allow_paid=True)
        self.assertEqual(third.attempts, 2)
        self.assertAlmostEqual(third.accounted_cost, 0.0001)
        self.assertEqual(third.generate(self.messages)["request_index"], 3)
        with self.assertRaises(BudgetExceeded):
            first.generate(self.messages)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_crash_retains_reservation_across_provider_restart(self):
        first = ApiProvider(self.cfg, allow_paid=True)
        self.opener.open.side_effect = KeyboardInterrupt("simulated process interruption")
        with self.assertRaises(KeyboardInterrupt):
            first.generate(self.messages)
        second = ApiProvider(self.cfg, allow_paid=True)
        self.assertEqual(second.attempts, 1)
        self.assertGreater(second.accounted_cost, 0)
        self.assertEqual(second.ledger.summary()["unresolved_reservations"], 1)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_unknown_failure_persists_and_prevents_cap_reset(self):
        first = ApiProvider(self.cfg, allow_paid=True)
        reservation = first.estimate_reservation(self.messages)["cost"]
        # A new ledger path/config is used BEFORE spending, never to reset spent budget.
        self.cfg["ledger_path"] = str(Path(self.temporary.name) / "small-budget.sqlite3")
        self.cfg["budget"]["max_cost"] = reservation * 1.5
        first = ApiProvider(self.cfg, allow_paid=True)
        self.opener.open.side_effect = TimeoutError("test timeout")
        response = first.generate(self.messages)
        self.assertFalse(response["cost_known"])
        second = ApiProvider(self.cfg, allow_paid=True)
        with self.assertRaises(BudgetExceeded):
            second.generate(self.messages)
        self.assertEqual(self.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
