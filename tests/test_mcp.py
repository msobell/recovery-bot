"""Tests for MCP tool implementations."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from recovery.db.models import GarminDaily, StravaActivity
from recovery.mcp import server as mcp_server


@pytest.fixture(autouse=True)
def patch_db(db_engine, populated_db, monkeypatch):
    """Wire MCP server to the in-memory test DB."""
    from recovery.db.session import get_session
    monkeypatch.setattr(mcp_server, "_session", lambda: get_session(db_engine))


@pytest.fixture()
def cfg_patch(config_toml, monkeypatch):
    import recovery.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg", cfg_mod.load(config_toml))


# ── get_today_status ──────────────────────────────────────────────────────

def test_get_today_status_returns_expected_keys(cfg_patch):
    result = mcp_server.get_today_status()
    assert "recovery_status" in result
    assert "recommended_intensity" in result
    assert "signals" in result
    assert "warnings" in result
    assert "as_of_date" in result


def test_get_today_status_valid_status_value(cfg_patch):
    result = mcp_server.get_today_status()
    valid = {"Excellent", "Good", "Moderate", "Poor", "No Data"}
    assert result["recovery_status"] in valid


def test_get_today_status_metrics_present_when_data_exists(cfg_patch):
    result = mcp_server.get_today_status()
    # populated_db has data up to 2024-01-14; as_of_date is yesterday relative to today
    # The metrics key should exist if data was found
    if result["recovery_status"] != "No Data":
        assert "metrics" in result


# ── get_recovery_trend ────────────────────────────────────────────────────

def test_get_recovery_trend_default_14_days():
    result = mcp_server.get_recovery_trend()
    assert "data" in result
    assert "days_requested" in result
    assert result["days_requested"] == 14


def test_get_recovery_trend_custom_days():
    result = mcp_server.get_recovery_trend(days=7)
    assert result["days_requested"] == 7
    assert result["days_available"] <= 7


def test_get_recovery_trend_data_has_required_fields():
    result = mcp_server.get_recovery_trend(days=14)
    for row in result["data"]:
        assert "date" in row
        assert "hrv_rmssd" in row
        assert "sleep_score" in row
        assert "resting_hr" in row


def test_get_recovery_trend_direction_is_valid():
    result = mcp_server.get_recovery_trend(days=14)
    assert result["hrv_trend_direction"] in ("improving", "stable", "declining")


# ── get_recent_activities ─────────────────────────────────────────────────

def test_get_recent_activities_structure():
    result = mcp_server.get_recent_activities(days=30)
    assert "activities" in result
    assert "activity_count" in result
    assert "total_suffer_score" in result
    assert result["activity_count"] == len(result["activities"])


def test_get_recent_activities_fields():
    result = mcp_server.get_recent_activities(days=30)
    for act in result["activities"]:
        assert "sport_type" in act
        assert "duration_min" in act


# ── recommend_workout ─────────────────────────────────────────────────────

def test_recommend_workout_returns_context(cfg_patch):
    result = mcp_server.recommend_workout()
    assert "recovery_status" in result
    assert "equipment" in result
    assert "instruction" in result
    assert "recent_activities" in result
    assert "sauna_available" in result


def test_recommend_workout_includes_sauna_when_configured(cfg_patch):
    result = mcp_server.recommend_workout()
    assert result["sauna_available"] is True


def test_recommend_workout_instruction_is_nonempty(cfg_patch):
    result = mcp_server.recommend_workout()
    assert len(result["instruction"]) > 50


# ── get_training_load ─────────────────────────────────────────────────────

def test_get_training_load_structure():
    result = mcp_server.get_training_load()
    assert "acute_7day" in result
    assert "chronic" in result
    assert "chronic_window_days" in result
    assert result["chronic_window_days"] == 28


def test_get_training_load_acute_has_expected_keys():
    result = mcp_server.get_training_load()
    acute = result["acute_7day"]
    assert "activity_count" in acute
    assert "total_duration_hours" in acute
    assert "total_suffer_score" in acute
    assert "sport_breakdown" in acute


# ── query_date_range ──────────────────────────────────────────────────────

def test_query_date_range_hrv():
    result = mcp_server.query_date_range("hrv", "2024-01-01", "2024-01-14")
    assert result["metric"] == "hrv"
    for row in result["data"]:
        assert "hrv_rmssd" in row
        assert "hrv_status" in row


def test_query_date_range_sleep():
    result = mcp_server.query_date_range("sleep", "2024-01-01", "2024-01-14")
    assert result["metric"] == "sleep"
    for row in result["data"]:
        assert "sleep_score" in row


def test_query_date_range_rhr():
    result = mcp_server.query_date_range("rhr", "2024-01-01", "2024-01-14")
    for row in result["data"]:
        assert "resting_hr" in row


def test_query_date_range_stress():
    result = mcp_server.query_date_range("stress", "2024-01-01", "2024-01-14")
    for row in result["data"]:
        assert "overnight_stress_avg" in row


def test_query_date_range_activities():
    result = mcp_server.query_date_range("activities", "2024-01-01", "2024-01-14")
    assert result["metric"] == "activities"
    for row in result["data"]:
        assert "sport_type" in row
        assert "duration_min" in row


def test_query_date_range_empty_window():
    result = mcp_server.query_date_range("hrv", "2010-01-01", "2010-01-07")
    assert result["data"] == []
