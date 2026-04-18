"""Tests for DB models and session."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from recovery.db.models import GarminDaily, StravaActivity, SyncLog


def test_garmin_daily_insert_and_retrieve(db_session):
    row = GarminDaily(
        date=date(2024, 1, 1),
        hrv_rmssd=55.3,
        hrv_status="BALANCED",
        resting_hr=48,
        sleep_score=82,
        sleep_duration_min=480,
        overnight_stress_avg=21.5,
        synced_at=datetime.now(),
    )
    db_session.add(row)
    db_session.commit()

    fetched = db_session.get(GarminDaily, date(2024, 1, 1))
    assert fetched is not None
    assert fetched.hrv_rmssd == 55.3
    assert fetched.resting_hr == 48
    assert fetched.sleep_score == 82


def test_garmin_daily_upsert(db_session):
    day = date(2024, 1, 1)
    db_session.add(GarminDaily(date=day, hrv_rmssd=50.0, synced_at=datetime.now()))
    db_session.commit()

    row = db_session.get(GarminDaily, day)
    row.hrv_rmssd = 60.0
    db_session.commit()

    updated = db_session.get(GarminDaily, day)
    assert updated.hrv_rmssd == 60.0


def test_garmin_daily_nullable_fields(db_session):
    db_session.add(GarminDaily(date=date(2024, 2, 1), synced_at=datetime.now()))
    db_session.commit()
    row = db_session.get(GarminDaily, date(2024, 2, 1))
    assert row.hrv_rmssd is None
    assert row.sleep_score is None


def test_strava_activity_insert_and_retrieve(db_session):
    act = StravaActivity(
        strava_id=999,
        date=date(2024, 1, 5),
        name="Test Run",
        sport_type="Run",
        duration_sec=3600,
        distance_m=10000.0,
        avg_hr=150,
        suffer_score=75,
        synced_at=datetime.now(),
    )
    db_session.add(act)
    db_session.commit()

    fetched = db_session.get(StravaActivity, 999)
    assert fetched.name == "Test Run"
    assert fetched.sport_type == "Run"
    assert fetched.distance_m == 10000.0


def test_strava_primary_key_is_strava_id(db_session):
    db_session.add(StravaActivity(strava_id=42, date=date(2024, 1, 1), synced_at=datetime.now()))
    db_session.commit()
    assert db_session.get(StravaActivity, 42) is not None
    assert db_session.get(StravaActivity, 99) is None


def test_sync_log_insert(db_session):
    log = SyncLog(
        source="garmin",
        date_from=date(2024, 1, 1),
        date_to=date(2024, 1, 14),
        rows_written=14,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )
    db_session.add(log)
    db_session.commit()

    rows = db_session.execute(select(SyncLog)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "garmin"
    assert rows[0].rows_written == 14


def test_sync_log_error_field(db_session):
    db_session.add(SyncLog(
        source="strava",
        rows_written=0,
        error="Connection timeout",
        started_at=datetime.now(),
    ))
    db_session.commit()
    rows = db_session.execute(select(SyncLog)).scalars().all()
    assert rows[0].error == "Connection timeout"
