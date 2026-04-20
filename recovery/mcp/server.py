"""MCP server for Claude Desktop — exposes recovery data and workout recommendations."""
from __future__ import annotations

from datetime import date, timedelta

from fastmcp import FastMCP

from recovery.db.session import get_session, init_db
from recovery.mcp.memory_tools import get_related_entities, query_memory, save_memory

mcp = FastMCP("Recovery Bot")

mcp.tool()(save_memory)
mcp.tool()(query_memory)
mcp.tool()(get_related_entities)


def _session():
    engine = init_db()
    return get_session(engine)


@mcp.tool()
def get_today_status() -> dict:
    """
    Get today's recovery status including HRV, sleep, resting heart rate,
    and overnight stress. Returns a plain-English assessment and all raw metrics.
    Call this when the user asks 'what's my condition today?' or similar.
    """
    from recovery.analysis.recovery import assess, get_snapshot
    from recovery import config as cfg_mod

    session = _session()
    try:
        day = date.today()
        snapshot = get_snapshot(session, day)
        assessment = assess(snapshot)
        cfg = cfg_mod.get()

        result = {
            "as_of_date": str(day),
            "recovery_status": assessment.status.value,
            "recommended_intensity": assessment.recommended_intensity.value,
            "signals": assessment.signals,
            "warnings": assessment.warnings,
        }

        if snapshot:
            result["metrics"] = {
                "hrv_rmssd_ms": snapshot.hrv_rmssd,
                "hrv_status": snapshot.hrv_status,
                "hrv_vs_baseline_pct": round(assessment.hrv_vs_baseline_pct * 100, 1) if assessment.hrv_vs_baseline_pct else None,
                "resting_hr_bpm": snapshot.resting_hr,
                "sleep_score": snapshot.sleep_score,
                "sleep_duration_hours": round(snapshot.sleep_duration_min / 60, 1) if snapshot.sleep_duration_min else None,
                "sleep_deep_min": snapshot.sleep_deep_min,
                "sleep_rem_min": snapshot.sleep_rem_min,
                "overnight_stress_avg": snapshot.overnight_stress_avg,
                "overnight_stress_qualifier": snapshot.overnight_stress_qualifier,
                "body_battery_on_wake": snapshot.body_battery_start,
            }

        return result
    finally:
        session.close()


@mcp.tool()
def get_recovery_trend(days: int = 14) -> dict:
    """
    Get HRV, sleep, and RHR trends over the past N days (default 14).
    Returns day-by-day data and a direction assessment (improving/stable/declining).
    """
    from recovery.analysis.recovery import get_trend

    session = _session()
    try:
        snapshots = get_trend(session, days=days)
        data = []
        for s in snapshots:
            data.append({
                "date": str(s.date),
                "hrv_rmssd": s.hrv_rmssd,
                "hrv_status": s.hrv_status,
                "resting_hr": s.resting_hr,
                "sleep_score": s.sleep_score,
                "sleep_duration_hours": round(s.sleep_duration_min / 60, 1) if s.sleep_duration_min else None,
                "overnight_stress": s.overnight_stress_avg,
            })

        hrv_vals = [s.hrv_rmssd for s in snapshots if s.hrv_rmssd]
        direction = "stable"
        if len(hrv_vals) >= 6:
            recent = sum(hrv_vals[-3:]) / 3
            older = sum(hrv_vals[:3]) / 3
            if recent > older * 1.05:
                direction = "improving"
            elif recent < older * 0.95:
                direction = "declining"

        return {
            "days_requested": days,
            "days_available": len(data),
            "hrv_trend_direction": direction,
            "data": data,
        }
    finally:
        session.close()


@mcp.tool()
def get_recent_activities(days: int = 7) -> dict:
    """
    Get recent Strava activities for the past N days (default 7).
    Includes sport type, duration, distance, heart rate, and effort score.
    """
    from recovery.analysis.recovery import get_recent_activities as _get

    session = _session()
    try:
        activities = _get(session, days=days)
        total_suffer = sum(a["suffer_score"] for a in activities if a.get("suffer_score"))
        return {
            "days": days,
            "activity_count": len(activities),
            "total_suffer_score": total_suffer,
            "activities": activities,
        }
    finally:
        session.close()


