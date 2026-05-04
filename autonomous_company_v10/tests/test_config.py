from __future__ import annotations
import pytest
from pathlib import Path
from autonomous_company.config import Settings, init_settings, get_settings

def test_default_values():
    s = Settings()
    assert s.ceo_model == "claude-opus-4-7"
    assert s.step_max_retries == 3
    assert s.step_retry_backoff_base == 2.0
    assert s.default_budget_usd == 5.00
    assert s.max_hop_count == 5
    assert s.otel_enabled is False
    assert s.log_format == "console"

def test_env_var_override(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_STEP_MAX_RETRIES", "7")
    monkeypatch.setenv("AUTONOMOUS_CEO_MODEL", "claude-haiku-4-5-20251001")
    s = Settings()
    assert s.step_max_retries == 7
    assert s.ceo_model == "claude-haiku-4-5-20251001"

def test_get_settings_raises_before_init():
    import autonomous_company.config as cfg_module
    original = cfg_module._settings
    cfg_module._settings = None
    try:
        with pytest.raises(RuntimeError, match="not initialised"):
            get_settings()
    finally:
        cfg_module._settings = original

def test_init_settings_returns_same_object():
    s1 = init_settings(override=Settings())
    s2 = get_settings()
    assert s1 is s2

def test_init_settings_with_override():
    override = Settings(ceo_model="claude-haiku-4-5-20251001", step_max_retries=1)
    result = init_settings(override=override)
    assert result.ceo_model == "claude-haiku-4-5-20251001"
    assert result.step_max_retries == 1

def test_settings_all_fields_have_defaults():
    s = Settings()
    assert s.sqlite_path is not None
    assert s.prompts_dir is not None
    assert s.tools_allowlist_path is not None
    assert s.cors_origins is not None

def test_env_file_parsing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("AUTONOMOUS_STEP_MAX_RETRIES=9\nAUTONOMOUS_CEO_MODEL=test-model\n")
    s = Settings(_env_file=str(env_file))
    assert s.step_max_retries == 9
    assert s.ceo_model == "test-model"
