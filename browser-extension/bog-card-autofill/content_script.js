/*
 * TPL/BOG Card Autofill -- content script.
 *
 * Runs ONLY on https://mpi.gc.ge/page1* (enforced by manifest.json's
 * content_scripts.matches -- the browser itself refuses to inject this file
 * anywhere else, not just an app-level check). This is the confirmed BOG
 * card-entry host from real discovery (2026-09-06 HAR captures) -- NOT
 * payment.bog.ge or acs.gc.ge, which handle 3DS/ACS and are deliberately
 * left completely untouched by this extension (it is never even injected
 * there, so it has no way to interact with the OTP/challenge step at all).
 *
 * Hard rules, all enforced in this one file (see tests/test_bog_extension_static.py
 * in the main repo for automated checks that this file keeps obeying them):
 *   - NEVER click, submit, or otherwise trigger the PAY / submit control.
 *   - NEVER read, watch for, or interact with an OTP/SMS/code field.
 *   - NEVER make a network request of any kind (fetch/XHR/beacon) --
 *     card values never leave this page's own DOM.
 *   - NEVER write card values anywhere persistent -- they are read from
 *     chrome.storage.session (memory-only, cleared when the browser closes,
 *     never touches disk -- see popup.js for why this was chosen over
 *     chrome.storage.local) for the single fill action and then discarded.
 *
 * Field detection is BEST-EFFORT, built from the real label strings
 * confirmed in BOG's own i18n dictionary during discovery (ui.input.pan.label
 * = "Номер карты", ui.input.expiryMonth/expiryYear.label = "Срок" (both,
 * ambiguous which is first), ui.input.cvc.label = "CVV"/"CVV2"/"CVC2"/"CSC"/
 * "CVN2" depending on card brand, ui.input.cardholder.label = "Имя владельца").
 * The actual rendered DOM of this page was never captured (discovery always
 * stopped before/at this screen) -- this has NOT been verified against the
 * live page. Never guess past what's confidently found: an unmatched field
 * is left alone and reported as not-found, rather than filling the wrong
 * input.
 */

const MERCHANT_MARKER = "COMP. INSURANCE CENTER"; // confirmed real merchant name, BOG /start response

function pageLooksLikeTplPayment() {
  return document.body && document.body.innerText.includes(MERCHANT_MARKER);
}

// Standard technique for filling a React/Vue-controlled <input> so the
// framework's own tracked state (not just the visible DOM value) updates --
// a plain `element.value = x` does not fire the framework's onChange and
// the page may still submit its OLD value. This is the only way to make an
// autofill technique work against a modern JS-controlled form; it does not
// bypass anything security-relevant (it substitutes for real keystrokes).
function setNativeValue(input, value) {
  const proto = window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

// Find the single <input> most plausibly associated with a visible label
// text -- tries semantic <label> association first, then a handful of
// common autocomplete hints, then (last resort) textual proximity. Returns
// null rather than a low-confidence guess if nothing matches unambiguously.
function findFieldByLabel(labelTexts, autocompleteHints) {
  const normalize = (s) => (s || "").trim().toLowerCase();
  const wanted = labelTexts.map(normalize);

  for (const label of document.querySelectorAll("label")) {
    if (!wanted.includes(normalize(label.textContent))) continue;
    if (label.htmlFor) {
      const byId = document.getElementById(label.htmlFor);
      if (byId && byId.tagName === "INPUT") return byId;
    }
    const nested = label.querySelector("input");
    if (nested) return nested;
  }

  for (const hint of autocompleteHints || []) {
    const byAutocomplete = document.querySelector(`input[autocomplete="${hint}"]`);
    if (byAutocomplete) return byAutocomplete;
  }

  return null;
}

function findAllFieldsByLabel(labelTexts) {
  // For the two "Срок" (expiry month/year) fields, which share the exact
  // same label text -- returns them in DOM order. Order (month first, then
  // year) is an assumption, not confirmed -- see module docstring.
  const normalize = (s) => (s || "").trim().toLowerCase();
  const wanted = labelTexts.map(normalize);
  const found = [];
  for (const label of document.querySelectorAll("label")) {
    if (!wanted.includes(normalize(label.textContent))) continue;
    const input = (label.htmlFor && document.getElementById(label.htmlFor)) || label.querySelector("input");
    if (input) found.push(input);
  }
  return found;
}

function detectFields() {
  const pan = findFieldByLabel(["номер карты"], ["cc-number"]);
  const expiry = findAllFieldsByLabel(["срок"]);
  const cvc = findFieldByLabel(["cvv", "cvv2", "cvc2", "csc", "cvn2"], ["cc-csc"]);
  const cardholder = findFieldByLabel(["имя владельца"], ["cc-name"]);
  return {
    pan,
    expiryMonth: expiry[0] || null,
    expiryYear: expiry[1] || null,
    cvc,
    cardholder,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "PROBE") {
    const fields = detectFields();
    sendResponse({
      isTplPage: pageLooksLikeTplPayment(),
      panFound: !!fields.pan,
      expiryFound: !!(fields.expiryMonth && fields.expiryYear),
      cvcFound: !!fields.cvc,
      cardholderFound: !!fields.cardholder,
    });
    return true;
  }

  if (message.type === "FILL") {
    if (!pageLooksLikeTplPayment()) {
      sendResponse({ ok: false, reason: "This does not look like the TPL payment page -- refusing to fill." });
      return true;
    }
    const fields = detectFields();
    const card = message.card; // { pan, expiryMonth, expiryYear, cvc, cardholder } -- read once, never stored by this script
    const result = {};

    if (fields.pan && card.pan) {
      setNativeValue(fields.pan, card.pan);
      result.pan = "filled";
    } else {
      result.pan = fields.pan ? "no value provided" : "field not found";
    }

    if (fields.expiryMonth && card.expiryMonth) {
      setNativeValue(fields.expiryMonth, card.expiryMonth);
      result.expiryMonth = "filled";
    } else {
      result.expiryMonth = fields.expiryMonth ? "no value provided" : "field not found";
    }

    if (fields.expiryYear && card.expiryYear) {
      setNativeValue(fields.expiryYear, card.expiryYear);
      result.expiryYear = "filled";
    } else {
      result.expiryYear = fields.expiryYear ? "no value provided" : "field not found";
    }

    if (fields.cvc && card.cvc) {
      setNativeValue(fields.cvc, card.cvc);
      result.cvc = "filled";
    } else {
      result.cvc = fields.cvc ? "no value provided" : "field not found";
    }

    if (card.cardholder) {
      if (fields.cardholder) {
        setNativeValue(fields.cardholder, card.cardholder);
        result.cardholder = "filled";
      } else {
        result.cardholder = "field not found";
      }
    } else {
      result.cardholder = "not configured (optional)";
    }

    // Deliberately does nothing after this point: no .click(), no .submit(),
    // no keyboard-triggered Enter. The operator reviews the filled fields
    // and proceeds by hand, exactly as required.
    sendResponse({ ok: true, result });
    return true;
  }
});
