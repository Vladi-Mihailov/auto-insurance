from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _format_rub(value: int) -> str:
    return f"{value:,}".replace(",", " ")


templates.env.filters["rub"] = _format_rub


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
