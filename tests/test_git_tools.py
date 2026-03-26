"""Tests for Ghost in the Machine (Git PR) tools."""

from unittest.mock import MagicMock, patch
import os

import pytest
from kubernetes.client.rest import ApiException

from sre_agent.git_tools import (
    propose_git_change,
    get_argo_app_source,
    _validate_file_path,
    _get_allowed_repos,
)


class TestValidateFilePath:
    def test_valid_path(self):
        assert _validate_file_path("apps/nginx/deployment.yaml") is None

    def test_rejects_dotdot(self):
        result = _validate_file_path("../../../.github/workflows/hack.yml")
        assert result is not None
        assert "path traversal" in result.lower()

    def test_rejects_absolute(self):
        result = _validate_file_path("/etc/passwd")
        assert result is not None

    def test_rejects_hidden_dotdot(self):
        result = _validate_file_path("apps/../../../hack.yml")
        assert result is not None

    def test_allows_dots_in_names(self):
        assert _validate_file_path("apps/my.app/v1.2.3/deploy.yaml") is None


class TestGetAllowedRepos:
    def test_empty_env(self, monkeypatch):
        monkeypatch.delenv("PULSE_ALLOWED_REPOS", raising=False)
        assert _get_allowed_repos() == set()

    def test_single_repo(self, monkeypatch):
        monkeypatch.setenv("PULSE_ALLOWED_REPOS", "org/repo")
        assert _get_allowed_repos() == {"org/repo"}

    def test_multiple_repos(self, monkeypatch):
        monkeypatch.setenv("PULSE_ALLOWED_REPOS", "org/repo1, org/repo2, org/repo3")
        assert _get_allowed_repos() == {"org/repo1", "org/repo2", "org/repo3"}


class TestProposeGitChange:
    def test_no_github_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        result = propose_git_change.call({
            "repo": "org/repo", "file_path": "f.yaml",
            "new_content": "x", "commit_message": "m", "pr_title": "t",
        })
        assert "GITHUB_TOKEN" in result

    def test_invalid_repo_format(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        result = propose_git_change.call({
            "repo": "invalid", "file_path": "f.yaml",
            "new_content": "x", "commit_message": "m", "pr_title": "t",
        })
        assert "Invalid repo format" in result

    def test_repo_not_allowed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setenv("PULSE_ALLOWED_REPOS", "org/allowed-repo")
        result = propose_git_change.call({
            "repo": "org/forbidden-repo", "file_path": "f.yaml",
            "new_content": "x", "commit_message": "m", "pr_title": "t",
        })
        assert "not in the allowed list" in result

    def test_path_traversal_blocked(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.delenv("PULSE_ALLOWED_REPOS", raising=False)
        result = propose_git_change.call({
            "repo": "org/repo", "file_path": "../../../.github/workflows/evil.yml",
            "new_content": "x", "commit_message": "m", "pr_title": "t",
        })
        assert "path traversal" in result.lower()

    def test_rate_limit(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.delenv("PULSE_ALLOWED_REPOS", raising=False)
        import sre_agent.git_tools as gt
        # Set the thread-local PR count to the limit
        old_count = gt._get_pr_count()
        gt._pr_local.count = gt._MAX_PRS_PER_SESSION
        try:
            result = propose_git_change.call({
                "repo": "org/repo", "file_path": "f.yaml",
                "new_content": "x", "commit_message": "m", "pr_title": "t",
            })
            assert "rate limit" in result.lower()
        finally:
            gt._pr_local.count = old_count


class TestGetArgoAppSource:
    def test_returns_source(self):
        with patch("sre_agent.git_tools.get_custom_client") as mock:
            mock.return_value.get_namespaced_custom_object.return_value = {
                "spec": {
                    "source": {
                        "repoURL": "https://github.com/myorg/k8s-manifests.git",
                        "path": "apps/frontend",
                        "targetRevision": "main",
                    },
                    "destination": {"namespace": "production"},
                },
            }
            result = get_argo_app_source.call({"name": "frontend"})
            assert "myorg/k8s-manifests" in result
            assert "apps/frontend" in result

    def test_strips_git_suffix_correctly(self):
        """Verify .git suffix removal doesn't corrupt repo names like 'legit'."""
        with patch("sre_agent.git_tools.get_custom_client") as mock:
            mock.return_value.get_namespaced_custom_object.return_value = {
                "spec": {
                    "source": {
                        "repoURL": "https://github.com/org/legit.git",
                        "path": "apps",
                        "targetRevision": "HEAD",
                    },
                    "destination": {"namespace": "default"},
                },
            }
            result = get_argo_app_source.call({"name": "app"})
            assert "org/legit" in result
            # Verify the repo name is "legit", not truncated to "legi"
            assert '"github_repo": "org/legit"' in result

    def test_not_found(self):
        with patch("sre_agent.git_tools.get_custom_client") as mock:
            mock.return_value.get_namespaced_custom_object.side_effect = ApiException(
                status=404, reason="Not Found"
            )
            result = get_argo_app_source.call({"name": "ghost"})
            assert "Error (404)" in result
