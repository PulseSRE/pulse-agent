"""``GET /changes``: what changed around one resource, read as the caller.

Drives the real route against fake Kubernetes clients: owner-chain resolution
(Pod -> ReplicaSet -> Deployment, StatefulSet, Job -> CronJob), referenced
ConfigMaps and Secrets (names only — no value is fetched or returned),
ClusterVersion upgrades, the time window, input validation, a refused read
answering 403 rather than an empty list, optional sources reported as
unreadable, and ``available: false`` when the cluster cannot be reached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import urllib3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from sre_agent import k8s_client
from sre_agent.api import changes_rest
from sre_agent.config import _reset_settings
from sre_agent.errors import classify_api_error
from sre_agent.monitor import change_correlation as cc
from sre_agent.monitor import resource_changes as rc

TOKEN = "changes-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime.now(UTC).replace(microsecond=0)


def _ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def _owner(kind, name):
    return [SimpleNamespace(kind=kind, name=name)]


def _pod_spec(configmaps=(), secrets=(), env_secret=None, pull_secret=None):
    volumes = [SimpleNamespace(config_map=SimpleNamespace(name=c), secret=None, projected=None) for c in configmaps]
    volumes += [
        SimpleNamespace(config_map=None, secret=SimpleNamespace(secret_name=s), projected=None) for s in secrets
    ]
    env = []
    if env_secret:
        env.append(
            SimpleNamespace(
                name="PASSWORD",
                value_from=SimpleNamespace(
                    config_map_key_ref=None, secret_key_ref=SimpleNamespace(name=env_secret, key="pw")
                ),
            )
        )
    return SimpleNamespace(
        volumes=volumes,
        containers=[SimpleNamespace(env_from=[], env=env)],
        init_containers=[],
        image_pull_secrets=[SimpleNamespace(name=pull_secret)] if pull_secret else [],
    )


def _obj(name, owners=(), created=None, spec=None, managed=()):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            owner_references=list(owners),
            creation_timestamp=created or _ago(60 * 24 * 30),
            annotations={},
            managed_fields=list(managed),
        ),
        spec=spec,
    )


def _rs(name, deploy, rev, created, image):
    rs = _obj(
        name,
        _owner("Deployment", deploy),
        created,
        SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]))),
    )
    rs.metadata.annotations = {"deployment.kubernetes.io/revision": str(rev)}
    return rs


def _cm(name, edited, manager="kubectl-edit"):
    return _obj(name, managed=[SimpleNamespace(manager=manager, time=edited, subresource=None)])


def _forbidden(what="replicasets"):
    return ApiException(status=403, reason="Forbidden")


class Cluster:
    """Fake clients; each getter records the token it was called under."""

    def __init__(self):
        self.core, self.apps, self.batch, self.custom = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        self.tokens: list[str | None] = []
        self.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])
        self.apps.list_namespaced_controller_revision.return_value = SimpleNamespace(items=[])
        self.core.list_namespaced_config_map.return_value = SimpleNamespace(items=[])
        self.custom.list_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        self.custom.get_cluster_custom_object.return_value = {"status": {"history": []}}
        self.secret_listing: object = {"items": []}
        self.raw_calls: list[tuple[str, str]] = []

    def _getter(self, client):
        def get():
            self.tokens.append(k8s_client.get_current_user_token())
            return client

        return get

    def raw(self, path, operation="", *, query=None, accept=""):
        self.tokens.append(k8s_client.get_current_user_token())
        self.raw_calls.append((path, accept))
        if isinstance(self.secret_listing, ApiException):
            return classify_api_error(self.secret_listing, operation)
        return self.secret_listing

    def patches(self):
        return [
            patch.object(cc, "get_core_client", side_effect=self._getter(self.core)),
            patch.object(cc, "get_apps_client", side_effect=self._getter(self.apps)),
            patch.object(cc, "get_custom_client", side_effect=self._getter(self.custom)),
            patch.object(cc, "get_raw_json", side_effect=self.raw),
            patch.object(rc, "get_core_client", side_effect=self._getter(self.core)),
            patch.object(rc, "get_apps_client", side_effect=self._getter(self.apps)),
            patch.object(rc, "get_batch_client", side_effect=self._getter(self.batch)),
        ]


@pytest.fixture
def cluster():
    c = Cluster()
    ps = c.patches()
    for p in ps:
        p.start()
    yield c
    for p in reversed(ps):
        p.stop()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_WS_TOKEN", TOKEN)
    monkeypatch.setenv("PULSE_AGENT_TOKEN_FORWARDING", "true")
    _reset_settings()
    app = FastAPI()
    app.include_router(changes_rest.router)
    yield TestClient(app)
    _reset_settings()


def _get(client, headers=None, **params):
    return client.get("/changes", params=params, headers={**AUTH, **(headers or {})})


def _crashlooping_pod(cluster):
    """shop/api-7d9-x: ReplicaSet api-7d9 -> Deployment api; mounts api-config, secret api-db, pulls with regcred."""
    pod = _obj(
        "api-7d9-x",
        _owner("ReplicaSet", "api-7d9"),
        spec=_pod_spec(configmaps=["api-config"], secrets=["api-tls"], env_secret="api-db", pull_secret="regcred"),
    )
    cluster.core.read_namespaced_pod.return_value = pod
    cluster.apps.read_namespaced_replica_set.return_value = _obj("api-7d9", _owner("Deployment", "api"))
    cluster.apps.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[
            _rs("api-6c1", "api", 6, _ago(60 * 24 * 3), "quay.io/acme/api:v6"),
            _rs("api-7d9", "api", 7, _ago(30), "quay.io/acme/api:v7"),
            _rs("web-1a2", "web", 3, _ago(20), "quay.io/acme/web:v3"),  # same namespace, not this pod's
        ]
    )
    cluster.core.list_namespaced_config_map.return_value = SimpleNamespace(
        items=[_cm("api-config", _ago(45)), _cm("unrelated", _ago(10))]
    )
    cluster.custom.get_cluster_custom_object.return_value = {
        "status": {"history": [{"version": "4.17.3", "startedTime": _ago(90).isoformat()}]}
    }
    cluster.secret_listing = {
        "items": [
            {
                "metadata": {
                    "name": "api-db",
                    "creationTimestamp": _ago(60 * 24 * 30).isoformat(),
                    "managedFields": [{"manager": "kubectl-edit", "time": _ago(40).isoformat()}],
                }
            },
            {
                "metadata": {
                    "name": "someone-elses",
                    "managedFields": [{"manager": "kubectl-edit", "time": _ago(5).isoformat()}],
                }
            },
        ]
    }


# -- Owner chain and scope -------------------------------------------------------


def test_pod_sees_its_deployments_rollout_its_config_and_the_cluster_upgrade(client, cluster):
    _crashlooping_pod(cluster)
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"changes", "available"}
    assert body["available"] is True
    summaries = [c["summary"] for c in body["changes"]]
    assert summaries == [
        "Deployment api rolled out revision 7 (quay.io/acme/api:v6 → quay.io/acme/api:v7)",
        "Secret api-db edited by kubectl-edit",
        "ConfigMap api-config edited by kubectl-edit",
        "Cluster upgrade to 4.17.3 started",
    ]  # newest first; web's rollout, "unrelated" and "someone-elses" are out of scope
    rollout = body["changes"][0]
    assert rollout == {
        "at": rollout["at"],
        "kind": "Deployment",
        "namespace": "shop",
        "name": "api",
        "summary": summaries[0],
        "actor": None,
        "source": "rollout",
    }
    assert datetime.fromisoformat(rollout["at"].replace("Z", "+00:00")) == _ago(30)
    by_source = {c["source"]: c for c in body["changes"]}
    assert by_source["configmap"]["actor"] == "kubectl-edit"
    assert by_source["secret"]["name"] == "api-db" and by_source["secret"]["kind"] == "Secret"
    assert by_source["clusterversion"] == {
        "at": by_source["clusterversion"]["at"],
        "kind": "ClusterVersion",
        "namespace": "",
        "name": "version",
        "summary": "Cluster upgrade to 4.17.3 started",
        "actor": None,
        "source": "clusterversion",
    }


def test_secrets_are_read_as_metadata_only_and_no_value_is_returned(client, cluster):
    _crashlooping_pod(cluster)
    # Even if the API server sent data, the endpoint never looks at it.
    cluster.secret_listing["items"][0]["data"] = {"password": "aHVudGVyMg=="}
    cluster.secret_listing["items"][0]["stringData"] = {"password": "hunter2"}
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 200
    assert "hunter2" not in r.text and "aHVudGVyMg==" not in r.text and "password" not in r.text
    assert cluster.raw_calls == [("/api/v1/namespaces/shop/secrets", cc._METADATA_ONLY)]
    cluster.core.list_namespaced_secret.assert_not_called()
    cluster.core.read_namespaced_secret.assert_not_called()


def test_statefulset_pod_sees_its_controller_revisions(client, cluster):
    cluster.core.read_namespaced_pod.return_value = _obj("db-0", _owner("StatefulSet", "db"), spec=_pod_spec())
    rev = _obj("db-5b7", _owner("StatefulSet", "db"), created=_ago(15))
    rev.revision = 4
    other = _obj("cache-1", _owner("StatefulSet", "cache"), created=_ago(10))
    other.revision = 2
    cluster.apps.list_namespaced_controller_revision.return_value = SimpleNamespace(items=[rev, other])
    body = _get(client, namespace="shop", kind="Pod", name="db-0").json()
    assert [c["summary"] for c in body["changes"]] == ["StatefulSet db rolled out revision 4"]
    cluster.apps.list_namespaced_replica_set.assert_not_called()


def test_cronjob_pod_resolves_job_and_cronjob(client, cluster):
    cluster.core.read_namespaced_pod.return_value = _obj(
        "nightly-123-abc", _owner("Job", "nightly-123"), spec=_pod_spec(configmaps=["nightly-cfg"])
    )
    cluster.batch.read_namespaced_job.return_value = _obj("nightly-123", _owner("CronJob", "nightly"), created=_ago(25))
    cluster.batch.read_namespaced_cron_job.return_value = _obj(
        "nightly",
        managed=[
            SimpleNamespace(manager="kubectl-patch", time=_ago(50), subresource=None),
            # The controller's status writes are not edits.
            SimpleNamespace(manager="kube-controller-manager", time=_ago(1), subresource="status"),
        ],
    )
    body = _get(client, namespace="batch", kind="Pod", name="nightly-123-abc").json()
    assert [c["summary"] for c in body["changes"]] == [
        "Job nightly-123 created by CronJob nightly",
        "CronJob nightly edited by kubectl-patch",
    ]
    assert {c["source"] for c in body["changes"]} == {"workload"}
    cluster.batch.read_namespaced_cron_job.assert_called_once_with("nightly", "batch")


def test_deleted_replicaset_ends_the_chain_without_failing(client, cluster):
    cluster.core.read_namespaced_pod.return_value = _obj("api-7d9-x", _owner("ReplicaSet", "api-7d9"), spec=_pod_spec())
    cluster.apps.read_namespaced_replica_set.side_effect = ApiException(status=404, reason="Not Found")
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 200
    assert r.json() == {"changes": [], "available": True}


def test_deployment_kind_reads_its_template_references(client, cluster):
    _crashlooping_pod(cluster)
    cluster.apps.read_namespaced_deployment.return_value = _obj(
        "api",
        spec=SimpleNamespace(template=SimpleNamespace(spec=_pod_spec(configmaps=["api-config"]))),
    )
    body = _get(client, namespace="shop", kind="deployment", name="api").json()
    assert [c["source"] for c in body["changes"]] == ["rollout", "configmap", "clusterversion"]


def test_namespace_only_lists_every_workload_change_in_it(client, cluster):
    _crashlooping_pod(cluster)
    body = _get(client, namespace="shop").json()
    names = [(c["kind"], c["name"]) for c in body["changes"]]
    assert ("Deployment", "web") in names and ("ConfigMap", "unrelated") in names
    assert ("Deployment", "api") in names


def test_no_namespace_is_cluster_level_only(client, cluster):
    _crashlooping_pod(cluster)
    body = _get(client).json()
    assert [c["source"] for c in body["changes"]] == ["clusterversion"]


# -- Window ----------------------------------------------------------------------


def test_default_window_is_two_hours(client, cluster):
    _crashlooping_pod(cluster)  # upgrade 90m ago is inside, revision 6 (3 days) is not
    body = _get(client, namespace="shop", kind="Pod", name="api-7d9-x").json()
    assert "Cluster upgrade to 4.17.3 started" in [c["summary"] for c in body["changes"]]


def test_empty_window_returns_an_empty_list(client, cluster):
    _crashlooping_pod(cluster)
    r = _get(
        client,
        namespace="shop",
        kind="Pod",
        name="api-7d9-x",
        since=_ago(10).isoformat(),
        until=_ago(1).isoformat(),
    )
    assert r.status_code == 200
    assert r.json() == {"changes": [], "available": True}


def test_explicit_window_selects_older_changes(client, cluster):
    _crashlooping_pod(cluster)
    body = _get(
        client,
        namespace="shop",
        kind="Pod",
        name="api-7d9-x",
        since=(NOW - timedelta(days=4)).isoformat().replace("+00:00", "Z"),
        until=_ago(60 * 24).isoformat(),
    ).json()
    # Revision 6, three days ago; its predecessor was pruned, so no image diff.
    assert [c["summary"] for c in body["changes"]] == ["Deployment api rolled out revision 6"]


# -- Validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "params, needle",
    [
        ({"namespace": "Bad_NS"}, "not a valid Kubernetes namespace"),
        ({"namespace": "shop", "kind": "Pod", "name": "../etc"}, "not a valid Kubernetes name"),
        ({"namespace": "shop", "kind": "Service", "name": "api"}, "not tracked for kind"),
        ({"namespace": "shop", "name": "api"}, "name requires kind"),
        ({"namespace": "shop", "kind": "Pod"}, "requires name"),
        ({"kind": "Pod", "name": "api"}, "requires namespace"),
        ({"kind": "ClusterVersion", "name": "other"}, "only ClusterVersion"),
        ({"since": "yesterday"}, "ISO 8601"),
        ({"since": _ago(1).isoformat(), "until": _ago(5).isoformat()}, "since must be before until"),
        ({"since": (NOW - timedelta(days=8)).isoformat(), "until": NOW.isoformat()}, "at most 7 days"),
    ],
)
def test_invalid_input_is_a_400_with_the_reason(client, cluster, params, needle):
    r = _get(client, **params)
    assert r.status_code == 400
    assert needle in r.json()["detail"]
    cluster.core.read_namespaced_pod.assert_not_called()


def test_requires_the_shared_token(client, cluster):
    r = client.get("/changes", params={"namespace": "shop"})
    assert r.status_code == 401


# -- RBAC: reads as the caller, a refusal is a 403 --------------------------------


def test_reads_use_the_forwarded_user_token(client, cluster):
    _crashlooping_pod(cluster)
    r = _get(
        client, headers={"X-Forwarded-Access-Token": "user-oauth-token"}, namespace="shop", kind="Pod", name="api-7d9-x"
    )
    assert r.status_code == 200
    assert cluster.tokens and set(cluster.tokens) == {"user-oauth-token"}


def test_forbidden_resource_read_is_403_not_an_empty_list(client, cluster):
    cluster.core.read_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 403
    assert "Pod shop/api-7d9-x" in r.json()["detail"]


def test_forbidden_rollout_listing_is_403(client, cluster):
    _crashlooping_pod(cluster)
    cluster.apps.list_namespaced_replica_set.side_effect = _forbidden()
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 403
    assert "ReplicaSets in shop" in r.json()["detail"]


def test_forbidden_owner_read_is_403(client, cluster):
    cluster.core.read_namespaced_pod.return_value = _obj("api-7d9-x", _owner("ReplicaSet", "api-7d9"), spec=_pod_spec())
    cluster.apps.read_namespaced_replica_set.side_effect = _forbidden()
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 403
    assert "ReplicaSet shop/api-7d9" in r.json()["detail"]


def test_missing_resource_is_404_with_a_code(client, cluster):
    cluster.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")
    r = _get(client, namespace="shop", kind="Pod", name="gone")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "resource_not_found"
    assert "Pod shop/gone" in body["detail"]


def test_route_missing_404_has_no_code_so_the_two_are_distinguishable(client):
    # What an older agent without /changes answers.
    r = client.get("/no-such-route", headers=AUTH)
    assert r.status_code == 404
    assert "code" not in r.json()


def test_server_error_on_a_required_read_is_502(client, cluster):
    _crashlooping_pod(cluster)
    cluster.core.list_namespaced_config_map.side_effect = ApiException(status=500, reason="Internal")
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 502


def test_optional_sources_refused_are_named_not_silently_dropped(client, cluster):
    _crashlooping_pod(cluster)
    cluster.secret_listing = ApiException(status=403, reason="Forbidden")
    cluster.custom.get_cluster_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 200
    body = r.json()
    assert [c["source"] for c in body["changes"]] == ["rollout", "configmap"]
    assert len(body["unreadable"]) == 2
    assert body["unreadable"][0].startswith("Secret edits in shop")
    assert body["unreadable"][1].startswith("ClusterVersion")


def test_argo_and_clusterversion_not_installed_are_not_unreadable(client, cluster):
    _crashlooping_pod(cluster)
    cluster.custom.get_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
    body = _get(client, namespace="shop", kind="Pod", name="api-7d9-x").json()
    assert "unreadable" not in body


def test_argo_sync_into_the_namespace_is_reported_with_who_started_it(client, cluster):
    cluster.core.read_namespaced_pod.return_value = _obj("p", spec=_pod_spec())
    cluster.custom.list_cluster_custom_object.side_effect = None
    cluster.custom.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "shop-app"},
                "spec": {"destination": {"namespace": "shop"}},
                "status": {
                    "operationState": {
                        "finishedAt": _ago(12).isoformat(),
                        "syncResult": {"revision": "abcdef1234"},
                        "operation": {"initiatedBy": {"username": "alice"}},
                    }
                },
            },
            {
                "metadata": {"name": "other"},
                "spec": {"destination": {"namespace": "elsewhere"}},
                "status": {"operationState": {"finishedAt": _ago(5).isoformat()}},
            },
        ]
    }
    body = _get(client, namespace="shop", kind="Pod", name="p").json()
    assert body["changes"] == [
        {
            "at": body["changes"][0]["at"],
            "kind": "Application",
            "namespace": "shop",
            "name": "shop-app",
            "summary": "Argo CD synced shop-app to abcdef12",
            "actor": "alice",
            "source": "argo",
        }
    ]


# -- Source not running ------------------------------------------------------------


def test_unreachable_cluster_is_available_false(client, cluster):
    cluster.core.read_namespaced_pod.side_effect = urllib3.exceptions.MaxRetryError(None, "/", "refused")
    r = _get(client, namespace="shop", kind="Pod", name="api-7d9-x")
    assert r.status_code == 200
    assert r.json() == {"changes": [], "available": False}


def test_no_cluster_config_is_available_false(client):
    from kubernetes.config import ConfigException

    with patch.object(cc, "get_custom_client", side_effect=ConfigException("no config")):
        r = _get(client)
    assert r.json() == {"changes": [], "available": False}


def test_mounted_in_the_app():
    from sre_agent.api.app import app

    included = [getattr(r, "original_router", None) for r in app.routes]
    assert changes_rest.router in included
