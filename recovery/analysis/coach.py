"""Workout generation — builds recovery-grounded context and streams a plan from Claude.

The dashboard's Coach tab calls this. Recovery-first: today's recovery score
governs intensity; the user's free-text instructions shape exercise choice and
tone but cannot push past what recovery allows. Generation streams token-by-token.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from recovery import config as cfg_mod
from recovery.analysis.recovery import build_workout_context
from recovery.db.models import GarminActivity

_G_TO_LBS = 0.00220462


class CoachUnavailable(RuntimeError):
    """Raised when generation is requested but ANTHROPIC_API_KEY is missing."""


def _recent_strength(session: Session, days: int = 21) -> list[dict]:
    """Recent strength sessions with per-set exercise/reps/weight, for progression."""
    today = date.today()
    start = today - timedelta(days=days - 1)
    acts = session.execute(
        select(GarminActivity)
        .where(GarminActivity.date >= start, GarminActivity.date <= today)
        .order_by(GarminActivity.date.desc(), GarminActivity.garmin_id)
    ).scalars().all()
    out = []
    for act in acts:
        sets_out = []
        for s in sorted(act.sets, key=lambda x: x.set_index):
            sets_out.append({
                "exercise": s.exercise_category_override or s.exercise_category or "UNKNOWN",
                "reps": s.reps,
                "weight_lbs": round(s.weight_g * _G_TO_LBS) if s.weight_g else None,
            })
        if sets_out:
            out.append({"date": str(act.date), "sets": sets_out})
    return out


def build_prompt(session: Session, workout_type: str, instructions: str) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for a cardio or strength workout."""
    import json

    ctx = build_workout_context(session)

    system = (
        "You are a recovery-aware strength & conditioning coach for a single athlete. "
        "You are RECOVERY-FIRST: today's recovery status and HRV-vs-baseline govern how hard "
        "the session should be. Honor the athlete's instructions for exercise selection, vibe, "
        "and focus — but never let them push intensity past what the recovery data supports. "
        "If recovery is poor and they ask for a hard day, give a smart scaled-back session and "
        "say plainly why you held back.\n\n"
        "Output a clear, structured workout in markdown: a short opening line on today's readiness, "
        "then **Warm-up**, **Main**, and **Cool-down** sections. For strength use exercise / sets x reps "
        "@ weight (lbs), grounded in the athlete's recent lifts for sensible progression. For cardio use "
        "modality / duration / target intensity (zone, pace, or RPE) and distance where it makes sense. "
        "End with 1-2 sentences of rationale tying the plan to today's data. Only suggest exercises the "
        "athlete's equipment supports."
    )

    payload: dict = {
        "workout_type": workout_type,
        "athlete_instructions": instructions.strip() or "(none given)",
        "recovery": {
            "status": ctx.get("recovery_status"),
            "recommended_intensity": ctx.get("recommended_intensity"),
            "signals": ctx.get("signals"),
            "warnings": ctx.get("warnings"),
            "hrv_rmssd": ctx.get("hrv_rmssd"),
            "hrv_vs_baseline_pct": ctx.get("hrv_vs_baseline_pct"),
            "hrv_7day_direction": ctx.get("hrv_7day_direction"),
            "sleep_score": ctx.get("sleep_score"),
            "sleep_duration_min": ctx.get("sleep_duration_min"),
            "overnight_stress": ctx.get("overnight_stress"),
            "resting_hr": ctx.get("resting_hr"),
            "body_battery_start": ctx.get("body_battery_start"),
        },
        "equipment": ctx.get("equipment"),
        "sauna_available": ctx.get("sauna_available"),
        "recent_cardio": ctx.get("recent_activities"),
    }
    if workout_type == "strength":
        payload["recent_strength_sessions"] = _recent_strength(session)

    user = (
        f"Generate a {workout_type} workout for today.\n\n"
        f"Context (JSON):\n{json.dumps(payload, indent=2, default=str)}"
    )
    return system, user


def stream_workout(session: Session, workout_type: str, instructions: str):
    """Yield text deltas of the generated workout from Claude (streaming)."""
    if workout_type not in ("cardio", "strength"):
        raise ValueError("workout_type must be 'cardio' or 'strength'")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise CoachUnavailable("ANTHROPIC_API_KEY not set; cannot generate workouts.")

    system, user = build_prompt(session, workout_type, instructions)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = cfg_mod.get().coach.model
    with client.messages.stream(
        model=model,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            yield text
