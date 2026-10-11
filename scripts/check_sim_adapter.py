#!/usr/bin/env python3
"""Exercise adapter read fidelity against the installed benchmark's actual fixture.

Requires sre-bench; no model, cluster, database, or generated score is involved.
"""

import json
from importlib.resources import files

from sre_bench.fixtures.sim import SimCluster

from sre_agent.evals.srebench_adapter import _SimBackedTool


def main() -> None:
    fixture = json.loads(files("sre_bench.fixtures").joinpath("data/sre_crashloop_resolution.json").read_text())
    # Add a conflicting namespaced row to the private test copy; never change
    # the benchmark fixture or derive diagnosis from the agent's answer.
    fixture["cluster"]["configmaps"].append(
        {"name": "db-config", "namespace": "unrelated", "data": {"DB_PORT": "9999"}}
    )
    backend = SimCluster(fixture)
    tool = _SimBackedTool({"name": "list_resources"}, backend, False)
    for selector in ("configmaps", "cm"):
        request = {"resource": selector, "namespace": "production"}
        result = json.loads(tool.call(request))
        assert result["configmaps"] == [fixture["cluster"]["configmaps"][0]], result
        assert result["configmaps"][0]["data"]["DB_PORT"] == "5432", result
        assert backend.calls[-1]["args"] == {"kind": "configmaps", "namespace": "production"}
        assert request == {"resource": selector, "namespace": "production"}
    service = json.loads(tool.call({"resource": "svc", "namespace": "production"}))
    assert service["services"][0]["spec"]["ports"][0]["port"] == 5432, service
    deployment = json.loads(tool.call({"resource": "deploy", "namespace": "production"}))
    assert deployment["deployments"][0]["name"] == "api-server", deployment
    print("Actual benchmark fixture read fidelity passed; no model or cluster execution.")


if __name__ == "__main__":
    main()
