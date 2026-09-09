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
    DocumentDownloadError,
    PolicyLookupHttpError,
    TplPoliciesError,
    create_application,
    download_document,
    fetch_policy,
    fetch_policy_documents,
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


def test_fetch_policy_returns_parsed_json_on_200():
    def handler(request):
        assert str(request.url) == f"{POLICIES_URL}/o-id-123"
        return httpx.Response(200, json={"policyNumber": "TPL0000001", "policyId": 7, "documents": []})

    with _client(handler) as client:
        policy = fetch_policy(client, "o-id-123")
    assert policy["policyNumber"] == "TPL0000001"
    assert policy["policyId"] == 7


def test_fetch_policy_raises_on_non_200():
    def handler(request):
        return httpx.Response(404, content=b"")

    with _client(handler) as client:
        with pytest.raises(PolicyLookupHttpError):
            fetch_policy(client, "o-id-123")


def test_fetch_policy_raises_on_unparseable_body():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    with _client(handler) as client:
        with pytest.raises(PolicyLookupHttpError):
            fetch_policy(client, "o-id-123")


def test_fetch_policy_documents_returns_parsed_list_on_200():
    def handler(request):
        assert str(request.url) == f"{POLICIES_URL}/o-id-123/documents"
        return httpx.Response(200, json=[{"documentType": "Policy", "url": "https://ext-stream.tpl.ge/x/policy-TPL1.pdf"}])

    with _client(handler) as client:
        documents = fetch_policy_documents(client, "o-id-123")
    assert documents[0]["documentType"] == "Policy"


def test_fetch_policy_documents_raises_on_non_200():
    def handler(request):
        return httpx.Response(500, content=b"")

    with _client(handler) as client:
        with pytest.raises(PolicyLookupHttpError):
            fetch_policy_documents(client, "o-id-123")


def test_download_document_succeeds_on_valid_pdf_response():
    def handler(request):
        assert str(request.url) == "https://ext-stream.tpl.ge/x/policy-TPL1.pdf"
        return httpx.Response(200, content=b"%PDF-1.4 fake pdf bytes", headers={"Content-Type": "application/pdf"})

    with _client(handler) as client:
        content = download_document(client, "https://ext-stream.tpl.ge/x/policy-TPL1.pdf")
    assert content == b"%PDF-1.4 fake pdf bytes"


def test_download_document_raises_on_non_200():
    def handler(request):
        return httpx.Response(404, content=b"")

    with _client(handler) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, "https://ext-stream.tpl.ge/x/policy-TPL1.pdf")


def test_download_document_raises_on_wrong_content_type():
    def handler(request):
        return httpx.Response(200, content=b"<html>not a pdf</html>", headers={"Content-Type": "text/html"})

    with _client(handler) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, "https://ext-stream.tpl.ge/x/policy-TPL1.pdf")


def test_download_document_raises_on_empty_body():
    def handler(request):
        return httpx.Response(200, content=b"", headers={"Content-Type": "application/pdf"})

    with _client(handler) as client:
        with pytest.raises(DocumentDownloadError):
            download_document(client, "https://ext-stream.tpl.ge/x/policy-TPL1.pdf")
