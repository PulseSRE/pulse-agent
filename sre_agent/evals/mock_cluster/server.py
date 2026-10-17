"""Kubernetes and Prometheus APIs over the eval scenario, for real tools to read.

Replay recordings return text, so they never produce the widget specs a
dashboard is built from. Pointing the agent's real tools at this server
(kubeconfig to the Kubernetes app, ``PULSE_AGENT_THANOS_URL`` to the
Prometheus app) does, which is what the rendered-page eval needs.

Covers what the view designer's tools call: core and apps lists and gets,
pod logs, metrics.k8s.io, OpenShift cluster version and operators, the Table
API used by list_resources, and Prometheus query, query_range, alerts and
metric names. Anything else answers 404 like a real API server would for an
absent resource.

Run: ``python -m sre_agent.evals.mock_cluster.server --k8s-port 18001 --prom-port 18002``
"""

from __future__ import annotations

import argparse
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import promql
from . import scenario as sc

# ---------------------------------------------------------------------------
# Kubernetes objects
# ---------------------------------------------------------------------------


def _meta(name: str, namespace: str = "", labels: dict | None = None, uid: str = "") -> dict:
    meta: dict[str, Any] = {
        "name": name,
        "uid": uid or f"uid-{abs(hash((namespace, name))) % 10**12}",
        "creationTimestamp": sc.CREATED,
        "resourceVersion": "1",
        "labels": labels or {},
    }
    if namespace:
        meta["namespace"] = namespace
    return meta


def _node(row: tuple) -> dict:
    name, roles, cpu, mem, _cpu_pct, _mem_pct, ready, pressure = row
    labels = {f"node-role.kubernetes.io/{r}": "" for r in roles}
    labels["kubernetes.io/hostname"] = name
    conditions = [{"type": "Ready", "status": "True" if ready else "False", "reason": "KubeletReady"}]
    for kind in ("MemoryPressure", "DiskPressure", "PIDPressure"):
        conditions.append({"type": kind, "status": "True" if pressure == kind else "False"})
    capacity = {"cpu": str(cpu), "memory": f"{mem}Gi", "pods": "250"}
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": _meta(name, labels=labels),
        "spec": {},
        "status": {
            "conditions": conditions,
            "capacity": capacity,
            "allocatable": capacity,
            "nodeInfo": {
                "kubeletVersion": "v1.31.6",
                "osImage": "Red Hat Enterprise Linux CoreOS 9.6",
                "containerRuntimeVersion": "cri-o://1.31.6",
                "kernelVersion": "5.14.0",
                "operatingSystem": "linux",
                "architecture": "amd64",
                "machineID": name,
                "systemUUID": name,
                "bootID": name,
                "kubeProxyVersion": "v1.31.6",
            },
        },
    }


def _pod(row: tuple) -> dict:
    name, deployment, namespace, node, phase, reason, restarts, _cpu, _mem = row
    image = next((d[3] for d in sc.DEPLOYMENTS if d[0] == deployment and d[1] == namespace), "registry/app:latest")
    container = deployment.split("-")[0] if deployment != "cache" else "redis"
    if reason == "CrashLoopBackOff":
        state = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off restarting failed container"}}
        last = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
        ready = False
    elif phase == "Pending":
        state = {"waiting": {"reason": "ContainerCreating"}}
        last = {}
        ready = False
    else:
        state = {"running": {"startedAt": sc.CREATED}}
        last = {}
        ready = True
    created = sc.ago(sc.POD_AGE_MINUTES[name]) if name in sc.POD_AGE_MINUTES else sc.CREATED
    status: dict[str, Any] = {
        "phase": phase,
        "conditions": [{"type": "Ready", "status": "True" if ready else "False", "lastTransitionTime": created}],
        "startTime": created,
    }
    if phase == "Pending":
        status["conditions"].append(
            {"type": "PodScheduled", "status": "False", "reason": reason, "message": "0/4 nodes: insufficient cpu"}
        )
    else:
        status["podIP"] = f"10.128.{abs(hash(name)) % 250}.{abs(hash(name[::-1])) % 250}"
        status["containerStatuses"] = [
            {
                "name": container,
                "image": image,
                "imageID": image,
                "ready": ready,
                "restartCount": restarts,
                "started": ready,
                "state": state,
                "lastState": last,
            }
        ]
    spec: dict[str, Any] = {
        "containers": [
            {
                "name": container,
                "image": image,
                "resources": {
                    "limits": {"cpu": "1", "memory": "512Mi"},
                    "requests": {"cpu": "250m", "memory": "256Mi"},
                },
            }
        ]
    }
    if phase != "Pending":
        spec["nodeName"] = node
    rs = f"{deployment}-{name.split('-')[-2]}" if name.count("-") >= 2 else deployment
    meta = _meta(name, namespace, labels={"app": deployment})
    meta["creationTimestamp"] = created
    if "running" in state:
        state["running"]["startedAt"] = created
    meta["ownerReferences"] = [
        {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": rs, "uid": f"uid-rs-{rs}", "controller": True}
    ]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec, "status": status}


