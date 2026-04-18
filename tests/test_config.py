"""Tests for recovery.config"""
import pytest
from recovery import config as cfg_mod
from recovery.config import Config, EquipmentConfig, load


def test_load_returns_defaults_when_no_file(tmp_path):
    cfg = load(tmp_path / "nonexistent.toml")
    assert isinstance(cfg, Config)
    assert cfg.user.name == "User"
    assert cfg.ui.port == 8080


def test_load_parses_all_sections(config_toml):
    cfg = load(config_toml)
    assert cfg.user.name == "Tester"
    assert cfg.user.timezone == "America/New_York"
    assert cfg.garmin.email == "test@example.com"
    assert cfg.strava.client_id == "12345"
    assert cfg.strava.client_secret == "secret"
    assert cfg.sync.backfill_days == 14
    assert cfg.ui.port == 8080
    assert cfg.ui.default_trend_days == 30


def test_load_equipment(config_toml):
    cfg = load(config_toml)
    eq = cfg.equipment
    assert eq.sauna is True
    assert eq.squat_rack is True
    assert eq.dumbbells is True
    assert eq.pullup_bar is True
    assert eq.cable_machine is False
    assert eq.dumbbell_max_kg == 40.0


def test_load_recovery_thresholds(config_toml):
    cfg = load(config_toml)
    rc = cfg.recovery
    assert rc.hrv_low_pct == 0.85
    assert rc.hrv_high_pct == 1.10
    assert rc.sleep_min_hours == 7.0
    assert rc.overnight_stress_low == 25
    assert rc.overnight_stress_high == 45


def test_equipment_available_lists_true_items(config_toml):
    cfg = load(config_toml)
    available = cfg.equipment.available()
    assert "sauna" in available
    assert "squat_rack" in available
    assert "cable_machine" not in available


def test_equipment_summary_includes_dumbbell_weight(config_toml):
    cfg = load(config_toml)
    summary = cfg.equipment.summary()
    assert "40" in summary
    assert "dumbbell" in summary.lower()


def test_equipment_summary_no_equipment():
    eq = EquipmentConfig()
    assert eq.summary() == "No equipment configured."


def test_equipment_summary_no_dumbbells():
    eq = EquipmentConfig(squat_rack=True, pullup_bar=True)
    summary = eq.summary()
    assert "squat rack" in summary
    assert "pullup bar" in summary
    assert "dumbbell" not in summary
