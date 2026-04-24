"""Tests for sre_agent.errors — error classification."""

import json

from kubernetes.client.rest import ApiException

from sre_agent.errors import ToolError, classify_api_error, classify_exception


def _make_api_error(status: int, reason: str = "", message: str = "") -> ApiException:
    """Create a mock ApiException."""
    e = ApiException(status=status, reason=reason)
    if message:
        e.body = json.dumps({"message": message})
    else:
        e.body = None
    return e


class TestClassifyApiError:
    def test_403_forbidden(self):
        err = classify_api_error(_make_api_error(403, "Forbidden", "pods is forbidden"))
        assert err.category == "forbidden"
        assert err.status_code == 403
        assert len(err.suggestions) > 0

    def test_403_quota(self):
        err = classify_api_error(_make_api_error(403, "Forbidden", "exceeded quota"))
        assert err.category == "quota"

    def test_401_unauthorized(self):
        err = classify_api_error(_make_api_error(401, "Unauthorized"))
        assert err.category == "unauthorized"
        assert err.status_code == 401
        assert any("reconnect" in s.lower() or "expired" in s.lower() for s in err.suggestions)

    def test_404_not_found(self):
        err = classify_api_error(_make_api_error(404, "Not Found", "pods 'foo' not found"))
        assert err.category == "not_found"
        assert "foo" in str(err)

    def test_409_conflict(self):
        err = classify_api_error(_make_api_error(409, "Conflict", "object has been modified"))
        assert err.category == "conflict"

    def test_422_validation(self):
        err = classify_api_error(_make_api_error(422, "Invalid", "spec is invalid"))
        assert err.category == "validation"

    def test_500_server(self):
        err = classify_api_error(_make_api_error(500, "Internal Server Error"))
        assert err.category == "server"

    def test_502_server(self):
        err = classify_api_error(_make_api_error(502, "Bad Gateway"))
        assert err.category == "server"

    def test_unknown_status(self):
        err = classify_api_error(_make_api_error(418, "I'm a teapot"))
        assert err.category == "unknown"

    def test_operation_preserved(self):
        err = classify_api_error(_make_api_error(404, "Not Found"), operation="list_pods")
        assert err.operation == "list_pods"


class TestClassifyException:
    def test_api_exception_delegates(self):
        e = _make_api_error(403, "Forbidden", "forbidden")
        err = classify_exception(e, "test_op")
        assert err.category == "forbidden"
        assert err.operation == "test_op"

    def test_connection_error(self):
        err = classify_exception(ConnectionError("refused"), "connect")
        assert err.category == "network"

    def test_timeout_error(self):
        err = classify_exception(TimeoutError("timed out"), "fetch")
        assert err.category == "network"

    def test_generic_error(self):
        err = classify_exception(ValueError("bad value"), "parse")
        assert err.category == "server"
        assert "ValueError" in str(err)


class TestToolError:
    def test_str_returns_message(self):
        err = ToolError(message="test error", category="server")
        assert str(err) == "test error"

    def test_to_dict(self):
        err = ToolError(message="test", category="permission", status_code=403, operation="list")
        d = err.to_dict()
        assert d["message"] == "test"
        assert d["category"] == "permission"
        assert d["status_code"] == 403

    def test_timestamp_auto_set(self):
        err = ToolError(message="test", category="server")
        assert err.timestamp  # non-empty