def _deployment(row: tuple) -> dict:
    name, namespace, desired, image, _cpu, _mem = row
    pods = [p for p in sc.PODS if p[1] == name and p[2] == namespace]
    ready = sum(1 for p in pods if p[4] == "Running" and not p[5])
    available = ready
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(name, namespace, labels={"app": name}),
        "spec": {
            "replicas": desired,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": name, "image": image}]},
            },
        },
        "status": {
            "replicas": len(pods),
            "readyReplicas": ready,
            "availableReplicas": available,
            "updatedReplicas": len(pods),
            "unavailableReplicas": max(0, desired - available),
            "conditions": [
                {"type": "Available", "status": "True" if available >= desired else "False"},
                {"type": "Progressing", "status": "True"},
            ],
        },
    }


def _event(row: tuple, now: datetime) -> dict:
    namespace, involved, etype, reason, message, count, minutes = row
    kind, name = involved.split("/", 1)
    stamp = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": _meta(f"{name}.{reason.lower()}", namespace),
        "involvedObject": {"kind": kind, "name": name, "namespace": namespace},
        "type": etype,
        "reason": reason,
        "message": message,
        "count": count,
        "firstTimestamp": stamp,
        "lastTimestamp": stamp,
    }


def _service(row: tuple) -> dict:
    name, namespace, port = row
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(name, namespace, labels={"app": name}),
        "spec": {
            "selector": {"app": name},
            "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}],
            "clusterIP": f"172.30.{port % 250}.{len(name)}",
            "type": "ClusterIP",
        },
    }


def _namespace(name: str) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(name), "status": {"phase": "Active"}}


def _collection(kind: str, api_version: str, items: list[dict]) -> dict:
    return {"kind": f"{kind}List", "apiVersion": api_version, "metadata": {"resourceVersion": "1"}, "items": items}


def _all(resource: str, now: datetime) -> list[dict]:
    if resource == "nodes":
        return [_node(r) for r in sc.NODES]
    if resource == "namespaces":
        return [_namespace(n) for n in sc.NAMESPACES]
    if resource == "pods":
        return [_pod(r) for r in sc.PODS]
    if resource == "events":
        return [_event(r, now) for r in sc.EVENTS]
    if resource == "services":
        return [_service(r) for r in sc.SERVICES]
    if resource == "deployments":
        return [_deployment(r) for r in sc.DEPLOYMENTS]
    return []


