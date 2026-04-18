"""Recovery scoring and workout recommendation context builder."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from recovery import config as cfg_mod
from recovery.db.models import GarminDaily, StravaActivity


class RecoveryStatus(str, Enum):
    EXCELLENT = "Excellent"
    GOOD = "Good"
    MODERATE = "Moderate"
    POOR = "Poor"
    NO_DATA = "No Data"


class RecommendedIntensity(str, Enum):
    HARD = "Hard"
    MODERATE = "Moderate"
    EASY = "Easy"
    ACTIVE_RECOVERY = "Active Recovery"
    REST = "Rest"


@dataclass
class DailySnapshot:
    date: date
    hrv_rmssd: float | None
    hrv_status: str | None
    hrv_baseline_low: float | None
    hrv_baseline_high: float | None
    resting_hr: int | None
    sleep_score: int | None
    sleep_duration_min: int | None
    sleep_deep_min: int | None
    sleep_rem_min: int | None
    overnight_stress_avg: float | None
    overnight_stress_qualifier: str | None
    body_battery_start: int | None


@dataclass
class RecoveryAssessment:
    status: RecoveryStatus
    recommended_intensity: RecommendedIntensity
    hrv_vs_baseline_pct: float | None
    signals: list[str]
    warnings: list[str]


def get_snapshot(session: Session, day: date) -> DailySnapshot | None:
    row = session.get(GarminDaily, day)
    if not row:
        return None
    return DailySnapshot(
        date=row.date,
        hrv_rmssd=row.hrv_rmssd,
        hrv_status=row.hrv_status,
        hrv_baseline_low=row.hrv_baseline_low,
        hrv_baseline_high=row.hrv_baseline_high,
        resting_hr=row.resting_hr,
        sleep_score=row.sleep_score,
        sleep_duration_min=row.sleep_duration_min,
        sleep_deep_min=row.sleep_deep_min,
        sleep_rem_min=row.sleep_rem_min,
        overnight_stress_avg=row.overnight_stress_avg,
        overnight_stress_qualifier=row.overnight_stress_qualifier,
        body_battery_start=row.body_battery_start,
    )


def get_trend(session: Session, days: int = 14) -> list[DailySnapshot]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows = session.execute(
        select(GarminDaily)
        .where(GarminDaily.date >= start, GarminDaily.date <= end)
        .order_by(GarminDaily.date)
    ).scalars().all()
    return [
        DailySnapshot(
            date=r.date,
            hrv_rmssd=r.hrv_rmssd,
            hrv_status=r.hrv_status,
            hrv_baseline_low=r.hrv_baseline_low,
            hrv_baseline_high=r.hrv_baseline_high,
            resting_hr=r.resting_hr,
            sleep_score=r.sleep_score,
            sleep_duration_min=r.sleep_duration_min,
            sleep_deep_min=r.sleep_deep_min,
            sleep_rem_min=r.sleep_rem_min,
            overnight_stress_avg=r.overnight_stress_avg,
            overnight_stress_qualifier=r.overnight_stress_qualifier,
            body_battery_start=r.body_battery_start,
        )
        for r in rows
    ]


def get_recent_activities(session: Session, days: int = 7) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    rows = session.execute(
        select(StravaActivity)
        .where(StravaActivity.date >= start, StravaActivity.date <= end)
        .order_by(StravaActivity.date.desc())
    ).scalars().all()
    return [
        {
            "date": str(r.date),
            "name": r.name,
            "sport_type": r.sport_type,
            "duration_min": round(r.duration_sec / 60) if r.duration_sec else None,
            "distance_km": round(r.distance_m / 1000, 1) if r.distance_m else None,
            "avg_hr": r.avg_hr,
            "suffer_score": r.suffer_score,
        }
        for r in rows
    ]


def assess(snapshot: DailySnapshot | None, cfg=None) -> RecoveryAssessment:
    if cfg is None:
        cfg = cfg_mod.get()
    rc = cfg.recovery

    if snapshot is None:
        return RecoveryAssessment(
            status=RecoveryStatus.NO_DATA,
            recommended_intensity=RecommendedIntensity.REST,
            hrv_vs_baseline_pct=None,
            signals=[],
            warnings=["No data available for today."],
        )

    signals: list[str] = []
    warnings: list[str] = []
    score = 0  # higher = better recovery

    # HRV assessment
    hrv_pct = None
    if snapshot.hrv_rmssd and snapshot.hrv_baseline_low and snapshot.hrv_baseline_high:
        baseline_mid = (snapshot.hrv_baseline_low + snapshot.hrv_baseline_high) / 2
        hrv_pct = snapshot.hrv_rmssd / baseline_mid
        if hrv_pct >= rc.hrv_high_pct:
            score += 2
            signals.append(f"HRV {snapshot.hrv_rmssd:.0f}ms — well above baseline ({hrv_pct:.0%})")
        elif hrv_pct >= 1.0:
            score += 1
            signals.append(f"HRV {snapshot.hrv_rmssd:.0f}ms — above baseline ({hrv_pct:.0%})")
        elif hrv_pct >= rc.hrv_low_pct:
            score += 0
            signals.append(f"HRV {snapshot.hrv_rmssd:.0f}ms — near baseline ({hrv_pct:.0%})")
        else:
            score -= 2
            warnings.append(f"HRV {snapshot.hrv_rmssd:.0f}ms — below baseline ({hrv_pct:.0%})")
    elif snapshot.hrv_status:
        status_map = {"POOR": -2, "LOW": -1, "BALANCED": 0, "GOOD": 1}
        score += status_map.get(snapshot.hrv_status.upper(), 0)
        signals.append(f"HRV status: {snapshot.hrv_status}")

    # Sleep assessment
    if snapshot.sleep_score is not None:
        if snapshot.sleep_score >= 80:
            score += 2
            signals.append(f"Sleep score {snapshot.sleep_score}/100 — excellent")
        elif snapshot.sleep_score >= 65:
            score += 1
            signals.append(f"Sleep score {snapshot.sleep_score}/100 — good")
        elif snapshot.sleep_score >= 50:
            score += 0
            signals.append(f"Sleep score {snapshot.sleep_score}/100 — fair")
        else:
            score -= 1
            warnings.append(f"Sleep score {snapshot.sleep_score}/100 — poor")

    if snapshot.sleep_duration_min is not None:
        hours = snapshot.sleep_duration_min / 60
        if hours < rc.sleep_min_hours:
            score -= 1
            warnings.append(f"Sleep duration {hours:.1f}h — below {rc.sleep_min_hours}h target")
        else:
            signals.append(f"Sleep duration {hours:.1f}h")

    # Overnight stress
    if snapshot.overnight_stress_avg is not None:
        if snapshot.overnight_stress_avg <= rc.overnight_stress_low:
            score += 1
            signals.append(f"Overnight stress {snapshot.overnight_stress_avg:.0f} — restful")
        elif snapshot.overnight_stress_avg >= rc.overnight_stress_high:
            score -= 1
            warnings.append(f"Overnight stress {snapshot.overnight_stress_avg:.0f} — elevated")
        else:
            signals.append(f"Overnight stress {snapshot.overnight_stress_avg:.0f} — moderate")

    # Map score to status
    if score >= 4:
        status = RecoveryStatus.EXCELLENT
        intensity = RecommendedIntensity.HARD
    elif score >= 2:
        status = RecoveryStatus.GOOD
        intensity = RecommendedIntensity.MODERATE
    elif score >= 0:
        status = RecoveryStatus.MODERATE
        intensity = RecommendedIntensity.EASY
    elif score >= -2:
        status = RecoveryStatus.POOR
        intensity = RecommendedIntensity.ACTIVE_RECOVERY
    else:
        status = RecoveryStatus.POOR
        intensity = RecommendedIntensity.REST

    return RecoveryAssessment(
        status=status,
        recommended_intensity=intensity,
        hrv_vs_baseline_pct=hrv_pct,
        signals=signals,
        warnings=warnings,
    )


def build_workout_context(session: Session, day: date | None = None) -> dict:
    """Build the full context dict passed to Claude for workout recommendation."""
    if day is None:
        day = date.today() - timedelta(days=1)  # most recent complete data

    cfg = cfg_mod.get()
    snapshot = get_snapshot(session, day)
    assessment = assess(snapshot, cfg)
    trend = get_trend(session, days=7)
    activities = get_recent_activities(session, days=7)

    hrv_trend = [s.hrv_rmssd for s in trend if s.hrv_rmssd is not None]
    hrv_direction = "stable"
    if len(hrv_trend) >= 3:
        recent_avg = sum(hrv_trend[-3:]) / 3
        older_avg = sum(hrv_trend[:3]) / 3
        if recent_avg > older_avg * 1.05:
            hrv_direction = "improving"
        elif recent_avg < older_avg * 0.95:
            hrv_direction = "declining"

    return {
        "date": str(day),
        "recovery_status": assessment.status.value,
        "recommended_intensity": assessment.recommended_intensity.value,
        "signals": assessment.signals,
        "warnings": assessment.warnings,
        "hrv_rmssd": snapshot.hrv_rmssd if snapshot else None,
        "hrv_vs_baseline_pct": assessment.hrv_vs_baseline_pct,
        "hrv_7day_direction": hrv_direction,
        "sleep_score": snapshot.sleep_score if snapshot else None,
        "sleep_duration_min": snapshot.sleep_duration_min if snapshot else None,
        "overnight_stress": snapshot.overnight_stress_avg if snapshot else None,
        "resting_hr": snapshot.resting_hr if snapshot else None,
        "body_battery_start": snapshot.body_battery_start if snapshot else None,
        "recent_activities": activities,
        "equipment": cfg.equipment.summary(),
        "sauna_available": cfg.equipment.sauna,
        "user_name": cfg.user.name,
    }
