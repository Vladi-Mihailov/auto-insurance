"""Regression: the live "OCR batch ... failed at ocr: UnicodeEncodeError".

Root cause: OPENAI_API_KEY contained non-ASCII characters (an untouched
Cyrillic placeholder); the OpenAI SDK sends the key as an HTTP header, and
header values must be ASCII -> every request failed inside httpx's header
encoding before leaving the machine. Everything here is offline: the real
OpenAIVisionOcrProvider over an httpx MockTransport, never api.openai.com."""

import io
import json
import logging
import os
import subprocess
import sys

import httpx
import openai
import pytest
from PIL import Image

from app.deps import PROJECT_ROOT
from app.diagnostics import describe_exception, unicode_error_details
from app.ocr import orientation as orientation_module
from app.ocr.orientation import TesseractOrientationDetector
from app.ocr.provider import OcrProvider, OcrProviderError, OpenAIVisionOcrProvider, api_key_problem, build_ocr_provider
from app.settings import load_settings
from telegram_bot_helpers import BotHarness, buttons, last_screen, make_user, seed_catalog

# Synthetic -- shaped like the live value (Cyrillic words), never a real key.
CYRILLIC_PLACEHOLDER = "ТВОЙ_ТЕКУЩИЙ_ключ_openai"
ASCII_TEST_KEY = "sk-test-" + "a" * 40


def _provider(api_key: str, handler=None) -> tuple[OpenAIVisionOcrProvider, list]:
    sent: list = []

    def respond(request):
        sent.append(request)
        return httpx.Response(500, json={"error": {"message": "mock"}})

    provider = OpenAIVisionOcrProvider(api_key=api_key, model="gpt-5-mini")
    provider._client = openai.OpenAI(
        api_key=api_key, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler or respond))
    )
    return provider, sent


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


# ------------------------------------------------------ root-cause boundary


def test_non_ascii_key_fails_at_the_http_header_boundary_with_safe_details():
    provider, sent = _provider(CYRILLIC_PLACEHOLDER)
    with pytest.raises(OcrProviderError) as excinfo:
        provider.recognize([(_jpeg(), "image/jpeg")])
    classification = excinfo.value.classification
    assert sent == []  # never left the machine
    assert classification["category"] == "encoding"
    assert classification["substage"] == "provider_request"
    assert classification["exception_class"] == "UnicodeEncodeError"
    assert classification["encoding"] == "ascii"
    assert classification["chars"][:3] == ["U+0422", "U+0412", "U+041E"]
    assert isinstance(excinfo.value.__cause__, UnicodeEncodeError)
    rendered = json.dumps(classification, ensure_ascii=False) + str(excinfo.value)
    assert CYRILLIC_PLACEHOLDER not in rendered and "ТВОЙ" not in rendered


@pytest.mark.parametrize(
    ("key", "problem"),
    [
        (CYRILLIC_PLACEHOLDER, "non-ASCII"),
        ("\ufeff" + ASCII_TEST_KEY, "non-ASCII"),  # BOM pasted in front
        (ASCII_TEST_KEY + "\u00a0", "non-ASCII"),  # non-breaking space
        (ASCII_TEST_KEY[:10] + " " + ASCII_TEST_KEY[10:], "whitespace"),
    ],
)
def test_unusable_keys_disable_ocr_instead_of_failing_every_batch(key, problem, caplog):
    assert problem in api_key_problem(key)
    with caplog.at_level(logging.WARNING):
        assert build_ocr_provider(key, "gpt-5-mini") is None
    assert "OCR disabled: OPENAI_API_KEY is not a valid API key" in caplog.text
    assert key not in caplog.text and key.strip() not in caplog.text


def test_valid_or_missing_keys():
    assert api_key_problem(ASCII_TEST_KEY) is None
    assert isinstance(build_ocr_provider(ASCII_TEST_KEY, "gpt-5-mini"), OpenAIVisionOcrProvider)
    assert build_ocr_provider(None, "gpt-5-mini") is None
    assert build_ocr_provider("", "gpt-5-mini") is None


def test_web_and_bot_both_use_the_validated_factory(monkeypatch):
    from app.deps import get_ocr_provider, get_settings
    from app.telegram_bot.app import default_ocr_provider

    monkeypatch.setenv("OPENAI_API_KEY", CYRILLIC_PLACEHOLDER)
    get_settings.cache_clear()
    try:
        assert get_ocr_provider() is None
        assert default_ocr_provider(get_settings()) is None
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------- unicode in the prompt


def test_cyrillic_prompt_and_images_travel_as_utf8_json():
    provider, sent = _provider(ASCII_TEST_KEY)
    with pytest.raises(OcrProviderError) as excinfo:  # the mock answers 500
        provider.recognize([(_jpeg(), "image/jpeg")])
    assert excinfo.value.classification["substage"] == "provider_request"
    assert sent, "the request must reach the transport"
    body = json.loads(sent[0].content.decode("utf-8"))
    assert "Ты анализируешь" in body["instructions"]
    assert body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert all(ord(ch) < 128 for name, value in sent[0].headers.items() for ch in value)


