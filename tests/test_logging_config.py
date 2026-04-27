"""Tests for sre_agent.logging_config — structured JSON logging."""

import json
import logging
import os
from unittest.mock import patch

from sre_agent.logging_config import configure_logging


class TestConfigureLogging:
    def setup_method(self):
        """Reset root logger handlers before each test."""
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def teardown_method(self):
        """Clean up root logger after each test."""
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_json_format_produces_valid_json(self, capsys):
        with patch.dict(os.environ, {"PULSE_AGENT_LOG_FORMAT": "json", "PULSE_AGENT_LOG_LEVEL": "INFO"}, clear=False):
            configure_logging()
            logger = logging.getLogger("test.json_output")
            logger.info("hello structured world")

        captured = capsys.readouterr()
        line = captured.err.strip()
        parsed = json.loads(line)

        assert parsed["message"] == "hello structured world"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.json_output"
        assert parsed["service"] == "pulse-agent"
        assert "timestamp" in parsed

    def test_log_level_filtering(self, capsys):
        with patch.dict(
            os.environ, {"PULSE_AGENT_LOG_FORMAT": "json", "PULSE_AGENT_LOG_LEVEL": "WARNING"}, clear=False
        ):
            configure_logging()
            logger = logging.getLogger("test.level_filter")
            logger.info("should be hidden")
            logger.warning("should be visible")

        captured = capsys.readouterr()
        lines = [line for line in captured.err.strip().splitlines() if line]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["message"] == "should be visible"

    def test_text_format_fallback(self, capsys):
        with patch.dict(os.environ, {"PULSE_AGENT_LOG_FORMAT": "text", "PULSE_AGENT_LOG_LEVEL": "INFO"}, clear=False):
            configure_logging()
            logger = logging.getLogger("test.text_output")
            logger.info("human readable message")

        captured = capsys.readouterr()
        line = captured.err.strip()

        # Should NOT be valid JSON
        try:
            json.loads(line)
            is_json = True
        except json.JSONDecodeError:
            is_json = False
        assert not is_json, f"Expected non-JSON output, got: {line}"

        # Should contain the human-readable markers (structlog uses lowercase level)
        assert "[info" in line.lower()
        assert "test.text_output" in line
        assert "human readable message" in line

    def test_default_format_is_json(self, capsys):
        """When PULSE_AGENT_LOG_FORMAT is not set, default to JSON."""
        env = {k: v for k, v in os.environ.items() if k != "PULSE_AGENT_LOG_FORMAT"}
        with patch.dict(os.environ, env, clear=True):
            configure_logging()
            logger = logging.getLogger("test.default_format")
            logger.info("default check")

        captured = capsys.readouterr()
        parsed = json.loads(captured.err.strip())
        assert parsed["service"] == "pulse-agent"

    def test_noisy_libraries_suppressed(self):
        with patch.dict(os.environ, {"PULSE_AGENT_LOG_FORMAT": "json", "PULSE_AGENT_LOG_LEVEL": "DEBUG"}, clear=False):
            configure_logging()

        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("kubernetes").level == logging.WARNING
