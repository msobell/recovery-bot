"""Garmin Connect data fetcher using garminconnect."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)

_TOKEN_DIR = Path.home() / ".recovery-bot" / "garmin_tokens"


def _from_garmin_local_ms(ts_ms: int) -> datetime:
    """Decode a Garmin '...TimestampLocal' value.

    These are fake epochs already shifted to local wall time, so they must be
    read as UTC; datetime.fromtimestamp() would re-apply the machine offset.
    """
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(tzinfo=None)


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
        baseline = summary.get("baseline", {})
        return {
            "hrv_status": summary.get("status"),
            "hrv_rmssd": summary.get("lastNightAvg"),
            "hrv_baseline_low": baseline.get("lowUpper"),
            "hrv_baseline_high": baseline.get("balancedUpper"),
        }
    except Exception as e:
        logger.warning("fetch_hrv failed for %s: %s", ds, e)
        return {}


def fetch_sleep(api: Garmin, day: date, sleep_data: dict | None = None) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = sleep_data if sleep_data is not None else (api.get_sleep_data(ds) or {})
        daily = data.get("dailySleepDTO", {})
        start_ts = daily.get("sleepStartTimestampLocal")
        end_ts = daily.get("sleepEndTimestampLocal")
        return {
            "sleep_start": _from_garmin_local_ms(start_ts) if start_ts else None,
            "sleep_end": _from_garmin_local_ms(end_ts) if end_ts else None,
            "sleep_duration_min": (daily.get("sleepTimeSeconds") or 0) // 60 or None,
            "sleep_deep_min": (daily.get("deepSleepSeconds") or 0) // 60 or None,
            "sleep_light_min": (daily.get("lightSleepSeconds") or 0) // 60 or None,
            "sleep_rem_min": (daily.get("remSleepSeconds") or 0) // 60 or None,
            "sleep_awake_min": (daily.get("awakeSleepSeconds") or 0) // 60 or None,
            "sleep_score": (daily.get("sleepScores") or {}).get("overall", {}).get("value"),
        }
    except Exception as e:
        logger.warning("fetch_sleep failed for %s: %s", ds, e)
        return {}


def fetch_rhr(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_rhr_day(ds)
        return {"resting_hr": data.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [{}])[0].get("value")}
    except Exception as e:
        logger.warning("fetch_rhr failed for %s: %s", ds, e)
        return {}


def fetch_overnight_stress(api: Garmin, day: date, sleep_data: dict | None = None) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = sleep_data if sleep_data is not None else (api.get_sleep_data(ds) or {})
        daily = data.get("dailySleepDTO", {})
        stress_score = (daily.get("sleepScores") or {}).get("stress", {})
        return {
            "overnight_stress_avg": daily.get("avgSleepStress"),
            "overnight_stress_qualifier": stress_score.get("qualifierKey"),
        }
    except Exception as e:
        logger.warning("fetch_overnight_stress failed for %s: %s", ds, e)
        return {}


def fetch_body_battery(api: Garmin, day: date, wake_ms: int | None = None) -> dict:
    """Body battery at wake — the peak after the overnight recharge.

    The readings are a full-day series (~midnight → night): body battery drains
    while awake and recharges during sleep, so it peaks around wake time. We
    take the reading nearest `wake_ms` (the GMT sleep-end epoch) when known;
    values[0] would grab the mid-sleep low and plain max() could grab a midday
    nap recharge, so neither is the wake value.
    """
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_body_battery(ds)
        if data and isinstance(data, list):
            readings = [
                (r[0], r[1]) for r in data[0].get("bodyBatteryValuesArray", [])
                if len(r) > 1 and r[1] is not None
            ]
            if not readings:
                return {}
            if wake_ms is not None:
                # Reading closest to wake time (within ±90 min); the series is
                # sparse, so exact-match isn't guaranteed
                ts, val = min(readings, key=lambda tv: abs(tv[0] - wake_ms))
                if abs(ts - wake_ms) <= 90 * 60 * 1000:
                    return {"body_battery_start": val}
            # Fallback: the daily peak is the best proxy for the wake value
            return {"body_battery_start": max(v for _, v in readings)}
        return {}
    except Exception as e:
        logger.warning("fetch_body_battery failed for %s: %s", ds, e)
        return {}


def fetch_stress_detail(
    api: Garmin,
    day: date,
    sleep_start: datetime | None,
    sleep_end: datetime | None,
    sleep_data: dict | None = None,
) -> dict:
    """Derive sleep-window stress metrics from the full-day stress time-series.

    Uses GMT sleep timestamps from the sleep API (the stress array uses the
    same epoch). Falls back to the passed-in sleep_start/sleep_end only if the
    GMT fields are absent.
    """
    if not sleep_start or not sleep_end:
        return {}
    ds = day.strftime("%Y-%m-%d")
    try:
        if sleep_data is None:
            sleep_data = api.get_sleep_data(ds) or {}
        daily = sleep_data.get("dailySleepDTO", {})
        start_gmt_ms = daily.get("sleepStartTimestampGMT")
        end_gmt_ms = daily.get("sleepEndTimestampGMT")
        if start_gmt_ms and end_gmt_ms:
            sleep_start_ms = start_gmt_ms
            sleep_end_ms = end_gmt_ms
        else:
            sleep_start_ms = sleep_start.timestamp() * 1000
            sleep_end_ms = sleep_end.timestamp() * 1000

        data = api.get_stress_data(ds) or {}
        readings = data.get("stressValuesArray", [])
        if not readings:
            return {}

        # All samples in the sleep window; rest (-1/-2) sentinels stay in the
        # denominator so each valid reading keeps its real ~3-min weight
        window = [(ts, v) for ts, v in readings if sleep_start_ms <= ts <= sleep_end_ms]
        valid = [(ts, v) for ts, v in window if v >= 0]
        if not valid:
            return {}

        # Split halves by elapsed time, not reading count, so gaps don't skew
        mid_ms = (sleep_start_ms + sleep_end_ms) / 2
        first_half = [v for ts, v in valid if ts < mid_ms]
        second_half = [v for ts, v in valid if ts >= mid_ms]

        if not first_half or not second_half:
            return {}

        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)
        all_valid = [v for _, v in valid]
        minutes_per_reading = (sleep_end_ms - sleep_start_ms) / 60000 / len(window)

        return {
            # Computed from the sleep-window series, overriding Garmin's
            # avgSleepStress (fetch_day merges this dict last): Garmin's number
            # doesn't match its own series (observed 25.0 vs an actual mean of
            # 18.2) and would disagree with the halves shown next to it.
            "overnight_stress_avg": round(sum(all_valid) / len(all_valid), 1),
            "stress_first_half_avg": round(first_avg, 1),
            "stress_second_half_avg": round(second_avg, 1),
            "stress_second_half_min": min(second_half),
            "stress_recovery_delta": round(first_avg - second_avg, 1),
            "stress_time_below_20_min": round(
                sum(1 for v in second_half if v < 20) * minutes_per_reading
            ),
        }
    except Exception as e:
        logger.warning("fetch_stress_detail failed for %s: %s", ds, e)
        return {}


def fetch_steps(api: Garmin, day: date) -> dict:
    ds = day.strftime("%Y-%m-%d")
    try:
        data = api.get_steps_data(ds)
        if data and isinstance(data, list):
            total = sum(entry.get("steps", 0) or 0 for entry in data)
            return {"steps": total if total > 0 else None}
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
        except Exception as e:
            # sets=None signals "fetch failed" (vs. genuinely no sets), so the
            # upsert keeps any previously synced sets instead of wiping them
            logger.warning("exercise sets fetch failed for activity %s: %s", garmin_id, e)
            sport_key = act.get("activityType", {}).get("typeKey", "strength_training")
            results.append({
                "garmin_id": garmin_id,
                "date": day,
                "name": act.get("activityName"),
                "sport_type": sport_key,
                "duration_sec": int(act.get("duration", 0) or 0),
                "avg_hr": act.get("averageHR"),
                "sets": None,
            })
            continue

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


def _parse_local_dt(s: str | None) -> datetime | None:
    """Parse Garmin's 'YYYY-MM-DD HH:MM:SS' local start time (naive)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def fetch_cardio_activities(api: Garmin, day: date) -> list[dict]:
    """Return non-strength Garmin activities (cardio, sport, etc.) for the day.

    Strength sessions are handled separately by fetch_strength_activities (they
    carry detailed sets). This captures everything else recorded on the watch —
    soccer, runs, cardio — so the app doesn't depend on Garmin→Strava
    forwarding to surface them.
    """
    ds = day.strftime("%Y-%m-%d")
    try:
        all_acts = api.get_activities_by_date(ds, ds) or []
    except Exception as e:
        logger.warning("get_activities_by_date failed for %s: %s", ds, e)
        return []

    results = []
    for act in all_acts:
        type_key = act.get("activityType", {}).get("typeKey", "")
        if type_key in _STRENGTH_TYPES:
            continue  # handled by fetch_strength_activities
        garmin_id = act.get("activityId")
        if not garmin_id:
            continue
        distance = act.get("distance")
        results.append({
            "garmin_id": garmin_id,
            "date": day,
            "name": act.get("activityName"),
            "sport_type": type_key or "other",
            "start_time": _parse_local_dt(act.get("startTimeLocal")),
            "duration_sec": int(act.get("duration", 0) or 0) or None,
            "distance_m": float(distance) if distance else None,
            "elevation_m": act.get("elevationGain"),
            "avg_hr": act.get("averageHR"),
            "max_hr": int(act["maxHR"]) if act.get("maxHR") else None,
            "calories": int(act["calories"]) if act.get("calories") else None,
        })
    return results


