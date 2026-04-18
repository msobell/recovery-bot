"""Shared fixtures for the test suite."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recovery.db.session import Base
from recovery.db.models import GarminDaily, StravaActivity


@pytest.fixture()
def db_engine(tmp_path):
    """File-based SQLite engine, fresh per test. File-based so it can be shared across threads."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Session bound to the in-memory engine."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def populated_db(db_session):
    """DB with 14 days of Garmin data and 5 Strava activities."""
    from datetime import timedelta
    base = date(2024, 1, 14)
    for i in range(14):
        day = base - timedelta(days=i)
        db_session.add(GarminDaily(
            date=day,
            hrv_rmssd=55.0 - i * 0.5,
            hrv_status="BALANCED",
            hrv_baseline_low=48.0,
            hrv_baseline_high=62.0,
            resting_hr=48 + i % 3,
            sleep_start=datetime(day.year, day.month, day.day, 22, 30),
            sleep_end=datetime(day.year, day.month, day.day + 1 if day.day < 28 else day.day, 6, 30),
            sleep_duration_min=480,
            sleep_deep_min=90,
            sleep_light_min=240,
            sleep_rem_min=120,
            sleep_awake_min=30,
            sleep_score=78 - i % 5,
            overnight_stress_avg=22.0 + i,
            overnight_stress_qualifier="restful",
            body_battery_start=85 - i,
            synced_at=datetime.now(),
        ))

    for i in range(5):
        db_session.add(StravaActivity(
            strava_id=1000 + i,
            date=base - timedelta(days=i * 2),
            name=f"Morning Run {i}",
            sport_type="Run",
            duration_sec=3600,
            distance_m=10000.0,
            elevation_m=50.0,
            avg_hr=145,
            max_hr=172,
            suffer_score=60 + i * 5,
            synced_at=datetime.now(),
        ))

    db_session.commit()
    return db_session


@pytest.fixture()
def config_toml(tmp_path):
    """Write a minimal config.toml and return its path."""
    content = """
[user]
name = "Tester"
timezone = "America/New_York"

[garmin]
email = "test@example.com"

[strava]
client_id = "12345"
client_secret = "secret"

[sync]
sync_time = "08:00"
backfill_days = 14

[equipment]
sauna = true
squat_rack = true
dumbbells = true
pullup_bar = true

[equipment.dumbbell_max_kg]
value = 40.0

[recovery]
hrv_low_pct = 0.85
hrv_high_pct = 1.10
rhr_high_offset = 5
sleep_min_hours = 7.0
overnight_stress_low = 25
overnight_stress_high = 45

[ui]
port = 8080
default_trend_days = 30
"""
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


@pytest.fixture()
def strava_token(tmp_path):
    """Write a fake Strava token file."""
    import time
    token = {
        "access_token": "fake_access",
        "refresh_token": "fake_refresh",
        "expires_at": int(time.time()) + 3600,
        "token_type": "Bearer",
    }
    p = tmp_path / "strava_token.json"
    p.write_text(json.dumps(token))
    return p
