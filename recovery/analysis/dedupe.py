"""De-duplicate Strava activities that mirror a Garmin activity.

A Garmin watch records a workout and auto-uploads it to Strava, so the same
physical session lands in BOTH garmin_activities and strava_activities. Garmin
is authoritative (it's the source, and now ingested directly for all activity
types), so this module identifies the Strava duplicates and the view layer
prefers the Garmin row.

Match rule: a Strava activity is a duplicate if a Garmin activity starts within
START_WINDOW_MIN of it. Timezones: Strava start_time is local (naive); Garmin
CARDIO start_time is local (naive, from startTimeLocal); Garmin STRENGTH start
comes from set rows in UTC — normalized to local before comparing. Activities
without a usable start time fall back to same-date matching.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from recovery.db.models import GarminActivity, GarminStrengthSet, StravaActivity

# Two activities are the same session if their starts are this close.
START_WINDOW_MIN = 30


def _garmin_activity_index(session: Session, tz: ZoneInfo):
    """Return (start_times_local, dates_without_start) across ALL Garmin activities.

    start_times_local: local-time start of each activity with a usable start.
    dates_without_start: dates of activities that have NO usable start time
    (e.g. manually-curated strength sessions) — matched on date alone.
    """
    acts = session.execute(
        select(GarminActivity.garmin_id, GarminActivity.date, GarminActivity.start_time)
    ).all()
    # Strength start times live on set rows (UTC naive), not the activity row.
    set_rows = session.execute(
        select(GarminStrengthSet.garmin_activity_id, GarminStrengthSet.start_time)
        .where(GarminStrengthSet.start_time.isnot(None))
    ).all()
    earliest_set: dict[int, datetime] = {}
    for gid, st in set_rows:
        if st is not None and (gid not in earliest_set or st < earliest_set[gid]):
            earliest_set[gid] = st

    starts_local: list[datetime] = []
    dates_without_start: set = set()
    for gid, d, act_start in acts:
        if act_start is not None:
            # Cardio: startTimeLocal is already local wall time (naive)
            starts_local.append(act_start)
        elif gid in earliest_set:
            # Strength: set start_time is UTC naive → convert to local
            starts_local.append(
                earliest_set[gid].replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
            )
        else:
            dates_without_start.add(d)
    return starts_local, dates_without_start


def strava_duplicate_ids(session: Session, tz_name: str | None = None) -> set[int]:
    """Return strava_ids that duplicate a Garmin activity.

    Computed once per request and passed to the view filters. Any Strava
    activity that lines up with a Garmin activity (by ±START_WINDOW_MIN start
    window, or same-date for Garmin activities without a start time) is
    excluded so the same session isn't counted twice.

    tz_name defaults to the configured user timezone.
    """
    if tz_name is None:
        from recovery import config as cfg_mod
        tz_name = cfg_mod.get().user.timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    garmin_starts, dateonly = _garmin_activity_index(session, tz)
    if not garmin_starts and not dateonly:
        return set()

    window = timedelta(minutes=START_WINDOW_MIN)
    candidates = session.execute(
        select(StravaActivity.strava_id, StravaActivity.start_time, StravaActivity.date)
    ).all()

    dupes: set[int] = set()
    for sid, s_start, s_date in candidates:
        if s_start is not None and any(abs(s_start - g) <= window for g in garmin_starts):
            dupes.add(sid)
        elif s_date in dateonly:
            dupes.add(sid)
    return dupes