_KINDS = {
    "nodes": ("Node", "v1", False),
    "namespaces": ("Namespace", "v1", False),
    "pods": ("Pod", "v1", True),
    "events": ("Event", "v1", True),
    "services": ("Service", "v1", True),
    "configmaps": ("ConfigMap", "v1", True),
    "persistentvolumeclaims": ("PersistentVolumeClaim", "v1", True),
    "resourcequotas": ("ResourceQuota", "v1", True),
    "limitranges": ("LimitRange", "v1", True),
    "secrets": ("Secret", "v1", True),
    "deployments": ("Deployment", "apps/v1", True),
    "replicasets": ("ReplicaSet", "apps/v1", True),
    "statefulsets": ("StatefulSet", "apps/v1", True),
    "daemonsets": ("DaemonSet", "apps/v1", True),
}


def _age(created: str, now: datetime) -> str:
    then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    days = (now - then).days
    return f"{days}d" if days else f"{int((now - then).total_seconds() // 3600)}h"


def _table(resource: str, items: list[dict], now: datetime) -> dict:
    """The Table API shape kubectl get and list_resources read."""
    cols = [{"name": "Name", "type": "string", "priority": 0}]
    if resource == "pods":
        cols += [{"name": n, "type": "string", "priority": 0} for n in ("Ready", "Status", "Restarts")]
        cols += [{"name": "Age", "type": "string", "priority": 0}, {"name": "Node", "type": "string", "priority": 1}]
    elif resource == "nodes":
        cols += [{"name": n, "type": "string", "priority": 0} for n in ("Status", "Roles", "Age", "Version")]
    elif resource == "deployments":
        cols += [{"name": n, "type": "string", "priority": 0} for n in ("Ready", "Up-to-date", "Available", "Age")]
    else:
        cols.append({"name": "Age", "type": "string", "priority": 0})

    rows = []
    for obj in items:
        meta = obj["metadata"]
        age = _age(meta["creationTimestamp"], now)
        if resource == "pods":
            cs = (obj["status"].get("containerStatuses") or [{}])[0]
            waiting = (cs.get("state") or {}).get("waiting", {})
            status = waiting.get("reason") or obj["status"]["phase"]
            cells = [meta["name"], f"{1 if cs.get('ready') else 0}/1", status, cs.get("restartCount", 0), age]
            cells.append(obj["spec"].get("nodeName", "<none>"))
        elif resource == "nodes":
            ready = next(c for c in obj["status"]["conditions"] if c["type"] == "Ready")["status"] == "True"
            roles = ",".join(k.split("/")[-1] for k in meta["labels"] if k.startswith("node-role"))
            cells = [meta["name"], "Ready" if ready else "NotReady", roles, age, "v1.31.6"]
        elif resource == "deployments":
            st = obj["status"]
            cells = [
                meta["name"],
                f"{st['readyReplicas']}/{obj['spec']['replicas']}",
                st["updatedReplicas"],
                st["availableReplicas"],
                age,
            ]
        else:
            cells = [meta["name"], age]
        rows.append({"cells": cells, "object": {"kind": "PartialObjectMetadata", "metadata": meta}})
    return {"kind": "Table", "apiVersion": "meta.k8s.io/v1", "columnDefinitions": cols, "rows": rows}


def _not_found(what: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "reason": "NotFound",
            "code": 404,
            "message": f"{what} not found",
        },
    )


