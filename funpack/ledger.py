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


def _ledger_summary(connection, signature, max_cost_units):
    row = connection.execute("""SELECT COUNT(*), COALESCE(SUM(accounted_units), 0),
        COALESCE(SUM(CASE WHEN status != 'known' THEN 1 ELSE 0 END), 0),
        COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0)
        FROM funpack_requests""").fetchone()
    return {"attempts": row[0], "accounted_cost": row[1] / _SCALE,
            "remaining_budget": max(0, max_cost_units - row[1]) / _SCALE,
            "unknown_cost_requests": row[2], "unresolved_reservations": row[3],
            "signature": signature}


def extend_request_limit(path, old_config, new_config, *, authorization_note,
                         expected_attempts=None):
    """Record an explicitly authorized request extension in an existing ledger.

    Only ``max_requests`` may increase: model, prices, total monetary cap and
    every other configuration field stay unchanged. No request row is edited
    or deleted. The caller supplies a brief authorization reference, never a
    credential or prompt. This function itself does not obtain authorization.
    An old->new retry fails once committed, rather than adding another batch.
    """
    if not isinstance(old_config, dict) or not isinstance(new_config, dict):
        raise ValueError("old_config and new_config must be objects")
    old_limit, new_limit = old_config.get("max_requests"), new_config.get("max_requests")
    if (type(old_limit) is not int or type(new_limit) is not int
            or not 0 < old_limit < new_limit <= _MAX_SQLITE_INT):
        raise ValueError("max_requests must increase from a positive integer")
    adjusted = dict(new_config)
    adjusted["max_requests"] = old_limit
    if adjusted != old_config:
        raise LedgerConfigMismatch("request extension may change only max_requests")
    if (not isinstance(authorization_note, str) or not authorization_note.strip()
            or len(authorization_note) > 256):
        raise ValueError("authorization_note must be a nonempty reference of at most 256 characters")
    if expected_attempts is not None and (type(expected_attempts) is not int or expected_attempts < 0):
        raise ValueError("expected_attempts must be a nonnegative integer or None")
    old_signature = budget_signature(old_config)
    new_signature = budget_signature(new_config)
    max_cost_units = _units(old_config["budget"]["max_cost"], ROUND_FLOOR)
    if max_cost_units <= 0:
        raise ValueError("monetary cap must remain positive")
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("request extension requires an existing ledger")
    # mode=rw prevents a race with deletion from silently creating an empty DB.
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                 timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT schema_version, signature FROM funpack_budget_meta WHERE singleton=1").fetchone()
        if row != (_SCHEMA_VERSION, old_signature):
            raise LedgerConfigMismatch("ledger does not match the old configuration; extension was not applied")
        count, used, pending = connection.execute("""SELECT COUNT(*), COALESCE(SUM(accounted_units), 0),
            COALESCE(SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END), 0)
            FROM funpack_requests""").fetchone()
        if expected_attempts is not None and count != expected_attempts:
            raise LedgerStateError("ledger attempts changed; inspect before authorizing an extension")
        if pending:
            raise LedgerStateError("unresolved reservations prevent extension; preserve and investigate them")
        if count > old_limit:
            raise LedgerStateError("ledger request count exceeds the old request limit")
        if used >= max_cost_units:
            raise LedgerBudgetExceeded("monetary budget exhausted; request-only extension cannot replenish money")
        connection.execute("""CREATE TABLE IF NOT EXISTS funpack_budget_extensions (
            extension_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_utc TEXT NOT NULL, authorization_note TEXT NOT NULL,
            old_max_requests INTEGER NOT NULL, new_max_requests INTEGER NOT NULL,
            old_signature TEXT NOT NULL, new_signature TEXT NOT NULL,
            attempts_at_extension INTEGER NOT NULL,
            accounted_units_at_extension INTEGER NOT NULL)""")
        cursor = connection.execute("""INSERT INTO funpack_budget_extensions
            (created_utc, authorization_note, old_max_requests, new_max_requests,
             old_signature, new_signature, attempts_at_extension, accounted_units_at_extension)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), authorization_note.strip(), old_limit, new_limit,
             old_signature, new_signature, count, used))
        connection.execute("UPDATE funpack_budget_meta SET signature=? WHERE singleton=1",
                           (new_signature,))
        result = {**_ledger_summary(connection, new_signature, max_cost_units),
                  "old_max_requests": old_limit, "new_max_requests": new_limit,
                  "extension_id": cursor.lastrowid}
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


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
        with self._transaction(check_signature=False) as connection:
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
    def _transaction(self, *, check_signature=True):
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            if check_signature:
                row = connection.execute(
                    "SELECT schema_version, signature FROM funpack_budget_meta WHERE singleton=1").fetchone()
                if row != (_SCHEMA_VERSION, self.signature):
                    raise LedgerConfigMismatch(
                        "ledger configuration changed; reopen with the explicitly authorized configuration")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _summary(self, connection):
        return _ledger_summary(connection, self.signature, self.max_cost_units)

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
