"""TrendWeight data fetcher — pulls from the public JSON API."""
from __future__ import annotations

from datetime import datetime

import httpx

_KG_TO_LBS = 2.20462
_SHARE_PREFIX = "https://trendweight.com/u/"
_API_PREFIX = "https://trendweight.com/api/data/"


def _share_url_to_api_url(share_url: str) -> str:
    """Convert a TrendWeight share URL to its API equivalent."""
    user_id = share_url.rstrip("/").removeprefix(_SHARE_PREFIX)
    return f"{_API_PREFIX}{user_id}"


def fetch_measurements(share_url: str) -> list[dict]:
    """Fetch all weight measurements from TrendWeight's public API.

    share_url: the public share URL from your TrendWeight profile settings,
               e.g. https://trendweight.com/u/<your-id>

    Returns a list of dicts ready to upsert into WeightEntry. Weights are
    converted from kg to lbs. Fat % values are converted from decimal (0.15)
    to percentage (15.0).
    """
    url = _share_url_to_api_url(share_url)
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    raw = resp.json().get("computedMeasurements", [])

    results = []
    for m in raw:
        raw_date = m.get("date")
        if not raw_date:
            continue
        d = datetime.strptime(raw_date, "%Y-%m-%d").date()

        actual_kg = m.get("actualWeight")
        trend_kg = m.get("trendWeight")
        actual_fat = m.get("actualFatPercent")
        trend_fat = m.get("trendFatPercent")

        results.append({
            "date": d,
            "actual_weight_lbs": round(actual_kg * _KG_TO_LBS, 2) if actual_kg else None,
            "trend_weight_lbs": round(trend_kg * _KG_TO_LBS, 2) if trend_kg else None,
            "weight_is_interpolated": m.get("weightIsInterpolated", False),
            "actual_fat_pct": round(actual_fat * 100, 2) if actual_fat else None,
            "trend_fat_pct": round(trend_fat * 100, 2) if trend_fat else None,
            "fat_is_interpolated": m.get("fatIsInterpolated", False),
        })

    return results
