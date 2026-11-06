"""Tests for the verify_query tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sre_agent.k8s_tools import verify_query


def _mock_prom_instant(status: str, results: list | None = None, error: str = ""):
    """Mock Prometheus instant query API response."""
    data = {"status": status}
    if status == "success":
        data["data"] = {"resultType": "vector", "result": results or []}
    if error:
        data["error"] = error
    response_data = json.dumps(data).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return patch("sre_agent.bearer_http.open_with_token", return_value=mock_resp)


class TestVerifyQuery:
    def test_pass_with_data(self):
        results = [
            {"metric": {"__name__": "up", "job": "kubelet"}, "value": [1711540800, "1"]},
            {"metric": {"__name__": "up", "job": "apiserver"}, "value": [1711540800, "1"]},
        ]
        with _mock_prom_instant("success", results):
            result = verify_query.call({"query": "up"})
        assert "PASS" in result
        assert "2 series" in result

    def test_fail_no_data(self):
        with _mock_prom_instant("success", []):
            result = verify_query.call({"query": "nonexistent_metric"})
        assert "FAIL_NO_DATA" in result

    def test_fail_syntax(self):
        with _mock_prom_instant("error", error="parse error"):
            result = verify_query.call({"query": "invalid{{"})
        assert "FAIL_SYNTAX" in result
        assert "parse error" in result

    def test_fail_unreachable(self):
        with patch("sre_agent.bearer_http.open_with_token", side_effect=Exception("connection refused")):
            result = verify_query.call({"query": "up"})
        assert "FAIL_UNREACHABLE" in result

    def test_single_series_shows_sample(self):
        results = [{"metric": {"__name__": "up"}, "value": [1711540800, "1"]}]
        with _mock_prom_instant("success", results):
            result = verify_query.call({"query": "up"})
        assert "PASS" in result
        assert "1 series" in result
        assert "up=1" in result

    def test_records_success(self):
        results = [{"metric": {"__name__": "up"}, "value": [1711540800, "1"]}]
        with (
            _mock_prom_instant("success", results),
            patch("sre_agent.promql_recipes.record_query_result") as mock_record,
        ):
            verify_query.call({"query": "up"})
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs["success"] is True

    def test_records_failure(self):
        with (
            _mock_prom_instant("success", []),
            patch("sre_agent.promql_recipes.record_query_result") as mock_record,
        ):
            verify_query.call({"query": "nonexistent"})
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs["success"] is False

    def test_invalid_characters_rejected(self):
        result = verify_query.call({"query": "up; drop table"})
        assert "Invalid" in result or "Error" in result

    def test_empty_query(self):
        result = verify_query.call({"query": ""})
        assert "Error" in result or "empty" in result.lower()
