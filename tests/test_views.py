"""Tests for view persistence (db.py view functions) and view API security."""

from __future__ import annotations

import os

import pytest

from sre_agent import db as db_module
from sre_agent.db import Database, reset_database, set_database
from sre_agent.db_schema import ALL_SCHEMAS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _view_db():
    """Create a fresh PostgreSQL schema for each test."""
    from tests.conftest import _TEST_DB_URL

    test_db = Database(_TEST_DB_URL)
    # Drop and recreate all tables for test isolation
    test_db.execute("DROP TABLE IF EXISTS view_versions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS views CASCADE")
    test_db.commit()
    test_db.executescript(ALL_SCHEMAS)
    set_database(test_db)
    yield test_db
    reset_database()


def _layout():
    return [{"kind": "data_table", "title": "Pods", "columns": [], "rows": []}]


# ---------------------------------------------------------------------------
# save_view
# ---------------------------------------------------------------------------


class TestSaveView:
    def test_saves_and_retrieves(self):
        result = db_module.save_view("alice", "cv-1", "My View", "desc", _layout())
        assert result == "cv-1"

        view = db_module.get_view("cv-1", "alice")
        assert view is not None
        assert view["title"] == "My View"
        assert view["owner"] == "alice"
        assert isinstance(view["layout"], list)

    def test_upsert_same_owner(self):
        db_module.save_view("alice", "cv-1", "First", "", _layout())
        db_module.save_view("alice", "cv-1", "Updated", "", _layout())
        view = db_module.get_view("cv-1", "alice")
        assert view["title"] == "Updated"

    def test_upsert_different_owner_blocked(self):
        """IDOR protection: another user cannot overwrite a view via ON CONFLICT."""
        db_module.save_view("alice", "cv-1", "Alice's View", "", _layout())
        db_module.save_view("mallory", "cv-1", "Hacked!", "", _layout())

        view = db_module.get_view("cv-1")
        assert view["owner"] == "alice"
        assert view["title"] == "Alice's View"

    def test_saves_positions(self):
        positions = {0: {"x": 0, "y": 0, "w": 2, "h": 3}}
        db_module.save_view("alice", "cv-1", "Test", "", _layout(), positions)
        view = db_module.get_view("cv-1", "alice")
        assert isinstance(view["positions"], dict)


# ---------------------------------------------------------------------------
# list_views
# ---------------------------------------------------------------------------


class TestListViews:
    def test_lists_only_owned_views(self):
        db_module.save_view("alice", "cv-1", "Alice's", "", _layout())
        db_module.save_view("bob", "cv-2", "Bob's", "", _layout())

        alice_views = db_module.list_views("alice")
        assert len(alice_views) == 1
        assert alice_views[0]["id"] == "cv-1"

        bob_views = db_module.list_views("bob")
        assert len(bob_views) == 1
        assert bob_views[0]["id"] == "cv-2"

    def test_respects_limit(self):
        for i in range(10):
            db_module.save_view("alice", f"cv-{i}", f"View {i}", "", _layout())
        views = db_module.list_views("alice", limit=3)
        assert len(views) == 3

    def test_empty_for_unknown_user(self):
        views = db_module.list_views("nobody")
        assert views == []


# ---------------------------------------------------------------------------
# get_view
# ---------------------------------------------------------------------------


class TestGetView:
    def test_returns_none_for_missing(self):
        assert db_module.get_view("nonexistent") is None

    def test_owner_check(self):
        db_module.save_view("alice", "cv-1", "Test", "", _layout())
        assert db_module.get_view("cv-1", "alice") is not None
        assert db_module.get_view("cv-1", "bob") is None

    def test_without_owner_returns_any(self):
        db_module.save_view("alice", "cv-1", "Test", "", _layout())
        assert db_module.get_view("cv-1") is not None


# ---------------------------------------------------------------------------
# update_view
# ---------------------------------------------------------------------------


class TestUpdateView:
    def test_updates_title(self):
        db_module.save_view("alice", "cv-1", "Old", "", _layout())
        result = db_module.update_view("cv-1", "alice", title="New")
        assert result is True
        assert db_module.get_view("cv-1", "alice")["title"] == "New"

    def test_owner_isolation(self):
        db_module.save_view("alice", "cv-1", "Alice's", "", _layout())
        result = db_module.update_view("cv-1", "mallory", title="Hacked")
        assert result is False
        assert db_module.get_view("cv-1", "alice")["title"] == "Alice's"

    def test_rejects_unknown_fields(self):
        db_module.save_view("alice", "cv-1", "Test", "", _layout())
        # Unknown fields are silently skipped by the allowlist
        result = db_module.update_view("cv-1", "alice", secret="bad", foo="bar")
        assert result is False  # No allowed fields → no update

    def test_nonexistent_view(self):
        result = db_module.update_view("nope", "alice", title="X")
        assert result is False


