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


def test_query_date_range_unknown_metric_returns_error():
    result = mcp_server.query_date_range("heartrate", "2024-01-01", "2024-01-14")
    assert "error" in result
    assert "hrv" in result["error"]  # names the valid metrics


def test_query_date_range_malformed_date_returns_error():
    result = mcp_server.query_date_range("hrv", "01/01/2024", "2024-01-14")
    assert "error" in result
    assert "YYYY-MM-DD" in result["error"]


# ── get_today_status: partial row ─────────────────────────────────────────

def test_get_today_status_empty_row_is_no_data(db_engine, cfg_patch):
    """A GarminDaily row with no metrics (partial early-morning sync) must
    score No Data, not Moderate-off-zero-evidence."""
    from sqlalchemy.orm import sessionmaker
    with sessionmaker(bind=db_engine)() as s:
        s.add(GarminDaily(date=date.today(), synced_at=datetime.now()))
        s.commit()
    result = mcp_server.get_today_status()
    assert result["recovery_status"] == "No Data"


# ── Strength fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def strength_db(db_engine, populated_db):
    """Recent strength sessions (relative to today) + weight entries."""
    from sqlalchemy.orm import sessionmaker
    from recovery.db.models import GarminActivity, GarminStrengthSet, WeightEntry

    today = date.today()
    with sessionmaker(bind=db_engine)() as s:
        s.add(GarminActivity(
            garmin_id=501, date=today - timedelta(days=1), name="Push Day",
            sport_type="strength_training", duration_sec=3600, avg_hr=112,
            synced_at=datetime.now(),
        ))
        s.add(GarminActivity(
            garmin_id=502, date=today - timedelta(days=3), name="Pull Day",
            sport_type="strength_training", duration_sec=2700, avg_hr=105,
            synced_at=datetime.now(),
        ))
        s.add_all([
            GarminStrengthSet(
                garmin_activity_id=501, set_index=0, exercise_category="BENCH_PRESS",
                reps=5, weight_g=83915,  # ≈185 lbs
                start_time=datetime.combine(today - timedelta(days=1), datetime.min.time()),
            ),
            # NULL start_time — regression for the datetime/date sort crash
            GarminStrengthSet(
                garmin_activity_id=501, set_index=1, exercise_category="BENCH_PRESS",
                reps=5, weight_g=83915, start_time=None,
            ),
            # Override takes precedence over the raw category
            GarminStrengthSet(
                garmin_activity_id=502, set_index=0, exercise_category="LATERAL_RAISE",
                exercise_category_override="BENCH_PRESS", reps=8, weight_g=13608,
                start_time=None,
            ),
            GarminStrengthSet(
                garmin_activity_id=502, set_index=1, exercise_category="CURL",
                reps=10, weight_g=13608, start_time=None,
            ),
        ])
        for i in range(10):
            d = today - timedelta(days=i)
            s.add(WeightEntry(
                date=d,
                actual_weight_lbs=180.0 - i * 0.2,
                trend_weight_lbs=180.5 - i * 0.1,
                actual_fat_pct=18.0,
                trend_fat_pct=18.0 + i * 0.05,
                weight_is_interpolated=0,
                fat_is_interpolated=0,
                imported_at=datetime.now(),
            ))
        s.commit()


# ── get_strength_sessions ─────────────────────────────────────────────────

def test_get_strength_sessions_returns_sessions_and_sets(strength_db):
    result = mcp_server.get_strength_sessions(days=7)
    assert result["session_count"] == 2
    push = next(x for x in result["sessions"] if x["name"] == "Push Day")
    assert push["set_count"] == 2
    assert all("exercise" in st and "reps" in st for st in push["sets"])


def test_get_strength_sessions_converts_weight_to_lbs(strength_db):
    result = mcp_server.get_strength_sessions(days=7)
    push = next(x for x in result["sessions"] if x["name"] == "Push Day")
    assert push["sets"][0]["weight_lbs"] == 185


def test_get_strength_sessions_respects_window(strength_db):
    result = mcp_server.get_strength_sessions(days=2)
    assert result["session_count"] == 1


# ── get_exercise_history ──────────────────────────────────────────────────

def test_get_exercise_history_matches_and_survives_null_start_time(strength_db):
    """Sets with NULL start_time used to crash the sort (datetime vs date)."""
    result = mcp_server.get_exercise_history("bench_press", days=30)
    assert result["exercise"] == "BENCH_PRESS"
    assert result["session_count"] == 2  # both dates, via override on Pull Day
    total_sets = sum(len(h["sets"]) for h in result["history"])
    assert total_sets == 3


