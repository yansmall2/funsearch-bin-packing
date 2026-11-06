"""Offline fixtures and guarded, explicitly enabled OpenAI-compatible requests.

Offline outputs are HANDWRITTEN FIXTURES, never evidence of LLM effectiveness.
API calls use one attempt only: failed/unknown billing retains its reservation.
All monetary values are token-price estimates, not a provider billing receipt.
"""

import copy
import http.client
import json
import math
import os
import re
import time
from urllib import error, parse, request

from .ledger import BudgetLedger, LedgerBudgetExceeded


class BudgetExceeded(RuntimeError):
    """Raised before a request when its reservation cannot fit the limits."""


class _NoRedirect(request.HTTPRedirectHandler):
    """An API redirect must never forward Authorization to another endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OFFLINE_FIXTURES = [
    "def priority(item, bins):\n    return [0.0 for r in bins]",
    "def priority(item, bins):\n    return [-(r - item) for r in bins]",
    "def priority(item, bins):\n    return [-(r - item) for r in bins]",
    "def priority(item, bins):\n    return [2.0 * (-(r - item)) + 3.0 for r in bins]",
    "def priority(item, bins):\n    return [(r - item) for r in bins]",
    "def priority(item, bins):\n    return open('forbidden')",
    "def priority(item, bins):\n    return [1.0 / (r - r) for r in bins]",
    "def priority(item, bins):\n    return [1000.0 if r == item else -(r - item) for r in bins]",
    "def priority(item, bins):\n    return [1.0 for r in bins]",
    "def priority(item, bins):\n    return [-abs((r - item) - item) for r in bins]",
    "def priority(item, bins):\n    return [0.0 for r in bins]",
    "def priority(item, bins):\n    return [-(r - item) ** 2 for r in bins]",
    "def priority(item, bins):\n    return [r % item for r in bins]",
    "def priority(item, bins):\n    return [-(r % item) for r in bins]",
    "def priority(item, bins):\n    return [1000.0 if r == item else r - item for r in bins]",
    "def priority(item, bins):\n    return [-(r - item) for r in bins]",
]


class OfflineProvider:
    """Same candidate stream for all arms; prompts affect the format wrapper only."""

    def __init__(self, seed=0, fixtures=None):
        self.fixtures = list(OFFLINE_FIXTURES if fixtures is None else fixtures)
        if not self.fixtures or not all(isinstance(x, str) for x in self.fixtures):
            raise ValueError("fixtures must be a nonempty list of strings")
        self.seed = int(seed)
        self.attempts = 0
        self.accounted_cost = 0.0

    def generate(self, messages):
        started = time.perf_counter()
        index = (self.seed + self.attempts) % len(self.fixtures)
        self.attempts += 1
        code = self.fixtures[index]
        text = "```python\n" + code + "\n```"
        enhanced_format = any("Start with 'Strategy:'" in message.get("content", "") for message in messages)
        if enhanced_format:
            text = ("Strategy: This is a handwritten offline fixture for pipeline testing. "
                    "Its fixed code does not depend on prompt quality and is not an LLM result.\n\n" + text)
        return {
            "text": text,
            "raw_response": {"offline_fixture": True, "fixture_index": index,
                             "code": code, "candidate_generation_ignores_prompt": True,
                             "enhanced_format_wrapper": enhanced_format},
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "prompt_tokens": 0, "completion_tokens": 0,
                      "input_tokens_known": True, "output_tokens_known": True},
            "model": "handwritten-offline-fixtures-v1",
            "provider": "offline_fixture", "offline_fixture": True,
            "elapsed_seconds": time.perf_counter() - started,
            "estimated_cost": 0.0, "cost_known": True,
            "request_attempted": False, "paid_api_request": False,
            "request_index": self.attempts,
        }


def _positive_number(value, allow_zero=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value >= 0 if allow_zero else value > 0))


def _contains_secret_field(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"api.?key|authorization|password|secret|access.?token", str(key), re.I):
                return True
            if _contains_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(x) for x in value)
    return False


def preflight(config, allow_paid=False):
    """Validate configuration and presence of ONE named key; never call a URL."""
    errors = []
    if not isinstance(config, dict):
        return {"ok": False, "errors": ["config must be an object"], "warnings": []}
    if config.get("enabled") is not True:
        errors.append("API is disabled; enabled must be true after approval")
    if allow_paid is not True:
        errors.append("paid calls require the explicit allow_paid flag")
    base_url = config.get("base_url")
    safe_base_url = "[invalid URL omitted]"
    try:
        parts = parse.urlsplit(base_url if isinstance(base_url, str) else "")
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            errors.append("base_url must be an HTTPS URL without credentials/query/fragment")
        else:
            safe_base_url = base_url
    except ValueError:
        errors.append("base_url is invalid")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        errors.append("model must be explicitly configured")
    key_name = config.get("api_key_env", "FUNPACK_API_KEY")
    valid_key_name = isinstance(key_name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name)
    key_present = bool(os.environ.get(key_name)) if valid_key_name else False
    if not valid_key_name:
        errors.append("api_key_env must be a valid environment variable name")
    elif not key_present:
        errors.append("the configured API-key environment variable is empty")
    budget = config.get("budget", {})
    if not isinstance(budget, dict):
        errors.append("budget must be an object")
        budget = {}
    if not isinstance(budget.get("currency"), str) or not budget["currency"].strip():
        errors.append("budget.currency must be specified")
    for name in ("max_cost", "input_per_million", "output_per_million"):
        if not _positive_number(budget.get(name)):
            errors.append("budget." + name + " must be a known positive number")
    if not isinstance(budget.get("pricing_source"), str) or not budget["pricing_source"].strip():
        errors.append("budget.pricing_source must identify the checked provider pricing")
    for name in ("max_tokens", "max_requests"):
        value = config.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(name + " must be a positive integer")
    if not _positive_number(config.get("timeout_seconds", 60)):
        errors.append("timeout_seconds must be positive")
    temperature = config.get("temperature", 0.8)
    if not _positive_number(temperature, allow_zero=True) or temperature > 2:
        errors.append("temperature must be between 0 and 2")
    extra = config.get("extra_body", {})
    reserved = {"messages", "model", "temperature", "max_tokens", "max_completion_tokens", "stream", "n"}
    if not isinstance(extra, dict):
        errors.append("extra_body must be an object")
    elif reserved.intersection(extra):
        errors.append("extra_body cannot override request or budget-control fields")
    elif _contains_secret_field(extra):
        errors.append("extra_body must not contain credentials")
    else:
        try:
            json.dumps(extra, allow_nan=False)
        except (TypeError, ValueError):
            errors.append("extra_body must be finite JSON data")
    if any(name in config for name in ("api_key", "key", "authorization", "password")):
        errors.append("keys must be supplied only through the named environment variable")
    if "ledger_path" in config and (not isinstance(config["ledger_path"], str) or not config["ledger_path"].strip()):
        errors.append("ledger_path must be a nonempty path string when configured")
    return {
        "ok": not errors, "errors": errors,
        "warnings": [
            "Token reservations are conservative estimates, not a provider billing guarantee.",
            "Uncached peak input prices are used; no cache discount is assumed.",
            ("The configured SQLite ledger preserves budget across providers and restarts."
             if config.get("ledger_path") else
             "No persistent ledger configured: a fresh provider object resets the in-memory budget."),
        ],
        "model": model, "base_url": safe_base_url, "api_key_env": key_name,
        "key_present": key_present, "currency": budget.get("currency"),
        "max_cost": budget.get("max_cost"), "max_requests": config.get("max_requests"),
    }


class ApiProvider:
    """One-attempt synchronous client with per-instance request/cost limits."""

    def __init__(self, config, allow_paid=False):
        checked = preflight(config, allow_paid)
        if not checked["ok"]:
            raise ValueError("API preflight failed: " + "; ".join(checked["errors"]))
        self.config = copy.deepcopy(config)
        self._key = os.environ[self.config.get("api_key_env", "FUNPACK_API_KEY")]
        self._opener = request.build_opener(_NoRedirect())
        self.attempts = 0
        self.accounted_cost = 0.0
        self.ledger = BudgetLedger(self.config["ledger_path"], self.config) if self.config.get("ledger_path") else None
        if self.ledger is not None:
            self._refresh_ledger(self.ledger.summary())

    def _refresh_ledger(self, summary):
        self.attempts = summary["attempts"]
        self.accounted_cost = summary["accounted_cost"]

    @property
    def remaining_budget(self):
        if self.ledger is not None:
            summary = self.ledger.summary()
            self._refresh_ledger(summary)
            return summary["remaining_budget"]
        return max(0.0, self.config["budget"]["max_cost"] - self.accounted_cost)

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._key, "[REDACTED]")
        if isinstance(value, list):
            return [self._redact(x) for x in value]
        if isinstance(value, dict):
            return {self._redact(str(k)): self._redact(v) for k, v in value.items()}
        return value

    def _body(self, messages):
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty list")
        for message in messages:
            if (not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant")
                    or not isinstance(message.get("content"), str)):
                raise ValueError("messages require a valid role and text content")
        body = {"model": self.config["model"], "messages": messages,
                "temperature": self.config.get("temperature", 0.8),
                "max_tokens": self.config["max_tokens"], "stream": False, "n": 1}
        body.update(self.config.get("extra_body", {}))
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def estimate_reservation(self, messages):
        """No network. UTF-8 bytes plus 4096 tokens for framing/hidden overhead."""
        body = self._body(messages)
        input_upper_estimate = len(body) + 4096
        b = self.config["budget"]
        amount = (input_upper_estimate * b["input_per_million"]
                  + self.config["max_tokens"] * b["output_per_million"]) / 1_000_000
        return {"input_token_reservation": input_upper_estimate,
                "output_token_reservation": self.config["max_tokens"], "cost": amount}

    def generate(self, messages):
        encoded = self._body(messages)
        reservation = self.estimate_reservation(messages)
        ledger_reservation = None
        if self.ledger is not None:
            try:
                ledger_reservation = self.ledger.reserve(reservation["cost"])
            except LedgerBudgetExceeded as exc:
                raise BudgetExceeded(str(exc)) from None
            self._refresh_ledger(ledger_reservation)
            reservation["ledger_reservation_id"] = ledger_reservation["reservation_id"]
            reservation["cost"] = ledger_reservation["reserved_cost"]
            request_index = ledger_reservation["request_index"]
        else:
            if self.attempts >= self.config["max_requests"]:
                raise BudgetExceeded("request limit reached; no request attempted")
            if self.accounted_cost + reservation["cost"] > self.config["budget"]["max_cost"]:
                raise BudgetExceeded("remaining budget cannot cover reservation; no request attempted")
            self.attempts += 1
            self.accounted_cost += reservation["cost"]
            request_index = self.attempts
        started = time.perf_counter()
        endpoint = self.config["base_url"].rstrip("/") + "/chat/completions"
        req = request.Request(endpoint, data=encoded, method="POST", headers={
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        raw = None
        error_info = None
        try:
            with self._opener.open(req, timeout=self.config.get("timeout_seconds", 60)) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raw = {"response_truncated": True, "read_bytes": len(payload)}
                raise ValueError("response exceeds 8 MiB limit")
            decoded = payload.decode("utf-8", errors="replace")
            raw = {"unparsed_body": decoded}
            raw = json.loads(decoded)
            if not isinstance(raw, dict):
                raise ValueError("response JSON must be an object")
            if "error" in raw:
                error_info = {"type": "provider_error", "message": str(raw["error"])}
        except error.HTTPError as exc:
            try:
                payload = exc.read(1024 * 1024).decode("utf-8", errors="replace")
            except Exception:
                payload = "error body unavailable"
            raw = {"http_status": exc.code, "body": payload}
            error_info = {"type": "http_error", "message": "HTTP " + str(exc.code)}
        except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            error_info = {"type": "network_error", "message": str(exc)}
        except (ValueError, UnicodeError, TypeError) as exc:
            error_info = {"type": "invalid_response", "message": str(exc)}

        raw = self._redact(raw)
        error_info = self._redact(error_info)
        usage_data = raw.get("usage", {}) if isinstance(raw, dict) else {}
        if not isinstance(usage_data, dict):
            usage_data = {}
        input_tokens = usage_data.get("prompt_tokens")
        output_tokens = usage_data.get("completion_tokens")
        input_known = isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0
        output_known = isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0
        cost_known = input_known and output_known
        price = self.config["budget"]
        accounted = reservation["cost"]
        if cost_known:
            accounted = (input_tokens * price["input_per_million"]
                         + output_tokens * price["output_per_million"]) / 1_000_000
            if self.ledger is None:
                self.accounted_cost += accounted - reservation["cost"]
        if self.ledger is not None:
            summary = self.ledger.settle(
                ledger_reservation["reservation_id"], accounted if cost_known else None,
                input_tokens if input_known else None, output_tokens if output_known else None)
            self._refresh_ledger(summary)
        text = ""
        finish_reason = None
        if error_info is None:
            try:
                choice = raw["choices"][0]
                text = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
                if not isinstance(text, str):
                    raise ValueError("message content must be text")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                text = ""
                error_info = {"type": "invalid_response", "message": str(exc)}
        result = {
            "text": text, "raw_response": raw,
            "usage": {"input_tokens": input_tokens if input_known else None,
                      "output_tokens": output_tokens if output_known else None,
                      "prompt_tokens": input_tokens if input_known else None,
                      "completion_tokens": output_tokens if output_known else None,
                      "input_tokens_known": input_known, "output_tokens_known": output_known},
            "model": raw.get("model") if isinstance(raw, dict) else None,
            "requested_model": self.config["model"], "provider": "api_compatible",
            "offline_fixture": False, "elapsed_seconds": time.perf_counter() - started,
            "estimated_cost": accounted, "cost_known": cost_known,
            "cost_basis": "uncached_token_price_estimate" if cost_known else "retained_reservation",
            "currency": price["currency"], "request_attempted": True, "paid_api_request": True,
            "request_index": request_index, "reservation": reservation,
            "budget_accounted_cost": self.accounted_cost, "remaining_budget": self.remaining_budget,
            "finish_reason": finish_reason,
            "reservation_exceeded": cost_known and accounted > reservation["cost"],
        }
        if error_info:
            result["error"] = error_info
        return self._redact(result)