@mcp.tool()
def recommend_workout() -> dict:
    """
    Generate a personalized workout recommendation based on today's recovery data,
    recent training load, and available equipment. Returns recovery context — Claude
    should use this data to reason about and produce a specific workout plan.
    """
    from recovery.analysis.recovery import build_workout_context

    session = _session()
    try:
        context = build_workout_context(session)
        context["instruction"] = (
            "Based on the recovery data and available equipment above, recommend a specific workout. "
            "Include: recovery status summary, recommended intensity, a concrete workout with "
            "sets/reps/distances/durations as appropriate for the sport types this athlete does, "
            "and 2-3 sentences of rationale tying it to today's data. "
            "If sauna_available is true, include whether to use it today and optimal timing "
            "(pre/post workout or rest day). Be specific and actionable."
        )
        return context
    finally:
        session.close()


@mcp.tool()
def get_training_load(days: int = 28) -> dict:
    """
    Get training load summary: acute load (last 7 days) vs chronic load (last 28 days),
    and how current recovery metrics relate to recent training.
    """
    from sqlalchemy import select, func
    from recovery.db.models import StravaActivity, GarminDaily

    session = _session()
    try:
        today = date.today()

        def load_for_window(d: int) -> dict:
            start = today - timedelta(days=d)
            rows = session.execute(
                select(StravaActivity)
                .where(StravaActivity.date >= start, StravaActivity.date <= today)
            ).scalars().all()
            return {
                "activity_count": len(rows),
                "total_duration_hours": round(sum(r.duration_sec or 0 for r in rows) / 3600, 1),
                "total_suffer_score": sum(r.suffer_score or 0 for r in rows),
                "sport_breakdown": _sport_breakdown(rows),
            }

        acute = load_for_window(7)
        chronic = load_for_window(days)

        # Average HRV over last 7 days
        hrv_rows = session.execute(
            select(GarminDaily)
            .where(GarminDaily.date >= today - timedelta(days=7))
        ).scalars().all()
        hrv_vals = [r.hrv_rmssd for r in hrv_rows if r.hrv_rmssd]
        avg_hrv = round(sum(hrv_vals) / len(hrv_vals), 1) if hrv_vals else None

        return {
            "acute_7day": acute,
            "chronic_window_days": days,
            "chronic": chronic,
            "acute_chronic_ratio": round(
                acute["total_suffer_score"] / (chronic["total_suffer_score"] / (days / 7)), 2
            ) if chronic["total_suffer_score"] else None,
            "avg_hrv_last_7days": avg_hrv,
        }
    finally:
        session.close()


@mcp.tool()
def query_date_range(metric: str, start_date: str, end_date: str) -> dict:
    """
    Query any metric over a date range.
    metric: one of 'hrv', 'sleep', 'rhr', 'stress', 'activities'
    start_date / end_date: YYYY-MM-DD strings
    """
    from sqlalchemy import select
    from recovery.db.models import GarminDaily, StravaActivity

    session = _session()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)

        if metric == "activities":
            rows = session.execute(
                select(StravaActivity)
                .where(StravaActivity.date >= start, StravaActivity.date <= end)
                .order_by(StravaActivity.date)
            ).scalars().all()
            return {
                "metric": "activities",
                "start": start_date,
                "end": end_date,
                "data": [
                    {"date": str(r.date), "sport_type": r.sport_type, "duration_min": round(r.duration_sec / 60) if r.duration_sec else None,
                     "distance_km": round(r.distance_m / 1000, 1) if r.distance_m else None, "suffer_score": r.suffer_score}
                    for r in rows
                ],
            }

        rows = session.execute(
            select(GarminDaily)
            .where(GarminDaily.date >= start, GarminDaily.date <= end)
            .order_by(GarminDaily.date)
        ).scalars().all()

        field_map = {
            "hrv": lambda r: {"hrv_rmssd": r.hrv_rmssd, "hrv_status": r.hrv_status},
            "sleep": lambda r: {"sleep_score": r.sleep_score, "sleep_duration_hours": round(r.sleep_duration_min / 60, 1) if r.sleep_duration_min else None},
            "rhr": lambda r: {"resting_hr": r.resting_hr},
            "stress": lambda r: {"overnight_stress_avg": r.overnight_stress_avg, "qualifier": r.overnight_stress_qualifier},
        }

        extract = field_map.get(metric, lambda r: {})
        return {
            "metric": metric,
            "start": start_date,
            "end": end_date,
            "data": [{"date": str(r.date), **extract(r)} for r in rows],
        }
    finally:
        session.close()


_G_TO_LBS = 0.00220462


