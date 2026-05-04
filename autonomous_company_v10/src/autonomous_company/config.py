"""Centralised configuration for autonomous_company_v10."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTONOMOUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ceo_model: str = "claude-opus-4-7"
    worker_model: str = "claude-sonnet-4-6"

    default_budget_usd: float = 5.00
    ceo_budget_usd: float = 2.00

    max_roles: int = 10
    drain_reaction_max_turns: int = 20
    max_plan_hitl_chain: int = 3
    snapshot_max_entries: int = 500

    step_max_retries: int = 3
    step_retry_backoff_base: float = 2.0
    llm_max_retries: int = 2
    step_timeout_sec: int = 300
    inactivity_timeout_sec: int = 600
    heartbeat_sec: int = 30

    message_ttl_seconds: int = 3600
    max_hop_count: int = 5
    default_ttl_sec: int = 120

    sqlite_path: Path = Path("./data/company.db")
    companies_root: Path = Path("./companies")
    prompts_dir: Path = Path("./prompts")
    tools_allowlist_path: Path = Path("./config/tools_allowlist.yaml")

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"
    otel_enabled: bool = False
    otel_exporter: Literal["console", "otlp", "none"] = "none"
    otel_otlp_endpoint: str = "http://localhost:4317"
    syslog_host: str | None = None
    llm_debug: bool = False

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    worker_permission_mode: Literal[
        "default", "acceptEdits", "bypassPermissions"
    ] = "default"


_settings: Settings | None = None


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError(
            "Settings not initialised. Call init_settings() from cli.py before use."
        )
    return _settings


def init_settings(override: Settings | None = None) -> Settings:
    global _settings
    if override is not None:
        _settings = override
    else:
        _settings = Settings()
    return _settings
