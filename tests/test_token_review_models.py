"""TokenReview must construct real client models, and its fallback must not stick.

The kubernetes Python client removed the un-versioned model aliases
(``client.TokenReview`` is gone by 36.x). auth.py constructed them anyway,
the AttributeError was swallowed by the surrounding except as "TokenReview
API unavailable", and every caller silently became a token-hash pseudonym
that can never match PULSE_AGENT_ADMIN_USERS — admin endpoints 403'd for
everyone, on every release built against a current client.

The existing tests never caught it because they mocked the model classes
along with the API. These construct the models for real and mock only the
network call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.api import auth as auth_mod
from sre_agent.api.auth import _get_current_user


def _clear_cache():
    auth_mod._user_cache.clear()


def _no_dev_user(monkeypatch):
    monkeypatch.delenv("PULSE_AGENT_DEV_USER", raising=False)
    from sre_agent.config import _reset_settings

    _reset_settings()


def test_token_review_uses_models_that_exist(monkeypatch):
    """Real V1TokenReview construction; only the HTTP call is mocked."""
    _no_dev_user(monkeypatch)
    _clear_cache()

    captured = {}

    def fake_create(review):
        captured["review"] = review
        result = MagicMock()
        result.status.authenticated = True
        result.status.user.username = "kube:admin"
        return result

    api = MagicMock()
    api.create_token_review.side_effect = fake_create

    with (
        patch("sre_agent.k8s_client._load_k8s"),
        patch("kubernetes.client.AuthenticationV1Api", return_value=api),
    ):
        user = _get_current_user("sha256~real-token", None)

    assert user == "kube:admin", (
        "a valid token must resolve to its real user — if this fell through to "
        "user-<hash>, the model construction raised before the API was called"
    )
    from kubernetes.client import V1TokenReview

    assert isinstance(captured["review"], V1TokenReview)
    assert captured["review"].spec.token == "sha256~real-token"
    _clear_cache()


def test_fallback_pseudonym_is_not_cached(monkeypatch):
    """A TokenReview outage must not poison the identity cache.

    The pseudonym used to be cached, and the exception path then 'extended'
    it forever — the caller stayed a ghost after TokenReview recovered.
    """
    _no_dev_user(monkeypatch)
    _clear_cache()

    api = MagicMock()
    api.create_token_review.side_effect = RuntimeError("apiserver away")

    with (
        patch("sre_agent.k8s_client._load_k8s"),
        patch("kubernetes.client.AuthenticationV1Api", return_value=api),
    ):
        ghost = _get_current_user("sha256~token-a", None)
    assert ghost.startswith("user-")

    # TokenReview recovers: the same token must resolve to the real user,
    # not the remembered pseudonym.
    ok = MagicMock()
    ok.status.authenticated = True
    ok.status.user.username = "alice"
    api2 = MagicMock()
    api2.create_token_review.return_value = ok

    with (
        patch("sre_agent.k8s_client._load_k8s"),
        patch("kubernetes.client.AuthenticationV1Api", return_value=api2),
    ):
        user = _get_current_user("sha256~token-a", None)
    assert user == "alice"
    _clear_cache()