@mcp.tool()
def get_strength_sessions(days: int = 7) -> dict:
    """
    Get strength training sessions with full exercise-level detail for the past N days (default 7).
    Returns each session with every set: exercise category, reps, and weight in lbs.
    Use this when the user asks about their lifting, strength work, sets, reps, or weights.
    """
    from sqlalchemy import select
    from recovery.db.models import GarminActivity

    session = _session()
    try:
        today = date.today()
        start = today - timedelta(days=days - 1)

        acts = session.execute(
            select(GarminActivity)
            .where(GarminActivity.date >= start, GarminActivity.date <= today)
            .order_by(GarminActivity.date.desc(), GarminActivity.garmin_id)
        ).scalars().all()

        sessions_out = []
        for act in acts:
            sets_out = []
            for s in sorted(act.sets, key=lambda x: x.set_index):
                weight_lbs = round(s.weight_g * _G_TO_LBS) if s.weight_g else None
                sets_out.append({
                    "exercise": s.exercise_category_override or s.exercise_category or "UNKNOWN",
                    "reps": s.reps,
                    "weight_lbs": weight_lbs,
                })
            sessions_out.append({
                "date": str(act.date),
                "name": act.name or "Strength",
                "duration_min": round(act.duration_sec / 60) if act.duration_sec else None,
                "avg_hr": act.avg_hr,
                "set_count": len(sets_out),
                "sets": sets_out,
            })

        return {
            "days": days,
            "session_count": len(sessions_out),
            "sessions": sessions_out,
        }
    finally:
        session.close()


@mcp.tool()
def get_exercise_history(exercise: str, days: int = 90) -> dict:
    """
    Get the full history of a specific exercise over the past N days (default 90).
    exercise: Garmin category name, e.g. 'BENCH_PRESS', 'CURL', 'ONE_ARM_KETTLEBELL_SWING'.
              Case-insensitive. Pass 'list' to see all known exercise names.
    Returns sets grouped by session date so you can track progression over time.
    """
    from sqlalchemy import select
    from recovery.db.models import GarminActivity, GarminStrengthSet

    session = _session()
    try:
        # Handle 'list' to aid discoverability
        if exercise.strip().lower() == "list":
            rows = session.execute(
                select(GarminStrengthSet.exercise_category)
                .where(GarminStrengthSet.exercise_category.isnot(None))
                .distinct()
                .order_by(GarminStrengthSet.exercise_category)
            ).scalars().all()
            overrides = session.execute(
                select(GarminStrengthSet.exercise_category_override)
                .where(GarminStrengthSet.exercise_category_override.isnot(None))
                .distinct()
                .order_by(GarminStrengthSet.exercise_category_override)
            ).scalars().all()
            return {"known_exercises": sorted(set(rows) | set(overrides))}

        needle = exercise.strip().upper()
        today = date.today()
        start = today - timedelta(days=days - 1)

        sets = session.execute(
            select(GarminStrengthSet)
            .join(GarminActivity, GarminStrengthSet.garmin_activity_id == GarminActivity.garmin_id)
            .where(
                GarminActivity.date >= start,
                GarminActivity.date <= today,
            )
        ).scalars().all()

        # Filter to matching sets (override takes precedence)
        matching = [
            s for s in sets
            if (s.exercise_category_override or s.exercise_category or "").upper() == needle
        ]

        # Group by date
        by_date: dict[str, list] = {}
        for s in sorted(matching, key=lambda x: (x.start_time or date.min,)):
            act_date = str(session.get(GarminActivity, s.garmin_activity_id).date)
            by_date.setdefault(act_date, []).append({
                "reps": s.reps,
                "weight_lbs": round(s.weight_g * _G_TO_LBS) if s.weight_g else None,
            })

        history = [{"date": d, "sets": ss} for d, ss in sorted(by_date.items(), reverse=True)]

        return {
            "exercise": needle,
            "days": days,
            "session_count": len(history),
            "history": history,
        }
    finally:
        session.close()


@mcp.tool()
def log_strength_note(note: str, exercises: list[str], date_str: str = "") -> str:
    """
    Save a free-text note about a strength session and link it to named exercises.
    note: what happened, e.g. 'Bench felt strong today, hit 185 lbs for 3x5'
    exercises: list of exercise names to link in the knowledge graph, e.g. ['bench press', 'deadlift']
    date_str: optional YYYY-MM-DD, defaults to today
    Use this to log observations, PRs, form cues, or pain/discomfort notes.
    """
    day = date_str or str(date.today())
    full_note = f"[{day}] {note}"
    return save_memory(full_note, exercises, metadata={"type": "workout_note", "date": day})


def _sport_breakdown(rows) -> dict:
    breakdown: dict[str, int] = {}
    for r in rows:
        sport = r.sport_type or "Unknown"
        breakdown[sport] = breakdown.get(sport, 0) + 1
    return breakdown


def run_mcp():
    """Entry point for stdio MCP server (used by Claude Desktop)."""
    from recovery.db.session import init_db
    init_db()
    mcp.run()
