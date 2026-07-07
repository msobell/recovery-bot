"""Orchestrates daily sync and historical backfill."""
from __future__ import annotations

import traceback
from datetime import date, datetime, timedelta

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from sqlalchemy.orm import Session

from recovery import config as cfg_mod
from recovery.db.models import GarminActivity, GarminDaily, GarminStrengthSet, StravaActivity, SyncLog
from recovery.db.session import get_session, init_db
from recovery.ingest import garmin, strava

console = Console()


def _upsert_garmin(session: Session, data: dict) -> bool:
    if not data.get("date"):
        return False
    existing = session.get(GarminDaily, data["date"])
    if existing:
        for k, v in data.items():
            if k != "date" and v is not None:
                setattr(existing, k, v)
        existing.synced_at = datetime.now()
    else:
        session.add(GarminDaily(**data, synced_at=datetime.now()))
    return True


def _upsert_strength(session: Session, activity: dict) -> bool:
    """Upsert a Garmin strength activity and replace its sets."""
    garmin_id = activity.get("garmin_id")
    if not garmin_id:
        return False

    new_sets = activity.get("sets")

    existing = session.get(GarminActivity, garmin_id)
    if existing:
        existing.name = activity.get("name") or existing.name
        existing.sport_type = activity.get("sport_type") or existing.sport_type
        existing.duration_sec = activity.get("duration_sec") or existing.duration_sec
        existing.avg_hr = activity.get("avg_hr") or existing.avg_hr
        existing.synced_at = datetime.now()
        # Hand-curated sessions are authoritative: refresh metadata above, but
        # never touch the sets. (Per-set overrides alone weren't enough — a
        # re-sync would wipe any non-overridden set on a curated day.)
        if existing.manually_edited:
            return True
        # Never replace existing sets with nothing: sets=None means the set
        # fetch failed, and an empty payload against a populated session is
        # more likely a transient API hiccup than a real deletion.
        if not new_sets and existing.sets:
            return True
        # Preserve user-set category overrides and manual reps/weight edits by set_index
        user_edits = {
            s.set_index: {
                "exercise_category_override": s.exercise_category_override,
                "reps": s.reps,
                "weight_g": s.weight_g,
            }
            for s in existing.sets
            if s.exercise_category_override is not None
        }
        for s in list(existing.sets):
            session.delete(s)
        session.flush()
        act = existing
    else:
        user_edits = {}
        act = GarminActivity(
            garmin_id=garmin_id,
            date=activity["date"],
            name=activity.get("name"),
            sport_type=activity.get("sport_type"),
            duration_sec=activity.get("duration_sec"),
            avg_hr=activity.get("avg_hr"),
            is_strength=1,
            synced_at=datetime.now(),
        )
        session.add(act)
        session.flush()

    for s in new_sets or []:
        idx = s["set_index"]
        edits = user_edits.get(idx, {})
        # If the user manually edited this set, keep their reps/weight; otherwise use Garmin's
        has_override = bool(edits.get("exercise_category_override"))
        session.add(GarminStrengthSet(
            garmin_activity_id=garmin_id,
            set_index=idx,
            exercise_category=s.get("exercise_category"),
            exercise_category_override=edits.get("exercise_category_override"),
            reps=edits["reps"] if has_override else s.get("reps"),
            weight_g=edits["weight_g"] if has_override else s.get("weight_g"),
            duration_sec=s.get("duration_sec"),
            start_time=datetime.fromisoformat(s["start_time"]) if s.get("start_time") else None,
        ))
    return True


def _upsert_garmin_activity(session: Session, activity: dict) -> bool:
    """Upsert a non-strength Garmin activity (cardio, sport, etc.). No sets."""
    garmin_id = activity.get("garmin_id")
    if not garmin_id:
        return False

    existing = session.get(GarminActivity, garmin_id)
    if existing:
        # Don't clobber a strength session that shares this id (shouldn't
        # happen — fetch_cardio_activities excludes strength types — but be safe)
        if existing.is_strength:
            return False
        for k in ("name", "sport_type", "start_time", "duration_sec",
                  "distance_m", "elevation_m", "avg_hr", "max_hr", "calories"):
            v = activity.get(k)
            if v is not None:
                setattr(existing, k, v)
        existing.synced_at = datetime.now()
    else:
        session.add(GarminActivity(
            garmin_id=garmin_id,
            date=activity["date"],
            name=activity.get("name"),
            sport_type=activity.get("sport_type"),
            start_time=activity.get("start_time"),
            duration_sec=activity.get("duration_sec"),
            distance_m=activity.get("distance_m"),
            elevation_m=activity.get("elevation_m"),
            avg_hr=activity.get("avg_hr"),
            max_hr=activity.get("max_hr"),
            calories=activity.get("calories"),
            is_strength=0,
            synced_at=datetime.now(),
        ))
    return True


