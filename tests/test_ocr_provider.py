"""FakeOcrProvider + OcrResult -- no test here ever touches a real OpenAI
Vision API. app.ocr.provider.OpenAIVisionOcrProvider is exercised via
construction plus a monkeypatched client.responses.parse, never a live
network call."""

import types

import httpx2
import openai
import pytest

from app.deps import get_ocr_provider, get_settings
from app.ocr.models import OcrResult
from app.ocr.provider import FakeOcrProvider, OcrProviderError, OpenAIVisionOcrProvider, classify_error


@pytest.fixture
def _cleared_settings_cache():
    """get_settings() is @lru_cache'd -- clear it so a monkeypatched env
    var is actually re-read, then clear it again after so later tests
    re-cache the normal test-fixture settings instead of leaking this
    one's env override."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_ocr_provider_is_none_without_openai_api_key(monkeypatch, _cleared_settings_cache):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # load_settings() calls load_dotenv(), which would otherwise re-fill
    # OPENAI_API_KEY right back in from whatever this developer's real
    # .env happens to contain (override=False only blocks clobbering an
    # ALREADY-set var, not filling in one just deleted) -- stub it out so
    # this test is deterministic regardless of the real .env's contents.
    monkeypatch.setattr("app.settings.load_dotenv", lambda *a, **k: None)
    assert get_ocr_provider() is None


def test_get_ocr_provider_selects_openai_provider_when_key_present(monkeypatch, _cleared_settings_cache):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    provider = get_ocr_provider()
    assert isinstance(provider, OpenAIVisionOcrProvider)


def test_fake_provider_returns_configured_result():
    expected = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="BMW",
        model="730 LD",
    )
    provider = FakeOcrProvider(result=expected)
    assert provider.recognize(b"fake-bytes", "image/jpeg") is expected


def test_fake_provider_defaults_to_all_none_when_unconfigured():
    provider = FakeOcrProvider()
    result = provider.recognize(b"fake-bytes", "image/jpeg")
    assert result.fields_found_count == 0


def test_fake_provider_raises_configured_error():
    provider = FakeOcrProvider(error=OcrProviderError("boom"))
    with pytest.raises(OcrProviderError):
        provider.recognize(b"fake-bytes", "image/jpeg")


def test_ocr_result_fields_found_count_counts_only_non_empty_fields():
    assert OcrResult("fake", None, None, None, None, None).fields_found_count == 0
    assert OcrResult("fake", "AB123CD", None, None, None, None).fields_found_count == 1
    assert OcrResult("fake", "AB123CD", "VIN123", None, "BMW", "730 LD").fields_found_count == 4


# ------------------------ OcrResult.is_complete_for_checkout -------------------
# vin/chassis_number are ALTERNATIVE identifiers -- either one satisfies
# "complete", matching the vehicle form's own VIN/chassis toggle.


def test_complete_with_vin_and_no_chassis_is_success():
    result = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="BMW",
        model="318 TD",
    )
    assert result.is_complete_for_checkout is True


def test_complete_with_chassis_and_no_vin_is_success():
    result = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin=None,
        chassis_number="CHS999999",
        manufacturer="BMW",
        model="318 TD",
    )
    assert result.is_complete_for_checkout is True


def test_no_identifier_at_all_is_not_complete():
    result = OcrResult(
        provider="fake", registration_number="AB123CD", vin=None, chassis_number=None, manufacturer="BMW", model="318 TD"
    )
    assert result.is_complete_for_checkout is False


def test_missing_manufacturer_or_model_is_not_complete():
    missing_manufacturer = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer=None,
        model="318 TD",
    )
    missing_model = OcrResult(
        provider="fake",
        registration_number="AB123CD",
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="BMW",
        model=None,
    )
    assert missing_manufacturer.is_complete_for_checkout is False
    assert missing_model.is_complete_for_checkout is False


# ------------------------ OpenAIVisionOcrProvider -----------------------------


def _fake_request():
    return httpx2.Request("POST", "https://api.openai.com/v1/responses")


def test_openai_provider_can_be_constructed_without_a_network_call():
    """Construction must not itself hit the network -- only .recognize()
    does. Confirms the SDK client is created lazily/locally."""
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")
    assert provider is not None


def test_openai_provider_parses_structured_response_into_ocr_result(monkeypatch):
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")

    parsed = types.SimpleNamespace(
        registration_number=" AB123CD ",  # provider._clean must trim this
        vin="WVWZZZ1JZXW000001",
        chassis_number=None,
        manufacturer="BMW",
        model="318 TD",
    )
    fake_response = types.SimpleNamespace(output_parsed=parsed)
    monkeypatch.setattr(provider._client.responses, "parse", lambda **kwargs: fake_response)

    result = provider.recognize(b"fake-image-bytes", "image/jpeg")
    assert result.provider == "openai"
    assert result.registration_number == "AB123CD"
    assert result.vin == "WVWZZZ1JZXW000001"
    assert result.chassis_number is None
    assert result.manufacturer == "BMW"
    assert result.model == "318 TD"


def test_openai_provider_raises_when_model_returns_no_parsed_output(monkeypatch):
    """A refusal or schema mismatch must surface as OcrProviderError, not a
    silently empty/garbage OcrResult."""
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")
    fake_response = types.SimpleNamespace(output_parsed=None)
    monkeypatch.setattr(provider._client.responses, "parse", lambda **kwargs: fake_response)

    with pytest.raises(OcrProviderError):
        provider.recognize(b"fake-image-bytes", "image/jpeg")


def test_openai_provider_wraps_status_errors_with_a_generic_message(monkeypatch):
    """Section 20 from the OCR spec: the raised OcrProviderError message
    must never contain the SDK's own error text (which could echo back
    request/account details)."""
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")

    def _raise(**kwargs):
        response = httpx2.Response(401, request=_fake_request())
        raise openai.AuthenticationError("super-secret-looking account detail 12345", response=response, body=None)

    monkeypatch.setattr(provider._client.responses, "parse", _raise)
    with pytest.raises(OcrProviderError) as excinfo:
        provider.recognize(b"fake-image-bytes", "image/jpeg")
    assert "super-secret-looking account detail" not in str(excinfo.value)


def test_openai_provider_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")
    attempts = {"count": 0}

    def _flaky(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 2:
            response = httpx2.Response(429, request=_fake_request())
            raise openai.RateLimitError("rate limited", response=response, body=None)
        parsed = types.SimpleNamespace(
            registration_number="AB123CD", vin=None, chassis_number=None, manufacturer=None, model=None
        )
        return types.SimpleNamespace(output_parsed=parsed)

    monkeypatch.setattr(provider._client.responses, "parse", _flaky)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda _: None)

    result = provider.recognize(b"fake-image-bytes", "image/jpeg")
    assert attempts["count"] == 2
    assert result.registration_number == "AB123CD"


def test_openai_provider_gives_up_after_one_retry_on_persistent_rate_limit(monkeypatch):
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")

    def _always_limited(**kwargs):
        response = httpx2.Response(429, request=_fake_request())
        raise openai.RateLimitError("rate limited", response=response, body=None)

    monkeypatch.setattr(provider._client.responses, "parse", _always_limited)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda _: None)

    with pytest.raises(OcrProviderError):
        provider.recognize(b"fake-image-bytes", "image/jpeg")


def test_openai_provider_does_not_retry_non_5xx_status_errors(monkeypatch):
    """Section 7: no automatic retry of semantic/client errors -- only
    transient network/rate-limit/5xx failures get the one retry."""
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")
    attempts = {"count": 0}

    def _bad_request(**kwargs):
        attempts["count"] += 1
        response = httpx2.Response(400, request=_fake_request())
        raise openai.BadRequestError("bad request", response=response, body=None)

    monkeypatch.setattr(provider._client.responses, "parse", _bad_request)
    with pytest.raises(OcrProviderError):
        provider.recognize(b"fake-image-bytes", "image/jpeg")
    assert attempts["count"] == 1


def test_openai_provider_retries_once_on_5xx_then_succeeds(monkeypatch):
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")
    attempts = {"count": 0}

    def _flaky_server(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 2:
            response = httpx2.Response(500, request=_fake_request())
            raise openai.InternalServerError("server error", response=response, body=None)
        parsed = types.SimpleNamespace(
            registration_number=None, vin=None, chassis_number=None, manufacturer=None, model=None
        )
        return types.SimpleNamespace(output_parsed=parsed)

    monkeypatch.setattr(provider._client.responses, "parse", _flaky_server)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda _: None)

    provider.recognize(b"fake-image-bytes", "image/jpeg")
    assert attempts["count"] == 2


# ------------------------------ classify_error --------------------------------
# Safe, non-sensitive classification for logging only -- see classify_error's
# docstring for why it never touches str(exc)/exc.body/exc.response.


def _request():
    return httpx2.Request("POST", "https://api.openai.com/v1/responses")


def test_classify_rate_limit_error():
    response = httpx2.Response(429, request=_request())
    exc = openai.RateLimitError("sensitive-looking rate limit detail", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["category"] == "rate_limit"
    assert result["status_code"] == 429
    assert result["exception_class"] == "RateLimitError"
    assert result["retry_attempt"] == 1


def test_classify_timeout_error():
    exc = openai.APITimeoutError(_request())
    result = classify_error(exc, attempt=2)
    assert result["category"] == "timeout"
    assert result["status_code"] is None
    assert result["exception_class"] == "APITimeoutError"


def test_classify_connection_error():
    exc = openai.APIConnectionError(request=_request())
    result = classify_error(exc, attempt=1)
    assert result["category"] == "connection"
    assert result["status_code"] is None


@pytest.mark.parametrize("status_code,error_cls", [(401, openai.AuthenticationError), (403, openai.PermissionDeniedError)])
def test_classify_auth_errors(status_code, error_cls):
    response = httpx2.Response(status_code, request=_request())
    exc = error_cls("sensitive-looking account detail", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["category"] == "auth"
    assert result["status_code"] == status_code


@pytest.mark.parametrize("status_code,error_cls", [(400, openai.BadRequestError), (422, openai.UnprocessableEntityError)])
def test_classify_bad_request_errors(status_code, error_cls):
    response = httpx2.Response(status_code, request=_request())
    exc = error_cls("sensitive-looking request detail", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["category"] == "bad_request"
    assert result["status_code"] == status_code


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_classify_server_5xx_errors(status_code):
    response = httpx2.Response(status_code, request=_request())
    exc = openai.InternalServerError("sensitive-looking server detail", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["category"] == "server_5xx"
    assert result["status_code"] == status_code


def test_classify_unmapped_status_falls_back_to_other():
    response = httpx2.Response(404, request=_request())
    exc = openai.NotFoundError("not found", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["category"] == "other"
    assert result["status_code"] == 404


def test_classify_error_never_includes_raw_message_or_body():
    """The classification dict must contain only the fixed, safe keys --
    never the exception's own message/body, which could carry echoed
    document content or account details."""
    response = httpx2.Response(429, request=_request())
    exc = openai.RateLimitError("AB123CD WVWZZZ1JZXW000001 super-secret", response=response, body={"error": "raw payload"})
    result = classify_error(exc, attempt=1)
    assert set(result.keys()) == {"exception_class", "status_code", "request_id", "retry_attempt", "category", "rate_limit_headers"}
    serialized = str(result)
    assert "AB123CD" not in serialized
    assert "WVWZZZ1JZXW000001" not in serialized
    assert "super-secret" not in serialized
    assert "raw payload" not in serialized


# ------------------------ rate-limit header allowlist -------------------------


def test_allowlisted_rate_limit_headers_are_extracted():
    response = httpx2.Response(
        429,
        request=_request(),
        headers={
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "12s",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "1000",
            "x-ratelimit-reset-tokens": "6s",
            "retry-after": "12",
        },
    )
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    headers = result["rate_limit_headers"]
    assert headers == {
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "12s",
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-remaining-tokens": "1000",
        "x-ratelimit-reset-tokens": "6s",
        "retry-after": "12",
    }


def test_missing_rate_limit_headers_yield_none_not_fabricated_values():
    response = httpx2.Response(429, request=_request())  # no rate-limit headers sent at all
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    headers = result["rate_limit_headers"]
    assert set(headers.keys()) == {
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
        "retry-after",
    }
    assert all(value is None for value in headers.values())


def test_connection_error_has_no_response_so_headers_are_none():
    exc = openai.APIConnectionError(request=_request())
    result = classify_error(exc, attempt=1)
    assert result["rate_limit_headers"] is None


def test_arbitrary_non_allowlisted_header_is_ignored():
    response = httpx2.Response(429, request=_request(), headers={"x-custom-secret-header": "should-never-appear"})
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert "x-custom-secret-header" not in result["rate_limit_headers"]
    assert "should-never-appear" not in str(result)


def test_authorization_header_never_appears_in_classification():
    response = httpx2.Response(429, request=_request(), headers={"authorization": "Bearer sk-should-never-leak"})
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert "sk-should-never-leak" not in str(result)
    assert "authorization" not in result["rate_limit_headers"]


def test_cookie_header_never_appears_in_classification():
    response = httpx2.Response(429, request=_request(), headers={"cookie": "session=super-secret-session-value"})
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert "super-secret-session-value" not in str(result)
    assert "cookie" not in result["rate_limit_headers"]


def test_response_body_never_appears_in_classification_even_with_headers_present():
    response = httpx2.Response(429, request=_request(), headers={"retry-after": "5"})
    exc = openai.RateLimitError(
        "rate limited", response=response, body={"error": {"message": "AB123CD sensitive body content"}}
    )
    result = classify_error(exc, attempt=1)
    assert "AB123CD" not in str(result)
    assert "sensitive body content" not in str(result)
    assert result["rate_limit_headers"]["retry-after"] == "5"


def test_classify_error_extracts_request_id_when_present():
    response = httpx2.Response(429, request=_request(), headers={"x-request-id": "req_abc123"})
    exc = openai.RateLimitError("rate limited", response=response, body=None)
    result = classify_error(exc, attempt=1)
    assert result["request_id"] == "req_abc123"


def test_recognize_propagates_safe_classification_through_ocr_provider_error(monkeypatch):
    """End-to-end wiring check: the classification computed inside
    recognize()'s except-branches actually reaches the raised
    OcrProviderError, not just the standalone classify_error() function."""
    provider = OpenAIVisionOcrProvider(api_key="test-key-not-real", model="gpt-5-mini")

    def _always_limited(**kwargs):
        response = httpx2.Response(429, request=_request())
        raise openai.RateLimitError("rate limited, sensitive detail", response=response, body=None)

    monkeypatch.setattr(provider._client.responses, "parse", _always_limited)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda _: None)

    with pytest.raises(OcrProviderError) as excinfo:
        provider.recognize(b"fake-image-bytes", "image/jpeg")

    classification = excinfo.value.classification
    assert classification["category"] == "rate_limit"
    assert classification["status_code"] == 429
    assert classification["retry_attempt"] == 2  # raised after the retry was exhausted
    assert "sensitive detail" not in str(classification)
