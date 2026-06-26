"""Tests for Garmin/Strava strength de-duplication."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from recovery.analysis.dedupe import strava_duplicate_ids
from recovery.db.models import GarminActivity, GarminStrengthSet, StravaActivity


@pytest.fixture()
def overlap_db(db_session):
    """One Garmin lift + its Strava mirror + a real run + a Strava-only lift."""
    y = date(2024, 3, 10)
    g = GarminActivity(garmin_id=1, date=y, name="Strength", sport_type="strength_training", duration_sec=3000)
    db_session.add(g)
    db_session.flush()
    # Set start in UTC (22:00 UTC == 16:00 America/Denver)
    db_session.add(GarminStrengthSet(
        garmin_activity_id=1, set_index=0, exercise_category="BENCH_PRESS",
        start_time=datetime(y.year, y.month, y.day, 22, 0, 0),
    ))
    # Strava mirror of the lift — local 16:00
    db_session.add(StravaActivity(
        strava_id=10, date=y, start_time=datetime(y.year, y.month, y.day, 16, 0, 0),
        name="Afternoon Weight Training", sport_type="WeightTraining", duration_sec=3000, suffer_score=40,
    ))
    # Genuine cardio same day — must never be deduped
    db_session.add(StravaActivity(
        strava_id=11, date=y, start_time=datetime(y.year, y.month, y.day, 7, 0, 0),
        name="Morning Run", sport_type="Run", duration_sec=1800, distance_m=5000.0, suffer_score=55,
    ))
    # Strava-only lift on a day with no Garmin session — must be kept
    far = y - timedelta(days=40)
    db_session.add(StravaActivity(
        strava_id=12, date=far, start_time=datetime(far.year, far.month, far.day, 12, 0, 0),
        name="Strava-only lift", sport_type="WeightTraining", duration_sec=2000, suffer_score=30,
    ))
    db_session.commit()
    return db_session


def test_mirror_lift_is_deduped(overlap_db):
    assert strava_duplicate_ids(overlap_db) == {10}


def test_cardio_never_deduped(overlap_db):
    assert 11 not in strava_duplicate_ids(overlap_db)


def test_strava_only_strength_kept(overlap_db):
    # No Garmin session that day → not a duplicate.
    assert 12 not in strava_duplicate_ids(overlap_db)


def test_dateonly_fallback_for_curated_session(db_session):
    """A curated Garmin session (no set start_time) dedupes on date alone."""
    y = date(2024, 3, 10)
    g = GarminActivity(garmin_id=2, date=y, name="Strength", sport_type="strength_training", duration_sec=3000)
    db_session.add(g)
    db_session.flush()
    db_session.add(GarminStrengthSet(  # curated set, no start_time
        garmin_activity_id=2, set_index=0, exercise_category="CURL",
        exercise_category_override="UP_AND_DOWN_BICEPS", reps=10,
    ))
    db_session.add(StravaActivity(
        strava_id=20, date=y, start_time=datetime(y.year, y.month, y.day, 14, 0, 0),
        name="Lunch Weight Training", sport_type="WeightTraining", duration_sec=3000,
    ))
    db_session.commit()
    assert strava_duplicate_ids(db_session) == {20}


def test_no_garmin_strength_means_no_dupes(db_session):
    db_session.add(StravaActivity(
        strava_id=30, date=date(2024, 3, 10), start_time=datetime(2024, 3, 10, 16, 0, 0),
        name="Weights", sport_type="WeightTraining", duration_sec=3000,
    ))
    db_session.commit()
    assert strava_duplicate_ids(db_session) == set()