def _upsert_strava(session: Session, data: dict) -> bool:
    existing = session.get(StravaActivity, data["strava_id"])
    if existing:
        for k, v in data.items():
            if k != "strava_id" and v is not None:
                setattr(existing, k, v)
        existing.synced_at = datetime.now()
    else:
        session.add(StravaActivity(**data, synced_at=datetime.now()))
    return True


def _upsert_weight(session: Session, data: dict) -> bool:
    from recovery.db.models import WeightEntry
    from datetime import datetime as dt
    existing = session.get(WeightEntry, data["date"])
    if existing:
        for k, v in data.items():
            if k != "date" and v is not None:
                setattr(existing, k, v)
        existing.imported_at = dt.now()
    else:
        session.add(WeightEntry(**data, imported_at=dt.now()))
    return True


def _log_sync(session: Session, source: str, date_from: date, date_to: date, rows: int, error: str | None = None):
    session.add(SyncLog(
        started_at=datetime.now(),
        finished_at=datetime.now(),
        source=source,
        date_from=date_from,
        date_to=date_to,
        rows_written=rows,
        error=error,
    ))
    session.commit()


def _last_garmin_date(session: Session) -> date | None:
    from sqlalchemy import select, func
    result = session.execute(select(func.max(GarminDaily.date))).scalar()
    return result


def _last_strava_date(session: Session) -> date | None:
    from sqlalchemy import select, func
    result = session.execute(select(func.max(StravaActivity.date))).scalar()
    return result


