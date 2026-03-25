"""Tests for Kubernetes resource unit parsing."""

import pytest

from sre_agent.units import (
    parse_cpu_millicores,
    parse_memory_bytes,
    format_cpu,
    format_memory,
)


class TestParseCpuMillicores:
    def test_nanocores(self):
        assert parse_cpu_millicores("500000000n") == 500

    def test_microcores(self):
        assert parse_cpu_millicores("500000u") == 500

    def test_millicores(self):
        assert parse_cpu_millicores("250m") == 250

    def test_whole_cores(self):
        assert parse_cpu_millicores("4") == 4000

    def test_fractional_cores(self):
        assert parse_cpu_millicores("0.5") == 500
        assert parse_cpu_millicores("1.5") == 1500
        assert parse_cpu_millicores("0.1") == 100

    def test_zero(self):
        assert parse_cpu_millicores("0") == 0

    def test_empty(self):
        assert parse_cpu_millicores("") == 0

    def test_garbage(self):
        assert parse_cpu_millicores("abc") == 0

    def test_small_nanocores(self):
        assert parse_cpu_millicores("100n") == 0  # rounds down to 0m


class TestParseMemoryBytes:
    def test_ki(self):
        assert parse_memory_bytes("1024Ki") == 1024 * 1024

    def test_mi(self):
        assert parse_memory_bytes("256Mi") == 256 * 1024 * 1024

    def test_gi(self):
        assert parse_memory_bytes("16Gi") == 16 * 1024 * 1024 * 1024

    def test_ti(self):
        assert parse_memory_bytes("1Ti") == 1024 * 1024 * 1024 * 1024

    def test_decimal_k(self):
        assert parse_memory_bytes("1000k") == 1_000_000

    def test_decimal_m(self):
        assert parse_memory_bytes("500M") == 500_000_000

    def test_decimal_g(self):
        assert parse_memory_bytes("2G") == 2_000_000_000

    def test_raw_bytes(self):
        assert parse_memory_bytes("1048576") == 1048576

    def test_exponential(self):
        assert parse_memory_bytes("1e9") == 1_000_000_000

    def test_zero(self):
        assert parse_memory_bytes("0") == 0

    def test_empty(self):
        assert parse_memory_bytes("") == 0

    def test_garbage(self):
        assert parse_memory_bytes("xyz") == 0


class TestFormatCpu:
    def test_millicores(self):
        assert format_cpu(250) == "250m"

    def test_whole_cores(self):
        assert format_cpu(4000) == "4"

    def test_zero(self):
        assert format_cpu(0) == "0m"

    def test_not_even_cores(self):
        assert format_cpu(1500) == "1500m"


class TestFormatMemory:
    def test_mi(self):
        assert format_memory(256 * 1024 * 1024) == "256Mi"

    def test_ki(self):
        assert format_memory(512 * 1024) == "512Ki"

    def test_zero(self):
        assert format_memory(0) == "0Mi"

    def test_bytes(self):
        assert format_memory(500) == "500B"

    def test_gi_as_mi(self):
        assert format_memory(4 * 1024 * 1024 * 1024) == "4096Mi"
