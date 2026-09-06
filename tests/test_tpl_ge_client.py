"""Low-level HTTP behaviour of app.integrations.tpl_ge.client, tested against
real httpx.Response objects via httpx.MockTransport -- no real network call,
but exercises the actual status/header parsing logic (not a monkeypatched
stand-in for it). See app.integrations.tpl_ge.client module docstring for
why this must never follow a redirect."""

import httpx
import pytest

from app.integrations.tpl_ge.client import (
    BOG_HANDOFF_URL,
    POLICIES_URL,
    BogHandoffHttpError,
    TplPoliciesError,
    create_application,
    initiate_bog_payment,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_create_application_succeeds_on_200_empty_body():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == POLICIES_URL
        return httpx.Response(200, content=b"")

    with _client(handler) as client:
        create_application(client, {"uId": "x"})  # must not raise
    assert len(calls) == 1


def test_create_application_raises_on_non_200():
    def handler(request):
        return httpx.Response(400, json={"error": "bad request"})

    with _client(handler) as client:
        with pytest.raises(TplPoliciesError):
            create_application(client, {"uId": "x"})


def test_initiate_bog_payment_returns_location_on_302():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://mpi.gc.ge/page1?merch_id=abc"})

    with _client(handler) as client:
        location = initiate_bog_payment(client, {"policyUId": "x"})
    assert location == "https://mpi.gc.ge/page1?merch_id=abc"


def test_initiate_bog_payment_does_not_follow_redirect():
    """The single most important safety property in this module: even
    though the transport WOULD happily answer a follow-up request, nothing
    in this code path ever issues one."""
    calls = []

    def handler(request):
        calls.append(request)
        if str(request.url).startswith(BOG_HANDOFF_URL):
            return httpx.Response(302, headers={"Location": "https://mpi.gc.ge/page1?merch_id=abc"})
        raise AssertionError(f"unexpected second request: {request.url}")

    with _client(handler) as client:
        initiate_bog_payment(client, {"policyUId": "x"})
    assert len(calls) == 1


def test_initiate_bog_payment_raises_if_status_not_302():
    def handler(request):
        return httpx.Response(200, json={})

    with _client(handler) as client:
        with pytest.raises(BogHandoffHttpError):
            initiate_bog_payment(client, {"policyUId": "x"})


def test_initiate_bog_payment_raises_if_location_missing():
    def handler(request):
        return httpx.Response(302, headers={})

    with _client(handler) as client:
        with pytest.raises(BogHandoffHttpError):
            initiate_bog_payment(client, {"policyUId": "x"})
