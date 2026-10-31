"""get_cluster_updates and get_rollout_history: output shapes against mocked API objects."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sre_agent.k8s_tools import WRITE_TOOLS, get_cluster_updates, get_rollout_history
from sre_agent.k8s_tools.diagnostics import build_cluster_updates, parse_cvo_progress
from sre_agent.quality_engine import QualityResult, _validate_component, normalize_component_spec
from sre_agent.tool_categories import TOOL_CATEGORIES

RISK = {
    "name": "OVNInterconnectMigration",
    "message": "Clusters on OVN may lose egress during migration.",
    "url": "https://issues.redhat.com/browse/OCPBUGS-1",
    "matchingRules": [{"type": "PromQL", "promql": {"promql": "cluster_infrastructure_provider"}}],
}


def _cv(progressing: bool = False, upgradeable: bool = True) -> dict:
    conditions = [
        {"type": "Available", "status": "True"},
        {"type": "Failing", "status": "False"},
        {
            "type": "Progressing",
            "status": "True" if progressing else "False",
            "message": "Working towards 4.16.8: 512 of 863 done (59% complete), waiting on network"
            if progressing
            else "Cluster version is 4.16.3",
        },
        {"type": "RetrievedUpdates", "status": "True"},
    ]
    if not upgradeable:
        conditions.append(
            {
                "type": "Upgradeable",
                "status": "False",
                "reason": "AdminAckRequired",
                "message": "Kubernetes 1.30 removes APIs; admin-ack required",
            }
        )
    history = [
        {
            "state": "Completed",
            "version": "4.16.3",
            "image": "quay.io/ocp-release@sha256:aaa",
            "startedTime": "2026-09-01T10:00:00Z",
            "completionTime": "2026-09-01T11:20:00Z",
            "verified": True,
        },
        {
            "state": "Completed",
            "version": "4.15.20",
            "image": "quay.io/ocp-release@sha256:999",
            "startedTime": "2026-07-01T10:00:00Z",
            "completionTime": "2026-07-01T11:00:00Z",
            "verified": False,
        },
    ]
    if progressing:
        history.insert(
            0,
            {
                "state": "Partial",
                "version": "4.16.8",
                "image": "quay.io/ocp-release@sha256:bbb",
                "startedTime": "2026-10-30T09:00:00Z",
                "verified": True,
            },
        )
    return {
        "spec": {"channel": "stable-4.16", "clusterID": "abc"},
        "status": {
            "desired": {
                "version": "4.16.8" if progressing else "4.16.3",
                "image": "quay.io/ocp-release@sha256:bbb" if progressing else "quay.io/ocp-release@sha256:aaa",
                "channels": ["candidate-4.16", "fast-4.16", "stable-4.16", "stable-4.17"],
            },
            "history": history,
            "conditions": conditions,
            "availableUpdates": [
                {"version": "4.16.8", "image": "quay.io/ocp-release@sha256:bbb", "channels": ["stable-4.16"]},
                {"version": "4.17.2", "image": "quay.io/ocp-release@sha256:ddd"},
            ],
            "conditionalUpdates": [
                {
                    "release": {"version": "4.17.1", "image": "quay.io/ocp-release@sha256:ccc"},
                    "risks": [RISK],
                    "conditions": [
                        {
                            "type": "Recommended",
                            "status": "False",
                            "reason": "OVNInterconnectMigration",
                            "message": "Clusters on OVN may lose egress during migration.",
                        }
                    ],
                }
            ],
        },
    }


OPERATORS = [
    {
        "metadata": {"name": "dns"},
        "status": {"versions": [{"name": "operator", "version": "4.16.8"}], "conditions": []},
    },
    {
        "metadata": {"name": "network"},
        "status": {
            "versions": [{"name": "operator", "version": "4.16.3"}],
            "conditions": [{"type": "Degraded", "status": "True"}],
        },
    },
    {"metadata": {"name": "ingress"}, "status": {"versions": [{"name": "operator", "version": "4.16.8"}]}},
]

POOLS = [
    {
        "metadata": {"name": "master"},
        "spec": {},
        "status": {
            "machineCount": 3,
            "updatedMachineCount": 3,
            "conditions": [{"type": "Updating", "status": "False"}],
        },
    },
    {
        "metadata": {"name": "worker"},
        "spec": {"paused": True},
        "status": {
            "machineCount": 5,
            "updatedMachineCount": 2,
            "conditions": [{"type": "Updating", "status": "True"}],
        },
    },
]


def _wire(mock_k8s, cv, operators=OPERATORS, pools=POOLS):
    mock_k8s["custom"].get_cluster_custom_object.return_value = cv

    def _list(group, version, plural):
        if plural == "clusteroperators":
            if isinstance(operators, Exception):
                raise operators
            return {"items": operators}
        if plural == "machineconfigpools":
            if isinstance(pools, Exception):
                raise pools
            return {"items": pools}
        raise AssertionError(plural)

    mock_k8s["custom"].list_cluster_custom_object.side_effect = _list


def _errors(comp: dict) -> list[str]:
    result = QualityResult()
    _validate_component(normalize_component_spec(dict(comp)), result)
    return result.errors


class TestParseProgress:
    def test_parses_cvo_message(self):
        assert parse_cvo_progress("Working towards 4.16.8: 512 of 863 done (59% complete)") == {
            "done": 512,
            "total": 863,
            "percent": 59,
        }

    def test_computes_percent_when_missing(self):
        assert parse_cvo_progress("Working towards 4.16.8: 1 of 4 done") == {"done": 1, "total": 4, "percent": 25}

    def test_absent(self):
        assert parse_cvo_progress("Cluster version is 4.16.3") is None


class TestGetClusterUpdates:
    def test_idle_cluster_shape(self, mock_k8s):
        _wire(mock_k8s, _cv())
        text, comp = get_cluster_updates.call({})
        assert "Current: 4.16.3 (channel stable-4.16)" in text
        assert "OVNInterconnectMigration" in text
        assert comp["kind"] == "upgrade_graph"
        assert comp["current"] == {
            "version": "4.16.3",
            "image": "quay.io/ocp-release@sha256:aaa",
            "channel": "stable-4.16",
        }
        assert comp["channels"] == ["candidate-4.16", "fast-4.16", "stable-4.16", "stable-4.17"]
        states = {n["version"]: n["state"] for n in comp["nodes"]}
        assert states == {"4.16.8": "recommended", "4.17.2": "recommended", "4.17.1": "conditional"}
        cond = next(n for n in comp["nodes"] if n["version"] == "4.17.1")
        assert cond["image"] == "quay.io/ocp-release@sha256:ccc"
        assert cond["risks"] == [RISK]
        assert comp["edges"] == [
            {"from": "4.16.3", "to": "4.16.8"},
            {"from": "4.16.3", "to": "4.17.2"},
            {"from": "4.16.3", "to": "4.17.1"},
        ]
        assert comp["history"][0] == {
            "version": "4.16.3",
            "state": "Completed",
            "startedTime": "2026-09-01T10:00:00Z",
            "completionTime": "2026-09-01T11:20:00Z",
            "verified": True,
        }
        assert comp["progress"]["phase"] == "idle"
        assert comp["progress"]["percent"] is None
        assert comp["blockers"] == []
        # The emitted spec passes the same validation every other component does.
        assert _errors(comp) == []

    def test_conditional_recommended_condition_is_reported(self):
        data = build_cluster_updates(_cv(), [], [])
        cu = data["conditionalUpdates"][0]
        assert cu["recommended"] == {
            "status": "False",
            "reason": "OVNInterconnectMigration",
            "message": "Clusters on OVN may lose egress during migration.",
        }

    def test_in_flight_progress(self, mock_k8s):
        _wire(mock_k8s, _cv(progressing=True))
        text, comp = get_cluster_updates.call({})
        # The running version is the last Completed one, not status.desired.
        assert comp["current"]["version"] == "4.16.3"
        assert comp["desired"] == {"version": "4.16.8", "image": "quay.io/ocp-release@sha256:bbb"}
        p = comp["progress"]
        assert p["phase"] == "progressing"
        assert (p["done"], p["total"], p["percent"]) == (512, 863, 59)
        assert p["clusterOperators"] == {"updated": 2, "total": 3, "degraded": ["network"]}
        assert p["pools"] == [
            {"name": "master", "updated": 3, "total": 3, "degraded": False, "updating": False, "paused": False},
            {"name": "worker", "updated": 2, "total": 5, "degraded": False, "updating": True, "paused": True},
        ]
        # Last completed update took 80 min; 41% remains.
        assert p["etaMinutes"] == 33
        assert p["etaBasis"] == "last completed update duration"
        assert [b["type"] for b in comp["blockers"]] == ["pool"]
        assert comp["blockers"][0]["link"] == "/r/machineconfiguration.openshift.io~v1~machineconfigpools/_/worker"
        assert "ClusterOperators at target: 2/3" in text
        assert "Pool worker: 2/5 updated paused" in text

    def test_in_flight_targets_come_from_the_release_being_reconciled(self, mock_k8s):
        # During 4.16.3 -> 4.17.2, CVO lists updates *from 4.17.2*; Upgradeable=False
        # then blocks 4.18 targets, not the 4.17 patch that is already in reach.
        cv = _cv(progressing=True, upgradeable=False)
        cv["status"]["desired"] = {"version": "4.17.2", "image": "quay.io/ocp-release@sha256:ddd"}
        cv["status"]["availableUpdates"] = [
            {"version": "4.17.5", "image": "quay.io/ocp-release@sha256:e1"},
            {"version": "4.18.1", "image": "quay.io/ocp-release@sha256:e2"},
        ]
        cv["status"]["conditionalUpdates"] = []
        _wire(mock_k8s, cv)
        text, comp = get_cluster_updates.call({})
        assert comp["current"]["version"] == "4.16.3"
        assert comp["edges"] == [{"from": "4.17.2", "to": "4.17.5"}, {"from": "4.17.2", "to": "4.18.1"}]
        states = {n["version"]: n["state"] for n in comp["nodes"]}
        assert states == {"4.17.5": "recommended", "4.18.1": "blocked"}
        assert "update targets below are from that release" in text

    def test_failing_without_progressing_reports_the_failure(self):
        cv = _cv()
        cv["status"]["conditions"] = [
            {"type": "Failing", "status": "True", "reason": "ClusterOperatorDegraded", "message": "etcd is degraded"},
            {"type": "Progressing", "status": "False", "message": "Error while reconciling 4.16.3"},
        ]
        data = build_cluster_updates(cv, [], [])
        assert data["progress"]["phase"] == "failing"
        assert data["progress"]["message"] == "etcd is degraded"

    def test_no_version_returns_text_only(self, mock_k8s):
        _wire(mock_k8s, {"spec": {"channel": "stable-4.16"}, "status": {"conditions": []}})
        result = get_cluster_updates.call({})
        assert isinstance(result, str)
        assert "Current: unknown" in result

    def test_upgradeable_false_blocks_minor_targets_only(self, mock_k8s):
        _wire(mock_k8s, _cv(upgradeable=False))
        text, comp = get_cluster_updates.call({})
        states = {n["version"]: n["state"] for n in comp["nodes"]}
        assert states == {"4.16.8": "recommended", "4.17.2": "blocked", "4.17.1": "blocked"}
        assert comp["blockers"][0]["type"] == "upgradeable"
        assert comp["blockers"][0]["reason"] == "AdminAckRequired"
        assert "Upgradeable=False (AdminAckRequired)" in text

    def test_unreadable_operators_and_pools_are_not_guessed(self, mock_k8s):
        _wire(
            mock_k8s,
            _cv(progressing=True),
            operators=ApiException(status=403, reason="Forbidden"),
            pools=ApiException(status=404, reason="Not Found"),
        )
        text, comp = get_cluster_updates.call({})
        assert comp["progress"]["clusterOperators"] is None
        assert comp["progress"]["pools"] is None
        assert "ClusterOperators: could not be read" in text
        assert "MachineConfigPools: could not be read" in text

    def test_not_openshift(self, mock_k8s):
        mock_k8s["custom"].get_cluster_custom_object.side_effect = ApiException(status=404, reason="Not Found")
        result = get_cluster_updates.call({})
        assert isinstance(result, str)
        assert "not an OpenShift cluster" in result

    def test_forbidden_is_an_error_not_a_claim(self, mock_k8s):
        mock_k8s["custom"].get_cluster_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
        result = get_cluster_updates.call({})
        assert isinstance(result, str)
        assert "not an OpenShift cluster" not in result

    def test_registered_read_only(self):
        assert "get_cluster_updates" not in WRITE_TOOLS
        assert "get_cluster_updates" in TOOL_CATEGORIES["operations"]["tools"]


def _meta(name, uid="", owner=None, annotations=None, created=None):
    refs = [SimpleNamespace(kind=owner[0], name=owner[1], uid=owner[2])] if owner else []
    return SimpleNamespace(
        name=name,
        uid=uid,
        owner_references=refs,
        annotations=annotations or {},
        creation_timestamp=created or datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )


def _container(image):
    return SimpleNamespace(image=image)


def _rs(name, rev, image, owner_uid="dep-uid", ready=0, desired=0, cause=""):
    ann = {"deployment.kubernetes.io/revision": str(rev)}
    if cause:
        ann["kubernetes.io/change-cause"] = cause
    return SimpleNamespace(
        metadata=_meta(name, owner=("Deployment", "api", owner_uid), annotations=ann),
        spec=SimpleNamespace(
            replicas=desired,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[_container(image)])),
        ),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _cr(name, rev, image, kind, owner_name, owner_uid):
    return SimpleNamespace(
        metadata=_meta(name, owner=(kind, owner_name, owner_uid), annotations={}),
        revision=rev,
        data={"spec": {"template": {"spec": {"containers": [{"name": "c", "image": image}]}}}},
    )


class TestGetRolloutHistory:
    def test_deployment_revisions(self, mock_k8s):
        apps = mock_k8s["apps"]
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            metadata=_meta("api", uid="dep-uid", annotations={"deployment.kubernetes.io/revision": "3"}),
            spec=SimpleNamespace(
                replicas=3,
                strategy=SimpleNamespace(type="RollingUpdate"),
                selector=SimpleNamespace(match_labels={"app": "api", "tier": "web"}),
            ),
            status=SimpleNamespace(
                updated_replicas=2,
                ready_replicas=3,
                available_replicas=3,
                conditions=[SimpleNamespace(type="Progressing", status="True", reason="ReplicaSetUpdated", message="")],
            ),
        )
        apps.list_namespaced_replica_set.return_value = SimpleNamespace(
            items=[
                _rs("api-3", 3, "reg/api:1.3", ready=2, desired=3, cause="bump to 1.3"),
                _rs("api-1", 1, "reg/api:1.1"),
                _rs("other-1", 1, "reg/other:1", owner_uid="someone-else"),
                _rs("api-2", 2, "reg/api:1.2", ready=1, desired=1),
            ]
        )
        text, comp = get_rollout_history.call({"kind": "deployment", "namespace": "prod", "name": "api"})
        assert comp["kind"] == "rollout_history"
        assert comp["workload"] == {
            "kind": "Deployment",
            "namespace": "prod",
            "name": "api",
            "gvr": "apps~v1~deployments",
        }
        assert [r["revision"] for r in comp["revisions"]] == [1, 2, 3]
        latest = comp["revisions"][-1]
        assert latest == {
            "revision": 3,
            "name": "api-3",
            "createdAt": "2026-10-01T12:00:00Z",
            "images": ["reg/api:1.3"],
            "changeCause": "bump to 1.3",
            "replicas": {"ready": 2, "desired": 3},
            "current": True,
            "source": "replicaset",
        }
        assert [r["current"] for r in comp["revisions"]] == [False, False, True]
        assert comp["inFlight"]["updated"] == 2
        assert comp["inFlight"]["desired"] == 3
        assert comp["inFlight"]["conditions"][0]["type"] == "Progressing"
        assert "(image changed)" in text
        assert "<- current" in text
        assert _errors(comp) == []
        apps.list_namespaced_replica_set.assert_called_once_with("prod", label_selector="app=api,tier=web")

    def test_statefulset_uses_controller_revisions(self, mock_k8s):
        apps = mock_k8s["apps"]
        apps.read_namespaced_stateful_set.return_value = SimpleNamespace(
            metadata=_meta("db", uid="sts-uid"),
            spec=SimpleNamespace(replicas=3, update_strategy=SimpleNamespace(type="RollingUpdate")),
            status=SimpleNamespace(
                update_revision="db-bbb",
                updated_replicas=1,
                ready_replicas=3,
                available_replicas=3,
                conditions=None,
            ),
        )
        apps.list_namespaced_controller_revision.return_value = SimpleNamespace(
            items=[
                _cr("db-aaa", 1, "reg/db:15", "StatefulSet", "db", "sts-uid"),
                _cr("db-bbb", 2, "reg/db:16", "StatefulSet", "db", "sts-uid"),
                _cr("cache-x", 1, "reg/cache:1", "StatefulSet", "cache", "other"),
            ]
        )
        _, comp = get_rollout_history.call({"kind": "StatefulSet", "namespace": "data", "name": "db"})
        assert comp["workload"]["gvr"] == "apps~v1~statefulsets"
        assert [(r["name"], r["images"], r["current"], r["source"]) for r in comp["revisions"]] == [
            ("db-aaa", ["reg/db:15"], False, "controllerrevision"),
            ("db-bbb", ["reg/db:16"], True, "controllerrevision"),
        ]
        assert comp["revisions"][0]["replicas"] is None
        assert comp["revisions"][1]["replicas"] == {"ready": 3, "desired": 3}
        assert comp["inFlight"]["updated"] == 1

    def test_daemonset_current_is_highest_revision(self, mock_k8s):
        apps = mock_k8s["apps"]
        apps.read_namespaced_daemon_set.return_value = SimpleNamespace(
            metadata=_meta("agent", uid="ds-uid"),
            spec=SimpleNamespace(update_strategy=SimpleNamespace(type="OnDelete")),
            status=SimpleNamespace(
                updated_number_scheduled=4,
                number_ready=5,
                number_available=5,
                desired_number_scheduled=5,
                conditions=None,
            ),
        )
        apps.list_namespaced_controller_revision.return_value = SimpleNamespace(
            items=[
                _cr("agent-2", 2, "reg/agent:2", "DaemonSet", "agent", "ds-uid"),
                _cr("agent-1", 1, "reg/agent:1", "DaemonSet", "agent", "ds-uid"),
            ]
        )
        _, comp = get_rollout_history.call({"kind": "daemonset", "namespace": "infra", "name": "agent"})
        assert [r["current"] for r in comp["revisions"]] == [False, True]
        assert comp["inFlight"] == {
            "strategy": "OnDelete",
            "updated": 4,
            "ready": 5,
            "available": 5,
            "desired": 5,
            "conditions": [],
        }

    def test_rejects_unknown_kind_and_bad_names(self, mock_k8s):
        assert "kind must be one of" in get_rollout_history.call({"kind": "Pod", "namespace": "a", "name": "b"})
        assert "not a valid" in get_rollout_history.call({"kind": "Deployment", "namespace": "a", "name": "Bad_Name"})
        mock_k8s["apps"].read_namespaced_deployment.assert_not_called()

    def test_api_error_returned(self, mock_k8s):
        mock_k8s["apps"].read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
        result = get_rollout_history.call({"kind": "Deployment", "namespace": "prod", "name": "gone"})
        assert isinstance(result, str)
        assert "404" in result or "not found" in result.lower()

    def test_registered_read_only(self):
        assert "get_rollout_history" not in WRITE_TOOLS
        assert "get_rollout_history" in TOOL_CATEGORIES["workloads"]["tools"]


class TestValidation:
    def test_upgrade_graph_rejects_bad_state(self):
        comp = {
            "kind": "upgrade_graph",
            "current": {"version": "4.16.3"},
            "nodes": [{"version": "4.17", "state": "maybe"}],
        }
        assert any("recommended|conditional|blocked" in e for e in _errors(comp))

    def test_upgrade_graph_requires_current_version(self):
        assert any("current" in e for e in _errors({"kind": "upgrade_graph", "current": {}, "nodes": []}))

    def test_rollout_history_requires_workload(self):
        comp = {"kind": "rollout_history", "workload": {"kind": "Deployment"}, "revisions": [{"revision": 1}]}
        assert any("workload" in e for e in _errors(comp))

    def test_stat_card_with_query_needs_no_value(self):
        assert _errors({"kind": "stat_card", "title": "Ready pods", "query": "sum(kube_pod_status_ready)"}) == []
        assert _errors({"kind": "stat_card", "title": "Ready pods"}) != []
