"""Customer-facing value formatting shared by every transport (web
templates, operator notifications, the Telegram bot)."""

import re


def format_rub(value: int) -> str:
    """Whole rubles with a space thousands separator: 2149 -> "2 149"."""
    return f"{value:,}".replace(",", " ")


def format_phone(value: str) -> str:
    """A Russian number in its usual human-readable form:
    "+79495205223" / "89495205223" -> "+7 949 520-52-23". Anything else
    (another country, already formatted text) is returned as configured."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78" and re.fullmatch(r"\s*(\+7|8)[\d\s()-]*", value):
        d = digits[1:]
        return f"+7 {d[0:3]} {d[3:6]}-{d[6:8]}-{d[8:10]}"
    return value.strip()