def test_prompt_is_encoding_independent_under_a_windows_codepage(tmp_path):
    """Same request built in a child interpreter whose stdio uses cp1252
    and UTF-8 mode off (a default Windows console): nothing in request
    construction depends on the console/locale encoding."""
    script = tmp_path / "child.py"
    script.write_text(
        "import httpx, openai\n"
        "from app.ocr.provider import OpenAIVisionOcrProvider, OcrProviderError\n"
        "sent = []\n"
        "p = OpenAIVisionOcrProvider(api_key='sk-test-' + 'a' * 40, model='gpt-5-mini')\n"
        "p._client = openai.OpenAI(api_key='sk-test-' + 'a' * 40, max_retries=0,\n"
        "    http_client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500))))\n"
        "try:\n"
        "    p.recognize([(b'\\xff\\xd8', 'image/jpeg')])\n"
        "except OcrProviderError as e:\n"
        "    print('substage=' + e.classification['substage'], 'sent=' + str(len(sent)))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", "PYTHONPATH": str(PROJECT_ROOT)}
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, env=env, cwd=PROJECT_ROOT, timeout=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")[-400:]
    assert b"substage=provider_request" in completed.stdout and b"sent=2" in completed.stdout


# ------------------------------------------- tesseract on a Windows codepage


def test_tesseract_boundary_is_bytes_only(monkeypatch):
    """OSD never goes through a text-mode pipe: image bytes in, raw bytes
    out, decoded explicitly -- so neither a cp125x console, a Cyrillic
    install path nor non-UTF-8 output can raise a Unicode error there."""
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, **kwargs)
        # Windows Tesseract warning in cp1251 bytes (invalid as UTF-8) + the real answer
        stdout = "Предупреждение\n".encode("cp1251") + b"Rotate: 90\nOrientation confidence: 4.2\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(orientation_module.subprocess, "run", fake_run)
    detector = TesseractOrientationDetector("C:\\Программы\\Tesseract-OCR\\tesseract.exe")
    guess = detector.detect(Image.new("RGB", (400, 300), "white"))
    assert guess.rotate_clockwise == 90
    assert isinstance(seen["input"], bytes)
    assert not seen.get("text") and "encoding" not in seen and "universal_newlines" not in seen
    assert seen["args"][0] == "C:\\Программы\\Tesseract-OCR\\tesseract.exe"


# ------------------------------------------------------------ diagnostics


def test_unicode_details_never_include_the_text():
    try:
        ("Bearer " + CYRILLIC_PLACEHOLDER).encode("ascii")
    except UnicodeEncodeError as exc:
        details = unicode_error_details(exc)
        line = describe_exception(exc)
    assert details["encoding"] == "ascii" and details["start"] == 7 and details["chars"][0] == "U+0422"
    assert line.startswith("UnicodeEncodeError encoding=ascii reason='ordinal not in range(128)' span=7-")
    assert "ТВОЙ" not in line and "Bearer" not in line
    try:
        b"\xff\xfe".decode("utf-8")
    except UnicodeDecodeError as exc:
        assert unicode_error_details(exc)["bytes"] == ["0xFF"]


# ------------------------------------------------- the bot, end to end


class CountingProvider(OcrProvider):
    """The REAL OpenAI provider with a Cyrillic key over a mock transport --
    reproduces the live failure exactly -- plus a call counter."""

    def __init__(self):
        self.inner, self.sent = _provider(CYRILLIC_PLACEHOLDER)
        self.calls = 0

    def recognize(self, images):
        self.calls += 1
        return self.inner.recognize(images)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("INSURANCE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("INSURANCE_DB_FILE", str(tmp_path / "bot.db"))
    loaded = load_settings(PROJECT_ROOT)
    seed_catalog(loaded.app.db_file)
    return loaded


def test_live_failure_reproduced_one_attempt_safe_log_explicit_idempotent_retry(settings, caplog):
    provider = CountingProvider()
    h = BotHarness(settings, ocr_provider=provider)
    try:
        user = make_user(1)
        h.send_text(user, "/start")
        h.press(user, "p:passenger_car:30d")
        h.press(user, "d:tomorrow")
        h.press(user, "e:documents")
        album = [("photo", f"f{i}", _jpeg()) for i in range(3)]
        with caplog.at_level(logging.INFO):
            calls = h.send_album(user, "live-album", album)

        assert provider.calls == 1 and provider.sent == []  # ONE automatic attempt, nothing sent anywhere
        failure = last_screen(calls)
        labels = [text for text, _ in buttons(failure)]
        assert labels[0] == "🔄 Попробовать распознать ещё раз"
        assert not any("Все документы загружены" in text for text in labels)
        assert "failed at ocr/provider_request" in caplog.text
        assert "UnicodeEncodeError encoding=ascii" in caplog.text and "chars=[U+0422" in caplog.text
        assert "ТВОЙ" not in caplog.text and "f0" not in caplog.text

        retry_data = buttons(failure)[0][1]
        h.press(user, retry_data)
        assert provider.calls == 2  # exactly one new attempt
        h.press(user, retry_data)  # the same (now stale) button again
        h.press(user, "dc:done")  # an old "done" button
        assert provider.calls == 2  # no silent re-runs
    finally:
        h.close()
