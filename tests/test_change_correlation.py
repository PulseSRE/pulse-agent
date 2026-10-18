"""What changed before an incident, read from the cluster and ranked by relevance."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sre_agent.monitor import change_correlation as cc

ONSET = 1_800_000_000


def _ts(seconds_before):
    return datetime.fromtimestamp(ONSET - seconds_before, tz=UTC)


def _rs(name, deploy, rev, seconds_before, image):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            annotations={"deployment.kubernetes.io/revision": str(rev)},
            owner_references=[SimpleNamespace(kind="Deployment", name=deploy)],
            creation_timestamp=_ts(seconds_before),
        ),
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]))),
    )


def _cm(name, seconds_before, manager="kubectl-edit"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            creation_timestamp=_ts(10 * 86400),
            managed_fields=[SimpleNamespace(manager=manager, time=_ts(seconds_before))],
        )
    )


def _deployment(name, configmap):
    spec = SimpleNamespace(
        volumes=[SimpleNamespace(config_map=SimpleNamespace(name=configmap))],
        containers=[SimpleNamespace(env_from=[], env=[])],
        init_containers=[],
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name), spec=SimpleNamespace(template=SimpleNamespace(spec=spec))
    )


def _cluster(replica_sets=(), configmaps=(), deployments=()):
    apps, core, custom = MagicMock(), MagicMock(), MagicMock()
    apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=list(replica_sets))
    apps.list_namespaced_controller_revision.return_value = SimpleNamespace(items=[])
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=list(deployments))
    core.list_namespaced_config_map.return_value = SimpleNamespace(items=list(configmaps))
    custom.list_cluster_custom_object.return_value = {"items": []}
    custom.get_cluster_custom_object.return_value = {"status": {"history": []}}
    return (
        patch.object(cc, "get_apps_client", return_value=apps),
        patch.object(cc, "get_core_client", return_value=core),
        patch.object(cc, "get_custom_client", return_value=custom),
    )


def _ranked(*patches, keys=("crashloop:shop:Pod/api-5f58f69bd6-w4x22",)):
    with patches[0], patches[1], patches[2]:
        return cc.ranked_changes(list(keys), [], ONSET)


def test_the_broken_workloads_own_rollout_comes_first_with_a_rollback():
    changes = _ranked(
        *_cluster(
            replica_sets=[
                _rs("api-old", "api", 6, 5 * 86400, "quay.io/acme/api:v6"),
                _rs("api-new", "api", 7, 600, "quay.io/acme/api:v7"),
                _rs("web-new", "web", 3, 120, "quay.io/acme/web:v3"),
            ]
        )
    )
    first = changes[0]
    assert first["title"] == "Deployment api rolled out revision 7 (quay.io/acme/api:v6 → quay.io/acme/api:v7)"
    assert first["relevance"] == "same workload"
    assert first["rollback"] == {"tool": "rollback_deployment", "namespace": "shop", "name": "api", "revision": 6}
    # web rolled out closer to the onset, but it is not what broke.
    assert changes[1]["name"] == "web" and changes[1]["relevance"] == "same namespace"


def test_a_configmap_the_broken_workload_mounts_outranks_one_it_does_not():
    changes = _ranked(
        *_cluster(
            configmaps=[_cm("api-config", 900), _cm("unrelated", 60)],
            deployments=[_deployment("api", "api-config")],
        )
    )
    assert [c["name"] for c in changes] == ["api-config", "unrelated"]
    assert changes[0]["relevance"] == "used by an affected workload"
    assert changes[0]["title"] == "ConfigMap api-config edited by kubectl-edit"


def test_changes_outside_the_window_are_left_out():
    changes = _ranked(*_cluster(configmaps=[_cm("old", 3 * 3600)]))
    assert changes == []


def test_a_cluster_upgrade_is_listed_as_cluster_wide():
    apps_p, core_p, custom_p = _cluster()
    with apps_p, core_p, custom_p as get_custom:
        get_custom.return_value.get_cluster_custom_object.return_value = {
            "status": {"history": [{"version": "4.16.3", "startedTime": _ts(1200).isoformat()}]}
        }
        changes = cc.ranked_changes(["crashloop:shop:Pod/api-0"], [], ONSET)
    assert [(c["title"], c["relevance"]) for c in changes] == [("Cluster upgrade to 4.16.3 started", "cluster-wide")]


def test_affected_scope_reads_correlation_keys():
    ns, workloads = cc.affected_scope(["crashloop:shop:Pod/api-5f58f69bd6-w4x22", "alerts::Alert/Watchdog"], ["ops"])
    assert ns == {"shop", "ops"}
    assert ("shop", "api") in workloads
