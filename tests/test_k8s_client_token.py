"""Tests for user token forwarding in k8s_client."""

from unittest.mock import MagicMock, patch

import pytest


class TestUserTokenVar:
    def test_sa_singleton_when_no_token(self):
        from sre_agent.k8s_client import _user_token_var, get_core_client

        assert _user_token_var.get() is None
        with patch("sre_agent.k8s_client._load_k8s"):
            c1 = get_core_client()
            c2 = get_core_client()
            assert c1 is c2

    def test_user_token_returns_new_client(self):
        from sre_agent.k8s_client import _user_api_client_var, _user_token_var, get_core_client

        reset_tok = _user_token_var.set("test-bearer-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                user_client = get_core_client()
                sa_token = _user_token_var.set(None)
                sa_client = get_core_client()
                _user_token_var.reset(sa_token)
                assert user_client is not sa_client
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_api_client_cached_within_scope(self):
        from sre_agent.k8s_client import (
            _user_api_client_var,
            _user_token_var,
            get_apps_client,
            get_core_client,
        )

        reset_tok = _user_token_var.set("test-bearer-token")
        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                core = get_core_client()
                apps = get_apps_client()
                assert core.api_client is apps.api_client
        finally:
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)

    def test_reset_returns_to_sa(self):
        from sre_agent.k8s_client import _user_api_client_var, _user_token_var, get_core_client

        with patch("sre_agent.k8s_client._load_k8s"):
            sa_before = get_core_client()
            reset_tok = _user_token_var.set("test-token")
            reset_cli = _user_api_client_var.set(None)
            _user_client = get_core_client()
            _user_token_var.reset(reset_tok)
            _user_api_client_var.reset(reset_cli)
            sa_after = get_core_client()
            assert sa_before is sa_after

    def test_bearer_token_configured(self):
        from sre_agent.k8s_client import _get_user_api_client, _user_api_client_var

        reset_cli = _user_api_client_var.set(None)
        try:
            with patch("sre_agent.k8s_client._load_k8s"):
                api_client = _get_user_api_client("my-secret-token")
                assert api_client.configuration.api_key["authorization"] == "Bearer my-secret-token"
        finally:
            _user_api_client_var.reset(reset_cli)


class TestUserTokenContext:
    def test_context_manager_sets_and_resets(self):
        from sre_agent.k8s_client import _user_token_var, user_token_context

        assert _user_token_var.get() is None
        with user_token_context("ctx-token"):
            assert _user_token_var.get() == "ctx-token"
        assert _user_token_var.get() is None

    def test_context_manager_noop_when_none(self):
        from sre_agent.k8s_client import _user_token_var, user_token_context

        assert _user_token_var.get() is None
        with user_token_context(None):
            assert _user_token_var.get() is None

    def test_context_manager_resets_on_exception(self):
        from sre_agent.k8s_client import _user_token_var, user_token_context

        with pytest.raises(ValueError):
            with user_token_context("err-token"):
                assert _user_token_var.get() == "err-token"
                raise ValueError("boom")
        assert _user_token_var.get() is None

    def test_token_forwarding_disabled(self):
        from sre_agent.k8s_client import _user_token_var, user_token_context

        with patch("sre_agent.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(token_forwarding=False)
            with user_token_context("should-be-ignored"):
                assert _user_token_var.get() is None
