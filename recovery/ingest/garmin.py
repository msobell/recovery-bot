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
        data = api.get_stress_data(ds)
        return {
            "overnight_stress_avg": data.get("avgStressLevel"),
            "overnight_stress_qualifier": data.get("stressQualifier"),
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

