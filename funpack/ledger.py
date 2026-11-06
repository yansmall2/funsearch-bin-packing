"""Persistent, atomic API budget accounting; contains no credentials or prompts.

The ledger reserves before network access. A crash leaves that charge in place.
Reopening the same ledger never resets requests or money. SQLite transactions
serialize independent processes; unresolved reservations are not auto-refunded.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


_SCALE = 1_000_000_000_000  # integer picocurrency units avoid float cap races
_MAX_SQLITE_INT = 2**63 - 1
_SCHEMA_VERSION = 1


class LedgerBudgetExceeded(RuntimeError):
    """The persistent ledger rejected a reservation before network access."""


class LedgerConfigMismatch(ValueError):
    """A ledger cannot silently be reused with different experiment parameters."""


class LedgerStateError(ValueError):
    """An unknown reservation or conflicting settlement was supplied."""


def _units(value, rounding=ROUND_CEILING):
    if isinstance(value, bool):
        raise ValueError("monetary amounts must be finite nonnegative numbers")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("monetary amounts must be finite nonnegative numbers")
        result = int((amount * _SCALE).to_integral_value(rounding=rounding))
    except (InvalidOperation, TypeError, OverflowError):
        raise ValueError("invalid monetary amount") from None
    if result > _MAX_SQLITE_INT:
        raise ValueError("monetary amount exceeds ledger integer range")
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


def budget_signature(config):
    """Fingerprint relevant public config only. The database stores this HASH.

    Credentials, environment variable values, paths, and prompts never enter
    this signature. Omitted default generation settings are normalized.
    """
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "base_url": config["base_url"].rstrip("/"),
        "model": config["model"],
        "expected_model_version": config.get("expected_model_version"),
        "temperature": config.get("temperature", 0.8),
        "max_tokens": config["max_tokens"],
        "timeout_seconds": config.get("timeout_seconds", 60),
        "extra_body": config.get("extra_body", {}),
        "max_requests": config["max_requests"],
        "budget": config["budget"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BudgetLedger:
    """One database is one cumulative budget, across all providers and restarts."""

    def __init__(self, path, config):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.signature = budget_signature(config)
        self.max_cost_units = _units(config["budget"]["max_cost"], ROUND_FLOOR)
        self.max_requests = config["max_requests"]
        if self.max_cost_units <= 0:
            raise ValueError("budget must be at least one ledger currency unit (1e-12)")
        if type(self.max_requests) is not int or self.max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        with self._transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS funpack_budget_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version INTEGER NOT NULL, signature TEXT NOT NULL,
                created_utc TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS funpack_requests (
                request_index INTEGER PRIMARY KEY AUTOINCREMENT,
                reservation_id TEXT NOT NULL UNIQUE,
                created_utc TEXT NOT NULL, settled_utc TEXT,
                status TEXT NOT NULL CHECK(status IN ('reserved', 'known', 'unknown')),
                reserved_units INTEGER NOT NULL CHECK(reserved_units > 0),
                accounted_units INTEGER NOT NULL CHECK(accounted_units >= 0),
                input_tokens INTEGER, output_tokens INTEGER)""")
            row = connection.execute(
                "SELECT schema_version, signature FROM funpack_budget_meta WHERE singleton=1").fetchone()
            if row is None:
                if connection.execute("SELECT COUNT(*) FROM funpack_requests").fetchone()[0]:
                    raise LedgerStateError("ledger has requests but no configuration signature")
                connection.execute("INSERT INTO funpack_budget_meta VALUES (1, ?, ?, ?)",
                                   (_SCHEMA_VERSION, self.signature, _now()))
            elif row != (_SCHEMA_VERSION, self.signature):
                raise LedgerConfigMismatch(
                    "ledger configuration/price/budget signature differs; do not reset or replace it to bypass a cap")

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _summary(self, connection):
        row = connection.execute("""SELECT COUNT(*), COALESCE(SUM(accounted_units), 0),
            COALESCE(SUM(CASE WHEN status != 'known' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0)
            FROM funpack_requests""").fetchone()
        return {"attempts": row[0], "accounted_cost": row[1] / _SCALE,
                "remaining_budget": max(0, self.max_cost_units - row[1]) / _SCALE,
                "unknown_cost_requests": row[2], "unresolved_reservations": row[3],
                "signature": self.signature}

    def summary(self):
        with self._transaction() as connection:
            return self._summary(connection)

    def reserve(self, cost):
        amount = _units(cost)
        if amount <= 0:
            raise ValueError("reservation must be positive")
        reservation_id = uuid.uuid4().hex
        with self._transaction() as connection:
            count, used = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(accounted_units), 0) FROM funpack_requests").fetchone()
            if count >= self.max_requests:
                raise LedgerBudgetExceeded("persistent request limit reached; no request attempted")
            if used + amount > self.max_cost_units:
                raise LedgerBudgetExceeded("persistent remaining budget cannot cover reservation; no request attempted")
            cursor = connection.execute("""INSERT INTO funpack_requests
                (reservation_id, created_utc, status, reserved_units, accounted_units)
                VALUES (?, ?, 'reserved', ?, ?)""", (reservation_id, _now(), amount, amount))
            return {"reservation_id": reservation_id, "request_index": cursor.lastrowid,
                    "reserved_cost": amount / _SCALE, **self._summary(connection)}

    def settle(self, reservation_id, actual_cost=None, input_tokens=None, output_tokens=None):
        """Settle known token costs; unknown results keep their full reservation.

        Repeating an identical settlement is harmless. Changing a settlement
        fails explicitly. An unresolved crash reservation can be settled only
        through an explicit caller action, never just by reopening this ledger.
        """
        for value in (input_tokens, output_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token counts must be nonnegative integers or None")
        known_units = None if actual_cost is None else _units(actual_cost)
        target_status = "unknown" if actual_cost is None else "known"
        with self._transaction() as connection:
            row = connection.execute("""SELECT status, reserved_units, accounted_units,
                input_tokens, output_tokens FROM funpack_requests WHERE reservation_id=?""",
                (reservation_id,)).fetchone()
            if row is None:
                raise LedgerStateError("unknown reservation ID")
            amount = row[1] if known_units is None else known_units
            if row[0] != "reserved":
                if (row[0], row[2], row[3], row[4]) != (target_status, amount, input_tokens, output_tokens):
                    raise LedgerStateError("conflicting settlement; existing accounting was preserved")
                return self._summary(connection)
            connection.execute("""UPDATE funpack_requests SET status=?, accounted_units=?,
                input_tokens=?, output_tokens=?, settled_utc=? WHERE reservation_id=?""",
                (target_status, amount, input_tokens, output_tokens, _now(), reservation_id))
            return self._summary(connection)
