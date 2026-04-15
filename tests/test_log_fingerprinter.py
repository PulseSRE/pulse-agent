"""Tests for log fingerprint extraction."""

from sre_agent.log_fingerprinter import fingerprint_finding, fingerprint_text


class TestFingerprintText:
    def test_oom_detection(self):
        text = "Container killed due to OOMKilled, memory limit 256Mi exceeded"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "oom" in categories

    def test_connection_refused(self):
        text = "dial tcp 10.0.0.1:5432: connection refused"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "connection" in categories

    def test_timeout(self):
        text = "context deadline exceeded while waiting for response"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "timeout" in categories

    def test_auth_failure(self):
        text = "401 Unauthorized: invalid bearer token"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "auth" in categories

    def test_crash_panic(self):
        text = "panic: runtime error: index out of range [5] with length 3"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "crash" in categories

    def test_python_traceback(self):
        text = "Traceback (most recent call last):\n  File 'app.py', line 42"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "crash" in categories

    def test_config_missing(self):
        text = "FileNotFoundError: /etc/config/database.yaml no such file or directory"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "config" in categories

    def test_image_pull(self):
        text = "Failed to pull image: ErrImagePull: manifest unknown"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "image" in categories

    def test_dns_failure(self):
        text = "could not resolve host: api.example.com NXDOMAIN"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "dns" in categories

    def test_storage_full(self):
        text = "write /data/log: no space left on device"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "storage" in categories

    def test_multiple_patterns(self):
        text = "OOMKilled after connection refused to database, timeout exceeded"
        fps = fingerprint_text(text)
        categories = [fp["category"] for fp in fps]
        assert "oom" in categories
        assert "connection" in categories
        assert "timeout" in categories

    def test_empty_text(self):
        assert fingerprint_text("") == []
        assert fingerprint_text(None) == []

    def test_no_matches(self):
        text = "INFO: Server started successfully on port 8080"
        assert fingerprint_text(text) == []

    def test_count_ordering(self):
        text = "timeout timeout timeout OOMKilled"
        fps = fingerprint_text(text)
        assert fps[0]["category"] == "timeout"
        assert fps[0]["count"] == 3

    def test_skill_hint(self):
        text = "401 Unauthorized"
        fps = fingerprint_text(text)
        assert fps[0]["skill_hint"] == "security"

    def test_resource_routes_to_capacity(self):
        text = "insufficient cpu to schedule pod"
        fps = fingerprint_text(text)
        assert fps[0]["skill_hint"] == "capacity_planner"


class TestFingerprintFinding:
    def test_finding_with_summary(self):
        finding = {
            "title": "Pod crashlooping",
            "summary": "Container OOMKilled after exceeding memory limit",
            "resources": [],
        }
        fps = fingerprint_finding(finding)
        categories = [fp["category"] for fp in fps]
        assert "oom" in categories

    def test_finding_empty(self):
        finding = {"title": "", "summary": "", "resources": []}
        assert fingerprint_finding(finding) == []