def fetch_day(day: date, api: Garmin | None = None, delay: float = 0.0) -> dict:
    """Fetch all metrics for a single day, returning a merged dict.

    `delay` is slept before EACH API call (not once per day) — the ~1 req/sec
    Garmin rate limit applies per request. The sleep payload is fetched once
    and shared by the three sleep-derived fetchers.
    """
    def pause():
        if delay:
            time.sleep(delay)

    if api is None:
        api = load_session()

    ds = day.strftime("%Y-%m-%d")
    pause()
    try:
        sleep_data = api.get_sleep_data(ds) or {}
    except Exception as e:
        logger.warning("get_sleep_data failed for %s: %s", ds, e)
        sleep_data = {}

    result: dict = {"date": day}
    pause()
    result.update(fetch_hrv(api, day))
    result.update(fetch_sleep(api, day, sleep_data=sleep_data))
    pause()
    result.update(fetch_rhr(api, day))
    result.update(fetch_overnight_stress(api, day, sleep_data=sleep_data))
    pause()
    wake_ms = sleep_data.get("dailySleepDTO", {}).get("sleepEndTimestampGMT")
    result.update(fetch_body_battery(api, day, wake_ms=wake_ms))
    pause()
    result.update(fetch_steps(api, day))
    pause()
    result.update(fetch_stress_detail(
        api, day, result.get("sleep_start"), result.get("sleep_end"), sleep_data=sleep_data,
    ))
    return result

