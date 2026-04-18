"""Tests for recovery analysis and scoring."""
from datetime import date, datetime, timedelta

import pytest

from recovery.analysis.recovery import (
    RecoveryStatus,
    RecommendedIntensity,
    DailySnapshot,
    assess,
    get_snapshot,
    get_trend,
    get_recent_activities,
    build_workout_context,
)
from recovery.config import Config, RecoveryConfig, EquipmentConfig
from recovery.db.models import GarminDaily, StravaActivity


def _snap(**kwargs) -> DailySnapshot:
    defaults = dict(
        date=date(2024, 1, 14),
        hrv_rmssd=55.0,
        hrv_status="BALANCED",
        hrv_baseline_low=48.0,
        hrv_baseline_high=62.0,
        resting_hr=48,
        sleep_score=80,
        sleep_duration_min=480,
        overnight_stress_avg=20.0,
        overnight_stress_qualifier="restful",
        body_battery_start=85,
    )
    defaults.update(kwargs)
    return DailySnapshot(**defaults)


def _cfg(**kwargs) -> Config:
    cfg = Config()
    for k, v in kwargs.items():
        setattr(cfg.recovery, k, v)
    return cfg


# ── assess() ──────────────────────────────────────────────────────────────

def test_assess_excellent_recovery():
    snap = _snap(
        hrv_rmssd=70.0,          # well above baseline mid (55)
        hrv_baseline_low=48.0,
        hrv_baseline_high=62.0,
        sleep_score=90,
        sleep_duration_min=510,
        overnight_stress_avg=18.0,
    )
    result = assess(snap)
    assert result.status == RecoveryStatus.EXCELLENT
    assert result.recommended_intensity == RecommendedIntensity.HARD


def test_assess_good_recovery():
    snap = _snap(
        hrv_rmssd=58.0,          # slightly above baseline mid (55)
        sleep_score=75,
        overnight_stress_avg=22.0,
    )
    result = assess(snap)
    assert result.status in (RecoveryStatus.GOOD, RecoveryStatus.EXCELLENT)
    assert result.recommended_intensity in (RecommendedIntensity.MODERATE, RecommendedIntensity.HARD)


def test_assess_poor_hrv_drives_down():
    snap = _snap(
        hrv_rmssd=40.0,          # 40/55 = 0.73 — below 0.85 threshold
        hrv_baseline_low=48.0,
        hrv_baseline_high=62.0,
        sleep_score=55,
        overnight_stress_avg=50.0,
    )
    result = assess(snap)
    assert result.status in (RecoveryStatus.POOR, RecoveryStatus.MODERATE)


def test_assess_no_data():
    result = assess(None)
    assert result.status == RecoveryStatus.NO_DATA
    assert result.recommended_intensity == RecommendedIntensity.REST
    assert len(result.warnings) > 0


def test_assess_poor_sleep_triggers_warning():
    snap = _snap(sleep_score=40, sleep_duration_min=300)  # 5h
    result = assess(snap)
    assert any("sleep" in w.lower() for w in result.warnings)


def test_assess_good_overnight_stress_adds_signal():
    snap = _snap(overnight_stress_avg=15.0)  # below low threshold (25)
    result = assess(snap)
    assert any("stress" in s.lower() for s in result.signals)


def test_assess_elevated_overnight_stress_triggers_warning():
    snap = _snap(overnight_stress_avg=50.0)  # above high threshold (45)
    result = assess(snap)
    assert any("stress" in w.lower() for w in result.warnings)


def test_assess_hrv_pct_calculated_correctly():
    # baseline mid = (48 + 62) / 2 = 55, rmssd = 66 → 66/55 = 1.2
    snap = _snap(hrv_rmssd=66.0, hrv_baseline_low=48.0, hrv_baseline_high=62.0)
    result = assess(snap)
    assert result.hrv_vs_baseline_pct == pytest.approx(66 / 55, rel=0.01)


def test_assess_falls_back_to_hrv_status_string():
    snap = _snap(hrv_rmssd=None, hrv_baseline_low=None, hrv_baseline_high=None, hrv_status="POOR")
    result = assess(snap)
    assert any("poor" in s.lower() for s in result.signals + result.warnings)


# ── get_snapshot() ────────────────────────────────────────────────────────

def test_get_snapshot_returns_row(populated_db):
    snap = get_snapshot(populated_db, date(2024, 1, 14))
    assert snap is not None
    assert snap.hrv_rmssd == 55.0
    assert snap.sleep_score == 78


def test_get_snapshot_missing_date(populated_db):
    snap = get_snapshot(populated_db, date(2020, 1, 1))
    assert snap is None


# ── get_trend() ───────────────────────────────────────────────────────────

def test_get_trend_returns_correct_count(populated_db):
    snapshots = get_trend(populated_db, days=7)
    assert len(snapshots) <= 7


def test_get_trend_ordered_ascending(populated_db):
    snapshots = get_trend(populated_db, days=14)
    dates = [s.date for s in snapshots]
    assert dates == sorted(dates)


# ── get_recent_activities() ───────────────────────────────────────────────

def test_get_recent_activities_returns_list(populated_db):
    acts = get_recent_activities(populated_db, days=30)
    assert isinstance(acts, list)
    assert all("sport_type" in a for a in acts)
    assert all("duration_min" in a for a in acts)


def test_get_recent_activities_duration_converted_to_minutes(populated_db):
    acts = get_recent_activities(populated_db, days=30)
    for a in acts:
        if a["duration_min"] is not None:
            assert a["duration_min"] == 60  # 3600 sec → 60 min


def test_get_recent_activities_distance_converted_to_km(populated_db):
    acts = get_recent_activities(populated_db, days=30)
    for a in acts:
        if a["distance_km"] is not None:
            assert a["distance_km"] == 10.0  # 10000m → 10km


# ── build_workout_context() ───────────────────────────────────────────────

def test_build_workout_context_structure(populated_db, config_toml, monkeypatch):
    import recovery.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg", cfg_mod.load(config_toml))

    ctx = build_workout_context(populated_db, day=date(2024, 1, 13))
    assert "recovery_status" in ctx
    assert "equipment" in ctx
    assert "recent_activities" in ctx
    assert "sauna_available" in ctx
    assert ctx["sauna_available"] is True


def test_build_workout_context_equipment_summary(populated_db, config_toml, monkeypatch):
    import recovery.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg", cfg_mod.load(config_toml))

    ctx = build_workout_context(populated_db, day=date(2024, 1, 13))
    assert "sauna" in ctx["equipment"].lower()
    assert "squat rack" in ctx["equipment"].lower()