def build_k8s_app() -> FastAPI:
    app = FastAPI()

    @app.get("/version")
    @app.get("/version/")
    def version():
        return {"major": "1", "minor": "31", "gitVersion": "v1.31.6", "platform": "linux/amd64"}

    @app.get("/api")
    def api_versions():
        return {"kind": "APIVersions", "versions": ["v1"]}

    @app.get("/apis")
    def api_groups():
        groups = [("apps", "v1"), ("metrics.k8s.io", "v1beta1"), ("config.openshift.io", "v1")]
        return {
            "kind": "APIGroupList",
            "apiVersion": "v1",
            "groups": [
                {
                    "name": g,
                    "versions": [{"groupVersion": f"{g}/{v}", "version": v}],
                    "preferredVersion": {"groupVersion": f"{g}/{v}", "version": v},
                }
                for g, v in groups
            ],
        }

    def _resource_list(group_version: str, names: list[str]) -> dict:
        return {
            "kind": "APIResourceList",
            "groupVersion": group_version,
            "resources": [
                {
                    "name": n,
                    "singularName": n.rstrip("s"),
                    "namespaced": _KINDS[n][2],
                    "kind": _KINDS[n][0],
                    "verbs": ["get", "list"],
                }
                for n in names
            ],
        }

    @app.get("/api/v1")
    def core_resources():
        return _resource_list("v1", [n for n, v in _KINDS.items() if v[1] == "v1"])

    @app.get("/apis/apps/v1")
    def apps_resources():
        return _resource_list("apps/v1", [n for n, v in _KINDS.items() if v[1] == "apps/v1"])

    @app.post("/apis/authentication.k8s.io/v1/tokenreviews")
    async def token_review(request: Request):
        body = await request.json()
        return {**body, "status": {"authenticated": False}}

    @app.get("/apis/config.openshift.io/v1/clusterversions/version")
    def cluster_version():
        return {
            "apiVersion": "config.openshift.io/v1",
            "kind": "ClusterVersion",
            "metadata": _meta("version"),
            "spec": {"channel": "stable-4.17"},
            "status": {
                "desired": {"version": "4.17.12"},
                "conditions": [
                    {"type": "Available", "status": "True"},
                    {"type": "Progressing", "status": "False"},
                    {"type": "Failing", "status": "False"},
                ],
            },
        }

    @app.get("/apis/config.openshift.io/v1/clusteroperators")
    def cluster_operators():
        names = ["authentication", "console", "dns", "ingress", "monitoring", "network", "storage"]
        items = [
            {
                "apiVersion": "config.openshift.io/v1",
                "kind": "ClusterOperator",
                "metadata": _meta(n),
                "status": {
                    "conditions": [
                        {"type": "Available", "status": "True"},
                        {
                            "type": "Degraded",
                            "status": "True" if n == "monitoring" else "False",
                            "message": "Prometheus storage at 87%" if n == "monitoring" else "",
                        },
                        {"type": "Progressing", "status": "False"},
                    ],
                    "versions": [{"name": "operator", "version": "4.17.12"}],
                },
            }
            for n in names
        ]
        return {"apiVersion": "config.openshift.io/v1", "kind": "ClusterOperatorList", "items": items}

    @app.get("/apis/metrics.k8s.io/v1beta1/nodes")
    def node_metrics():
        items = [
            {
                "metadata": {"name": n[0]},
                "timestamp": sc.CREATED,
                "window": "30s",
                "usage": {"cpu": f"{int(n[2] * n[4] * 10)}m", "memory": f"{int(n[3] * n[5] * 10.24)}Mi"},
            }
            for n in sc.NODES
        ]
        return {"kind": "NodeMetricsList", "apiVersion": "metrics.k8s.io/v1beta1", "items": items}

    @app.get("/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
    @app.get("/apis/metrics.k8s.io/v1beta1/pods")
    def pod_metrics(namespace: str = ""):
        items = [
            {
                "metadata": {"name": p[0], "namespace": p[2]},
                "timestamp": sc.CREATED,
                "window": "30s",
                "containers": [{"name": p[1], "usage": {"cpu": f"{int(p[7] * 1000)}m", "memory": f"{p[8]}Mi"}}],
            }
            for p in sc.PODS
            if p[4] == "Running" and (not namespace or p[2] == namespace)
        ]
        return {"kind": "PodMetricsList", "apiVersion": "metrics.k8s.io/v1beta1", "items": items}

    @app.get("/api/v1/namespaces/{namespace}/pods/{name}/log")
    def pod_log(namespace: str, name: str):
        pod = next((p for p in sc.PODS if p[0] == name and p[2] == namespace), None)
        if not pod:
            return _not_found(f'pods "{name}"')
        return PlainTextResponse("\n".join(sc.LOG_LINES.get(pod[1], ["ready"])) + "\n")

    @app.get("/api/v1/namespaces/{namespace}/services/{name}/proxy/{rest:path}")
    def service_proxy(namespace: str, name: str, rest: str):
        # No in-cluster services to proxy to; tools fall back to Prometheus.
        return _not_found(f'services "{name}"')

    def _respond(resource: str, group_version: str, namespace: str, name: str, request: Request):
        now = datetime.now(UTC)
        if resource not in _KINDS:
            return _not_found(f'resource "{resource}"')
        items = [o for o in _all(resource, now) if not namespace or o["metadata"].get("namespace") == namespace]
        selector = request.query_params.get("labelSelector", "")
        for clause in filter(None, selector.split(",")):
            if "=" in clause and "!=" not in clause:
                key, value = clause.split("=", 1)
                items = [o for o in items if o["metadata"].get("labels", {}).get(key.strip()) == value.strip()]
        if name:
            match = next((o for o in items if o["metadata"]["name"] == name), None)
            return match if match else _not_found(f'{resource} "{name}"')
        if "as=Table" in request.headers.get("accept", ""):
            return _table(resource, items, now)
        kind, api_version, _ = _KINDS[resource]
        return _collection(kind, api_version, items)

    @app.get("/api/v1/{resource}")
    def core_list(resource: str, request: Request):
        return _respond(resource, "v1", "", "", request)

    @app.get("/api/v1/{resource}/{name}")
    def core_cluster_get(resource: str, name: str, request: Request):
        return _respond(resource, "v1", "", name, request)

    @app.get("/api/v1/namespaces/{namespace}")
    def namespace_get(namespace: str):
        return _namespace(namespace) if namespace in sc.NAMESPACES else _not_found(f'namespaces "{namespace}"')

    @app.get("/api/v1/namespaces/{namespace}/{resource}")
    def core_ns_list(namespace: str, resource: str, request: Request):
        return _respond(resource, "v1", namespace, "", request)

    @app.get("/api/v1/namespaces/{namespace}/{resource}/{name}")
    def core_ns_get(namespace: str, resource: str, name: str, request: Request):
        return _respond(resource, "v1", namespace, name, request)

    @app.get("/apis/apps/v1/{resource}")
    def apps_list(resource: str, request: Request):
        return _respond(resource, "apps/v1", "", "", request)

    @app.get("/apis/apps/v1/namespaces/{namespace}/{resource}")
    def apps_ns_list(namespace: str, resource: str, request: Request):
        return _respond(resource, "apps/v1", namespace, "", request)

    @app.get("/apis/apps/v1/namespaces/{namespace}/{resource}/{name}")
    def apps_ns_get(namespace: str, resource: str, name: str, request: Request):
        return _respond(resource, "apps/v1", namespace, name, request)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def anything_else(path: str):
        return _not_found(f"/{path}")

    return app


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d|w)?$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, None: 1}


