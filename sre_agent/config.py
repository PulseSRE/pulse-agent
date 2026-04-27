"""Startup configuration with Pydantic v2 Settings."""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("pulse_agent")


# --- Nested sub-models ---


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str = "claude-opus-4-6"
    max_tokens: int = 16000
    harness: bool = True
    memory: bool = True
    prompt_experiment: str = ""
    dev_user: str = ""
    tool_timeout: int = 30
    cb_threshold: int = 3
    cb_timeout: float = 60.0
    token_forwarding: bool = True


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: str = ""
    pool_min: int = 2
    pool_max: int = 20


class MonitorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    scan_interval: int = 60
    crashloop_threshold: int = 3
    max_daily_investigations: int = 20
    investigations_max_per_scan: int = 2
    investigation_timeout: int = 20
    investigation_cooldown: int = 300
    autofix_enabled: bool = True
    security_followup: bool = False
    noise_threshold: float = 0.7
    max_trust_level: int = 3
    investigation_categories: str = (
        "crashloop,workloads,nodes,alerts,cert_expiry,scheduling,oom,image_pull,operators,daemonsets,hpa"
    )
    max_concurrent_investigations: int = 3


class RoutingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    multi_skill: bool = True
    multi_skill_threshold: float = 0.15
    multi_skill_max: int = 2
    chain_hints: bool = True
    chain_min_probability: float = 0.6
    chain_min_frequency: int = 3
    temporal_cache_ttl: int = 60


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    host: str = "0.0.0.0"
    port: int = 8080
    socket: str = ""
    ws_token: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    max_conversation_messages: int = 50
    max_agent_sessions: int = 20
    max_monitor_clients: int = 50
    user_skills_dir: str = "/tmp/pulse_agent/skills"
    log_format: str = "json"
    log_level: str = "INFO"
    trusted_registries: str = (
        "registry.redhat.io,registry.access.redhat.com,quay.io,image-registry.openshift-image-registry.svc"
    )


class PrometheusConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    thanos_url: str = ""
    acm_thanos_url: str = ""
    acm_thanos_enabled: bool | None = None
    insecure: bool = False


# --- Main settings class ---


class PulseAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PULSE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Nested sub-models (new canonical access path)
    agent: AgentConfig = AgentConfig()
    database: DatabaseConfig = DatabaseConfig()
    monitor: MonitorConfig = MonitorConfig()
    routing: RoutingConfig = RoutingConfig()
    server: ServerConfig = ServerConfig()
    prometheus: PrometheusConfig = PrometheusConfig()

    # --- Flat fields (env-var backed, synced to nested in model_post_init) ---

    # Agent
    model: str = "claude-opus-4-6"
    max_tokens: int = 16000
    harness: bool = True
    memory: bool = True
    prompt_experiment: str = ""
    dev_user: str = ""
    cb_threshold: int = 3
    cb_timeout: float = 60.0
    tool_timeout: int = 30
    token_forwarding: bool = True

    # Database
    database_url: str = ""
    db_pool_min: int = 2
    db_pool_max: int = 20

    # Monitor
    scan_interval: int = 60
    crashloop_threshold: int = 3
    max_daily_investigations: int = 20
    investigations_max_per_scan: int = 2
    investigation_timeout: int = 20
    investigation_cooldown: int = 300
    autofix_enabled: bool = True
    security_followup: bool = False
    noise_threshold: float = 0.7
    max_trust_level: int = 3
    investigation_categories: str = (
        "crashloop,workloads,nodes,alerts,cert_expiry,scheduling,oom,image_pull,operators,daemonsets,hpa"
    )
    max_concurrent_investigations: int = 3

    # Routing
    multi_skill: bool = True
    multi_skill_threshold: float = 0.15
    multi_skill_max: int = 2
    chain_hints: bool = True
    chain_min_probability: float = 0.6
    chain_min_frequency: int = 3
    temporal_cache_ttl: int = 60

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    socket: str = ""
    ws_token: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    max_conversation_messages: int = 50
    max_agent_sessions: int = 20
    max_monitor_clients: int = 50
    user_skills_dir: str = "/tmp/pulse_agent/skills"
    log_format: str = "json"
    log_level: str = "INFO"
    trusted_registries: str = (
        "registry.redhat.io,registry.access.redhat.com,quay.io,image-registry.openshift-image-registry.svc"
    )

    # Prometheus
    thanos_url: str = ""
    acm_thanos_url: str = ""
    acm_thanos_enabled: bool | None = None
    prometheus_insecure: bool = False

    def model_post_init(self, __context: object) -> None:
        """Sync flat env-parsed fields into nested sub-models."""
        self.agent = AgentConfig(
            model=self.model,
            max_tokens=self.max_tokens,
            harness=self.harness,
            memory=self.memory,
            prompt_experiment=self.prompt_experiment,
            dev_user=self.dev_user,
            tool_timeout=self.tool_timeout,
            cb_threshold=self.cb_threshold,
            cb_timeout=self.cb_timeout,
            token_forwarding=self.token_forwarding,
        )
        self.database = DatabaseConfig(
            url=self.database_url,
            pool_min=self.db_pool_min,
            pool_max=self.db_pool_max,
        )
        self.monitor = MonitorConfig(
            scan_interval=self.scan_interval,
            crashloop_threshold=self.crashloop_threshold,
            max_daily_investigations=self.max_daily_investigations,
            investigations_max_per_scan=self.investigations_max_per_scan,
            investigation_timeout=self.investigation_timeout,
            investigation_cooldown=self.investigation_cooldown,
            autofix_enabled=self.autofix_enabled,
            security_followup=self.security_followup,
            noise_threshold=self.noise_threshold,
            max_trust_level=self.max_trust_level,
            investigation_categories=self.investigation_categories,
            max_concurrent_investigations=self.max_concurrent_investigations,
        )
        self.routing = RoutingConfig(
            multi_skill=self.multi_skill,
            multi_skill_threshold=self.multi_skill_threshold,
            multi_skill_max=self.multi_skill_max,
            chain_hints=self.chain_hints,
            chain_min_probability=self.chain_min_probability,
            chain_min_frequency=self.chain_min_frequency,
            temporal_cache_ttl=self.temporal_cache_ttl,
        )
        self.server = ServerConfig(
            host=self.host,
            port=self.port,
            socket=self.socket,
            ws_token=self.ws_token,
            webhook_url=self.webhook_url,
            webhook_secret=self.webhook_secret,
            max_conversation_messages=self.max_conversation_messages,
            max_agent_sessions=self.max_agent_sessions,
            max_monitor_clients=self.max_monitor_clients,
            user_skills_dir=self.user_skills_dir,
            log_format=self.log_format,
            log_level=self.log_level,
            trusted_registries=self.trusted_registries,
        )
        self.prometheus = PrometheusConfig(
            thanos_url=self.thanos_url,
            acm_thanos_url=self.acm_thanos_url,
            acm_thanos_enabled=self.acm_thanos_enabled,
            insecure=self.prometheus_insecure,
        )

    def get_trusted_registries(self) -> list[str]:
        return [s.strip() for s in self.trusted_registries.split(",") if s.strip()]

    @field_validator("model")
    @classmethod
    def model_must_be_claude(cls, v: str) -> str:
        if not v.startswith("claude"):
            raise ValueError(f"Model '{v}' doesn't look like a Claude model")
        return v

    @field_validator("cb_timeout")
    @classmethod
    def cb_timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("cb_timeout must be > 0")
        return v

    @field_validator("tool_timeout")
    @classmethod
    def tool_timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("tool_timeout must be > 0")
        return v


# Singleton
_settings: PulseAgentSettings | None = None


def get_settings() -> PulseAgentSettings:
    global _settings
    if _settings is None:
        _settings = PulseAgentSettings()
    return _settings


def _reset_settings() -> None:
    """Reset the singleton — for testing only."""
    global _settings
    _settings = None


def validate_config() -> None:
    """Validate config on startup. Raises SystemExit on error."""
    try:
        _reset_settings()
        get_settings()

        # Check AI backend (these are Claude SDK env vars, not PULSE_AGENT_ prefixed)
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        has_vertex = bool(os.getenv("ANTHROPIC_VERTEX_PROJECT_ID"))
        if not has_key and not has_vertex:
            logger.critical("Config error: Must set ANTHROPIC_API_KEY or ANTHROPIC_VERTEX_PROJECT_ID")
            raise SystemExit(1)

    except SystemExit:
        raise
    except Exception as e:
        logger.critical("Config error: %s", e)
        raise SystemExit(1)
