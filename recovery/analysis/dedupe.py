"""De-duplicate Strava activities that mirror a Garmin strength session.

A Garmin watch records a lift and auto-uploads it to Strava, so the same
physical workout lands in BOTH the garmin_activities table (as a strength
session with sets) and the strava_activities table (as WeightTraining/Workout).
Garmin is authoritative for strength; this module identifies the Strava
duplicates so cardio/activity/load views can exclude them.

Match rule: a Strava strength-type activity is a duplicate if a Garmin strength
session starts within START_WINDOW_MIN of it. Garmin set start_times come from
the API in UTC (naive); Strava start_time is local — so we normalize the Garmin
start to the configured local timezone before comparing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from recovery.db.models import GarminActivity, GarminStrengthSet, StravaActivity

# Strava sport types that represent strength work (and thus can collide with a
# Garmin strength session). Cardio types are never deduped against Garmin.
STRAVA_STRENGTH_TYPES = {"WeightTraining", "Workout"}

# Two strength activities are the same session if their starts are this close.
START_WINDOW_MIN = 30


def _garmin_strength_index(session: Session, tz: ZoneInfo):
    """Return (start_times_local, dates_without_start) for Garmin strength.

    start_times_local: local-time start of each session that has set times.
    dates_without_start: dates of sessions that have NO usable set start time
    (e.g. manually-curated sessions) — matched on date alone as a fallback.
    """
    sessions = session.execute(select(GarminActivity.garmin_id, GarminActivity.date)).all()
    set_rows = session.execute(
        select(GarminStrengthSet.garmin_activity_id, GarminStrengthSet.start_time)
        .where(GarminStrengthSet.start_time.isnot(None))
    ).all()

    earliest: dict[int, datetime] = {}
    for gid, st in set_rows:
        if st is None:
            continue
        if gid not in earliest or st < earliest[gid]:
            earliest[gid] = st

    starts_local = [
        st.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
        for st in earliest.values()
    ]
    dates_without_start = {d for gid, d in sessions if gid not in earliest}
    return starts_local, dates_without_start


def strava_duplicate_ids(session: Session, tz_name: str = "America/Denver") -> set[int]:
    """Return strava_ids that duplicate a Garmin strength session.

    Computed once per request and passed to the view filters. Only Strava
    strength-type activities are eligible; cardio is never excluded. Sessions
    with set times match on a ±START_WINDOW_MIN start window; curated sessions
    with no set times fall back to same-date matching.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    garmin_starts, dateonly = _garmin_strength_index(session, tz)
    if not garmin_starts and not dateonly:
        return set()

    window = timedelta(minutes=START_WINDOW_MIN)
    candidates = session.execute(
        select(StravaActivity.strava_id, StravaActivity.start_time, StravaActivity.date)
        .where(StravaActivity.sport_type.in_(STRAVA_STRENGTH_TYPES))
    ).all()

    dupes: set[int] = set()
    for sid, s_start, s_date in candidates:
        if s_date in dateonly:
            dupes.add(sid)
        elif s_start is not None and any(abs(s_start - g) <= window for g in garmin_starts):
            dupes.add(sid)
    return dupes
