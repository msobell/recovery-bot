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

    existing = session.get(GarminActivity, garmin_id)
    if existing:
        existing.name = activity.get("name") or existing.name
        existing.sport_type = activity.get("sport_type") or existing.sport_type
        existing.duration_sec = activity.get("duration_sec") or existing.duration_sec
        existing.avg_hr = activity.get("avg_hr") or existing.avg_hr
        existing.synced_at = datetime.now()
        # Drop and re-insert sets so set_index stays canonical
        for s in list(existing.sets):
            session.delete(s)
        session.flush()
        act = existing
    else:
        act = GarminActivity(
            garmin_id=garmin_id,
            date=activity["date"],
            name=activity.get("name"),
            sport_type=activity.get("sport_type"),
            duration_sec=activity.get("duration_sec"),
            avg_hr=activity.get("avg_hr"),
            synced_at=datetime.now(),
        )
        session.add(act)
        session.flush()

    for s in activity.get("sets", []):
        session.add(GarminStrengthSet(
            garmin_activity_id=garmin_id,
            set_index=s["set_index"],
            exercise_category=s.get("exercise_category"),
            reps=s.get("reps"),
            weight_g=s.get("weight_g"),
            duration_sec=s.get("duration_sec"),
            start_time=datetime.fromisoformat(s["start_time"]) if s.get("start_time") else None,
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

    # Garmin
    try:
        console.print(f"  Fetching Garmin data for {yesterday}...")
        data = garmin.fetch_day(yesterday)
        _upsert_garmin(session, data)
        session.commit()
        _log_sync(session, "garmin", yesterday, yesterday, 1)
        console.print("  [green]Garmin sync complete.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "garmin", yesterday, yesterday, 0, str(e))
        console.print(f"  [red]Garmin sync failed: {e}[/red]")

    # Garmin strength
    try:
        console.print(f"  Fetching Garmin strength activities for {yesterday}...")
        garmin_api = garmin.load_session()
        strength_acts = garmin.fetch_strength_activities(garmin_api, yesterday)
        rows = 0
        for act in strength_acts:
            if _upsert_strength(session, act):
                rows += 1
        session.commit()
        _log_sync(session, "garmin_strength", yesterday, yesterday, rows)
        console.print(f"  [green]Garmin strength sync complete. {rows} activities written.[/green]")
    except Exception as e:
        session.rollback()
        _log_sync(session, "garmin_strength", yesterday, yesterday, 0, str(e))
        console.print(f"  [red]Garmin strength sync failed: {e}[/red]")

    # Strava
    try:
        last = _last_strava_date(session)
        after = last + timedelta(days=1) if last else yesterday
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
        _log_sync(session, "strava", yesterday, date.today(), 0, str(e))
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

    # Resume from last synced date only when not explicitly overriding days
    last_garmin = _last_garmin_date(session)
    if days is None and last_garmin and last_garmin < end_date:
        garmin_start = max(start_date, last_garmin + timedelta(days=1))
    else:
        garmin_start = start_date

    total_days = (end_date - garmin_start).days + 1
    console.print(f"[bold]Backfilling Garmin: {garmin_start} → {end_date} ({total_days} days)[/bold]")
    console.print("[yellow]Note: Garmin rate-limits to ~1 req/sec. This will take ~{:.0f} minutes.[/yellow]".format(total_days / 60))

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
                strength_acts = garmin.fetch_strength_activities(garmin_api, current)
                for act in strength_acts:
                    _upsert_strength(session, act)
                session.commit()
            except Exception as e:
                session.rollback()
                console.print(f"  [yellow]Warning: {current} strength sync failed: {e}[/yellow]")

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