def _seconds(value: str, default: float) -> float:
    match = _DURATION.match(str(value).strip())
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)] if match else default


def _timestamp(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _bad(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"status": "error", "errorType": "bad_data", "error": message})


def build_prometheus_app() -> FastAPI:
    app = FastAPI()

    async def _params(request: Request) -> dict:
        params = dict(request.query_params)
        if request.method == "POST":
            params.update({k: str(v) for k, v in (await request.form()).items()})
        return params

    @app.api_route("/api/v1/query", methods=["GET", "POST"])
    async def query(request: Request):
        params = await _params(request)
        at = _timestamp(params.get("time"), time.time())
        try:
            kind, value = promql.evaluate(params.get("query", ""), at)
        except Exception as e:  # parse errors and unsupported constructs, as Prometheus reports them
            return _bad(str(e))
        if kind == "vector":
            result: Any = [{"metric": lbl, "value": [at, _fmt(v)]} for lbl, v in value]
        else:
            result = [at, _fmt(value) if kind == "scalar" else value]
        return {"status": "success", "data": {"resultType": kind, "result": result}}

    @app.api_route("/api/v1/query_range", methods=["GET", "POST"])
    async def query_range(request: Request):
        params = await _params(request)
        end = _timestamp(params.get("end"), time.time())
        start = _timestamp(params.get("start"), end - 3600)
        step = max(1.0, _seconds(params.get("step", "60"), 60.0))
        steps = max(1, min(600, int((end - start) / step) + 1))
        series: dict[tuple, dict] = {}
        try:
            for i in range(steps):
                at = start + i * step
                kind, value = promql.evaluate(params.get("query", ""), at)
                points = value if kind == "vector" else [({}, value)] if kind == "scalar" else []
                for lbl, v in points:
                    key = tuple(sorted(lbl.items()))
                    series.setdefault(key, {"metric": lbl, "values": []})["values"].append([at, _fmt(v)])
        except Exception as e:
            return _bad(str(e))
        return {"status": "success", "data": {"resultType": "matrix", "result": list(series.values())}}

    @app.get("/api/v1/alerts")
    def alerts():
        active = [
            {
                "labels": {
                    "alertname": a[0],
                    "severity": a[1],
                    **({"namespace": a[2]} if a[2] else {}),
                    ("pod" if a[2] else "node"): a[3],
                },
                "annotations": {"summary": a[4], "description": a[4]},
                "state": "firing",
                "activeAt": sc.ago(45),
                "value": "1",
            }
            for a in sc.ALERTS
        ]
        return {"status": "success", "data": {"alerts": active}}

    @app.get("/api/v1/rules")
    def rules():
        groups = [
            {
                "name": "scenario",
                "file": "scenario.yaml",
                "rules": [
                    {
                        "name": a[0],
                        "type": "alerting",
                        "state": "firing",
                        "labels": {"severity": a[1]},
                        "annotations": {"summary": a[4]},
                        "alerts": [alert],
                    }
                    for a, alert in zip(sc.ALERTS, alerts()["data"]["alerts"], strict=True)
                ],
            }
        ]
        return {"status": "success", "data": {"groups": groups}}

    @app.get("/api/v1/label/__name__/values")
    def metric_names():
        names = [
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "kube_pod_container_status_restarts_total",
            "kube_pod_status_phase",
            "node_cpu_seconds_total",
            "node_memory_MemAvailable_bytes",
            "up",
        ]
        return {"status": "success", "data": names}

    @app.get("/api/v1/{rest:path}")
    def other(rest: str):
        return {"status": "success", "data": []}

    return app


