"""Tests for sre_agent.config — startup validation."""

import os
import pytest
from unittest.mock import patch

from sre_agent.config import validate_config


class TestValidateConfig:
    def test_valid_config_with_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            validate_config()  # Should not raise

    def test_valid_config_with_vertex(self):
        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "my-project", "CLOUD_ML_REGION": "us-east5"}, clear=False):
            validate_config()  # Should not raise

    def test_missing_api_key_and_vertex(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID")}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit):
                validate_config()

    def test_negative_cb_timeout(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "PULSE_AGENT_CB_TIMEOUT": "-1"}):
            with pytest.raises(SystemExit):
                validate_config()

    def test_zero_cb_threshold(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "PULSE_AGENT_CB_THRESHOLD": "0"}):
            with pytest.raises(SystemExit):
                validate_config()

    def test_invalid_model_name(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "PULSE_AGENT_MODEL": "gpt-4"}):
            with pytest.raises(SystemExit):
                validate_config()

    def test_non_numeric_timeout(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "PULSE_AGENT_CB_TIMEOUT": "abc"}):
            with pytest.raises(SystemExit):
                validate_config()

    def test_negative_tool_timeout(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "PULSE_AGENT_TOOL_TIMEOUT": "-5"}):
            with pytest.raises(SystemExit):
                validate_config()
