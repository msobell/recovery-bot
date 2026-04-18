"""Tests for Strava ingest module (HTTP calls mocked via respx)."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest
import respx

from recovery.ingest import strava


@pytest.fixture()
def token_file(tmp_path, monkeypatch):
    token = {
        "access_token": "test_access",
        "refresh_token": "test_refresh",
        "expires_at": int(time.time()) + 3600,
    }
    p = tmp_path / "strava_token.json"
    p.write_text(json.dumps(token))
    monkeypatch.setattr(strava, "_TOKEN_PATH", p)
    return p


_SAMPLE_ACTIVITY = {
    "id": 12345,
    "name": "Morning Run",
    "sport_type": "Run",
    "type": "Run",
    "start_date_local": "2024-01-10T07:30:00Z",
    "moving_time": 3600,
    "distance": 10000.0,
    "total_elevation_gain": 50.0,
    "average_heartrate": 148.0,
    "max_heartrate": 172.0,
    "average_watts": None,
    "suffer_score": 72,
    "perceived_exertion": None,
}


# ── _parse_activity ────────────────────────────────────────────────────────

def test_parse_activity_maps_fields():
    result = strava._parse_activity(_SAMPLE_ACTIVITY)
    assert result["strava_id"] == 12345
    assert result["name"] == "Morning Run"
    assert result["sport_type"] == "Run"
    assert result["duration_sec"] == 3600
    assert result["distance_m"] == 10000.0
    assert result["avg_hr"] == 148.0
    assert result["suffer_score"] == 72
    assert result["date"] == date(2024, 1, 10)


def test_parse_activity_uses_sport_type_over_type():
    act = {**_SAMPLE_ACTIVITY, "sport_type": "TrailRun", "type": "Run"}
    result = strava._parse_activity(act)
    assert result["sport_type"] == "TrailRun"


def test_parse_activity_falls_back_to_type_when_no_sport_type():
    act = {**_SAMPLE_ACTIVITY, "sport_type": None}
    result = strava._parse_activity(act)
    assert result["sport_type"] == "Run"


def test_parse_activity_handles_missing_optional_fields():
    minimal = {
        "id": 99,
        "name": "Ride",
        "sport_type": "Ride",
        "type": "Ride",
        "start_date_local": "2024-01-05T08:00:00Z",
        "moving_time": None,
        "distance": None,
        "total_elevation_gain": None,
        "average_heartrate": None,
        "max_heartrate": None,
        "average_watts": None,
        "suffer_score": None,
        "perceived_exertion": None,
    }
    result = strava._parse_activity(minimal)
    assert result["strava_id"] == 99
    assert result["avg_hr"] is None


# ── get_auth_url ───────────────────────────────────────────────────────────

def test_get_auth_url_contains_client_id():
    url = strava.get_auth_url("42")
    assert "client_id=42" in url
    assert "activity:read_all" in url
    assert strava._REDIRECT_URI in url


# ── fetch_activities ───────────────────────────────────────────────────────

@respx.mock
def test_fetch_activities_single_page(token_file):
    respx.get("https://www.strava.com/api/v3/athlete/activities").mock(
        return_value=httpx.Response(200, json=[_SAMPLE_ACTIVITY])
    )
    results = strava.fetch_activities("cid", "csecret")
    assert len(results) == 1
    assert results[0]["strava_id"] == 12345


@respx.mock
def test_fetch_activities_empty_response(token_file):
    respx.get("https://www.strava.com/api/v3/athlete/activities").mock(
        return_value=httpx.Response(200, json=[])
    )
    results = strava.fetch_activities("cid", "csecret")
    assert results == []


@respx.mock
def test_fetch_activities_passes_after_param(token_file):
    route = respx.get("https://www.strava.com/api/v3/athlete/activities").mock(
        return_value=httpx.Response(200, json=[])
    )
    strava.fetch_activities("cid", "csecret", after=date(2024, 1, 1))
    request = route.calls[0].request
    assert "after" in str(request.url)


@respx.mock
def test_fetch_activities_paginates_until_empty(token_file):
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Return full page of 1 item (< per_page=200 triggers stop)
            return httpx.Response(200, json=[_SAMPLE_ACTIVITY])
        return httpx.Response(200, json=[])

    respx.get("https://www.strava.com/api/v3/athlete/activities").mock(side_effect=handler)
    results = strava.fetch_activities("cid", "csecret")
    assert len(results) == 1


# ── token refresh ──────────────────────────────────────────────────────────

def test_load_token_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(strava, "_TOKEN_PATH", tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="No Strava token"):
        strava._load_token()


@respx.mock
def test_expired_token_triggers_refresh(tmp_path, monkeypatch):
    expired_token = {
        "access_token": "old",
        "refresh_token": "ref",
        "expires_at": int(time.time()) - 100,  # expired
    }
    p = tmp_path / "token.json"
    p.write_text(json.dumps(expired_token))
    monkeypatch.setattr(strava, "_TOKEN_PATH", p)

    new_token = {
        "access_token": "new",
        "refresh_token": "ref2",
        "expires_at": int(time.time()) + 3600,
    }
    respx.post(strava._TOKEN_URL).mock(return_value=httpx.Response(200, json=new_token))

    client, _ = strava._get_client("cid", "csecret")
    saved = json.loads(p.read_text())
    assert saved["access_token"] == "new"
