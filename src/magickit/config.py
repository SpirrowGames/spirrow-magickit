"""Configuration management for Magickit using Pydantic Settings."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceConfig(BaseSettings):
    """Configuration for an external service."""

    url: str
    timeout: float = 30.0


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
    lexora_url: str = Field(default="http://localhost:8001")
    lexora_timeout: float = Field(default=30.0)

    cognilens_url: str = Field(default="http://localhost:8003")
    cognilens_timeout: float = Field(default=30.0)

    prismind_url: str = Field(default="http://localhost:8002")
    prismind_timeout: float = Field(default=30.0)

    unrealwise_url: str = Field(default="http://localhost:8005")
    unrealwise_timeout: float = Field(default=60.0)

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
    webhook_timeout: float = Field(default=10.0)
    webhook_max_retries: int = Field(default=3)

    # Phase 2: WebSocket settings
    ws_heartbeat_interval: int = Field(default=30)

    # MCP Server settings
    mcp_port: int = Field(default=8114)

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

        with open(config_path) as f:
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
