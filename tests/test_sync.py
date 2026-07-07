"""Tests for sync orchestration."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from recovery.db.models import GarminActivity, GarminDaily, StravaActivity, SyncLog
from recovery.ingest import sync


@pytest.fixture()
def patched_cfg(config_toml, monkeypatch):
    import recovery.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg", cfg_mod.load(config_toml))


@pytest.fixture()
def session_factory(db_engine):
    """Returns a factory that creates sessions from the test engine."""
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=db_engine)


# ── _upsert_garmin ────────────────────────────────────────────────────────

def test_upsert_garmin_inserts_new_row(db_session):
    data = {"date": date(2024, 1, 1), "hrv_rmssd": 55.0, "sleep_score": 80}
    sync._upsert_garmin(db_session, data)
    db_session.commit()
    row = db_session.get(GarminDaily, date(2024, 1, 1))
    assert row.hrv_rmssd == 55.0


def test_upsert_garmin_updates_existing_row(db_session):
    db_session.add(GarminDaily(date=date(2024, 1, 1), hrv_rmssd=50.0, synced_at=datetime.now()))
    db_session.commit()
    sync._upsert_garmin(db_session, {"date": date(2024, 1, 1), "hrv_rmssd": 60.0})
    db_session.commit()
    row = db_session.get(GarminDaily, date(2024, 1, 1))
    assert row.hrv_rmssd == 60.0


def test_upsert_garmin_ignores_none_values_on_update(db_session):
    db_session.add(GarminDaily(date=date(2024, 1, 1), hrv_rmssd=55.0, sleep_score=80, synced_at=datetime.now()))
    db_session.commit()
    sync._upsert_garmin(db_session, {"date": date(2024, 1, 1), "hrv_rmssd": None, "sleep_score": 85})
    db_session.commit()
    row = db_session.get(GarminDaily, date(2024, 1, 1))
    assert row.hrv_rmssd == 55.0   # unchanged — None not written
    assert row.sleep_score == 85   # updated


def test_upsert_garmin_returns_false_when_no_date(db_session):
    result = sync._upsert_garmin(db_session, {"hrv_rmssd": 55.0})
    assert result is False


# ── _upsert_strava ────────────────────────────────────────────────────────

def test_upsert_strava_inserts_new_activity(db_session):
    data = {"strava_id": 1, "date": date(2024, 1, 5), "sport_type": "Run", "duration_sec": 3600}
    sync._upsert_strava(db_session, data)
    db_session.commit()
    row = db_session.get(StravaActivity, 1)
    assert row.sport_type == "Run"


def test_upsert_strava_updates_existing_activity(db_session):
    db_session.add(StravaActivity(strava_id=1, date=date(2024, 1, 5), suffer_score=50, synced_at=datetime.now()))
    db_session.commit()
    sync._upsert_strava(db_session, {"strava_id": 1, "date": date(2024, 1, 5), "suffer_score": 75})
    db_session.commit()
    row = db_session.get(StravaActivity, 1)
    assert row.suffer_score == 75


# ── _upsert_garmin_activity (cardio/other) ────────────────────────────────

def test_upsert_garmin_activity_inserts_cardio(db_session):
    act = {
        "garmin_id": 900, "date": date(2024, 1, 5), "name": "Soccer",
        "sport_type": "soccer", "start_time": datetime(2024, 1, 5, 16, 0, 0),
        "duration_sec": 3600, "distance_m": None, "avg_hr": 129.0,
        "max_hr": 172, "calories": 500,
    }
    assert sync._upsert_garmin_activity(db_session, act) is True
    db_session.commit()
    row = db_session.get(GarminActivity, 900)
    assert row.sport_type == "soccer"
    assert row.is_strength == 0
    assert row.max_hr == 172


def test_upsert_garmin_activity_updates_existing(db_session):
    db_session.add(GarminActivity(
        garmin_id=901, date=date(2024, 1, 5), sport_type="running",
        duration_sec=1000, is_strength=0, synced_at=datetime.now(),
    ))
    db_session.commit()
    sync._upsert_garmin_activity(db_session, {
        "garmin_id": 901, "date": date(2024, 1, 5), "sport_type": "running",
        "duration_sec": 1800, "avg_hr": 140.0,
    })
    db_session.commit()
    row = db_session.get(GarminActivity, 901)
    assert row.duration_sec == 1800
    assert row.avg_hr == 140.0


def test_upsert_garmin_activity_wont_clobber_strength(db_session):
    """A strength session sharing the id must never be downgraded to cardio."""
    db_session.add(GarminActivity(
        garmin_id=902, date=date(2024, 1, 5), name="Strength",
        sport_type="strength_training", is_strength=1, synced_at=datetime.now(),
    ))
    db_session.commit()
    result = sync._upsert_garmin_activity(db_session, {
        "garmin_id": 902, "date": date(2024, 1, 5), "sport_type": "running",
    })
    assert result is False
    row = db_session.get(GarminActivity, 902)
    assert row.is_strength == 1
    assert row.sport_type == "strength_training"


def test_upsert_garmin_activity_returns_false_when_no_id(db_session):
    assert sync._upsert_garmin_activity(db_session, {"date": date(2024, 1, 5)}) is False


# ── _last_garmin_date / _last_strava_date ─────────────────────────────────

def test_last_garmin_date_returns_none_when_empty(db_session):
    assert sync._last_garmin_date(db_session) is None


def test_last_garmin_date_returns_max(db_session):
    for d in [date(2024, 1, 1), date(2024, 1, 5), date(2024, 1, 3)]:
        db_session.add(GarminDaily(date=d, synced_at=datetime.now()))
    db_session.commit()
    assert sync._last_garmin_date(db_session) == date(2024, 1, 5)


def test_last_strava_date_returns_none_when_empty(db_session):
    assert sync._last_strava_date(db_session) is None


def test_last_strava_date_returns_max(db_session):
    for i, d in enumerate([date(2024, 1, 1), date(2024, 1, 10)]):
        db_session.add(StravaActivity(strava_id=i, date=d, synced_at=datetime.now()))
    db_session.commit()
    assert sync._last_strava_date(db_session) == date(2024, 1, 10)


# ── daily_sync ────────────────────────────────────────────────────────────

def test_daily_sync_writes_garmin_and_strava(db_engine, patched_cfg, monkeypatch):
    yesterday = date.today() - timedelta(days=1)
    garmin_data = {"date": yesterday, "hrv_rmssd": 55.0, "sleep_score": 80}
    strava_data = [{"strava_id": 999, "date": yesterday, "sport_type": "Run", "duration_sec": 3600}]

    from recovery.db.session import get_session
    monkeypatch.setattr(sync, "get_session", lambda *a, **kw: get_session(db_engine))
    monkeypatch.setattr(sync, "init_db", lambda *a, **kw: db_engine)

    from recovery.ingest import garmin as g_mod, strava as s_mod
    monkeypatch.setattr(g_mod, "load_session", lambda: MagicMock())
    monkeypatch.setattr(g_mod, "fetch_day", lambda *a, **kw: garmin_data)
    monkeypatch.setattr(s_mod, "fetch_activities", lambda *a, **kw: strava_data)

    sync.daily_sync()

    session = get_session(db_engine)
    assert session.get(GarminDaily, yesterday) is not None
    assert session.get(StravaActivity, 999) is not None
    logs = session.execute(select(SyncLog)).scalars().all()
    assert len(logs) >= 2
    session.close()
