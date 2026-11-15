"""Muse-as-provider handshake for the FunSearch bin-packing search.

Why this exists: the DeepSeek route from background execution is policy-blocked
(2026-11-15: sentinel 403, do not retry), and the 2026-11-09 thinking run lost
all 200 calls to SSL failures. The study still needs a real generator to
produce candidates. This provider does NOT call any paid API and spends 0 CNY:
it writes the exact messages the search loop built to a request file and waits
for a Muse-side generator (a coordinator agent) to answer with candidate text.

Protocol honesty:
- The generator sees exactly the messages run_search built (same mechanism_v3 /
  basic prompts, same parents). No extra information enters a prompt.
- Every request/response pair is preserved by run_search itself in
  provider_responses.jsonl, plus the handshake files under the handshake dir.
- The returned result is labelled provider="muse_handshake",
  model="muse-spark-1.3", paid_api_request=False, estimated_cost=0.0.
  Nothing here pretends to be a DeepSeek response, and no DeepSeek result is
  mixed into Muse runs. A/B/C/D arm comparisons inside a Muse run are valid;
  absolute numbers are NOT comparable to the 2026-11-06 DeepSeek pilot.
- On timeout or an invalid response file this returns an error dict (the run
  then stops on first provider error, exactly like a broken API). It never
  invents a candidate and never raises inside generate(), except for local
  filesystem failures when writing the request file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .storage import digest, write_json

MODEL_NAME = "muse-spark-1.3"
PROVIDER_NAME = "muse_handshake"


class HandshakeProvider:
    """File-handshake generator: one request file per generate() call."""

    def __init__(self, base_dir, run_name, timeout_seconds=1800, poll_seconds=2.0):
        if not run_name or "/" in run_name or "\\" in run_name:
            raise ValueError("run_name must be a plain directory name")
        timeout = float(timeout_seconds)
        if not timeout > 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_dir = Path(base_dir)
        self.run_dir = self.base_dir / run_name
        self.requests_dir = self.run_dir / "requests"
        self.responses_dir = self.run_dir / "responses"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout
        self.poll_seconds = float(poll_seconds)
        self.attempts = 0
        self.accounted_cost = 0.0

    def _result(self, text, started, error=None):
        result = {
            "text": text,
            "raw_response": {"handshake": True, "provider": PROVIDER_NAME,
                             "model": MODEL_NAME},
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "prompt_tokens": 0, "completion_tokens": 0,
                      "input_tokens_known": True, "output_tokens_known": True},
            "model": MODEL_NAME,
            "requested_model": MODEL_NAME,
            "provider": PROVIDER_NAME,
            "offline_fixture": False,
            "elapsed_seconds": time.perf_counter() - started,
            "estimated_cost": 0.0,
            "cost_known": True,
            "currency": "CNY",
            "request_attempted": True,
            "paid_api_request": False,
            "request_index": self.attempts,
            "finish_reason": "stop" if error is None else None,
        }
        if error is not None:
            result["error"] = error
        return result

    def generate(self, messages):
        started = time.perf_counter()
        self.attempts += 1
        request_id = f"request_{self.attempts:06d}"
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(m, dict) or not isinstance(m.get("content"), str)
                       for m in messages)):
            return self._result("", started, {"type": "invalid_request",
                                              "message": "messages must be a nonempty list of role/content dicts"})
        request = {"request_id": request_id, "request_index": self.attempts,
                   "messages": messages, "messages_hash": digest(messages),
                   "created_at": time.time(), "generator": MODEL_NAME,
                   "expect": "response JSON object with a 'text' string: Strategy + one python block (enhanced) or one python block only (basic)"}
        write_json(self.requests_dir / f"{request_id}.json", request)
        response_path = self.responses_dir / f"{request_id}.json"
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if response_path.exists():
                try:
                    payload = json.loads(response_path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    return self._result("", started, {"type": "handshake_invalid_response",
                                                      "message": f"response file unreadable: {exc}"})
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str) or not text.strip():
                    return self._result("", started, {"type": "handshake_invalid_response",
                                                      "message": "response file must carry a nonempty 'text' string"})
                result = self._result(text, started)
                result["raw_response"]["handshake_request_id"] = request_id
                result["raw_response"]["generator_note"] = payload.get("generator_note", "")
                return result
            if time.monotonic() >= deadline:
                return self._result("", started, {"type": "handshake_timeout",
                                                  "message": f"no response file within {self.timeout_seconds}s for {request_id}"})
            time.sleep(self.poll_seconds)
