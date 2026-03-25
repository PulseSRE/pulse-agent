"""Kubernetes resource unit parsing.

Handles CPU (cores, millicores, microcores, nanocores) and memory
(bytes, Ki, Mi, Gi, Ti, and decimal k, M, G) quantities.
"""

from __future__ import annotations


def parse_cpu_millicores(value: str) -> int:
    """Parse a Kubernetes CPU quantity to millicores.

    Supports: nanocores (n), microcores (u), millicores (m),
    whole cores (integer), and fractional cores (e.g. "0.5").
    Returns 0 for empty or unparseable input.
    """
    if not value:
        return 0
    try:
        if value.endswith("n"):
            return int(value[:-1]) // 1_000_000
        if value.endswith("u"):
            return int(value[:-1]) // 1_000
        if value.endswith("m"):
            return int(value[:-1])
        return int(float(value) * 1000)
    except (ValueError, ArithmeticError):
        return 0


def parse_memory_bytes(value: str) -> int:
    """Parse a Kubernetes memory quantity to bytes.

    Supports binary suffixes (Ki, Mi, Gi, Ti) and decimal suffixes (k, M, G, T),
    plus raw byte integers. Returns 0 for empty or unparseable input.
    """
    if not value:
        return 0
    try:
        # Binary suffixes (IEC)
        if value.endswith("Ki"):
            return int(value[:-2]) * 1024
        if value.endswith("Mi"):
            return int(value[:-2]) * 1024 * 1024
        if value.endswith("Gi"):
            return int(value[:-2]) * 1024 * 1024 * 1024
        if value.endswith("Ti"):
            return int(value[:-2]) * 1024 * 1024 * 1024 * 1024
        # Decimal suffixes (SI)
        if value.endswith("k"):
            return int(value[:-1]) * 1000
        if value.endswith("M"):
            return int(value[:-1]) * 1000_000
        if value.endswith("G"):
            return int(value[:-1]) * 1000_000_000
        if value.endswith("T"):
            return int(value[:-1]) * 1000_000_000_000
        # Exponential notation or raw bytes
        return int(float(value))
    except (ValueError, ArithmeticError):
        return 0


def format_cpu(millicores: int) -> str:
    """Format millicores to a human-readable string."""
    if millicores >= 1000 and millicores % 1000 == 0:
        return f"{millicores // 1000}"
    return f"{millicores}m"


def format_memory(mem_bytes: int) -> str:
    """Format bytes to a human-readable string (Mi preferred)."""
    if mem_bytes == 0:
        return "0Mi"
    mi = mem_bytes // (1024 * 1024)
    if mi > 0:
        return f"{mi}Mi"
    ki = mem_bytes // 1024
    if ki > 0:
        return f"{ki}Ki"
    return f"{mem_bytes}B"
