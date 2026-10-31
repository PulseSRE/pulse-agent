"""SLO/SLI registry — per-service SLO tracking with burn rate alerting.

Services define SLOs (availability, latency, error_rate) with targets and windows.
The registry monitors burn rates and generates alerts when error budgets are depleting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("pulse_agent.slo")


@dataclass
class SLODefinition:
    """A single SLO target."""

    service_name: str
    slo_type: str  # "availability" | "latency" | "error_rate"
    target: float  # e.g., 0.999 for 99.9% availability
    window_days: int = 30  # rolling window
    description: str = ""

    def __post_init__(self) -> None:
        # The API accepts a target "between 0 and 100", and 99.9 is how people
        # write an SLO. Taken literally it made the error budget negative, and
        # check_burn_rate answered "ok" for every such SLO forever.
        if self.target >= 1.0:
            self.target = round(self.target / 100.0, 9)


@dataclass
class SLOStatus:
    """Current state of an SLO."""

    definition: SLODefinition
    current_value: float = 0.0
    error_budget_remaining: float = 1.0  # 0.0 = exhausted, 1.0 = full
    burn_rate: float = 0.0  # multiple of the sustainable rate (1.0 = budget lasts exactly the window)
    alert_level: str = "ok"  # "ok" | "warning" | "critical"
    # Hours until the budget is gone at the current burn rate; None when not burning.
    budget_remaining_hours: float | None = None


# Multiwindow burn-rate thresholds from the Google SRE workbook: burning 14.4x
# spends 2% of a 30-day budget in an hour (page); 6x spends 5% in six hours.
CRITICAL_BURN = 14.4
WARNING_BURN = 6.0
# The short window the current burn rate is measured over.
BURN_WINDOW = "1h"


class SLORegistry:
    """Manages SLO definitions and monitors burn rates."""

    def __init__(self):
        self._slos: dict[str, SLODefinition] = {}

    def register(self, slo: SLODefinition, persist: bool = True) -> None:
        key = f"{slo.service_name}:{slo.slo_type}"
        self._slos[key] = slo
        logger.info(
            "Registered SLO: %s %s target=%.3f",
            slo.service_name,
            slo.slo_type,
            slo.target,
        )
        if persist:
            # Best-effort: the registry must keep working without a database
            # (tests, local dev) — but with one, registrations survive pod
            # restarts. Before this, API-registered SLOs were memory-only and
            # every restart silently reset the SLO stack to defaults.
            try:
                from .repositories import get_slo_repo

                repo = get_slo_repo()
                repo.save(slo.service_name, slo.slo_type, slo.target, slo.window_days, slo.description)
                # Registering again is how a deleted default comes back.
                repo.clear_tombstone(slo.service_name, slo.slo_type)
            except Exception:
                logger.debug("SLO persistence unavailable; registered in memory only", exc_info=True)

    def unregister(self, service_name: str, slo_type: str, persist: bool = True, deleted_by: str = "") -> bool:
        key = f"{service_name}:{slo_type}"
        if persist:
            try:
                from .repositories import get_slo_repo

                repo = get_slo_repo()
                repo.delete(service_name, slo_type)
                # Remembered so a default SLO is not registered again on the
                # next restart; see _register_defaults.
                repo.add_tombstone(service_name, slo_type, deleted_by)
            except Exception:
                logger.debug("SLO persistence unavailable; unregistered in memory only", exc_info=True)
        if key in self._slos:
            del self._slos[key]
            return True
        return False

    def get(self, service_name: str, slo_type: str) -> SLODefinition | None:
        return self._slos.get(f"{service_name}:{slo_type}")

    def list_all(self) -> list[SLODefinition]:
        return list(self._slos.values())

    def check_burn_rate(self, slo: SLODefinition, current_value: float, recent_value: float | None = None) -> SLOStatus:
        """Evaluate an SLO from its measured SLI.

        Args:
            slo: The SLO definition
            current_value: The SLI over the SLO window (e.g. 0.998 = 99.8% good)
            recent_value: The SLI over the last hour, for the current burn rate.
                Without it the window average is used.

        The error budget is the share of bad events the target allows (0.1% for
        99.9%). Budget consumed is the share actually bad over the window
        divided by that. This used to measure only the shortfall *below the
        target*, so a service at 99.95% against 99.9% showed a full budget when
        it had spent half of it.
        """
        error_budget_total = 1.0 - slo.target  # e.g., 0.001 for 99.9% SLO
        if error_budget_total <= 0:
            return SLOStatus(definition=slo, current_value=current_value, alert_level="ok")

        consumed = max(0.0, 1.0 - current_value) / error_budget_total
        error_budget_remaining = max(0.0, 1.0 - consumed)
        recent = current_value if recent_value is None else recent_value
        burn_rate = max(0.0, 1.0 - recent) / error_budget_total

        window_hours = slo.window_days * 24
        remaining_hours = (error_budget_remaining * window_hours / burn_rate) if burn_rate > 0 else None

        if error_budget_remaining < 0.1 or burn_rate >= CRITICAL_BURN:
            alert_level = "critical"
        elif error_budget_remaining < 0.3 or burn_rate >= WARNING_BURN:
            alert_level = "warning"
        else:
            alert_level = "ok"

        return SLOStatus(
            definition=slo,
            current_value=current_value,
            error_budget_remaining=round(error_budget_remaining, 4),
            burn_rate=round(burn_rate, 4),
            alert_level=alert_level,
            budget_remaining_hours=round(remaining_hours, 1) if remaining_hours is not None else None,
        )

    def evaluate_all(
        self, current_values: dict[str, float], recent_values: dict[str, float] | None = None
    ) -> list[SLOStatus]:
        """Evaluate all registered SLOs against current values.

        Args:
            current_values: Map of "service:type" -> SLI over the SLO window
            recent_values: Map of "service:type" -> SLI over the last hour
        """
        recent_values = recent_values or {}
        results: list[SLOStatus] = []
        for key, slo in self._slos.items():
            # No measurement is no verdict. Assuming the target here reported
            # every unmeasured SLO as healthy.
            if key not in current_values:
                continue
            results.append(self.check_burn_rate(slo, current_values[key], recent_values.get(key)))
        return results

    def query_prometheus_values(self, window: str | None = None) -> dict[str, float]:
        """Query Prometheus for current SLO metric values.

        Returns map of "service:type" -> SLI, over each SLO's own window or,
        when ``window`` is given (e.g. "1h"), over that window instead.
        """
        if not self._slos:
            return {}

        values: dict[str, float] = {}
        try:
            from .prometheus import get_prometheus_client

            client = get_prometheus_client()
        except Exception as e:
            _report_scan_failure(f"Prometheus not available for SLO queries: {e}")
            return values

        for key, slo in self._slos.items():
            query = self._build_prom_query(slo, window)
            if not query:
                continue
            # The raw API result, not the get_prometheus_query tool: that
            # returns a (text, component) tuple for the chat UI, which this
            # parser never matched — every SLO fell back to its target and
            # read "ok", so the burn-rate scanner could not fire.
            try:
                data = client.query(query, timeout=15)
            except Exception as e:
                _report_scan_failure(f"SLO query failed for {key}: {e}")
                continue
            if data.get("status") != "success":
                _report_scan_failure(f"SLO query failed for {key}: {data.get('error', 'unknown')}")
                continue
            series = data.get("data", {}).get("result", [])
            if not series:
                continue  # no data for this service: no verdict, not a pass
            try:
                values[key] = float(series[0]["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                logger.debug("Unparseable SLO query result for %s: %r", key, series[0])

        return values

    def _build_prom_query(self, slo: SLODefinition, window: str | None = None) -> str:
        """Build a PromQL query for an SLO's SLI: a good-events ratio in [0, 1].

        Uses kube-state-metrics and API-server metrics (scraped on every
        OpenShift cluster) rather than http_requests_total, which needs app
        instrumentation. A type with no honest ratio for this service returns
        "" (no data, no verdict). Restart *rates* used to stand in for latency
        and error ratios; compared with a 0.999 target they read as a 0.0 SLI,
        so every such SLO was permanently critical.
        """
        svc = slo.service_name
        window = window or f"{slo.window_days}d"
        apiserver = svc in ("kube-apiserver", "apiserver")
        if slo.slo_type == "availability":
            # Ready replicas over desired, averaged over the window.
            return (
                f"avg_over_time(kube_deployment_status_replicas_available"
                f'{{deployment="{svc}"}}[{window}]) / '
                f"avg_over_time(kube_deployment_spec_replicas"
                f'{{deployment="{svc}"}}[{window}])'
            )
        if slo.slo_type == "latency" and apiserver:
            # Fraction of (non-watch) API requests served under 1s.
            return (
                f'sum(rate(apiserver_request_duration_seconds_bucket{{le="1",verb!~"WATCH|CONNECT"}}[{window}])) / '
                f'sum(rate(apiserver_request_duration_seconds_count{{verb!~"WATCH|CONNECT"}}[{window}]))'
            )
        if slo.slo_type == "error_rate" and apiserver:
            # Fraction of API requests that did not fail with a 5xx.
            return (
                f'1 - (sum(rate(apiserver_request_total{{code=~"5.."}}[{window}])) / '
                f"sum(rate(apiserver_request_total[{window}])))"
            )
        return ""

    def evaluate_with_prometheus(self) -> list[SLOStatus]:
        """Evaluate all SLOs using live Prometheus data: the window, and the last hour."""
        values = self.query_prometheus_values()
        if not values:
            return []
        recent = self.query_prometheus_values(BURN_WINDOW)
        return self.evaluate_all(values, recent)

    def get_context_for_selector(self) -> str:
        """Generate context text for the skill selector about SLO status."""
        try:
            statuses = self.evaluate_with_prometheus()
            alerts = [s for s in statuses if s.alert_level != "ok"]
            if not alerts:
                return ""

            lines = ["### SLO Alerts"]
            for s in alerts:
                lines.append(
                    f"- {s.definition.service_name} {s.definition.slo_type}: "
                    f"budget {s.error_budget_remaining:.0%} remaining ({s.alert_level})"
                )
            return "\n".join(lines)
        except Exception:
            return ""


# Singleton
_registry: SLORegistry | None = None


def get_slo_registry() -> SLORegistry:
    global _registry
    if _registry is None:
        _registry = SLORegistry()
        _register_defaults(_registry, skip=_load_tombstones())
        _load_persisted(_registry)
    return _registry


def _load_persisted(registry: SLORegistry) -> None:
    """Reload SLOs from the database (memory-only: they're already persisted).

    Runs after defaults so a persisted definition for the same service:type
    (e.g. an operator retuned a target through the API) wins over the default.
    """
    try:
        from .repositories import get_slo_repo

        rows = get_slo_repo().fetch_all()
    except Exception:
        logger.debug("SLO persistence unavailable; using in-memory defaults only", exc_info=True)
        return
    for row in rows:
        registry.register(SLODefinition(**row), persist=False)
    if rows:
        logger.info("Loaded %d persisted SLOs", len(rows))


def _load_tombstones() -> set[str]:
    """``service:type`` keys an operator deleted; empty when there is no database."""
    try:
        from .repositories import get_slo_repo

        return get_slo_repo().fetch_tombstones()
    except Exception:
        logger.debug("SLO tombstones unavailable; registering every default", exc_info=True)
        return set()


def _register_defaults(registry: SLORegistry, skip: set[str] | None = None) -> None:
    """Register default SLOs for known Pulse Agent services, except ones an operator deleted."""
    defaults = [
        SLODefinition(
            service_name="pulse-openshift-sre-agent",
            slo_type="availability",
            target=0.999,
            window_days=30,
            description="Agent API must be available 99.9% over rolling 30 days",
        ),
        SLODefinition(
            service_name="openshiftpulse",
            slo_type="availability",
            target=0.999,
            window_days=30,
            description="UI must be available 99.9% over rolling 30 days",
        ),
        SLODefinition(
            service_name="pulse-openshift-sre-agent-postgresql",
            slo_type="availability",
            target=0.999,
            window_days=30,
            description="PostgreSQL must be available 99.9% over rolling 30 days",
        ),
        SLODefinition(
            service_name="pulse-openshift-sre-agent-mcp",
            slo_type="availability",
            target=0.999,
            window_days=30,
            description="OpenShift MCP server must be available 99.9% over rolling 30 days",
        ),
    ]
    registered = 0
    for slo in defaults:
        if skip and f"{slo.service_name}:{slo.slo_type}" in skip:
            logger.info("Default SLO %s %s stays deleted", slo.service_name, slo.slo_type)
            continue
        # persist=False: defaults are code, not operator state — writing them
        # to the DB would resurrect any default an operator later deletes.
        registry.register(slo, persist=False)
        registered += 1
    logger.info("Registered %d default SLOs", registered)


def list_slo_definitions() -> list[dict]:
    """Return all registered SLO definitions as dicts."""
    registry = get_slo_registry()
    return [
        {
            "id": f"{slo.service_name}:{slo.slo_type}",
            "service_name": slo.service_name,
            "slo_type": slo.slo_type,
            "target": slo.target,
            "window_days": slo.window_days,
            "description": slo.description,
        }
        for slo in registry._slos.values()
    ]


def query_slo_burn_rate(slo_id: str) -> dict | None:
    """Measured burn rate for one SLO by id (service:type), or None without data.

    This returned ``window_days * 24 * (1 - target)`` — 0.72 for every 99.9%
    SLO, whatever the service was doing — labelled as hours of budget left.
    """
    registry = get_slo_registry()
    parts = slo_id.split(":", 1)
    if len(parts) != 2:
        return None
    slo = registry.get(parts[0], parts[1])
    if not slo:
        return None
    for status in registry.evaluate_with_prometheus():
        if status.definition is slo:
            return {
                "slo_id": slo_id,
                "target": slo.target,
                "window_days": slo.window_days,
                "current_value": status.current_value,
                "error_budget_remaining": status.error_budget_remaining,
                "burn_rate": status.burn_rate,
                "alert_level": status.alert_level,
                "budget_remaining_hours": status.budget_remaining_hours,
            }
    return None


def _report_scan_failure(message: str) -> None:
    """Mark the running scanner degraded (no-op outside a monitor scan)."""
    logger.debug(message)
    try:
        from .monitor.scanner_health import report_failure

        report_failure(message)
    except Exception:
        logger.debug("Could not report SLO scan failure", exc_info=True)
