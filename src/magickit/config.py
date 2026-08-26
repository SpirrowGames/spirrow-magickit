"""Configuration management for Magickit using Pydantic Settings."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceConfig(BaseSettings):
    """Configuration for an external service."""

    url: str
    timeout: float = 60.0


class DatabaseConfig(BaseSettings):
    """Database configuration."""

    path: str = "data/magickit.db"


class LoggingConfig(BaseSettings):
    """Logging configuration."""

    level: str = "INFO"
    format: str = "json"


class TaskQueueConfig(BaseSettings):
    """Task queue configuration."""

    max_concurrent: int = 5
    default_priority: int = 5
    max_retries: int = 3


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_prefix="MAGICKIT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8004
    debug: bool = False

    # Service URLs (can be overridden via env vars)
    # Service timeouts cover the worst-case latency for each backend.
    # Light reads (list_tasks, get_project_status) finish in <3s; the
    # higher ceilings exist for heavy paths like smart_create_document
    # where Drive API + BGE-M3 embedding + Qdrant write run serially.
    lexora_url: str = Field(default="http://localhost:8001")
    lexora_timeout: float = Field(default=240.0)

    cognilens_url: str = Field(default="http://localhost:8003")
    cognilens_timeout: float = Field(default=240.0)

    prismind_url: str = Field(default="http://localhost:8002")
    prismind_timeout: float = Field(default=360.0)

    unrealwise_url: str = Field(default="http://localhost:8005")
    unrealwise_timeout: float = Field(default=120.0)

    conclair_url: str = Field(default="http://localhost:8115")
    conclair_timeout: float = Field(default=30.0)

    # Database
    db_path: str = Field(default="data/magickit.db")

    # Logging
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    # Task Queue
    task_max_concurrent: int = Field(default=5)
    task_default_priority: int = Field(default=5)
    task_max_retries: int = Field(default=3)

    # Phase 2: Authentication
    jwt_secret: str = Field(default="change-me-in-production-use-strong-secret")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expire_minutes: int = Field(default=60)
    jwt_refresh_expire_days: int = Field(default=7)
    auth_enabled: bool = Field(default=True)

    # Phase 2: Webhook settings
    webhook_timeout: float = Field(default=20.0)
    webhook_max_retries: int = Field(default=3)

    # Phase 2: WebSocket settings
    ws_heartbeat_interval: int = Field(default=30)

    # MCP Server settings
    mcp_port: int = Field(default=8114)

    # Project archive settings
    archive_path: str = Field(default="data/archives")

    # Chatroom "design-decide naysayer gate" (binding design threads tagged
    # with `naysayer_gate_tag` may only be closed when a fresh independent
    # naysayer review approves, or a human override is supplied). See
    # src/magickit/mcp/tools/chatroom.py for the enforcement.
    naysayer_gate_enabled: bool = Field(default=True)
    naysayer_gate_tag: str = Field(default="gate:naysayer")
    # v1 identification of the independent naysayer is a configurable
    # allowlist (no Prismind independence_class read path exists yet; migrate
    # to `independence_class == "independent"` when one lands). Authors in
    # this list are treated as the independent naysayer whose review the gate
    # requires.
    naysayer_identities: list[str] = Field(default_factory=lambda: ["Einstein"])

    # Ops view: how long a project may go without any chatroom activity or
    # loop heartbeat before the page calls it stalled. Long enough to sit
    # through an implementation turn (minutes) plus a slow review, short
    # enough that "it died overnight" is not discovered the next morning.
    # This is a *suspicion* threshold, not a fact -- see web/ops.py.
    ops_stall_minutes: int = Field(default=30)

    # Chatroom thread digests. Magickit is the producer (Cognilens -> Lexora
    # `light`); Conclair stores and renders. See core/digest_producer.py.
    #
    # Two enable flags, not one, because they are two different risks:
    # unattended GPU use (the sweeper) and attended GPU use (the button).
    # Shipping the sweeper off and the button on is what makes the feature
    # evaluable before it is trusted.
    digest_on_demand_enabled: bool = Field(default=True)
    # The only thing in Magickit that would start consuming the local GPU
    # with nobody asking, on the same card as the loop that writes code.
    digest_sweeper_enabled: bool = Field(default=False)
    digest_sweep_interval_minutes: int = Field(default=15)

    # Per-cycle budget. 5 threads run sequentially, so worst case is a few
    # minutes out of every 15 -- the GPU is free the rest of the time. The
    # per-project cap stops one busy project from spending the whole budget
    # and starving the others (paired with a round-robin cursor).
    digest_max_threads_per_cycle: int = Field(default=5)
    digest_max_threads_per_project_per_cycle: int = Field(default=2)
    # One GPU, one vLLM instance: concurrency buys no throughput and only
    # makes the coding loop's own requests queue behind these.
    digest_max_concurrency: int = Field(default=1)

    # Floors below which a digest is worse than the original. A propose plus
    # one reply already *is* the shortest accurate account of itself.
    # Message count alone lies (six one-line acks vs two 8KB essays), so
    # both apply.
    digest_min_msg_count: int = Field(default=4)
    digest_min_input_chars: int = Field(default=1200)
    # A single new msg makes a digest stale, so without a floor one busy
    # thread would take the whole budget every cycle.
    digest_min_redigest_minutes: int = Field(default=60)

    # Input ceiling, derived from a *timeout* rather than a context length.
    # The binding limit is not Lexora's light tier (60s) but Cognilens's own
    # `llm.timeout: 30` with `max_retries: 3` -- an over-long prompt spends
    # ~120s of GPU and returns nothing. Japanese runs >= 1 token/char, so
    # 24k chars is already a 24k-token prefill. Raise
    # spirrow-cognilens/config.yaml's llm.timeout before raising this.
    digest_max_input_chars: int = Field(default=24000)
    # Where the elision goes when a thread does not fit: keep the propose
    # (why the thread exists) and the latest exchange (where it is stuck).
    digest_head_chars_ratio: float = Field(default=0.6)
    # Per-message cap, so one pasted log cannot eat the head budget.
    digest_max_msg_chars: int = Field(default=4000)

    # Cognilens summarize arguments. `bullet` does not fit the dashboard's
    # one-line slot and `detailed` loses the point at this compression.
    digest_style: str = Field(default="concise")
    digest_max_tokens: int = Field(default=400)
    # Cut-off for one summarize call: Cognilens's own worst case (30s x 4
    # attempts) plus slack. Deliberately *not* `cognilens_timeout` (240s),
    # which is sized for real document work and would outlive the sweep
    # interval -- the same mistake ops.PROBE_TIMEOUT documents.
    digest_summarize_timeout_seconds: float = Field(default=150.0)

    # Which threads the sweeper considers. `resolved` is excluded by default:
    # those digests would never go stale (nothing more can be posted), but
    # turning them on means the first sweep grinds through a 3,000+ message
    # archive nobody asked about. A backfill is a script, not a default.
    digest_include_statuses: list[str] = Field(
        default_factory=lambda: ["active", "awaiting_reply"]
    )

    # Per-thread failure backoff. The only thing stopping one structurally
    # undigestable thread from burning the budget every cycle forever.
    digest_failure_backoff_minutes: int = Field(default=30)
    digest_failure_backoff_max_minutes: int = Field(default=720)
    digest_max_consecutive_failures: int = Field(default=5)

    # Outer bound on the on-demand path (read + summarize + PUT), so the
    # person who pressed the button is not left waiting forever. Must stay
    # longer than digest_summarize_timeout_seconds.
    digest_on_demand_timeout_seconds: float = Field(default=180.0)

    # How much of a digest the ops dashboard shows inline (full text in the
    # title attribute). 20 rows x 5 lines stops being scannable in one
    # screen, which is that page's entire claim.
    digest_dashboard_chars: int = Field(default=160)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load settings from a YAML config file.

        Args:
            config_path: Path to the YAML configuration file.

        Returns:
            Settings instance with values from the YAML file.
        """
        config_path = Path(config_path)
        if not config_path.exists():
            return cls()

        # encoding is explicit because open()'s default is locale-dependent, and
        # the config carries non-ASCII. CI never sees this (ubuntu defaults to
        # UTF-8) but any host whose locale says otherwise -- a Windows-JP box
        # (cp932), or a minimal Linux container with LC_ALL=C (ASCII) -- raises
        # UnicodeDecodeError here before a single setting is read.
        with open(config_path, encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}

        # Flatten the nested YAML structure
        flat_config: dict[str, Any] = {}

        # Server settings
        if server := yaml_config.get("server"):
            flat_config["host"] = server.get("host", "0.0.0.0")
            flat_config["port"] = server.get("port", 8004)
            flat_config["debug"] = server.get("debug", False)

        # Service settings
        if services := yaml_config.get("services"):
            for name, cfg in services.items():
                if cfg:
                    flat_config[f"{name}_url"] = cfg.get("url")
                    flat_config[f"{name}_timeout"] = cfg.get("timeout")

        # Database settings
        if database := yaml_config.get("database"):
            flat_config["db_path"] = database.get("path")

        # Logging settings
        if logging_cfg := yaml_config.get("logging"):
            flat_config["log_level"] = logging_cfg.get("level")
            flat_config["log_format"] = logging_cfg.get("format")

        # Task queue settings
        if task_queue := yaml_config.get("task_queue"):
            flat_config["task_max_concurrent"] = task_queue.get("max_concurrent")
            flat_config["task_default_priority"] = task_queue.get("default_priority")
            flat_config["task_max_retries"] = task_queue.get("max_retries")

        # Phase 2: Authentication settings
        if auth := yaml_config.get("auth"):
            flat_config["jwt_secret"] = auth.get("jwt_secret")
            flat_config["jwt_algorithm"] = auth.get("jwt_algorithm")
            flat_config["jwt_expire_minutes"] = auth.get("jwt_expire_minutes")
            flat_config["jwt_refresh_expire_days"] = auth.get("jwt_refresh_expire_days")
            # Support both "enabled" (YAML style) and "auth_enabled" (flat style)
            if "enabled" in auth:
                flat_config["auth_enabled"] = auth.get("enabled")
            elif "auth_enabled" in auth:
                flat_config["auth_enabled"] = auth.get("auth_enabled")

        # Phase 2: Webhook settings
        if webhook := yaml_config.get("webhook"):
            flat_config["webhook_timeout"] = webhook.get("timeout")
            flat_config["webhook_max_retries"] = webhook.get("max_retries")

        # Phase 2: WebSocket settings
        if websocket := yaml_config.get("websocket"):
            flat_config["ws_heartbeat_interval"] = websocket.get("heartbeat_interval")

        # MCP Server settings
        if mcp := yaml_config.get("mcp"):
            flat_config["mcp_port"] = mcp.get("port")

        # Archive settings
        if archive := yaml_config.get("archive"):
            flat_config["archive_path"] = archive.get("path")

        # Chatroom naysayer-gate settings
        if naysayer_gate := yaml_config.get("naysayer_gate"):
            if "enabled" in naysayer_gate:
                flat_config["naysayer_gate_enabled"] = naysayer_gate.get("enabled")
            flat_config["naysayer_gate_tag"] = naysayer_gate.get("tag")
            flat_config["naysayer_identities"] = naysayer_gate.get("identities")

        # Ops view settings
        if ops := yaml_config.get("ops"):
            flat_config["ops_stall_minutes"] = ops.get("stall_minutes")

        # Chatroom thread digests. `False` survives the None-strip below, so
        # `sweeper_enabled: false` in YAML does take effect.
        if digest := yaml_config.get("digest"):
            for yaml_key, field in (
                ("on_demand_enabled", "digest_on_demand_enabled"),
                ("sweeper_enabled", "digest_sweeper_enabled"),
                ("sweep_interval_minutes", "digest_sweep_interval_minutes"),
                ("max_threads_per_cycle", "digest_max_threads_per_cycle"),
                (
                    "max_threads_per_project_per_cycle",
                    "digest_max_threads_per_project_per_cycle",
                ),
                ("max_concurrency", "digest_max_concurrency"),
                ("min_msg_count", "digest_min_msg_count"),
                ("min_input_chars", "digest_min_input_chars"),
                ("min_redigest_minutes", "digest_min_redigest_minutes"),
                ("max_input_chars", "digest_max_input_chars"),
                ("head_chars_ratio", "digest_head_chars_ratio"),
                ("max_msg_chars", "digest_max_msg_chars"),
                ("style", "digest_style"),
                ("max_tokens", "digest_max_tokens"),
                ("summarize_timeout_seconds", "digest_summarize_timeout_seconds"),
                ("include_statuses", "digest_include_statuses"),
                ("failure_backoff_minutes", "digest_failure_backoff_minutes"),
                ("failure_backoff_max_minutes", "digest_failure_backoff_max_minutes"),
                ("max_consecutive_failures", "digest_max_consecutive_failures"),
                ("on_demand_timeout_seconds", "digest_on_demand_timeout_seconds"),
                ("dashboard_chars", "digest_dashboard_chars"),
            ):
                flat_config[field] = digest.get(yaml_key)

        # Remove None values
        flat_config = {k: v for k, v in flat_config.items() if v is not None}

        return cls(**flat_config)


def get_settings() -> Settings:
    """Get application settings.

    Loads from config file if available, with environment variable overrides.

    Returns:
        Settings instance.
    """
    config_path = Path("config/magickit_config.yaml")
    if config_path.exists():
        return Settings.from_yaml(config_path)
    return Settings()
