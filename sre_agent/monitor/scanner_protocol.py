"""Scanner protocol for typed, self-describing scanners.

Each scanner declares its metadata (name, category, auto-fixability, scan
frequency) via a ``ScannerMeta`` dataclass. The ``Scanner`` protocol
defines the scan interface with an optional ``shared_resources`` dict
for pre-fetched K8s objects (e.g., shared pod list).

Existing scanner functions are wrapped in ``FunctionScanner`` for
backward compatibility.  New scanners can implement ``Scanner`` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ScannerMeta:
    """Metadata for a scanner — replaces the SCANNER_REGISTRY dict entries."""

    name: str
    display_name: str
    description: str
    category: str
    checks: list[str] = field(default_factory=list)
    auto_fixable: bool = False
    scan_every: int = 1

    @classmethod
    def from_registry(cls, name: str, entry: dict[str, Any]) -> ScannerMeta:
        """Build from a SCANNER_REGISTRY dict entry."""
        return cls(
            name=name,
            display_name=entry["displayName"],
            description=entry["description"],
            category=entry["category"],
            checks=entry.get("checks", []),
            auto_fixable=entry.get("auto_fixable", False),
            scan_every=entry.get("scan_every", 1),
        )


@runtime_checkable
class Scanner(Protocol):
    """Protocol for cluster health scanners."""

    meta: ScannerMeta

    def scan(self, shared_resources: dict[str, Any] | None = None) -> list[dict]: ...


class FunctionScanner:
    """Wraps an existing ``scan_*()`` function as a ``Scanner`` instance.

    Handles the pod-sharing convention: if ``accepts_pods`` is True,
    the scanner's first argument receives ``shared_resources["pods"]``.
    """

    def __init__(self, meta: ScannerMeta, fn: Callable[..., list[dict]], *, accepts_pods: bool = False):
        self.meta = meta
        self._fn = fn
        self._accepts_pods = accepts_pods

    def scan(self, shared_resources: dict[str, Any] | None = None) -> list[dict]:
        if self._accepts_pods and shared_resources and "pods" in shared_resources:
            return self._fn(shared_resources["pods"])
        return self._fn()
