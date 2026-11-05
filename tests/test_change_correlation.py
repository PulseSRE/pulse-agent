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
    """A ConfigMap as the metadata-only list returns it."""
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": _ts(10 * 86400).isoformat(),
            "managedFields": [{"manager": manager, "time": _ts(seconds_before).isoformat()}],
        }
    }


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
    apps, custom = MagicMock(), MagicMock()
    apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=list(replica_sets))
    apps.list_namespaced_controller_revision.return_value = SimpleNamespace(items=[])
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=list(deployments))
    raw = MagicMock(return_value={"items": list(configmaps)})
    custom.list_cluster_custom_object.return_value = {"items": []}
    custom.get_cluster_custom_object.return_value = {"status": {"history": []}}
    return (
        patch.object(cc, "get_apps_client", return_value=apps),
        patch.object(cc, "get_raw_json", raw),
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
    apps_p, raw_p, custom_p = _cluster()
    with apps_p, raw_p, custom_p as get_custom:
        get_custom.return_value.get_cluster_custom_object.return_value = {
            "status": {"history": [{"version": "4.16.3", "startedTime": _ts(1200).isoformat()}]}
        }
        changes = cc.ranked_changes(["crashloop:shop:Pod/api-0"], [], ONSET)
    assert [(c["title"], c["relevance"]) for c in changes] == [("Cluster upgrade to 4.16.3 started", "cluster-wide")]


def test_affected_scope_reads_correlation_keys():
    ns, workloads = cc.affected_scope(["crashloop:shop:Pod/api-5f58f69bd6-w4x22", "alerts::Alert/Watchdog"], ["ops"])
    assert ns == {"shop", "ops"}
    assert ("shop", "api") in workloads


def test_configmaps_are_listed_metadata_only_and_paged():
    pages = [
        {"items": [_cm("a", 60)], "metadata": {"continue": "tok1"}},
        {"items": [_cm("b", 120)], "metadata": {}},
    ]
    raw = MagicMock(side_effect=pages)
    with patch.object(cc, "get_raw_json", raw):
        changes = cc._configmap_edits("shop", ONSET - 3600, ONSET)
    assert sorted(c["name"] for c in changes) == ["a", "b"]
    first, second = raw.call_args_list
    assert first.kwargs["accept"] == cc._METADATA_ONLY and second.kwargs["accept"] == cc._METADATA_ONLY
    assert first.kwargs["query"] == {"limit": "500"}
    assert second.kwargs["query"] == {"limit": "500", "continue": "tok1"}


def test_a_list_too_long_to_read_is_reported_incomplete():
    page = {"items": [_cm(f"cm-{i}", 60) for i in range(cc.PAGE_SIZE)], "metadata": {"continue": "more"}}
    calls = []

    def raw(path, what, *, query=None, accept=""):
        calls.append(query)
        return {**page, "metadata": {"continue": f"more{len(calls)}"}}

    incomplete: list[str] = []
    with patch.object(cc, "get_raw_json", side_effect=raw):
        cc._configmap_edits("shop", ONSET - 3600, ONSET, [], incomplete)
    assert len(calls) == cc.MAX_LIST_ITEMS // cc.PAGE_SIZE
    assert incomplete == ["ConfigMaps in shop"]


def test_replicasets_are_read_in_pages():
    apps = MagicMock()
    apps.list_namespaced_replica_set.side_effect = [
        SimpleNamespace(items=[], metadata=SimpleNamespace(_continue="t")),
        SimpleNamespace(items=[], metadata=SimpleNamespace(_continue=None)),
    ]
    with patch.object(cc, "get_apps_client", return_value=apps):
        cc._deployment_rollouts("shop", ONSET - 3600, ONSET)
    calls = apps.list_namespaced_replica_set.call_args_list
    assert calls[0].kwargs == {"limit": 500} and calls[1].kwargs == {"limit": 500, "_continue": "t"}


def test_argo_applications_are_read_once_per_identity_per_minute():
    from sre_agent import k8s_client

    custom = MagicMock()
    custom.list_cluster_custom_object.return_value = {"items": []}
    with patch.object(cc, "get_custom_client", return_value=custom):
        cc._argo_syncs({"shop"}, ONSET - 3600, ONSET)
        cc._argo_syncs({"web"}, ONSET - 3600, ONSET)
        assert custom.list_cluster_custom_object.call_count == 1
        token = k8s_client._user_token_var.set("someone-else")
        try:
            cc._argo_syncs({"shop"}, ONSET - 3600, ONSET)
        finally:
            k8s_client._user_token_var.reset(token)
        assert custom.list_cluster_custom_object.call_count == 2
    assert custom.list_cluster_custom_object.call_args.kwargs == {"limit": 500}


def test_a_refused_argo_list_is_not_cached():
    from kubernetes.client.rest import ApiException

    custom = MagicMock()
    custom.list_cluster_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
    errors: list = []
    with patch.object(cc, "get_custom_client", return_value=custom):
        cc._argo_syncs({"shop"}, ONSET - 3600, ONSET, errors)
        cc._argo_syncs({"shop"}, ONSET - 3600, ONSET, errors)
    assert custom.list_cluster_custom_object.call_count == 2
    assert [e.status_code for e in errors] == [403, 403]


def _container(env_from=(), env=()):
    return SimpleNamespace(env_from=list(env_from), env=list(env))


def test_referenced_config_covers_every_place_a_pod_names_config():
    spec = SimpleNamespace(
        volumes=[
            SimpleNamespace(config_map=SimpleNamespace(name="vol-cm"), secret=None, projected=None, csi=None),
            SimpleNamespace(
                config_map=None, secret=SimpleNamespace(secret_name="vol-secret"), projected=None, csi=None
            ),
            SimpleNamespace(
                config_map=None,
                secret=None,
                csi=None,
                projected=SimpleNamespace(
                    sources=[
                        SimpleNamespace(config_map=SimpleNamespace(name="proj-cm"), secret=None),
                        SimpleNamespace(config_map=None, secret=SimpleNamespace(name="proj-secret")),
                        SimpleNamespace(config_map=None, secret=None),  # serviceAccountToken / downwardAPI
                    ]
                ),
            ),
            SimpleNamespace(
                config_map=None,
                secret=None,
                projected=None,
                csi=SimpleNamespace(node_publish_secret_ref=SimpleNamespace(name="csi-secret")),
            ),
        ],
        containers=[
            _container(
                env_from=[
                    SimpleNamespace(config_map_ref=SimpleNamespace(name="envfrom-cm"), secret_ref=None),
                    SimpleNamespace(config_map_ref=None, secret_ref=SimpleNamespace(name="envfrom-secret")),
                ]
            )
        ],
        init_containers=[
            _container(
                env=[
                    SimpleNamespace(
                        value_from=SimpleNamespace(
                            config_map_key_ref=SimpleNamespace(name="init-cm"), secret_key_ref=None
                        )
                    )
                ]
            )
        ],
        ephemeral_containers=[
            _container(
                env_from=[SimpleNamespace(config_map_ref=None, secret_ref=SimpleNamespace(name="debug-secret"))],
                env=[
                    SimpleNamespace(
                        value_from=SimpleNamespace(
                            config_map_key_ref=None, secret_key_ref=SimpleNamespace(name="debug-key-secret")
                        )
                    )
                ],
            )
        ],
        image_pull_secrets=[SimpleNamespace(name="regcred")],
    )
    cms, secrets = cc.referenced_config(spec)
    assert cms == {"vol-cm", "proj-cm", "envfrom-cm", "init-cm"}
    assert secrets == {
        "vol-secret",
        "proj-secret",
        "csi-secret",
        "envfrom-secret",
        "debug-secret",
        "debug-key-secret",
        "regcred",
    }
