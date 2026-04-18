"""JSON API routes consumed by Chart.js on the frontend."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from recovery.analysis.recovery import assess, get_snapshot, get_trend, get_recent_activities
from recovery.db.models import GarminDaily, StravaActivity
from recovery.db.session import get_session, init_db

router = APIRouter(tags=["data"])


def _session():
    return get_session(init_db())


@router.get("/today")
def today_status():
    session = _session()
    try:
        day = date.today() - timedelta(days=1)
        snapshot = get_snapshot(session, day)
        assessment = assess(snapshot)
        metrics = {}
        if snapshot:
            metrics = {
                "hrv_rmssd": snapshot.hrv_rmssd,
                "hrv_status": snapshot.hrv_status,
                "hrv_vs_baseline_pct": round(assessment.hrv_vs_baseline_pct * 100, 1) if assessment.hrv_vs_baseline_pct else None,
                "resting_hr": snapshot.resting_hr,
                "sleep_score": snapshot.sleep_score,
                "sleep_duration_hours": round(snapshot.sleep_duration_min / 60, 1) if snapshot.sleep_duration_min else None,
                "sleep_deep_min": snapshot.sleep_deep_min,
                "sleep_rem_min": snapshot.sleep_rem_min,
                "overnight_stress": snapshot.overnight_stress_avg,
                "overnight_stress_qualifier": snapshot.overnight_stress_qualifier,
                "body_battery_start": snapshot.body_battery_start,
            }
        return {
            "date": str(day),
            "status": assessment.status.value,
            "intensity": assessment.recommended_intensity.value,
            "signals": assessment.signals,
            "warnings": assessment.warnings,
            "metrics": metrics,
        }
    finally:
        session.close()


@router.get("/trend")
def trend(days: int = Query(default=30, ge=7, le=365)):
    session = _session()
    try:
        snapshots = get_trend(session, days=days)
        return {
            "labels": [str(s.date) for s in snapshots],
            "hrv": [s.hrv_rmssd for s in snapshots],
            "hrv_baseline_low": [s.hrv_baseline_low for s in snapshots],
            "hrv_baseline_high": [s.hrv_baseline_high for s in snapshots],
            "resting_hr": [s.resting_hr for s in snapshots],
            "sleep_score": [s.sleep_score for s in snapshots],
            "sleep_hours": [round(s.sleep_duration_min / 60, 1) if s.sleep_duration_min else None for s in snapshots],
            "overnight_stress": [s.overnight_stress_avg for s in snapshots],
        }
    finally:
        session.close()


@router.get("/activities")
def activities(days: int = Query(default=30, ge=1, le=365), sport: str | None = None):
    session = _session()
    try:
        end = date.today()
        start = end - timedelta(days=days)
        q = select(StravaActivity).where(
            StravaActivity.date >= start,
            StravaActivity.date <= end,
        ).order_by(StravaActivity.date.desc())
        rows = session.execute(q).scalars().all()
        data = []
        for r in rows:
            if sport and r.sport_type != sport:
                continue
            data.append({
                "id": r.strava_id,
                "date": str(r.date),
                "name": r.name,
                "sport_type": r.sport_type,
                "duration_min": round(r.duration_sec / 60) if r.duration_sec else None,
                "distance_km": round(r.distance_m / 1000, 1) if r.distance_m else None,
                "elevation_m": r.elevation_m,
                "avg_hr": r.avg_hr,
                "suffer_score": r.suffer_score,
            })
        return {"days": days, "count": len(data), "activities": data}
    finally:
        session.close()


@router.get("/training-load")
def training_load(days: int = Query(default=60, ge=14, le=365)):
    """Suffer score by day for overlay on HRV chart."""
    session = _session()
    try:
        end = date.today()
        start = end - timedelta(days=days)
        rows = session.execute(
            select(StravaActivity)
            .where(StravaActivity.date >= start, StravaActivity.date <= end)
            .order_by(StravaActivity.date)
        ).scalars().all()

        by_date: dict[str, int] = {}
        for r in rows:
            ds = str(r.date)
            by_date[ds] = by_date.get(ds, 0) + (r.suffer_score or 0)

        return {"labels": list(by_date.keys()), "suffer_score": list(by_date.values())}
    finally:
        session.close()
