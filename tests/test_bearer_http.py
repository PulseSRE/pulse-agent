"""Bearer tokens do not follow a redirect off their origin (bearer_http)."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch

import pytest

from sre_agent import bearer_http
from sre_agent.bearer_http import InsecureRedirectError, open_with_token

TOKEN = "Bearer tok-for-test"


def _fake_transport(routes: dict[str, tuple[int, dict[str, str], bytes]], seen: list):
    """An https/http handler that serves canned responses and records each request's Authorization."""

    class FakeTransport(urllib.request.BaseHandler):
        def __init__(self, *args, **kwargs):
            pass

        def _open(self, req):
            seen.append((req.full_url, req.get_header("Authorization")))
            code, hdrs, body = routes[req.full_url]
            msg = Message()
            for k, v in hdrs.items():
                msg[k] = v
            resp = urllib.response.addinfourl(io.BytesIO(body), msg, req.full_url, code)
            resp.msg = "OK" if code < 300 else "Found"
            return resp

        def https_open(self, req):
            return self._open(req)

        def http_open(self, req):
            return self._open(req)

    return FakeTransport


def _open(url: str, routes, seen):
    transport = _fake_transport(routes, seen)
    with (
        patch.object(urllib.request, "HTTPSHandler", transport),
        patch.object(urllib.request, "HTTPHandler", transport),
    ):
        req = urllib.request.Request(url, headers={"Authorization": TOKEN})
        return open_with_token(req, timeout=5, context=ssl.create_default_context())


OK_BODY = json.dumps({"status": "success", "data": {"result": []}}).encode()


class TestRedirects:
    def test_same_origin_redirect_keeps_the_token_and_reads_the_response(self):
        seen: list = []
        routes = {
            "https://thanos:9091/api/v1/query": (302, {"Location": "/v2/query"}, b""),
            "https://thanos:9091/v2/query": (200, {}, OK_BODY),
        }
        resp = _open("https://thanos:9091/api/v1/query", routes, seen)
        assert json.loads(resp.read()) == {"status": "success", "data": {"result": []}}
        assert seen == [
            ("https://thanos:9091/api/v1/query", TOKEN),
            ("https://thanos:9091/v2/query", TOKEN),
        ]

    def test_default_port_is_the_same_origin(self):
        seen: list = []
        routes = {
            "https://thanos/a": (301, {"Location": "https://THANOS:443/b"}, b""),
            "https://THANOS:443/b": (200, {}, OK_BODY),
        }
        _open("https://thanos/a", routes, seen)
        assert seen[1][1] == TOKEN

    @pytest.mark.parametrize(
        "target",
        ["https://evil.example/steal", "https://thanos:9443/other", "https://thanos.evil.example:9091/x"],
    )
    def test_cross_origin_redirect_drops_the_token(self, target):
        seen: list = []
        routes = {
            "https://thanos:9091/api/v1/query": (302, {"Location": target}, b""),
            target: (200, {}, OK_BODY),
        }
        resp = _open("https://thanos:9091/api/v1/query", routes, seen)
        assert json.loads(resp.read())["status"] == "success"
        assert seen == [("https://thanos:9091/api/v1/query", TOKEN), (target, None)]

    def test_token_is_not_restored_when_a_later_redirect_comes_back(self):
        seen: list = []
        routes = {
            "https://thanos:9091/a": (302, {"Location": "https://other:9091/b"}, b""),
            "https://other:9091/b": (302, {"Location": "https://thanos:9091/c"}, b""),
            "https://thanos:9091/c": (200, {}, OK_BODY),
        }
        _open("https://thanos:9091/a", routes, seen)
        assert [h for _, h in seen] == [TOKEN, None, None]

    def test_https_to_http_is_refused(self):
        seen: list = []
        routes = {
            "https://thanos:9091/api/v1/query": (302, {"Location": "http://thanos:9091/api/v1/query"}, b""),
            "http://thanos:9091/api/v1/query": (200, {}, OK_BODY),
        }
        with pytest.raises(InsecureRedirectError, match="https to http"):
            _open("https://thanos:9091/api/v1/query", routes, seen)
        assert seen == [("https://thanos:9091/api/v1/query", TOKEN)]

    def test_insecure_redirect_is_a_urlerror(self):
        # Callers that already handle URLError (unreachable endpoint) handle this too.
        assert issubclass(InsecureRedirectError, urllib.error.URLError)

    def test_plain_http_redirect_never_carries_the_token(self):
        seen: list = []
        routes = {
            "http://mcp:8081/mcp": (307, {"Location": "/mcp/"}, b""),
            "http://mcp:8081/mcp/": (200, {}, b"{}"),
        }
        transport = _fake_transport(routes, seen)
        with patch.object(urllib.request, "HTTPHandler", transport):
            req = urllib.request.Request("http://mcp:8081/mcp", headers={"Authorization": TOKEN})
            open_with_token(req, timeout=5)
        assert [h for _, h in seen] == [TOKEN, None]


