"""HTMX-rendered dashboard routes."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from recovery import config as cfg_mod

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = cfg_mod.get()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"user_name": cfg.user.name, "default_trend_days": cfg.ui.default_trend_days},
    )


@router.get("/activity/{strava_id}", response_class=HTMLResponse)
def activity_page(request: Request, strava_id: int):
    cfg = cfg_mod.get()
    return templates.TemplateResponse(
        request=request,
        name="activity.html",
        context={"user_name": cfg.user.name, "strava_id": strava_id},
    )
