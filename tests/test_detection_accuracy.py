"""Two different problems must not share a finding, and one problem must not be two."""

from __future__ import annotations

from sre_agent.monitor.confidence import _finding_key
from sre_agent.monitor.findings import _make_finding
from sre_agent.monitor.scanners import _alert_resources


def _key(alertname, labels, ns="app"):
    return _finding_key(_make_finding("warning", "alerts", alertname, "x", _alert_resources(alertname, labels, ns)))


def test_one_alert_for_two_operators_is_two_findings():
    assert _key("ClusterOperatorDegraded", {"name": "dns"}, "") != _key(
        "ClusterOperatorDegraded", {"name": "ingress"}, ""
    )


def test_one_alert_on_two_mounts_is_two_findings():
    a = _key("NodeFilesystemSpaceFillingUp", {"instance": "w1", "mountpoint": "/"})
    b = _key("NodeFilesystemSpaceFillingUp", {"instance": "w1", "mountpoint": "/var"})
    assert a != b


def test_statefulset_and_pvc_alerts_name_their_resource():
    assert _alert_resources("KubeStatefulSetReplicasMismatch", {"statefulset": "db"}, "app") == [
        {"kind": "StatefulSet", "name": "db", "namespace": "app"}
    ]
    assert _alert_resources("KubePersistentVolumeFillingUp", {"persistentvolumeclaim": "data"}, "app")[0]["kind"] == (
        "PersistentVolumeClaim"
    )


def test_pod_alerts_stay_fixable_shaped():
    assert _alert_resources("KubePodCrashLooping", {"pod": "web-0"}, "app") == [
        {"kind": "Pod", "name": "web-0", "namespace": "app"}
    ]


def test_two_security_checks_under_one_heading_are_two_findings():
    from unittest.mock import patch

    from sre_agent.monitor.scanners import scan_security_posture

    summary = "  [!!] [WARNING] Pod Security: 3 privileged pods\n  [!!] [WARNING] Pod Security: 2 pods use hostPath\n"
    with patch("sre_agent.security_tools.get_security_summary") as tool:
        tool.func.return_value = summary
        findings = scan_security_posture()
    assert len(findings) == 2
    assert len({_finding_key(f) for f in findings}) == 2