class TestPrometheusUsesIt:
    def _client(self):
        from sre_agent.prometheus import PrometheusClient

        return PrometheusClient()

    def test_prometheus_request_through_a_cross_host_redirect(self):
        from sre_agent.prometheus import PrometheusBackend, PrometheusClient

        seen: list = []
        routes = {
            "https://thanos:9091/api/v1/query?query=up": (302, {"Location": "https://elsewhere/q"}, b""),
            "https://elsewhere/q": (200, {}, OK_BODY),
        }
        transport = _fake_transport(routes, seen)
        with (
            patch.object(PrometheusClient, "_get_url", return_value="https://thanos:9091"),
            patch.object(PrometheusClient, "_build_ssl_context", return_value=ssl.create_default_context()),
            patch.object(PrometheusClient, "_get_token", return_value="sa-token"),
            patch.object(urllib.request, "HTTPSHandler", transport),
        ):
            data = self._client().request("api/v1/query", {"query": "up"}, 5, PrometheusBackend.LOCAL)
        assert data["status"] == "success"
        assert seen == [
            ("https://thanos:9091/api/v1/query?query=up", "Bearer sa-token"),
            ("https://elsewhere/q", None),
        ]

    def test_prometheus_request_does_not_use_urlopen(self):
        from sre_agent.prometheus import PrometheusBackend, PrometheusClient

        with (
            patch.object(PrometheusClient, "_get_url", return_value="https://thanos:9091"),
            patch.object(PrometheusClient, "_build_ssl_context", return_value=None),
            patch.object(PrometheusClient, "_get_token", return_value="sa-token"),
            patch("urllib.request.urlopen", side_effect=AssertionError("urlopen follows redirects with the token")),
            patch.object(bearer_http, "open_with_token") as opened,
        ):
            opened.return_value.read.return_value = OK_BODY
            self._client().request("api/v1/query", None, 5, PrometheusBackend.LOCAL)
        assert opened.call_args.args[0].get_header("Authorization") == "Bearer sa-token"


def test_mcp_post_uses_it():
    from unittest.mock import MagicMock

    from sre_agent.k8s_client import user_token_context
    from sre_agent.mcp_client import _mcp_post

    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.headers = {}
    resp.read.return_value = b'{"result": {}}'
    with (
        patch("urllib.request.urlopen", side_effect=AssertionError("urlopen follows redirects with the token")),
        patch.object(bearer_http, "open_with_token", return_value=resp) as opened,
        user_token_context("u-token"),
    ):
        _mcp_post("http://localhost:8081", {"jsonrpc": "2.0", "id": 1})
    assert opened.call_args.args[0].get_header("Authorization") == "Bearer u-token"


class TestTls:
    def test_the_given_ssl_context_is_the_one_https_uses(self):
        ctx = ssl.create_default_context()
        used: list = []

        def fake_open(self, req, data=None, timeout=None):
            used.extend(h._context for h in self.handlers if isinstance(h, urllib.request.HTTPSHandler))
            return io.BytesIO(OK_BODY)

        with patch.object(urllib.request.OpenerDirector, "open", fake_open):
            open_with_token(urllib.request.Request("https://thanos:9091/q"), timeout=5, context=ctx)
        assert used == [ctx]


class TestOrigins:
    def test_malformed_port_is_refused(self):
        seen: list = []
        routes = {"https://thanos:9091/a": (302, {"Location": "https://thanos:99999/b"}, b"")}
        with pytest.raises(InsecureRedirectError, match="invalid port"):
            _open("https://thanos:9091/a", routes, seen)
        assert len(seen) == 1

    def test_dropping_the_token_logs_origins_only(self, caplog):
        seen: list = []
        target = "https://elsewhere:8443/path?secret=q1"
        routes = {
            "https://thanos:9091/api?query=up": (302, {"Location": target}, b""),
            target: (200, {}, OK_BODY),
        }
        with caplog.at_level("WARNING", logger="pulse_agent.bearer_http"):
            _open("https://thanos:9091/api?query=up", routes, seen)
        text = caplog.text
        assert "https://thanos:9091" in text and "https://elsewhere:8443" in text
        for leaked in ("secret=q1", "/path", "query=up", "/api", "tok-for-test"):
            assert leaked not in text


def test_no_module_sends_authorization_through_urlopen():
    """Anything sending an Authorization header over urllib must use bearer_http.open_with_token.

    urllib.request.urlopen follows redirects with the header attached, to any host.
    """
    import ast
    import pathlib

    root = pathlib.Path(bearer_http.__file__).parent
    # Modules allowed to do both (each with a reason). Keep this empty unless unavoidable.
    allowlist: dict[str, str] = {}
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        uses_urlopen = any(
            (isinstance(n, ast.Attribute) and n.attr == "urlopen") or (isinstance(n, ast.Name) and n.id == "urlopen")
            for n in ast.walk(tree)
        )
        sets_auth = any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.lower() == "authorization"
            for n in ast.walk(tree)
        )
        if uses_urlopen and sets_auth and rel not in allowlist:
            offenders.append(rel)
    assert offenders == [], f"use bearer_http.open_with_token instead of urlopen in: {offenders}"