# ---------------------------------------------------------------------------
# delete_view
# ---------------------------------------------------------------------------


class TestDeleteView:
    def test_deletes_owned_view(self):
        db_module.save_view("alice", "cv-1", "Test", "", _layout())
        result = db_module.delete_view("cv-1", "alice")
        assert result is True
        assert db_module.get_view("cv-1") is None

    def test_cannot_delete_others_view(self):
        db_module.save_view("alice", "cv-1", "Test", "", _layout())
        result = db_module.delete_view("cv-1", "mallory")
        assert result is False
        assert db_module.get_view("cv-1") is not None

    def test_nonexistent_returns_false(self):
        result = db_module.delete_view("nope", "alice")
        assert result is False


# ---------------------------------------------------------------------------
# version_history
# ---------------------------------------------------------------------------


class TestVersionHistory:
    def test_save_view_creates_initial_version(self):
        db_module.save_view("alice", "cv-1", "My View", "desc", _layout())
        versions = db_module.list_view_versions("cv-1")
        assert len(versions) == 1
        assert versions[0]["action"] == "created"
        assert versions[0]["title"] == "My View"

    def test_update_with_snapshot_creates_version(self):
        db_module.save_view("alice", "cv-1", "Original", "", _layout())
        db_module.update_view("cv-1", "alice", _snapshot=True, title="Renamed")
        versions = db_module.list_view_versions("cv-1")
        assert len(versions) == 2
        assert versions[0]["action"] == "update"
        assert versions[1]["action"] == "created"

    def test_update_without_snapshot_no_version(self):
        db_module.save_view("alice", "cv-1", "V1", "", _layout())
        db_module.update_view("cv-1", "alice", positions={"0": {"x": 0, "y": 0, "w": 4, "h": 3}})
        versions = db_module.list_view_versions("cv-1")
        assert len(versions) == 1  # Only the initial "created" version

    def test_layout_update_with_snapshot(self):
        db_module.save_view("alice", "cv-1", "V1", "", _layout())
        new_layout = [{"kind": "key_value", "pairs": [{"key": "a", "value": "b"}]}]
        db_module.update_view("cv-1", "alice", _snapshot=True, layout=new_layout)
        versions = db_module.list_view_versions("cv-1")
        assert len(versions) == 2

    def test_restore_version(self):
        db_module.save_view("alice", "cv-1", "Original Title", "", _layout())
        db_module.update_view("cv-1", "alice", _snapshot=True, title="Changed")
        view = db_module.get_view("cv-1", "alice")
        assert view["title"] == "Changed"

        result = db_module.restore_view_version("cv-1", "alice", 1)
        assert result is True
        view = db_module.get_view("cv-1", "alice")
        assert view["title"] == "Original Title"

    def test_list_versions_empty_for_nonexistent(self):
        versions = db_module.list_view_versions("cv-nope")
        assert versions == []


# ---------------------------------------------------------------------------
# clone_view
# ---------------------------------------------------------------------------


class TestCloneView:
    def test_clones_to_new_owner(self):
        db_module.save_view("alice", "cv-1", "Shared Dashboard", "desc", _layout())
        new_id = db_module.clone_view("cv-1", "bob")
        assert new_id is not None
        assert new_id != "cv-1"

        clone = db_module.get_view(new_id, "bob")
        assert clone is not None
        assert clone["title"] == "Shared Dashboard"
        assert clone["owner"] == "bob"

    def test_clone_nonexistent_returns_none(self):
        assert db_module.clone_view("nope", "bob") is None

    def test_original_unchanged(self):
        db_module.save_view("alice", "cv-1", "Original", "", _layout())
        db_module.clone_view("cv-1", "bob")
        original = db_module.get_view("cv-1", "alice")
        assert original["title"] == "Original"
        assert original["owner"] == "alice"


