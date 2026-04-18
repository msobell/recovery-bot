from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from recovery.api.routes import dashboard, data

app = FastAPI(title="Recovery Bot", docs_url=None, redoc_url=None)

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

app.include_router(dashboard.router)
app.include_router(data.router, prefix="/api")
