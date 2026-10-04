"""Safe, content-free exception diagnostics for logs.

Describes WHAT went wrong without ever including the text/bytes it went
wrong on (which may be a document value, a name, a key...): for Unicode
errors only the codec, the reason, the offset range and the offending
characters as code points (U+XXXX) or bytes (0xNN), capped."""

_MAX_ITEMS = 5


def unicode_error_details(exc: UnicodeError) -> dict:
    details: dict = {"exception_class": type(exc).__name__}
    for name in ("encoding", "reason", "start", "end"):
        value = getattr(exc, name, None)
        if value is not None:
            details[name] = value
    obj, start, end = getattr(exc, "object", None), getattr(exc, "start", None), getattr(exc, "end", None)
    if obj is not None and start is not None and end is not None:
        chunk = obj[start:end][:_MAX_ITEMS]
        if isinstance(chunk, str):
            details["chars"] = [f"U+{ord(ch):04X}" for ch in chunk]
        else:
            details["bytes"] = [f"0x{b:02X}" for b in bytes(chunk)]
    return details


def describe_exception(exc: BaseException) -> str:
    """One log-safe line: the class, plus codec/reason/code points for
    Unicode errors. Never str(exc) -- that can echo the failing data."""
    if isinstance(exc, UnicodeError):
        d = unicode_error_details(exc)
        parts = [d["exception_class"]]
        if "encoding" in d:
            parts.append(f"encoding={d['encoding']}")
        if "reason" in d:
            parts.append(f"reason={d['reason']!r}")
        if "start" in d and "end" in d:
            parts.append(f"span={d['start']}-{d['end']}")
        if "chars" in d:
            parts.append(f"chars=[{', '.join(d['chars'])}]")
        if "bytes" in d:
            parts.append(f"bytes=[{', '.join(d['bytes'])}]")
        return " ".join(parts)
    return type(exc).__name__
