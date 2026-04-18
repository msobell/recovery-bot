"""Tests for FastAPI routes using TestClient."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from recovery.api.app import app
from recovery.db.models import GarminDaily, StravaActivity
from recovery.db.session import get_session, init_db


@pytest.fixture()
def client(db_engine, populated_db):
    """TestClient wired to the file-based test DB."""
    from recovery.api.routes import data as data_mod
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=db_engine)
    with patch.object(data_mod, "_session", lambda: SessionLocal()):
        yield TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def cfg_patch(config_toml, monkeypatch):
    import recovery.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "_cfg", cfg_mod.load(config_toml))


# ── GET / ─────────────────────────────────────────────────────────────────

def test_index_returns_200(client, cfg_patch):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recovery Bot" in resp.text


# ── GET /api/today ────────────────────────────────────────────────────────

def test_today_returns_json(client, populated_db):
    resp = client.get("/api/today")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "metrics" in body
    assert "signals" in body


def test_today_status_values_are_valid(client, populated_db):
    resp = client.get("/api/today")
    body = resp.json()
    valid_statuses = {"Excellent", "Good", "Moderate", "Poor", "No Data"}
    assert body["status"] in valid_statuses


# ── GET /api/trend ────────────────────────────────────────────────────────

def test_trend_default_days(client):
    resp = client.get("/api/trend")
    assert resp.status_code == 200
    body = resp.json()
    assert "labels" in body
    assert "hrv" in body
    assert "sleep_score" in body
    assert "resting_hr" in body
    assert "overnight_stress" in body


def test_trend_custom_days(client):
    resp = client.get("/api/trend?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["labels"]) <= 7


def test_trend_labels_and_data_same_length(client):
    resp = client.get("/api/trend?days=14")
    body = resp.json()
    n = len(body["labels"])
    assert len(body["hrv"]) == n
    assert len(body["sleep_score"]) == n
    assert len(body["resting_hr"]) == n


def test_trend_days_validation(client):
    resp = client.get("/api/trend?days=6")  # below minimum of 7
    assert resp.status_code == 422


# ── GET /api/activities ───────────────────────────────────────────────────

def test_activities_returns_list(client):
    resp = client.get("/api/activities")
    assert resp.status_code == 200
    body = resp.json()
    assert "activities" in body
    assert isinstance(body["activities"], list)


def test_activities_fields_present(client):
    resp = client.get("/api/activities?days=60")
    body = resp.json()
    if body["activities"]:
        act = body["activities"][0]
        assert "date" in act
        assert "sport_type" in act
        assert "duration_min" in act
        assert "distance_km" in act


def test_activities_count_matches(client):
    resp = client.get("/api/activities?days=60")
    body = resp.json()
    assert body["count"] == len(body["activities"])


# ── GET /api/training-load ────────────────────────────────────────────────

def test_training_load_returns_labels_and_scores(client):
    resp = client.get("/api/training-load")
    assert resp.status_code == 200
    body = resp.json()
    assert "labels" in body
    assert "suffer_score" in body
    assert len(body["labels"]) == len(body["suffer_score"])
