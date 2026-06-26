"""JSON API routes consumed by Chart.js on the frontend."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from recovery.analysis.recovery import assess, get_snapshot, get_trend, get_recent_activities
from recovery.db.models import GarminActivity, GarminDaily, GarminStrengthSet, StravaActivity, WeightEntry
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
                "steps": snapshot.steps,
                "stress_first_half_avg": snapshot.stress_first_half_avg,
                "stress_second_half_avg": snapshot.stress_second_half_avg,
                "stress_second_half_min": snapshot.stress_second_half_min,
                "stress_recovery_delta": snapshot.stress_recovery_delta,
                "stress_time_below_20_min": snapshot.stress_time_below_20_min,
                "stress_recovery_pct": round(snapshot.stress_recovery_delta / snapshot.stress_first_half_avg * 100, 1)
                    if snapshot.stress_recovery_delta and snapshot.stress_first_half_avg else None,
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
    "DB_PRESS_EACH_ARM",
    "CABLE_ROW",
    "UP_AND_DOWN_BICEPS",
    "TRICEPS_AMRAP",
    "SINGLE_LEG_KB_DEADLIFT",
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
        # Any manual edit locks the session so a future re-sync won't clobber it.
        act = session.get(GarminActivity, s.garmin_activity_id)
        if act:
            act.manually_edited = 1
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


@router.get("/sleep")
def sleep(days: int = Query(default=30, ge=7, le=365)):
    session = _session()
    try:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = session.execute(
            select(GarminDaily)
            .where(GarminDaily.date >= start, GarminDaily.date <= end)
            .order_by(GarminDaily.date.desc())
        ).scalars().all()

        data = []
        for r in rows:
            duration_h = round(r.sleep_duration_min / 60, 2) if r.sleep_duration_min else None
            data.append({
                "date": str(r.date),
                "sleep_score": r.sleep_score,
                "duration_hours": duration_h,
                "deep_min": r.sleep_deep_min,
                "rem_min": r.sleep_rem_min,
                "light_min": r.sleep_light_min,
                "awake_min": r.sleep_awake_min,
                "overnight_stress": round(r.overnight_stress_avg, 1) if r.overnight_stress_avg else None,
                "body_battery": r.body_battery_start,
                "steps": r.steps,
                "stress_first_half_avg": round(r.stress_first_half_avg, 1) if r.stress_first_half_avg else None,
                "stress_second_half_avg": round(r.stress_second_half_avg, 1) if r.stress_second_half_avg else None,
                "stress_second_half_min": r.stress_second_half_min,
                "stress_recovery_delta": round(r.stress_recovery_delta, 1) if r.stress_recovery_delta else None,
                "stress_time_below_20_min": r.stress_time_below_20_min,
            })

        valid = [d for d in data if d["sleep_score"] is not None]
        avg = lambda key: round(sum(d[key] for d in data if d.get(key)) / max(sum(1 for d in data if d.get(key)), 1), 1)

        from recovery import config as cfg_mod
        cfg = cfg_mod.get()
        return {
            "days": days,
            "thresholds": {
                "sleep_min_hours": cfg.recovery.sleep_min_hours,
                "stress_low": cfg.recovery.overnight_stress_low,
                "stress_high": cfg.recovery.overnight_stress_high,
            },
            "averages": {
                "sleep_score": avg("sleep_score"),
                "duration_hours": avg("duration_hours"),
                "overnight_stress": avg("overnight_stress"),
                "body_battery": avg("body_battery"),
                "deep_min": avg("deep_min"),
                "rem_min": avg("rem_min"),
                "stress_first_half_avg": avg("stress_first_half_avg"),
                "stress_second_half_avg": avg("stress_second_half_avg"),
                "stress_time_below_20_min": avg("stress_time_below_20_min"),
            },
            "data": data,
        }
    finally:
        session.close()


@router.get("/sleep/{night_date}")
def sleep_night(night_date: str):
    """Detail for one night of sleep (the night ending on the morning of night_date)."""
    try:
        d = date.fromisoformat(night_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date, expected YYYY-MM-DD")

    session = _session()
    try:
        row = session.get(GarminDaily, d)
        if not row:
            raise HTTPException(status_code=404, detail="No data for that night")

        # 30-day window averages for comparison
        win_start = d - timedelta(days=30)
        win = session.execute(
            select(GarminDaily).where(GarminDaily.date >= win_start, GarminDaily.date < d)
        ).scalars().all()

        def _avg(field, digits=1):
            vals = [getattr(r, field) for r in win if getattr(r, field) is not None]
            return round(sum(vals) / len(vals), digits) if vals else None

        averages = {
            "sleep_score": _avg("sleep_score", 0),
            "sleep_duration_min": _avg("sleep_duration_min", 0),
            "sleep_deep_min": _avg("sleep_deep_min", 0),
            "sleep_rem_min": _avg("sleep_rem_min", 0),
            "overnight_stress_avg": _avg("overnight_stress_avg"),
            "stress_first_half_avg": _avg("stress_first_half_avg"),
            "stress_second_half_avg": _avg("stress_second_half_avg"),
            "stress_time_below_20_min": _avg("stress_time_below_20_min", 0),
            "body_battery_start": _avg("body_battery_start", 0),
            "hrv_rmssd": _avg("hrv_rmssd", 0),
            "resting_hr": _avg("resting_hr", 0),
        }

        # Workouts the previous day (what this night was recovering from)
        prev = d - timedelta(days=1)
        workouts = []
        for a in session.execute(select(StravaActivity).where(StravaActivity.date == prev)).scalars():
            workouts.append({
                "id": a.strava_id, "source": "strava", "name": a.name or a.sport_type,
                "sport_type": a.sport_type,
                "duration_min": round(a.duration_sec / 60) if a.duration_sec else None,
                "suffer_score": a.suffer_score,
            })
        for a in session.execute(select(GarminActivity).where(GarminActivity.date == prev)).scalars():
            workouts.append({
                "id": a.garmin_id, "source": "garmin", "name": a.name or "Strength",
                "sport_type": a.sport_type,
                "duration_min": round(a.duration_sec / 60) if a.duration_sec else None,
                "suffer_score": None,
            })

        # Live stress curve from Garmin (best effort — page degrades gracefully without it)
        curve = []
        try:
            from recovery.ingest import garmin as garmin_ingest
            api = garmin_ingest.load_session()
            ds = d.strftime("%Y-%m-%d")
            daily = (api.get_sleep_data(ds) or {}).get("dailySleepDTO", {})
            start_gmt = daily.get("sleepStartTimestampGMT")
            end_gmt = daily.get("sleepEndTimestampGMT")
            start_local = daily.get("sleepStartTimestampLocal")
            if start_gmt and end_gmt and start_local:
                tz_offset_ms = start_local - start_gmt
                readings = []
                for cd in (prev, d):
                    try:
                        readings += (api.get_stress_data(cd.strftime("%Y-%m-%d")) or {}).get("stressValuesArray", [])
                    except Exception:
                        pass
                from datetime import datetime, timezone
                for ts, v in sorted(set(map(tuple, readings))):
                    if start_gmt <= ts <= end_gmt and v >= 0:
                        local_dt = datetime.fromtimestamp((ts + tz_offset_ms) / 1000, tz=timezone.utc)
                        curve.append({"t": local_dt.strftime("%H:%M"), "v": v})
        except Exception:
            curve = []

        recovery_pct = (
            round(row.stress_recovery_delta / row.stress_first_half_avg * 100, 1)
            if row.stress_recovery_delta is not None and row.stress_first_half_avg
            else None
        )

        return {
            "date": str(d),
            "sleep_start": row.sleep_start.isoformat() if row.sleep_start else None,
            "sleep_end": row.sleep_end.isoformat() if row.sleep_end else None,
            "sleep_score": row.sleep_score,
            "sleep_duration_min": row.sleep_duration_min,
            "sleep_deep_min": row.sleep_deep_min,
            "sleep_rem_min": row.sleep_rem_min,
            "sleep_light_min": row.sleep_light_min,
            "sleep_awake_min": row.sleep_awake_min,
            "overnight_stress_avg": row.overnight_stress_avg,
            "overnight_stress_qualifier": row.overnight_stress_qualifier,
            "stress_first_half_avg": row.stress_first_half_avg,
            "stress_second_half_avg": row.stress_second_half_avg,
            "stress_second_half_min": row.stress_second_half_min,
            "stress_recovery_delta": row.stress_recovery_delta,
            "stress_recovery_pct": recovery_pct,
            "stress_time_below_20_min": row.stress_time_below_20_min,
            "body_battery_start": row.body_battery_start,
            "hrv_rmssd": row.hrv_rmssd,
            "hrv_status": row.hrv_status,
            "resting_hr": row.resting_hr,
            "averages_30d": averages,
            "pre_sleep_workouts": workouts,
            "stress_curve": curve,
        }
    finally:
        session.close()


@router.get("/trend")
def trend(days: int = Query(default=30, ge=7, le=365)):
    session = _session()
    try:
        snapshots = get_trend(session, days=days)

        # For each snapshot date D, look up workouts on D-1 (the evening before that sleep night).
        # Include both Strava and Garmin strength activities.
        dates = [s.date for s in snapshots]
        prev_dates = [d - timedelta(days=1) for d in dates]
        strava_rows = session.execute(
            select(StravaActivity)
            .where(StravaActivity.date.in_(prev_dates))
        ).scalars().all()
        garmin_rows = session.execute(
            select(GarminActivity)
            .where(GarminActivity.date.in_(prev_dates))
        ).scalars().all()

        # Map previous-day date → list of workout dicts
        workouts: dict[date, list] = {}
        for a in strava_rows:
            workouts.setdefault(a.date, []).append({
                "id": a.strava_id, "name": a.name or a.sport_type, "source": "strava"
            })
        for a in garmin_rows:
            workouts.setdefault(a.date, []).append({
                "id": a.garmin_id, "name": a.name or "Strength", "source": "garmin"
            })

        from recovery import config as cfg_mod
        cfg = cfg_mod.get()
        return {
            "stress_low_threshold": cfg.recovery.overnight_stress_low,
            "labels": [str(s.date) for s in snapshots],
            "hrv": [s.hrv_rmssd for s in snapshots],
            "hrv_baseline_low": [s.hrv_baseline_low for s in snapshots],
            "hrv_baseline_high": [s.hrv_baseline_high for s in snapshots],
            "resting_hr": [s.resting_hr for s in snapshots],
            "sleep_score": [s.sleep_score for s in snapshots],
            "sleep_hours": [round(s.sleep_duration_min / 60, 1) if s.sleep_duration_min else None for s in snapshots],
            "overnight_stress": [s.overnight_stress_avg for s in snapshots],
            "steps": [s.steps for s in snapshots],
            "stress_first_half_avg": [s.stress_first_half_avg for s in snapshots],
            "stress_recovery_delta": [s.stress_recovery_delta for s in snapshots],
            "stress_time_below_20_min": [s.stress_time_below_20_min for s in snapshots],
            # workouts[i] = list of workouts on the day before snapshots[i] (the pre-sleep evening)
            "pre_sleep_workouts": [workouts.get(d - timedelta(days=1), []) for d in dates],
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


@router.get("/weight")
def weight(days: int = Query(default=90, ge=7, le=1825)):
    session = _session()
    try:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = session.execute(
            select(WeightEntry)
            .where(WeightEntry.date >= start, WeightEntry.date <= end)
            .order_by(WeightEntry.date.desc())
        ).scalars().all()

        data = []
        for r in rows:
            data.append({
                "date": str(r.date),
                "actual_weight_lbs": r.actual_weight_lbs,
                "trend_weight_lbs": r.trend_weight_lbs,
                "actual_fat_pct": r.actual_fat_pct,
                "trend_fat_pct": r.trend_fat_pct,
                "weight_is_interpolated": bool(r.weight_is_interpolated),
                "fat_is_interpolated": bool(r.fat_is_interpolated),
            })

        actual = [d["actual_weight_lbs"] for d in data if d["actual_weight_lbs"] and not d["weight_is_interpolated"]]
        trend = [d["trend_weight_lbs"] for d in data if d["trend_weight_lbs"]]
        fat = [d["actual_fat_pct"] for d in data if d["actual_fat_pct"] and not d["fat_is_interpolated"]]

        def _avg(lst):
            return round(sum(lst) / len(lst), 1) if lst else None

        latest = next((d for d in data if d["trend_weight_lbs"]), None)

        return {
            "days": days,
            "averages": {
                "actual_weight_lbs": _avg(actual),
                "trend_weight_lbs": _avg(trend),
                "actual_fat_pct": _avg(fat),
            },
            "latest": {
                "trend_weight_lbs": latest["trend_weight_lbs"] if latest else None,
                "trend_fat_pct": latest["trend_fat_pct"] if latest else None,
                "date": latest["date"] if latest else None,
            },
            "data": data,
        }
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


# ── Document corpus (knowledge.db — separate from personal memory) ───────────

@router.post("/documents")
async def upload_document(file: UploadFile = File(...)):
    """Ingest a PDF into the document knowledge base (knowledge.db)."""
    from recovery.config import get as get_config
    from recovery.knowledge.ingest import ingest_pdf

    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    data = await file.read()
    max_bytes = get_config().knowledge.max_pdf_mb * 1024 * 1024
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {get_config().knowledge.max_pdf_mb} MB limit.")

    try:
        return ingest_pdf(name, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingest failed: {e}")


@router.get("/documents")
def get_documents():
    """List documents in the knowledge base."""
    from recovery.knowledge.ingest import list_documents
    return {"documents": list_documents()}


@router.delete("/documents/{doc_id}")
def remove_document(doc_id: str):
    """Delete a document and all its chunks from the knowledge base."""
    from recovery.knowledge.ingest import delete_document
    removed = delete_document(doc_id)
    if removed == 0:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted_chunks": removed}