# ---------------------------------------------------------------------------


def write_kubeconfig(path: str, k8s_port: int) -> None:
    """A kubeconfig pointing the agent's Kubernetes client at the mock."""
    with open(path, "w") as f:
        f.write(
            "apiVersion: v1\nkind: Config\ncurrent-context: mock\n"
            f"clusters:\n- name: mock\n  cluster:\n    server: http://127.0.0.1:{k8s_port}\n"
            "contexts:\n- name: mock\n  context:\n    cluster: mock\n    user: mock\n"
            "users:\n- name: mock\n  user:\n    token: mock-token\n"
        )


def serve(k8s_port: int, prom_port: int) -> None:
    import uvicorn

    prom = uvicorn.Server(uvicorn.Config(build_prometheus_app(), host="127.0.0.1", port=prom_port, log_level="warning"))
    threading.Thread(target=prom.run, daemon=True).start()
    uvicorn.run(build_k8s_app(), host="127.0.0.1", port=k8s_port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k8s-port", type=int, default=18001)
    parser.add_argument("--prom-port", type=int, default=18002)
    parser.add_argument("--kubeconfig", default="", help="also write a kubeconfig for the mock to this path")
    args = parser.parse_args()
    if args.kubeconfig:
        write_kubeconfig(args.kubeconfig, args.k8s_port)
    serve(args.k8s_port, args.prom_port)


if __name__ == "__main__":
    main()
