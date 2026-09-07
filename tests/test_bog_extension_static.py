"""Static/structural checks on browser-extension/bog-card-autofill -- this
project has no JS test runner (it's a pure server-rendered Python app), so
these are the only automated tests for the extension: they parse its actual
manifest.json and read its actual source text, asserting the security
invariants the delivery report commits to rather than trusting the code by
inspection alone. This is NOT a live-browser/DOM behavioural test -- there
is no real mpi.gc.ge page here, no real card submission is exercised, and
none is ever needed for these checks.

No real TPL/BOG/card/OTP interaction happens anywhere in this file.
"""

import json
import re
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "browser-extension" / "bog-card-autofill"

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _read(name: str) -> str:
    return (EXTENSION_DIR / name).read_text(encoding="utf-8")


def _read_code_only(name: str) -> str:
    """Same file, with /* block */ and // line comments stripped -- so
    "must never do X" checks assert against actual executable code, not
    against this file's own explanatory prose mentioning X for humans."""
    text = _read(name)
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def _manifest() -> dict:
    return json.loads(_read("manifest.json"))


def test_manifest_is_valid_mv3():
    manifest = _manifest()
    assert manifest["manifest_version"] == 3


def test_content_script_scoped_only_to_confirmed_bog_card_entry_host():
    """The single most important boundary: the browser itself (not just
    app logic) must refuse to run this anywhere except the confirmed
    card-entry page -- never payment.bog.ge/acs.gc.ge (3DS/ACS), never any
    osagogo24.ru page."""
    manifest = _manifest()
    matches = manifest["content_scripts"][0]["matches"]
    assert matches == ["https://mpi.gc.ge/page1*"]
    assert manifest["host_permissions"] == ["https://mpi.gc.ge/page1*"]


def test_manifest_requests_no_broad_host_permissions():
    manifest = _manifest()
    all_matches = manifest["host_permissions"] + manifest["content_scripts"][0]["matches"]
    for pattern in all_matches:
        assert "<all_urls>" not in pattern
        assert "*://*/*" not in pattern
        assert not pattern.startswith("https://*.")  # no wildcard-subdomain grants either


def test_manifest_permissions_are_minimal():
    manifest = _manifest()
    assert set(manifest["permissions"]) == {"storage", "activeTab"}
    # No "tabs" (broad tab-info read), no "webRequest", no "scripting" beyond
    # the one declared content script, no "cookies", no "downloads".
    for forbidden in ("tabs", "webRequest", "webRequestBlocking", "cookies", "downloads", "scripting", "debugger"):
        assert forbidden not in manifest["permissions"]


def test_content_script_never_calls_click_or_submit():
    """Enforces "must NOT click submit/PAY" as a property of the actual
    shipped source, not just a design intention."""
    source = _read_code_only("content_script.js")
    assert ".click(" not in source
    assert ".submit(" not in source
    assert "requestSubmit" not in source


def test_content_script_never_touches_otp_or_sms():
    source = _read_code_only("content_script.js").lower()
    for forbidden in ("otp", "one-time", "sms", "code input", "challenge"):
        assert forbidden not in source


def test_content_script_never_makes_a_network_request():
    """Card values must never leave the page's own DOM -- no fetch/XHR/
    beacon call anywhere in the content script."""
    source = _read_code_only("content_script.js")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "osagogo24"):
        assert forbidden not in source


def test_popup_never_makes_a_network_request():
    source = _read_code_only("popup.js")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "osagogo24"):
        assert forbidden not in source


def test_popup_uses_session_storage_not_local_storage_for_card_data():
    """The core "no persistent disk storage of card data" guarantee --
    chrome.storage.local must never be used to hold the card object."""
    source = _read_code_only("popup.js")
    assert "chrome.storage.session" in source
    assert "chrome.storage.local" not in source


def test_content_script_reads_card_only_from_the_incoming_message():
    """The content script itself must never read chrome.storage directly --
    it only ever sees the card object handed to it in a FILL message from
    the popup, for the one-shot fill action, and never stores a copy."""
    source = _read_code_only("content_script.js")
    assert "chrome.storage" not in source


def test_popup_clears_its_own_form_after_saving():
    """Hygiene: a previously entered PAN/CVC must not linger visibly in the
    popup's own input fields after being handed off to session storage."""
    source = _read("popup.js")
    assert 'document.getElementById(id).value = ""' in source


def test_readme_documents_the_session_storage_rationale_and_limitation():
    readme = _read("README.md")
    assert "chrome.storage.session" in readme
    assert "not an encrypted vault" in readme
    assert "never verified against the live page" in readme or "not yet verified against the live page" in readme


def test_no_real_card_values_committed_anywhere_in_the_extension():
    """Guards against ever accidentally committing a real-looking PAN --
    the extension ships with zero embedded card data of any kind (values
    are only ever entered live by the operator into the popup)."""
    digit_run_pattern = re.compile(r"\d{12,19}")
    for filename in ("manifest.json", "content_script.js", "popup.js", "popup.html", "README.md"):
        text = _read(filename)
        assert not digit_run_pattern.search(text), f"found a long digit run in {filename}"
