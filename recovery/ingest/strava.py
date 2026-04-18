"""Strava activity fetcher using httpx + OAuth2."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

_TOKEN_PATH = Path.home() / ".recovery-bot" / "strava_token.json"
_AUTH_URL = "https://www.strava.com/oauth/authorize"
_TOKEN_URL = "https://www.strava.com/oauth/token"
_API_BASE = "https://www.strava.com/api/v3"
_REDIRECT_URI = "http://localhost:8081/callback"


def _load_token() -> dict:
    if not _TOKEN_PATH.exists():
        raise RuntimeError("No Strava token found. Run `recovery auth strava` first.")
    with open(_TOKEN_PATH) as f:
        return json.load(f)


def _save_token(token: dict) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_PATH, "w") as f:
        json.dump(token, f, indent=2)


def _refresh_token(client_id: str, client_secret: str, token: dict) -> dict:
    resp = httpx.post(_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    })
    resp.raise_for_status()
    new_token = resp.json()
    _save_token(new_token)
    return new_token


def get_auth_url(client_id: str) -> str:
    return (
        f"{_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=activity:read_all"
    )


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    resp = httpx.post(_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    token = resp.json()
    _save_token(token)
    return token


def _get_client(client_id: str, client_secret: str) -> tuple[httpx.Client, dict]:
    token = _load_token()
    now = datetime.now(timezone.utc).timestamp()
    if token.get("expires_at", 0) < now + 300:
        token = _refresh_token(client_id, client_secret, token)
    client = httpx.Client(
        headers={"Authorization": f"Bearer {token['access_token']}"},
        base_url=_API_BASE,
        timeout=30,
    )
    return client, token


def _parse_activity(a: dict) -> dict:
    start_dt = datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
    return {
        "strava_id": a["id"],
        "date": start_dt.date(),
        "name": a.get("name"),
        "sport_type": a.get("sport_type") or a.get("type"),
        "duration_sec": a.get("moving_time"),
        "distance_m": a.get("distance"),
        "elevation_m": a.get("total_elevation_gain"),
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "avg_power": a.get("average_watts"),
        "suffer_score": a.get("suffer_score"),
        "perceived_exertion": a.get("perceived_exertion"),
    }


def fetch_activities(
    client_id: str,
    client_secret: str,
    after: date | None = None,
    before: date | None = None,
) -> list[dict]:
    client, _ = _get_client(client_id, client_secret)
    params: dict = {"per_page": 200, "page": 1}
    if after:
        params["after"] = int(datetime(after.year, after.month, after.day, tzinfo=timezone.utc).timestamp())
    if before:
        params["before"] = int(datetime(before.year, before.month, before.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())

    activities = []
    while True:
        resp = client.get("/athlete/activities", params=params)
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        activities.extend(_parse_activity(a) for a in page)
        if len(page) < 200:
            break
        params["page"] += 1

    return activities
