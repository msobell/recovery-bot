"""JSON API routes consumed by Chart.js on the frontend."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from recovery.analysis.recovery import assess, get_snapshot, get_trend, get_recent_activities
from recovery.db.models import GarminActivity, GarminDaily, GarminStrengthSet, StravaActivity
from recovery.db.session import get_session, init_db

router = APIRouter(tags=["data"])


def _session():
    return get_session(init_db())


@router.get("/today")
def today_status():
    session = _session()
    try:
        day = date.today()
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


_CARDIO_SPORT_TYPES = {
    "Ride", "Run", "Hike", "Walk", "Swim", "Rowing", "Kayaking",
    "AlpineSki", "NordicSki", "Snowboard", "StandUpPaddling",
    "Workout", "Elliptical", "StairStepper", "Yoga", "Pilates",
    "Golf", "MountainBikeRide",
}

_G_TO_LBS = 0.00220462

_EXTRA_CATEGORIES = {
    "ONE_ARM_KETTLEBELL_SWING",
    "LEG_BAND_REHAB",
}


@router.get("/today/activities")
def today_activities():
    session = _session()
    try:
        today = date.today()
        week_ago = today - timedelta(days=6)

        # Cardio — from Strava, last 7 days
        strava_rows = session.execute(
            select(StravaActivity)
            .where(StravaActivity.date >= week_ago, StravaActivity.date <= today)
            .order_by(StravaActivity.date.desc(), StravaActivity.strava_id)
        ).scalars().all()

        cardio = []
        for r in strava_rows:
            if r.sport_type not in _CARDIO_SPORT_TYPES:
                continue
            cardio.append({
                "id": r.strava_id,
                "date": str(r.date),
                "name": r.name or r.sport_type,
                "sport_type": r.sport_type,
                "duration_min": round(r.duration_sec / 60) if r.duration_sec else None,
                "avg_hr": r.avg_hr,
            })

        # Strength — from Garmin, last 7 days
        garmin_acts = session.execute(
            select(GarminActivity)
            .where(GarminActivity.date >= week_ago, GarminActivity.date <= today)
            .order_by(GarminActivity.date.desc(), GarminActivity.garmin_id)
        ).scalars().all()

        strength = []
        for act in garmin_acts:
            sets = []
            for s in sorted(act.sets, key=lambda x: x.set_index):
                weight_lbs = round(s.weight_g * _G_TO_LBS) if s.weight_g else None
                sets.append({
                    "set_id": s.id,
                    "set_index": s.set_index,
                    "exercise_category": s.exercise_category_override or s.exercise_category or "UNKNOWN",
                    "reps": s.reps,
                    "weight_lbs": weight_lbs,
                })
            strength.append({
                "date": str(act.date),
                "garmin_id": act.garmin_id,
                "name": act.name or "Strength",
                "duration_min": round(act.duration_sec / 60) if act.duration_sec else None,
                "avg_hr": act.avg_hr,
                "sets": sets,
            })

        all_categories = sorted(
            {s["exercise_category"] for act in strength for s in act["sets"]}
            | _EXTRA_CATEGORIES
        )

        return {"cardio": cardio, "strength": strength, "known_categories": all_categories}
    finally:
        session.close()


class SetUpdate(BaseModel):
    category: str | None = None
    reps: int | None = None
    weight_lbs: float | None = None


@router.patch("/strength/set/{set_id}")
def patch_set(set_id: int, body: SetUpdate):
    session = _session()
    try:
        s = session.get(GarminStrengthSet, set_id)
        if not s:
            raise HTTPException(status_code=404, detail="Set not found")
        if body.category is not None:
            s.exercise_category_override = body.category.upper()
        if body.reps is not None:
            s.reps = body.reps
        if body.weight_lbs is not None:
            s.weight_g = body.weight_lbs / _G_TO_LBS
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.get("/strength/categories")
def strength_categories():
    """All distinct exercise categories seen across all synced sets."""
    session = _session()
    try:
        from sqlalchemy import func as sqlfunc
        rows = session.execute(
            select(GarminStrengthSet.exercise_category)
            .where(GarminStrengthSet.exercise_category.isnot(None))
            .distinct()
        ).scalars().all()
        return {"categories": sorted(set(rows) | _EXTRA_CATEGORIES)}
    finally:
        session.close()


@router.get("/activity/{strava_id}")
def activity_detail(strava_id: int):
    session = _session()
    try:
        act = session.get(StravaActivity, strava_id)
        if not act:
            raise HTTPException(status_code=404, detail="Activity not found")

        # Pull that day's Garmin recovery snapshot for context
        garmin = session.get(GarminDaily, act.date)

        pace_per_km = None
        if act.duration_sec and act.distance_m and act.distance_m > 0:
            pace_per_km = act.duration_sec / (act.distance_m / 1000)

        return {
            "id": act.strava_id,
            "date": str(act.date),
            "name": act.name,
            "sport_type": act.sport_type,
            "duration_sec": act.duration_sec,
            "duration_min": round(act.duration_sec / 60, 1) if act.duration_sec else None,
            "distance_m": act.distance_m,
            "distance_km": round(act.distance_m / 1000, 2) if act.distance_m else None,
            "elevation_m": round(act.elevation_m) if act.elevation_m else None,
            "avg_hr": round(act.avg_hr) if act.avg_hr else None,
            "max_hr": act.max_hr,
            "avg_power": act.avg_power,
            "suffer_score": act.suffer_score,
            "perceived_exertion": act.perceived_exertion,
            "pace_per_km_sec": round(pace_per_km) if pace_per_km else None,
            "recovery_context": {
                "hrv_rmssd": garmin.hrv_rmssd if garmin else None,
                "hrv_status": garmin.hrv_status if garmin else None,
                "resting_hr": garmin.resting_hr if garmin else None,
                "sleep_score": garmin.sleep_score if garmin else None,
                "sleep_duration_hours": round(garmin.sleep_duration_min / 60, 1) if garmin and garmin.sleep_duration_min else None,
                "body_battery": garmin.body_battery_start if garmin else None,
                "overnight_stress": garmin.overnight_stress_avg if garmin else None,
            },
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
