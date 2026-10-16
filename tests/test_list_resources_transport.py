"""list_resources goes through the shared client, so the user's RBAC applies.

It used to read the pod's service-account token from disk and call
kubernetes.default.svc directly: every listing ran with the agent's own
permissions, ignoring token forwarding, and it failed anywhere but in-cluster.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sre_agent.k8s_tools.generic import _fetch_table_rows

TABLE = {
    "kind": "Table",
    "columnDefinitions": [{"name": "Name", "type": "string", "priority": 0}],
    "rows": [{"cells": ["worker-1"], "object": {"metadata": {"name": "worker-1"}}}],
}


def _api_client():
    api = MagicMock()
    api.call_api.return_value = SimpleNamespace(data=json.dumps(TABLE).encode())
    return api


def test_uses_the_forwarded_user_token_and_table_accept_header():
    from sre_agent.k8s_client import user_token_context

    api = _api_client()
    # Token forwarding is on by default.
    with (
        patch("sre_agent.k8s_client._get_user_api_client", return_value=api) as user_client,
        patch("builtins.open", side_effect=AssertionError("must not read the service-account token")),
        user_token_context("alice-token"),
    ):
        result = _fetch_table_rows("nodes", label_selector="role=worker")

    user_client.assert_called_with("alice-token")
    args, kwargs = api.call_api.call_args
    assert args[:2] == ("/api/v1/nodes", "GET")
    assert kwargs["header_params"] == {"Accept": "application/json;as=Table;g=meta.k8s.io;v=v1"}
    assert ("labelSelector", "role=worker") in kwargs["query_params"]
    assert isinstance(result, dict) and result["rows"]


def test_works_out_of_cluster_with_the_configured_client():
    api = _api_client()
    core = SimpleNamespace(api_client=api)
    with (
        patch("sre_agent.k8s_client.get_core_client", return_value=core),
        patch("builtins.open", side_effect=FileNotFoundError),
    ):
        result = _fetch_table_rows("pods", namespace="production")

    assert api.call_api.call_args[0][0] == "/api/v1/namespaces/production/pods"
    assert isinstance(result, dict) and result["rows"]


def test_api_errors_are_reported_not_raised():
    from kubernetes.client.rest import ApiException

    api = MagicMock()
    api.call_api.side_effect = ApiException(status=403, reason="Forbidden")
    with patch("sre_agent.k8s_client.get_core_client", return_value=SimpleNamespace(api_client=api)):
        result = _fetch_table_rows("secrets", namespace="production")

    assert isinstance(result, str) and result.startswith("Error listing secrets")
