"""Tests for k8s_client helpers."""

from unittest.mock import Mock


def test_safe_list_all_namespaces():
    from sre_agent.k8s_client import safe_list

    list_all = Mock(return_value=["all"])
    list_ns = Mock()
    result = safe_list(list_all, list_ns, "ALL")
    list_all.assert_called_once()
    list_ns.assert_not_called()
    assert result == ["all"]


def test_safe_list_specific_namespace():
    from sre_agent.k8s_client import safe_list

    list_all = Mock()
    list_ns = Mock(return_value=["ns"])
    result = safe_list(list_all, list_ns, "default")
    list_all.assert_not_called()
    list_ns.assert_called_once_with("default")
    assert result == ["ns"]


def test_safe_list_with_kwargs():
    from sre_agent.k8s_client import safe_list

    list_all = Mock(return_value=["filtered"])
    list_ns = Mock()
    result = safe_list(list_all, list_ns, "ALL", field_selector="status=Running")
    list_all.assert_called_once_with(field_selector="status=Running")
    list_ns.assert_not_called()
    assert result == ["filtered"]
