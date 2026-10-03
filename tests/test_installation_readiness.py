"""Controlled readiness checks: no database, cluster or provider calls."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sre_agent import readiness as subject


class ProviderTests(unittest.TestCase):
    def test_missing_and_partial_fail_closed(self):
        for env in ({}, {"ANTHROPIC_VERTEX_PROJECT_ID": "secretproject"}):
            checks = subject.provider_checks(env)
            self.assertEqual(checks[0]["status"], "unhealthy")
            self.assertEqual(checks[1]["status"], "unknown")
            self.assertNotIn("secretproject", str(checks))

    def test_configured_is_not_connectivity(self):
        for env in (
            {"ANTHROPIC_API_KEY": "sensitive"},
            {"ANTHROPIC_AUTH_TOKEN": "sensitive"},
            {"ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "r"},
        ):
            checks = subject.provider_checks(env)
            self.assertEqual([c["status"] for c in checks], ["healthy", "unknown"])
            self.assertNotIn("sensitive", str(checks))

    def test_cluster_probe_uses_installation_identity_and_bound(self):
        api = MagicMock()
        with (
            patch("kubernetes.client.ApiClient") as constructor,
            patch("sre_agent.k8s_client._load_k8s"),
            patch("kubernetes.client.CoreV1Api", return_value=api),
        ):
            self.assertTrue(subject.kubernetes_probe("pods"))
            constructor.assert_called_once_with()
            api.list_pod_for_all_namespaces.assert_called_once_with(limit=1, _request_timeout=(3, 5))

    def test_http_auth_and_structured_report_without_lifespan(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from sre_agent.api import readiness_rest as route

        app = FastAPI()
        app.include_router(route.router)
        route._cached, route._expires = None, 0
        route._lock = asyncio.Lock()
        settings = SimpleNamespace(server=SimpleNamespace(ws_token="test-secret"))
        with (
            patch("sre_agent.api.auth.get_settings", return_value=settings),
            patch.object(
                route, "collect_readiness", new_callable=AsyncMock, return_value={"status": "unknown", "checks": []}
            ) as collect,
            TestClient(app) as http,
        ):
            self.assertEqual(http.get("/readiness").status_code, 401)
            self.assertEqual(collect.await_count, 0)
            response = http.get("/readiness", headers={"Authorization": "Bearer test-secret"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "unknown")
        route._cached, route._expires = None, 0

    def test_logs_require_explicit_permission_no_log_contents(self):
        api = MagicMock()
        with (
            patch("kubernetes.client.ApiClient"),
            patch("sre_agent.k8s_client._load_k8s"),
            patch("kubernetes.client.AuthorizationV1Api", return_value=api),
        ):
            for value in (True, False):
                api.create_self_subject_access_review.return_value = SimpleNamespace(
                    status=SimpleNamespace(allowed=value, evaluation_error=None)
                )
                self.assertIs(subject.kubernetes_probe("logs"), value)
            api.create_self_subject_access_review.return_value.status.evaluation_error = "cannot determine"
            with self.assertRaises(RuntimeError):
                subject.kubernetes_probe("logs")


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_denial_unknown_and_redaction(self):
        for code, expected in ((403, "unhealthy"), (500, "unknown"), (None, "unknown")):
            error = RuntimeError("token=private")
            error.status = code

            def fail():
                raise error

            result = await subject.observed("x", fail, "passed", "fix", "test")
            self.assertEqual(result["status"], expected)
            self.assertNotIn("private", str(result))

    async def test_truthy_probe_is_not_success(self):
        result = await subject.observed("x", lambda: "yes", "passed", "fix", "test")
        self.assertEqual(result["status"], "unhealthy")

    async def test_timeout_is_unknown(self):
        async def timed_out(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        with patch.object(subject.asyncio, "wait_for", side_effect=timed_out):
            result = await subject.observed("x", lambda: True, "passed", "fix", "test")
        self.assertEqual(result["status"], "unknown")

    async def test_report_coverage_aggregation_and_no_network(self):
        with (
            patch.dict(subject.os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=True),
            patch.object(subject, "database_probe", return_value=True),
            patch.object(subject, "kubernetes_probe", return_value=True),
            patch.object(
                subject, "monitor_check", return_value=subject.check("monitor", "healthy", "Running", "", "test")
            ),
        ):
            report = await subject.collect_readiness()
            self.assertEqual(len(report["checks"]), 9)
            self.assertEqual(report["status"], "unknown")
            with patch.object(subject, "database_probe", return_value=False):
                self.assertEqual((await subject.collect_readiness())["status"], "degraded")

    async def test_route_authenticated_singleflight_cache(self):
        from sre_agent.api import readiness_rest as route
        from sre_agent.api.auth import verify_token

        self.assertIn(verify_token, [dep.call for dep in route.router.routes[0].dependant.dependencies])
        route._cached, route._expires = None, 0
        route._lock = asyncio.Lock()
        with patch.object(
            route, "collect_readiness", new_callable=AsyncMock, return_value={"status": "unknown"}
        ) as collect:
            reports = await asyncio.gather(route.installation_readiness(), route.installation_readiness())
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(collect.await_count, 1)
        route._cached, route._expires = None, 0


if __name__ == "__main__":
    unittest.main()