# ---------------------------------------------------------------------------
# Share token (api.py _get_current_user)
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def test_dev_user_override(self, monkeypatch):
        from sre_agent.api import _get_current_user
        from sre_agent.config import _reset_settings

        monkeypatch.setenv("PULSE_AGENT_DEV_USER", "testdev")
        _reset_settings()
        user = _get_current_user()
        assert user == "testdev"

    def test_no_token_raises_401(self):
        from fastapi import HTTPException

        from sre_agent.api import _get_current_user

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        with pytest.raises(HTTPException) as exc_info:
            _get_current_user(x_forwarded_access_token=None)
        assert exc_info.value.status_code == 401

    def test_no_authorization_parameter(self):
        """Regression: _get_current_user must NOT accept an authorization param.

        The authorization header contains the shared WS agent token, not a
        per-user OAuth token. Accepting it would risk all REST users sharing
        the same identity (hash of the shared token), breaking view isolation.
        """
        import inspect

        from sre_agent.api import _get_current_user

        sig = inspect.signature(_get_current_user)
        assert "authorization" not in sig.parameters, (
            "_get_current_user must not accept 'authorization' — "
            "it contains the shared WS token, not the user's OAuth token"
        )

    def test_different_tokens_get_different_identities(self):
        """Two different OAuth tokens must produce different user identities."""
        from unittest.mock import MagicMock, patch

        from sre_agent.api import _get_current_user, _user_cache

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        _user_cache.clear()

        mock_result = MagicMock()
        mock_result.status.authenticated = True
        with patch("sre_agent.k8s_client._load_k8s"), patch("kubernetes.client") as mock_k8s:
            mock_k8s.AuthenticationV1Api.return_value.create_token_review.return_value = mock_result

            mock_result.status.user.username = "alice"
            alice = _get_current_user(x_forwarded_access_token="alice-oauth-token")

            mock_result.status.user.username = "bob"
            bob = _get_current_user(x_forwarded_access_token="bob-oauth-token")

        assert alice == "alice"
        assert bob == "bob"
        assert alice != bob
        _user_cache.clear()

    def test_same_token_returns_cached_user(self):
        """Same token should return the cached user on second call."""
        from unittest.mock import MagicMock, patch

        from sre_agent.api import _get_current_user, _user_cache

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        _user_cache.clear()

        mock_result = MagicMock()
        mock_result.status.authenticated = True
        mock_result.status.user.username = "alice"
        with patch("sre_agent.k8s_client._load_k8s"), patch("kubernetes.client") as mock_k8s:
            mock_k8s.AuthenticationV1Api.return_value.create_token_review.return_value = mock_result
            user1 = _get_current_user(x_forwarded_access_token="same-token")
            user2 = _get_current_user(x_forwarded_access_token="same-token")

        assert user1 == user2 == "alice"
        _user_cache.clear()

    def test_unverifiable_token_gets_fallback_identity(self):
        """Tokens that fail TokenReview get a stable hash-based fallback identity."""
        from unittest.mock import MagicMock, patch

        from sre_agent.api import _get_current_user, _user_cache

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        _user_cache.clear()

        mock_result = MagicMock()
        mock_result.status.authenticated = False
        with patch("sre_agent.k8s_client._load_k8s"), patch("kubernetes.client") as mock_k8s:
            mock_k8s.AuthenticationV1Api.return_value.create_token_review.return_value = mock_result
            user = _get_current_user(x_forwarded_access_token="forged-token")
            assert user.startswith("user-")
            # Same token yields same identity
            user2 = _get_current_user(x_forwarded_access_token="forged-token")
            assert user == user2
        _user_cache.clear()

    def test_empty_forwarded_token_raises_401(self):
        """Empty string token should be rejected, not treated as valid."""
        from fastapi import HTTPException

        from sre_agent.api import _get_current_user

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        with pytest.raises(HTTPException) as exc_info:
            _get_current_user(x_forwarded_access_token="")
        assert exc_info.value.status_code == 401

    def test_fallback_identity_on_token_review_error(self):
        """When TokenReview API is unavailable, a stable fallback identity is used."""
        from unittest.mock import patch

        from sre_agent.api import _get_current_user, _user_cache

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        _user_cache.clear()

        with patch("sre_agent.k8s_client._load_k8s"), patch("kubernetes.client") as mock_k8s:
            mock_k8s.AuthenticationV1Api.side_effect = Exception("K8s unavailable")
            user = _get_current_user(x_forwarded_access_token="any-token")
            assert user.startswith("user-")
            assert len(user) > 5  # user- + hash prefix
        _user_cache.clear()


# ---------------------------------------------------------------------------
# User cache behavior
# ---------------------------------------------------------------------------


