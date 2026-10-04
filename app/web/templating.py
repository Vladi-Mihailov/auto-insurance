from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.formatting import format_rub as _format_rub

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["rub"] = _format_rub


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
