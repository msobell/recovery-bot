"""Tests for Garmin ingest module (garminconnect API mocked)."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from recovery.ingest import garmin


@pytest.fixture()
def mock_api():
    """A MagicMock standing in for a garminconnect.Garmin instance."""
    return MagicMock()


@pytest.fixture(autouse=True)
def patch_token_dir(tmp_path, monkeypatch):
    """Redirect token storage to tmp so tests never touch ~/.recovery-bot."""
    token_dir = tmp_path / "garmin_tokens"
    token_dir.mkdir()
    monkeypatch.setattr(garmin, "_TOKEN_DIR", token_dir)


# ── fetch_hrv ─────────────────────────────────────────────────────────────

def test_fetch_hrv_parses_response(mock_api):
    mock_api.get_hrv_data.return_value = {"hrvSummary": {
        "status": "BALANCED",
        "lastNight": 55.3,
        "baselineLowUpper": 48.0,
        "baselineBalancedUpper": 62.0,
    }}
    result = garmin.fetch_hrv(mock_api, date(2024, 1, 1))
    assert result["hrv_status"] == "BALANCED"
    assert result["hrv_rmssd"] == 55.3
    assert result["hrv_baseline_low"] == 48.0
    assert result["hrv_baseline_high"] == 62.0


def test_fetch_hrv_returns_empty_on_error(mock_api):
    mock_api.get_hrv_data.side_effect = Exception("API error")
    assert garmin.fetch_hrv(mock_api, date(2024, 1, 1)) == {}


def test_fetch_hrv_returns_none_when_no_summary(mock_api):
    mock_api.get_hrv_data.return_value = {}
    result = garmin.fetch_hrv(mock_api, date(2024, 1, 1))
    assert result.get("hrv_rmssd") is None


def test_fetch_hrv_handles_none_response(mock_api):
    mock_api.get_hrv_data.return_value = None
    result = garmin.fetch_hrv(mock_api, date(2024, 1, 1))
    assert result.get("hrv_rmssd") is None


# ── fetch_sleep ───────────────────────────────────────────────────────────

def test_fetch_sleep_parses_response(mock_api):
    mock_api.get_sleep_data.return_value = {"dailySleepDTO": {
        "sleepStartTimestampLocal": 1704150600000,
        "sleepEndTimestampLocal":   1704178800000,
        "sleepTimeSeconds": 28800,   # 8h → 480min
        "deepSleepSeconds": 5400,    # 90min
        "lightSleepSeconds": 14400,  # 240min
        "remSleepSeconds": 7200,     # 120min
        "awakeSleepSeconds": 1800,   # 30min
        "sleepScores": {"overall": {"value": 82}},
    }}
    result = garmin.fetch_sleep(mock_api, date(2024, 1, 2))
    assert result["sleep_duration_min"] == 480
    assert result["sleep_deep_min"] == 90
    assert result["sleep_score"] == 82


def test_fetch_sleep_returns_empty_on_error(mock_api):
    mock_api.get_sleep_data.side_effect = Exception("404")
    assert garmin.fetch_sleep(mock_api, date(2024, 1, 1)) == {}


# ── fetch_rhr ─────────────────────────────────────────────────────────────

def test_fetch_rhr_parses_response(mock_api):
    mock_api.get_rhr_day.return_value = {
        "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 48}]}}
    }
    result = garmin.fetch_rhr(mock_api, date(2024, 1, 1))
    assert result["resting_hr"] == 48


def test_fetch_rhr_returns_empty_on_error(mock_api):
    mock_api.get_rhr_day.side_effect = Exception("timeout")
    assert garmin.fetch_rhr(mock_api, date(2024, 1, 1)) == {}


# ── fetch_overnight_stress ────────────────────────────────────────────────

def test_fetch_overnight_stress_parses_response(mock_api):
    mock_api.get_stress_data.return_value = {
        "avgStressLevel": 22.5,
        "stressQualifier": "restful",
    }
    result = garmin.fetch_overnight_stress(mock_api, date(2024, 1, 1))
    assert result["overnight_stress_avg"] == 22.5
    assert result["overnight_stress_qualifier"] == "restful"


def test_fetch_overnight_stress_returns_empty_on_error(mock_api):
    mock_api.get_stress_data.side_effect = Exception("error")
    assert garmin.fetch_overnight_stress(mock_api, date(2024, 1, 1)) == {}


# ── fetch_body_battery ────────────────────────────────────────────────────

def test_fetch_body_battery_parses_first_reading(mock_api):
    mock_api.get_body_battery.return_value = [
        {"bodyBatteryValuesArray": [[0, 85], [1, 80]]}
    ]
    result = garmin.fetch_body_battery(mock_api, date(2024, 1, 1))
    assert result["body_battery_start"] == 85


def test_fetch_body_battery_returns_empty_on_error(mock_api):
    mock_api.get_body_battery.side_effect = Exception("error")
    assert garmin.fetch_body_battery(mock_api, date(2024, 1, 1)) == {}


# ── fetch_day ─────────────────────────────────────────────────────────────

def test_fetch_day_merges_all_sources(mock_api):
    with patch.object(garmin, "load_session", return_value=mock_api), \
         patch.object(garmin, "fetch_hrv", return_value={"hrv_rmssd": 55.0}), \
         patch.object(garmin, "fetch_sleep", return_value={"sleep_score": 80}), \
         patch.object(garmin, "fetch_rhr", return_value={"resting_hr": 48}), \
         patch.object(garmin, "fetch_overnight_stress", return_value={"overnight_stress_avg": 20.0}), \
         patch.object(garmin, "fetch_body_battery", return_value={"body_battery_start": 85}):
        result = garmin.fetch_day(date(2024, 1, 1))

    assert result["date"] == date(2024, 1, 1)
    assert result["hrv_rmssd"] == 55.0
    assert result["sleep_score"] == 80
    assert result["resting_hr"] == 48
    assert result["overnight_stress_avg"] == 20.0
    assert result["body_battery_start"] == 85


def test_fetch_day_accepts_api_arg(mock_api):
    with patch.object(garmin, "fetch_hrv", return_value={}), \
         patch.object(garmin, "fetch_sleep", return_value={"sleep_score": 75}), \
         patch.object(garmin, "fetch_rhr", return_value={"resting_hr": 50}), \
         patch.object(garmin, "fetch_overnight_stress", return_value={}), \
         patch.object(garmin, "fetch_body_battery", return_value={}):
        result = garmin.fetch_day(date(2024, 1, 1), api=mock_api)

    assert result["sleep_score"] == 75
    assert result["resting_hr"] == 50
    assert result.get("hrv_rmssd") is None


def test_fetch_day_partial_failure_still_returns_available_data(mock_api):
    with patch.object(garmin, "load_session", return_value=mock_api), \
         patch.object(garmin, "fetch_hrv", return_value={}), \
         patch.object(garmin, "fetch_sleep", return_value={"sleep_score": 75}), \
         patch.object(garmin, "fetch_rhr", return_value={"resting_hr": 50}), \
         patch.object(garmin, "fetch_overnight_stress", return_value={}), \
         patch.object(garmin, "fetch_body_battery", return_value={}):
        result = garmin.fetch_day(date(2024, 1, 1))

    assert result["sleep_score"] == 75
    assert result["resting_hr"] == 50
    assert result.get("hrv_rmssd") is None