def test_get_exercise_history_override_takes_precedence(strength_db):
    result = mcp_server.get_exercise_history("lateral_raise", days=30)
    # The LATERAL_RAISE set is overridden to BENCH_PRESS, so no matches
    assert result["session_count"] == 0


def test_get_exercise_history_list(strength_db):
    result = mcp_server.get_exercise_history("list")
    assert "BENCH_PRESS" in result["known_exercises"]
    assert "CURL" in result["known_exercises"]


# ── log_strength_note ─────────────────────────────────────────────────────

def test_log_strength_note_formats_and_delegates(monkeypatch):
    captured = {}

    def fake_save(content, entities, metadata=None):
        captured.update(content=content, entities=entities, metadata=metadata)
        return "Saved memory 1, linked to 2 entities."

    monkeypatch.setattr(mcp_server, "save_memory", fake_save)
    result = mcp_server.log_strength_note(
        "Bench felt strong", ["bench press", "deadlift"], date_str="2024-02-01",
    )
    assert "Saved memory" in result
    assert captured["content"] == "[2024-02-01] Bench felt strong"
    assert captured["entities"] == ["bench press", "deadlift"]
    assert captured["metadata"]["type"] == "workout_note"
    assert captured["metadata"]["date"] == "2024-02-01"


# ── get_weight_trend ──────────────────────────────────────────────────────

def test_get_weight_trend_data_and_summary(strength_db):
    result = mcp_server.get_weight_trend(days=30)
    assert result["days_available"] == 10
    summary = result["summary"]
    assert summary["current_trend_weight_lbs"] == 180.5
    assert summary["weight_change_lbs"] == round(180.5 - 179.6, 1)
    # lean mass = trend_weight * (1 - fat/100)
    assert summary["current_lean_mass_lbs"] == round(180.5 * (1 - 18.0 / 100), 1)


def test_get_weight_trend_empty_window():
    result = mcp_server.get_weight_trend(days=7)
    assert result["days_available"] == 0
    assert result["summary"] == {}


# ── get_body_composition_vs_training ──────────────────────────────────────

def test_body_composition_vs_training_weekly_bins(strength_db):
    result = mcp_server.get_body_composition_vs_training(days=30)
    assert result["total_strength_sessions"] == 2
    assert result["weeks_with_strength_training"] >= 1
    assert result["total_lifting_volume_lbs"] > 0
    for wk in result["weekly"]:
        assert "week_starting" in wk
        assert "strength_sessions" in wk


# ── search_documents ──────────────────────────────────────────────────────

def test_search_documents_formats_citations(monkeypatch):
    monkeypatch.setattr(
        "recovery.knowledge.ingest.search_corpus",
        lambda query, n_results=5: [
            {"content": "Brace the core.", "source": "lifting.pdf", "page": 3, "doc_id": "abc"},
        ],
    )
    result = mcp_server.search_documents("deadlift bracing")
    assert result["count"] == 1
    assert result["results"][0]["citation"] == "lifting.pdf (p.3)"


def test_search_documents_empty_returns_note(monkeypatch):
    monkeypatch.setattr(
        "recovery.knowledge.ingest.search_corpus", lambda query, n_results=5: [],
    )
    result = mcp_server.search_documents("nothing matches")
    assert result["count"] == 0
    assert "note" in result


# ── sync_missing_days / get_sync_status ───────────────────────────────────

@pytest.fixture()
def clean_sync_state():
    saved = dict(mcp_server._sync_state)
    mcp_server._sync_state.clear()
    mcp_server._sync_state.update(status="idle")
    yield
    mcp_server._sync_state.clear()
    mcp_server._sync_state.update(saved)


def test_sync_missing_days_starts_worker(clean_sync_state, monkeypatch):
    ran = {}
    monkeypatch.setattr(mcp_server, "_run_sync_missing", lambda days: ran.update(days=days))
    result = mcp_server.sync_missing_days(days=3)
    assert result["status"] == "started"
    assert result["scope"] == "last 3 day(s)"


def test_sync_missing_days_guards_against_double_start(clean_sync_state, monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_sync_missing", lambda days: None)
    mcp_server._sync_state.update(status="running", progress="Garmin day 1/5")
    result = mcp_server.sync_missing_days(days=3)
    assert result["status"] == "already_running"
    assert result["progress"] == "Garmin day 1/5"


def test_get_sync_status_returns_copy(clean_sync_state):
    status = mcp_server.get_sync_status()
    assert status["status"] == "idle"
    status["status"] = "mutated"
    assert mcp_server._sync_state["status"] == "idle"
