"""Garmin Connect data fetcher using garminconnect."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import Garmin

_TOKEN_DIR = Path.home() / ".recovery-bot" / "garmin_tokens"


def _client(email: str = "", password: str = "") -> Garmin:
    return Garmin(email=email, password=password)


def login(email: str, password: str) -> None:
    api = _client(email, password)
    api.login(tokenstore=str(_TOKEN_DIR))


def load_session() -> Garmin:
    if not _TOKEN_DIR.exists():
        raise RuntimeError("No Garmin session found. Run `recovery auth garmin` first.")
    api = _client()
    api.login(tokenstore=str(_TOKEN_DIR))
    return api


def fetch_hrv(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_hrv_data(ds) or {}
        summary = data.get("hrvSummary", {})
        return {
            "hrv_status": summary.get("status"),
            "hrv_rmssd": summary.get("lastNight"),
            "hrv_baseline_low": summary.get("baselineLowUpper"),
            "hrv_baseline_high": summary.get("baselineBalancedUpper"),
        }
    except Exception:
        return {}


def fetch_sleep(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_sleep_data(ds)
        daily = data.get("dailySleepDTO", {})
        start_ts = daily.get("sleepStartTimestampLocal")
        end_ts = daily.get("sleepEndTimestampLocal")
        return {
            "sleep_start": datetime.fromtimestamp(start_ts / 1000) if start_ts else None,
            "sleep_end": datetime.fromtimestamp(end_ts / 1000) if end_ts else None,
            "sleep_duration_min": daily.get("sleepTimeSeconds", 0) // 60 or None,
            "sleep_deep_min": daily.get("deepSleepSeconds", 0) // 60 or None,
            "sleep_light_min": daily.get("lightSleepSeconds", 0) // 60 or None,
            "sleep_rem_min": daily.get("remSleepSeconds", 0) // 60 or None,
            "sleep_awake_min": daily.get("awakeSleepSeconds", 0) // 60 or None,
            "sleep_score": daily.get("sleepScores", {}).get("overall", {}).get("value"),
        }
    except Exception:
        return {}


def fetch_rhr(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_rhr_day(ds)
        return {"resting_hr": data.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value")}
    except Exception:
        return {}


def fetch_overnight_stress(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_sleep_data(ds)
        daily = data.get("dailySleepDTO", {})
        stress_score = daily.get("sleepScores", {}).get("stress", {})
        return {
            "overnight_stress_avg": daily.get("avgSleepStress"),
            "overnight_stress_qualifier": stress_score.get("qualifierKey"),
        }
    except Exception:
        return {}


def fetch_body_battery(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_body_battery(ds)
        if data and isinstance(data, list):
            readings = data[0].get("bodyBatteryValuesArray", [])
            values = [r[1] for r in readings if len(r) > 1 and r[1] is not None]
            if values:
                return {"body_battery_start": max(values)}
        return {}
    except Exception:
        return {}


_CARDIO_TYPES = {
    "cycling", "running", "walking", "hiking", "swimming", "rowing",
    "elliptical", "cardio", "yoga", "pilates", "other",
}

_STRENGTH_TYPES = {"strength_training", "weight_training"}


def fetch_strength_activities(api: Garmin, day: date) -> list[dict]:
    """Return strength activities for the day with their exercise sets.

    Each item in the returned list is an activity dict with a 'sets' key
    containing only ACTIVE (non-rest) sets.
    """
    ds = day.strftime("%Y-%m-%d")
    try:
        all_acts = api.get_activities_by_date(ds, ds) or []
    except Exception:
        return []

    activities = [
        a for a in all_acts
        if a.get("activityType", {}).get("typeKey") in _STRENGTH_TYPES
    ]

    results = []
    for act in activities:
        garmin_id = act.get("activityId")
        if not garmin_id:
            continue

        try:
            sets_data = api.get_activity_exercise_sets(garmin_id) or {}
        except Exception:
            sets_data = {}

        raw_sets = sets_data.get("exerciseSets", [])
        active_sets = []
        for i, s in enumerate(raw_sets):
            if s.get("setType") != "ACTIVE":
                continue
            exercises = s.get("exercises") or []
            # Pick the exercise with highest probability that isn't UNKNOWN
            category = None
            best_prob = -1.0
            for ex in exercises:
                cat = ex.get("category", "UNKNOWN")
                prob = ex.get("probability", 0.0)
                if cat != "UNKNOWN" and prob > best_prob:
                    best_prob = prob
                    category = cat
            if category is None:
                category = "UNKNOWN"

            active_sets.append({
                "set_index": i,
                "exercise_category": category,
                "reps": s.get("repetitionCount"),
                "weight_g": s.get("weight"),
                "duration_sec": s.get("duration"),
                "start_time": s.get("startTime"),
            })

        sport_key = act.get("activityType", {}).get("typeKey", "strength_training")
        results.append({
            "garmin_id": garmin_id,
            "date": day,
            "name": act.get("activityName"),
            "sport_type": sport_key,
            "duration_sec": int(act.get("duration", 0) or 0),
            "avg_hr": act.get("averageHR"),
            "sets": active_sets,
        })

    return results


def fetch_day(day: date, api: Garmin | None = None, delay: float = 0.0) -> dict:
    """Fetch all metrics for a single day, returning a merged dict."""
    if delay:
        time.sleep(delay)
    if api is None:
        api = load_session()
    result: dict = {"date": day}
    result.update(fetch_hrv(api, day))
    result.update(fetch_sleep(api, day))
    result.update(fetch_rhr(api, day))
    result.update(fetch_overnight_stress(api, day))
    result.update(fetch_body_battery(api, day))
    return result