def daily_sync() -> None:
    """Sync yesterday's Garmin data and any new Strava activities."""
    cfg = cfg_mod.get()
    engine = init_db()
    session = get_session(engine)
    yesterday = date.today() - timedelta(days=1)

    console.print("[bold]Running daily sync...[/bold]")

    # Garmin — catch up from the last synced day (capped) so missed days
    # aren't silently skipped, not just yesterday
    garmin_start = yesterday
    try:
        last_garmin = _last_garmin_date(session)
        if last_garmin and last_garmin < yesterday:
            garmin_start = max(last_garmin + timedelta(days=1), yesterday - timedelta(days=13))
        console.print(f"  Fetching Garmin data for {garmin_start} → {yesterday}...")
        garmin_api = garmin.load_session()
        multi_day = garmin_start < yesterday
        rows = 0
        current = garmin_start
        while current <= yesterday:
            data = garmin.fetch_day(current, api=garmin_api, delay=1.1 if multi_day else 0.0)
            if _upsert_garmin(session, data):
                rows += 1
            current += timedelta(days=1)
        session.commit()
        _log_sync(session, "garmin", garmin_start, yesterday, rows)
        console.print(f"  [green]Garmin sync complete. {rows} days written.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "garmin", garmin_start, yesterday, 0, str(e))
        console.print(f"  [red]Garmin sync failed: {e}[/red]")

    # Garmin activities (strength w/ sets + cardio/other) across the catch-up range
    try:
        console.print(f"  Fetching Garmin activities for {garmin_start} → {yesterday}...")
        garmin_api = garmin.load_session()
        rows = 0
        current = garmin_start
        while current <= yesterday:
            for act in garmin.fetch_strength_activities(garmin_api, current):
                if _upsert_strength(session, act):
                    rows += 1
            for act in garmin.fetch_cardio_activities(garmin_api, current):
                if _upsert_garmin_activity(session, act):
                    rows += 1
            current += timedelta(days=1)
        session.commit()
        _log_sync(session, "garmin_activities", garmin_start, yesterday, rows)
        console.print(f"  [green]Garmin activity sync complete. {rows} activities written.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "garmin_activities", garmin_start, yesterday, 0, str(e))
        console.print(f"  [red]Garmin activity sync failed: {e}[/red]")

    # Strava — refetch from a day before the last-synced date: activities
    # recorded later on that day would otherwise be skipped forever, and
    # _upsert_strava is idempotent by strava_id so the overlap is free
    after = yesterday
    try:
        last = _last_strava_date(session)
        if last:
            after = last - timedelta(days=1)
        console.print(f"  Fetching Strava activities since {after}...")
        activities = strava.fetch_activities(cfg.strava.client_id, cfg.strava.client_secret, after=after)
        rows = 0
        for act in activities:
            if _upsert_strava(session, act):
                rows += 1
        session.commit()
        _log_sync(session, "strava", after, date.today(), rows)
        console.print(f"  [green]Strava sync complete. {rows} activities written.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "strava", after, date.today(), 0, str(e))
        console.print(f"  [red]Strava sync failed: {e}[/red]")

    session.close()


def backfill(days: int | None = None) -> None:
    """Backfill historical data. Resumes from last synced date if interrupted."""
    cfg = cfg_mod.get()
    engine = init_db()
    session = get_session(engine)

    backfill_days = days or cfg.sync.backfill_days
    end_date = date.today()
    start_date = end_date - timedelta(days=backfill_days)

    # Resume from last synced date only when not explicitly overriding days.
    # If already current (last == end_date), no-op instead of restarting the
    # full window.
    last_garmin = _last_garmin_date(session)
    if days is None and last_garmin:
        garmin_start = max(start_date, last_garmin + timedelta(days=1))
    else:
        garmin_start = start_date

    total_days = max((end_date - garmin_start).days + 1, 0)
    if total_days == 0:
        console.print("[green]Garmin already current — skipping backfill.[/green]")
        garmin_rows = 0
        garmin_errors = 0
    else:
        console.print(f"[bold]Backfilling Garmin: {garmin_start} → {end_date} ({total_days} days)[/bold]")
        # ~7 rate-limited API calls per day at 1.1 s each
        console.print("[yellow]Note: Garmin rate-limits to ~1 req/sec. This will take ~{:.0f} minutes.[/yellow]".format(total_days * 8 / 60))

        garmin_rows = 0
        garmin_errors = 0
        garmin_api = garmin.load_session()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Garmin days", total=total_days)
            current = garmin_start
            while current <= end_date:
                try:
                    data = garmin.fetch_day(current, api=garmin_api, delay=1.1)
                    _upsert_garmin(session, data)
                    session.commit()
                    garmin_rows += 1
                except Exception as e:
                    session.rollback()
                    console.print(f"  [yellow]Warning: {current} Garmin daily failed: {e}[/yellow]")
                    garmin_errors += 1

                try:
                    for act in garmin.fetch_strength_activities(garmin_api, current):
                        _upsert_strength(session, act)
                    for act in garmin.fetch_cardio_activities(garmin_api, current):
                        _upsert_garmin_activity(session, act)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    console.print(f"  [yellow]Warning: {current} activity sync failed: {e}[/yellow]")

                progress.advance(task)
                current = current + timedelta(days=1)

        _log_sync(session, "garmin", garmin_start, end_date, garmin_rows,
                  f"{garmin_errors} errors" if garmin_errors else None)
    console.print(f"[green]Garmin backfill done. {garmin_rows} days written, {garmin_errors} errors.[/green]")

    # Strava backfill (no rate limiting needed — pagination handles it)
    console.print(f"[bold]Backfilling Strava: {start_date} → {end_date}...[/bold]")
    try:
        activities = strava.fetch_activities(
            cfg.strava.client_id, cfg.strava.client_secret,
            after=start_date, before=end_date,
        )
        rows = 0
        for act in activities:
            if _upsert_strava(session, act):
                rows += 1
        session.commit()
        _log_sync(session, "strava", start_date, end_date, rows)
        console.print(f"[green]Strava backfill done. {rows} activities written.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "strava", start_date, end_date, 0, str(e))
        console.print(f"[red]Strava backfill failed: {e}[/red]")
        traceback.print_exc()

    session.close()
