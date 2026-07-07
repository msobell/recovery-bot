"""Tests for Garmin ingest module (garminconnect API mocked)."""
from __future__ import annotations

from datetime import date, datetime
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
        "lastNightAvg": 55.3,
        "baseline": {"lowUpper": 48.0, "balancedUpper": 62.0},
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
    # Overnight stress is the sleep-window average, read from the sleep DTO.
    mock_api.get_sleep_data.return_value = {
        "dailySleepDTO": {
            "avgSleepStress": 22.5,
            "sleepScores": {"stress": {"qualifierKey": "restful"}},
        }
    }
    result = garmin.fetch_overnight_stress(mock_api, date(2024, 1, 1))
    assert result["overnight_stress_avg"] == 22.5
    assert result["overnight_stress_qualifier"] == "restful"


def test_fetch_overnight_stress_returns_empty_on_error(mock_api):
    mock_api.get_sleep_data.side_effect = Exception("error")
    assert garmin.fetch_overnight_stress(mock_api, date(2024, 1, 1)) == {}


# ── fetch_body_battery ────────────────────────────────────────────────────

def test_fetch_body_battery_falls_back_to_daily_max(mock_api):
    # No wake time → daily peak is the best proxy for the wake value
    mock_api.get_body_battery.return_value = [
        {"bodyBatteryValuesArray": [[0, 30], [1, 85], [2, 60]]}
    ]
    result = garmin.fetch_body_battery(mock_api, date(2024, 1, 1))
    assert result["body_battery_start"] == 85


def test_fetch_body_battery_picks_reading_nearest_wake(mock_api):
    # Series: mid-sleep low (14), wake peak (72), midday drain (40).
    # With a wake time near the 72 reading, we must get 72 — not the low, and
    # not a later/earlier value.
    wake = 8_000_000
    mock_api.get_body_battery.return_value = [
        {"bodyBatteryValuesArray": [
            [1_000_000, 14],       # mid-sleep low
            [wake + 60_000, 72],   # ~1 min after wake — the peak
            [20_000_000, 40],      # midday
        ]}
    ]
    result = garmin.fetch_body_battery(mock_api, date(2024, 1, 1), wake_ms=wake)
    assert result["body_battery_start"] == 72


def test_fetch_body_battery_wake_far_from_readings_uses_max(mock_api):
    # Wake time with no reading within ±90 min → fall back to daily max
    mock_api.get_body_battery.return_value = [
        {"bodyBatteryValuesArray": [[1_000_000, 30], [2_000_000, 88]]}
    ]
    result = garmin.fetch_body_battery(mock_api, date(2024, 1, 1), wake_ms=500_000_000)
    assert result["body_battery_start"] == 88


def test_fetch_body_battery_returns_empty_on_error(mock_api):
    mock_api.get_body_battery.side_effect = Exception("error")
    assert garmin.fetch_body_battery(mock_api, date(2024, 1, 1)) == {}


# ── fetch_cardio_activities ───────────────────────────────────────────────

def test_fetch_cardio_activities_parses_non_strength(mock_api):
    mock_api.get_activities_by_date.return_value = [
        {
            "activityId": 555, "activityName": "Cardio",
            "activityType": {"typeKey": "indoor_cardio"},
            "startTimeLocal": "2026-07-01 16:53:54",
            "duration": 3550.0, "distance": 0.0,
            "averageHR": 129.0, "maxHR": 172.0, "calories": 492.0,
        },
    ]
    result = garmin.fetch_cardio_activities(mock_api, date(2026, 7, 1))
    assert len(result) == 1
    a = result[0]
    assert a["garmin_id"] == 555
    assert a["sport_type"] == "indoor_cardio"
    assert a["start_time"] == datetime(2026, 7, 1, 16, 53, 54)
    assert a["duration_sec"] == 3550
    assert a["avg_hr"] == 129.0
    assert a["max_hr"] == 172
    assert a["calories"] == 492
    assert a["distance_m"] is None  # 0.0 → None


def test_fetch_cardio_activities_excludes_strength(mock_api):
    mock_api.get_activities_by_date.return_value = [
        {"activityId": 1, "activityType": {"typeKey": "strength_training"}, "startTimeLocal": "2026-07-01 12:00:00"},
        {"activityId": 2, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-07-01 07:00:00", "distance": 5000.0},
    ]
    result = garmin.fetch_cardio_activities(mock_api, date(2026, 7, 1))
    assert [a["garmin_id"] for a in result] == [2]
    assert result[0]["distance_m"] == 5000.0


def test_fetch_cardio_activities_returns_empty_on_error(mock_api):
    mock_api.get_activities_by_date.side_effect = Exception("boom")
    assert garmin.fetch_cardio_activities(mock_api, date(2026, 7, 1)) == []


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