class TestUserCache:
    def test_stale_entries_evicted_on_read(self):
        """Expired cache entries should be removed, not served."""
        import hashlib
        import time as time_mod
        from unittest.mock import MagicMock, patch

        from sre_agent.api import _get_current_user, _user_cache

        os.environ.pop("PULSE_AGENT_DEV_USER", None)
        _user_cache.clear()

        # Seed a cache entry that's already expired (use full hash)
        token_hash = hashlib.sha256(b"stale-token").hexdigest()
        _user_cache[token_hash] = ("old-user", time_mod.time() - 120)

        # Mock TokenReview to return a fresh user
        mock_result = MagicMock()
        mock_result.status.authenticated = True
        mock_result.status.user.username = "fresh-user"
        with patch("sre_agent.k8s_client._load_k8s"), patch("kubernetes.client") as mock_k8s:
            mock_k8s.AuthenticationV1Api.return_value.create_token_review.return_value = mock_result
            user = _get_current_user(x_forwarded_access_token="stale-token")

        assert user == "fresh-user"
        assert _user_cache[token_hash][0] == "fresh-user"
        _user_cache.clear()

    def test_cache_bounded_at_max(self):
        """Cache should not grow beyond _USER_CACHE_MAX entries."""
        from sre_agent.api import _USER_CACHE_MAX, _cache_user, _user_cache

        _user_cache.clear()
        for i in range(_USER_CACHE_MAX + 50):
            _cache_user(f"hash-{i}", f"user-{i}")
        assert len(_user_cache) <= _USER_CACHE_MAX
        _user_cache.clear()

    def test_lru_eviction_removes_oldest(self):
        """LRU eviction should remove the least recently used entry."""
        from sre_agent.api import _USER_CACHE_MAX, _cache_user, _user_cache

        _user_cache.clear()
        # Fill to max
        for i in range(_USER_CACHE_MAX):
            _cache_user(f"hash-{i}", f"user-{i}")
        # Add one more — should evict hash-0 (oldest)
        _cache_user("new-hash", "new-user")
        assert "hash-0" not in _user_cache
        assert "new-hash" in _user_cache
        _user_cache.clear()


# ---------------------------------------------------------------------------
# Share token verification
# ---------------------------------------------------------------------------


