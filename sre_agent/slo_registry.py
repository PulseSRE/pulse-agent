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


@dataclass
class SLOStatus:
    """Current state of an SLO."""

    definition: SLODefinition
    current_value: float = 0.0
    error_budget_remaining: float = 1.0  # 0.0 = exhausted, 1.0 = full
    burn_rate: float = 0.0  # current burn rate (1.0 = normal)
    alert_level: str = "ok"  # "ok" | "warning" | "critical"


class SLORegistry:
    """Manages SLO definitions and monitors burn rates."""

    def __init__(self):
        self._slos: dict[str, SLODefinition] = {}

    def register(self, slo: SLODefinition) -> None:
        key = f"{slo.service_name}:{slo.slo_type}"
        self._slos[key] = slo
        logger.info(
            "Registered SLO: %s %s target=%.3f",
            slo.service_name,
            slo.slo_type,
            slo.target,
        )

    def unregister(self, service_name: str, slo_type: str) -> bool:
        key = f"{service_name}:{slo_type}"
        if key in self._slos:
            del self._slos[key]
            return True
        return False

    def get(self, service_name: str, slo_type: str) -> SLODefinition | None:
        return self._slos.get(f"{service_name}:{slo_type}")

    def list_all(self) -> list[SLODefinition]:
        return list(self._slos.values())

    def check_burn_rate(self, slo: SLODefinition, current_value: float) -> SLOStatus:
        """Evaluate current burn rate for an SLO.

        Args:
            slo: The SLO definition
            current_value: Current measured value (e.g., 0.998 for 99.8% availability)
        """
        error_budget_total = 1.0 - slo.target  # e.g., 0.001 for 99.9% SLO
        if error_budget_total <= 0:
            return SLOStatus(definition=slo, alert_level="ok")

        error_used = max(0, slo.target - current_value)
        error_budget_remaining = max(0, 1.0 - (error_used / error_budget_total))
        burn_rate = error_used / error_budget_total if error_budget_total > 0 else 0

        if error_budget_remaining < 0.1:
            alert_level = "critical"
        elif error_budget_remaining < 0.3:
            alert_level = "warning"
        else:
            alert_level = "ok"

        return SLOStatus(
            definition=slo,
            current_value=current_value,
            error_budget_remaining=round(error_budget_remaining, 4),
            burn_rate=round(burn_rate, 4),
            alert_level=alert_level,
        )

    def evaluate_all(self, current_values: dict[str, float]) -> list[SLOStatus]:
        """Evaluate all registered SLOs against current values.

        Args:
            current_values: Map of "service:type" -> current measured value
        """
        results: list[SLOStatus] = []
        for key, slo in self._slos.items():
            value = current_values.get(key, slo.target)  # Assume target if no data
            results.append(self.check_burn_rate(slo, value))
        return results

    def get_context_for_selector(self) -> str:
        """Generate context text for the skill selector about SLO status."""
        try:
            statuses = self.evaluate_all({})  # No live data in this context
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
    return _registry
