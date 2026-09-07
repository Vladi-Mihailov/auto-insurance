/*
 * TPL/BOG Card Autofill -- popup logic.
 *
 * Card values are written ONLY to chrome.storage.session (an in-memory-only
 * storage area added in Chrome 102 specifically for this kind of case --
 * unlike chrome.storage.local, it is never written to disk and is cleared
 * automatically when the browser closes). This is a deliberate choice: a
 * private extension's chrome.storage.local is plain unencrypted local
 * storage, not a payment-grade vault, and this project explicitly avoids
 * presenting it as one -- see the architecture audit in the delivery
 * report. Session storage does not solve every threat (something with
 * access to this OS user's running processes could still inspect memory),
 * but it never leaves a copy on disk, and it is wiped the moment the
 * browser is closed -- an honest, meaningfully better default than
 * permanent local storage for this specific, narrow use case.
 *
 * The popup input fields are always blank on open, even when values are
 * already saved for this session -- re-displaying a previously entered PAN
 * would mean it lingers on-screen (shoulder-surfing risk) for no benefit;
 * "Card data saved for this session: yes/no" is enough for the operator to
 * know the state.
 */

const STORAGE_KEY = "bogCard";

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

async function refreshSavedIndicator() {
  const data = await chrome.storage.session.get(STORAGE_KEY);
  setStatus(data[STORAGE_KEY] ? "Card data saved for this session: yes" : "Card data saved for this session: no");
}

document.getElementById("save").addEventListener("click", async () => {
  const card = {
    pan: document.getElementById("pan").value.replace(/\s+/g, ""),
    expiryMonth: document.getElementById("expiryMonth").value.trim(),
    expiryYear: document.getElementById("expiryYear").value.trim(),
    cvc: document.getElementById("cvc").value.trim(),
    cardholder: document.getElementById("cardholder").value.trim() || null,
  };
  await chrome.storage.session.set({ [STORAGE_KEY]: card });
  // Clear the form immediately after saving -- nothing sensitive should
  // linger visibly in the popup once it's been handed to session storage.
  for (const id of ["pan", "expiryMonth", "expiryYear", "cvc", "cardholder"]) {
    document.getElementById(id).value = "";
  }
  await refreshSavedIndicator();
});

document.getElementById("clear").addEventListener("click", async () => {
  await chrome.storage.session.remove(STORAGE_KEY);
  await refreshSavedIndicator();
});

document.getElementById("fill").addEventListener("click", async () => {
  const data = await chrome.storage.session.get(STORAGE_KEY);
  const card = data[STORAGE_KEY];
  if (!card) {
    setStatus("No card data saved for this session yet -- enter it above first.");
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || !tab.url.startsWith("https://mpi.gc.ge/page1")) {
    setStatus("This tab is not the confirmed BOG payment page (mpi.gc.ge/page1) -- refusing to fill.");
    return;
  }

  chrome.tabs.sendMessage(tab.id, { type: "FILL", card }, (response) => {
    if (chrome.runtime.lastError) {
      setStatus("Could not reach the page (reload it and try again): " + chrome.runtime.lastError.message);
      return;
    }
    if (!response.ok) {
      setStatus(response.reason);
      return;
    }
    const lines = Object.entries(response.result).map(([field, outcome]) => `${field}: ${outcome}`);
    setStatus(lines.join("\n") + "\n\nReview the fields, then continue and enter the SMS/OTP code yourself.");
  });
});

refreshSavedIndicator();
