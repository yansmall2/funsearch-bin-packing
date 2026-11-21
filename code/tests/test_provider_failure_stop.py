"""Connection/API failures must never consume the rest of a planned study."""
import io
import unittest
from unittest.mock import patch

import experiment
from funpack.search import run_search
from funpack.storage import read_json
from test_search import INSTANCES, PROBES, _config, _events, workspace_temp


class FailedProvider:
    def __init__(self, error_type="network_error", http_status=None):
        self.calls = 0
        self.error_type, self.http_status = error_type, http_status

    def generate(self, messages):
        self.calls += 1
        return {"text": "", "raw_response": {"http_status": self.http_status},
                "error": {"type": self.error_type, "message": "test fixture failure"},
                "request_attempted": True, "paid_api_request": True, "cost_known": False, "usage": {}}


class FailureStopTests(unittest.TestCase):
    def test_first_network_failure_stops_after_saving_its_evidence(self):
        with workspace_temp() as directory:
            provider = FailedProvider()
            result = run_search(INSTANCES, PROBES, provider, _config(arm="A", calls=50), directory)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(result["termination"], "provider_network_error")
            self.assertEqual(result["metrics"]["provider_errors"], 1)
            self.assertEqual(result["metrics"]["candidate_responses"], 0)
            self.assertEqual(result["metrics"]["unknown_cost_requests"], 1)
            self.assertEqual(len(_events(directory)), 1)
            self.assertFalse((directory / "pending_request.json").exists())

    def test_http_request_and_server_errors_also_stop(self):
        for code, expected in ((400, "provider_request_error"), (429, "provider_request_error"), (500, "provider_error")):
            with self.subTest(code=code), workspace_temp() as directory:
                provider = FailedProvider("http_error", code)
                result = run_search(INSTANCES, PROBES, provider, _config(arm="A", calls=50), directory)
                self.assertEqual(provider.calls, 1)
                self.assertEqual(result["termination"], expected)

    def test_outer_study_does_not_start_remaining_arms(self):
        provider = FailedProvider()
        with workspace_temp() as directory:
            with (patch.object(experiment, "load_instances", return_value=INSTANCES),
                  patch.object(experiment, "load_probes", return_value=PROBES),
                  patch.object(experiment, "OfflineProvider", return_value=provider),
                  patch("sys.stdout", new=io.StringIO())):
                experiment.main(["--stage", "search", "--out_dir", str(directory), "--calls", "50", "--seeds", "11"])
            study = read_json(directory / "study.json")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(len(study["runs"]), 1)
            self.assertEqual(study["stop_reason"], "provider_network_error")
            self.assertFalse((directory / "B_seed11").exists())


if __name__ == "__main__":
    unittest.main()