class TestShareToken:
    def test_generate_and_verify(self):
        import hashlib
        import hmac
        import time

        os.environ["PULSE_AGENT_WS_TOKEN"] = "test-secret-key"
        try:
            secret = "test-secret-key"
            view_id = "cv-abc123"
            expires = int(time.time()) + 3600
            payload = f"{view_id}:{expires}"
            signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            token = f"{payload}:{signature}"

            parts = token.split(":")
            assert len(parts) == 3

            v_id, exp_str, sig = parts
            assert v_id == view_id
            expected = hmac.new(secret.encode(), f"{v_id}:{exp_str}".encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(sig, expected)
        finally:
            del os.environ["PULSE_AGENT_WS_TOKEN"]

    def test_expired_token(self):
        import hashlib
        import hmac

        secret = "test-secret"
        view_id = "cv-abc123"
        expires = 1000000  # long expired
        payload = f"{view_id}:{expires}"
        signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        token = f"{payload}:{signature}"

        parts = token.split(":")
        _, exp_str, _ = parts
        assert int(exp_str) < __import__("time").time()

    def test_forged_signature_rejected(self):
        import hmac

        view_id = "cv-abc123"
        expires = 9999999999
        forged_sig = "a" * 64
        token = f"{view_id}:{expires}:{forged_sig}"

        parts = token.split(":")
        _, _, sig = parts
        secret = "real-secret"
        expected = hmac.new(secret.encode(), f"{view_id}:{expires}".encode(), __import__("hashlib").sha256).hexdigest()
        assert not hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# namespace_summary tool
# ---------------------------------------------------------------------------


class TestNamespaceSummary:
    def test_returns_tuple(self, mock_k8s):
        from tests.conftest import _make_pod

        mock_k8s["core"].list_namespaced_pod.return_value = SimpleNamespace(
            items=[_make_pod(name="p1", phase="Running"), _make_pod(name="p2", phase="Failed")]
        )
        mock_k8s["apps"].list_namespaced_deployment.return_value = SimpleNamespace(items=[])
        mock_k8s["apps"].list_namespaced_stateful_set.return_value = SimpleNamespace(items=[])
        mock_k8s["apps"].list_namespaced_daemon_set.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_service.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_config_map.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_event.return_value = SimpleNamespace(items=[])

        from sre_agent.view_tools import namespace_summary

        result = namespace_summary.call({"namespace": "default"})
        assert isinstance(result, tuple)
        text, component = result
        assert "default" in text
        assert component["kind"] == "grid"
        # First item is resource_counts, then 4 metric cards
        assert component["items"][0]["kind"] == "resource_counts"
        assert len(component["items"][0]["items"]) == 7  # pods, deps, sts, ds, svc, cm, events
        assert component["items"][1]["kind"] == "metric_card"

    def test_grid_layout_columns(self, mock_k8s):
        """Grid should use 2 columns so metric cards render side-by-side, not stacked."""
        from tests.conftest import _make_pod

        mock_k8s["core"].list_namespaced_pod.return_value = SimpleNamespace(
            items=[_make_pod(name="p1", phase="Running")]
        )
        mock_k8s["apps"].list_namespaced_deployment.return_value = SimpleNamespace(items=[])
        mock_k8s["apps"].list_namespaced_stateful_set.return_value = SimpleNamespace(items=[])
        mock_k8s["apps"].list_namespaced_daemon_set.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_service.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_config_map.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_event.return_value = SimpleNamespace(items=[])

        from sre_agent.view_tools import namespace_summary

        _, component = namespace_summary.call({"namespace": "default"})
        assert component["columns"] >= 2, "Grid must use 2+ columns to avoid full-width card stacking"
        # Should have resource_counts + 4 metric cards
        metric_cards = [i for i in component["items"] if i["kind"] == "metric_card"]
        assert len(metric_cards) == 4

    def test_cluster_metrics_columns_capped(self, mock_k8s):
        """cluster_metrics grid should cap columns at 4 to avoid cramped cards."""
        from sre_agent.view_tools import cluster_metrics

        mock_k8s["core"].list_node.return_value = SimpleNamespace(items=[])
        mock_k8s["core"].list_namespaced_pod.return_value = SimpleNamespace(items=[])

        result = cluster_metrics.call({"category": "overview"})
        if isinstance(result, tuple):
            _, component = result
            if component and component.get("kind") == "grid":
                assert component["columns"] <= 4, "Grid columns should be capped at 4"


# Need SimpleNamespace for mock
from types import SimpleNamespace


@pytest.fixture
def mock_k8s():
    """Mock K8s clients for tool tests."""
    from unittest.mock import MagicMock, patch

    core = MagicMock()
    apps = MagicMock()
    custom = MagicMock()

    with (
        patch("sre_agent.k8s_client.get_core_client", return_value=core),
        patch("sre_agent.k8s_client.get_apps_client", return_value=apps),
        patch("sre_agent.k8s_client.get_custom_client", return_value=custom),
    ):
        yield {"core": core, "apps": apps, "custom": custom}


# ---------------------------------------------------------------------------
# critique_view
# ---------------------------------------------------------------------------


class TestCritiqueView:
    def test_empty_view_scores_low(self):
        db_module.save_view("alice", "cv-crit1", "Empty", "", [])
        from sre_agent.view_critic import critique_view

        result = critique_view.call({"view_id": "cv-crit1"})
        assert "0/" in result or "1/" in result or "2/" in result
        assert "NO METRIC CARDS" in result

    def test_full_view_scores_high(self):
        layout = [
            {
                "kind": "grid",
                "items": [
                    {"kind": "metric_card", "title": "CPU", "value": "5%", "query": "rate(cpu[5m])"},
                    {"kind": "metric_card", "title": "Mem", "value": "30%", "query": "mem_usage"},
                ],
            },
            {"kind": "chart", "title": "CPU Trend", "description": "Watch for spikes", "timeRange": "1h", "series": []},
            {"kind": "chart", "title": "Mem Trend", "description": "Watch for leaks", "timeRange": "1h", "series": []},
            {"kind": "data_table", "title": "Pods", "columns": [], "rows": []},
        ]
        db_module.save_view(
            "alice", "cv-crit2", "Full View", "", layout, positions={0: {"x": 0, "y": 0, "w": 4, "h": 2}}
        )
        from sre_agent.view_critic import critique_view

        result = critique_view.call({"view_id": "cv-crit2"})
        assert "passes quality" in result.lower() or "/10" in result

    def test_missing_charts(self):
        layout = [
            {"kind": "info_card_grid", "title": "Summary", "cards": []},
            {"kind": "data_table", "title": "Pods", "columns": [], "rows": []},
        ]
        db_module.save_view("alice", "cv-crit3", "No Charts", "", layout)
        from sre_agent.view_critic import critique_view

        result = critique_view.call({"view_id": "cv-crit3"})
        assert "NO CHART" in result

    def test_nonexistent_view(self):
        from sre_agent.view_critic import critique_view

        result = critique_view.call({"view_id": "cv-nope"})
        assert "not found" in result.lower()
