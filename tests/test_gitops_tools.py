"""Tests for ArgoCD Shadow tools."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.gitops_tools import (
    get_argo_applications,
    get_argo_app_detail,
    detect_gitops_drift,
    get_argo_sync_diff,
)


@pytest.fixture
def mock_custom():
    with patch("sre_agent.gitops_tools.get_custom_client") as mock:
        yield mock.return_value


def _make_argo_app(name="my-app", namespace="openshift-gitops",
                   sync="Synced", health="Healthy", repo="https://github.com/org/repo.git",
                   path="apps/nginx", resources=None):
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "source": {"repoURL": repo, "path": path, "targetRevision": "HEAD"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
        },
        "status": {
            "sync": {"status": sync, "revision": "abc12345"},
            "health": {"status": health},
            "conditions": [],
            "resources": resources or [],
            "operationState": {"phase": "Succeeded", "message": "", "startedAt": "", "finishedAt": ""},
        },
    }


class TestGetArgoApplications:
    def test_lists_apps(self, mock_custom):
        mock_custom.list_cluster_custom_object.return_value = {
            "items": [_make_argo_app("frontend"), _make_argo_app("backend")]
        }
        result = get_argo_applications.call({"namespace": "ALL"})
        assert "frontend" in result
        assert "backend" in result
        assert "Synced" in result

    def test_drift_detected_badge(self, mock_custom):
        mock_custom.list_cluster_custom_object.return_value = {
            "items": [_make_argo_app("drifted", sync="OutOfSync")]
        }
        result = get_argo_applications.call({"namespace": "ALL"})
        assert "DRIFT DETECTED" in result

    def test_argocd_not_installed(self, mock_custom):
        mock_custom.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_argo_applications.call({"namespace": "ALL"})
        assert "not found" in result.lower() or "not available" in result.lower()

    def test_empty(self, mock_custom):
        mock_custom.list_cluster_custom_object.return_value = {"items": []}
        result = get_argo_applications.call({"namespace": "ALL"})
        assert "No ArgoCD" in result


class TestGetArgoAppDetail:
    def test_returns_detail(self, mock_custom):
        app = _make_argo_app("frontend", resources=[
            {"kind": "Deployment", "name": "frontend", "namespace": "default",
             "status": "Synced", "health": {"status": "Healthy"}},
        ])
        mock_custom.get_namespaced_custom_object.return_value = app
        result = get_argo_app_detail.call({"name": "frontend"})
        assert "frontend" in result
        assert "Synced" in result

    def test_not_found(self, mock_custom):
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_argo_app_detail.call({"name": "ghost"})
        assert "Error (404)" in result


class TestDetectGitopsDrift:
    def test_finds_drifted_apps(self, mock_custom):
        mock_custom.list_cluster_custom_object.return_value = {
            "items": [
                _make_argo_app("synced", sync="Synced"),
                _make_argo_app("drifted", sync="OutOfSync", resources=[
                    {"kind": "Deployment", "name": "web", "namespace": "default", "status": "OutOfSync"},
                ]),
            ]
        }
        result = detect_gitops_drift.call({"namespace": "ALL"})
        assert "DRIFT DETECTED" in result
        assert "drifted" in result
        assert "Deployment/web" in result

    def test_no_drift(self, mock_custom):
        mock_custom.list_cluster_custom_object.return_value = {
            "items": [_make_argo_app("synced", sync="Synced")]
        }
        result = detect_gitops_drift.call({"namespace": "ALL"})
        assert "in sync" in result.lower()

    def test_argocd_not_installed(self, mock_custom):
        mock_custom.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = detect_gitops_drift.call({"namespace": "ALL"})
        assert "not installed" in result.lower()


class TestGetArgoSyncDiff:
    def test_synced_app(self, mock_custom):
        mock_custom.get_namespaced_custom_object.return_value = _make_argo_app("app", sync="Synced")
        result = get_argo_sync_diff.call({"name": "app"})
        assert "in sync" in result.lower()

    def test_out_of_sync_with_resources(self, mock_custom):
        app = _make_argo_app("app", sync="OutOfSync", resources=[
            {"kind": "Deployment", "name": "web", "namespace": "default",
             "status": "OutOfSync", "diff": "- replicas: 3\n+ replicas: 5"},
        ])
        mock_custom.get_namespaced_custom_object.return_value = app
        result = get_argo_sync_diff.call({"name": "app"})
        assert "Deployment/web" in result
        assert "replicas" in result
