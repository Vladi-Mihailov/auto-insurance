# TPL/BOG Card Autofill (private, operator-only)

A minimal Manifest V3 Chrome/Edge extension that fills the company card into
the confirmed Bank of Georgia TPL payment page (`mpi.gc.ge/page1`), so an
operator doesn't have to retype it by hand every time they complete a TPL
purchase from `/admin/orders`. It exists **only** on the operator's own
machine — it is never installed on, served by, or connected to
`osagogo24.ru` in any way.

## What it does

- Runs *only* on `https://mpi.gc.ge/page1*` — the browser itself enforces
  this via `manifest.json`'s `content_scripts.matches`; the extension has no
  code path that could run anywhere else, including `payment.bog.ge` or
  `acs.gc.ge` (the 3DS/ACS domains).
- On an explicit click of **"Заполнить на текущей странице"** in the
  popup, fills the card number / expiry / CVC / (optional) cardholder name
  fields it can confidently locate on the page.
- Does **nothing else**. It never clicks "Оплатить"/submits the form,
  never looks at or handles an OTP/SMS field, never makes a network
  request of any kind. The operator reviews what was filled, then
  continues by hand — including entering the SMS/OTP code themselves.

## What it does *not* do (by design, not merely by convention)

- Never sends card data anywhere — there is no `fetch`/`XMLHttpRequest`/
  `navigator.sendBeacon` call anywhere in this extension's code.
- Never persists card data to disk. Card values live only in
  `chrome.storage.session` (an in-memory-only storage area — see
  "Why session storage, not local storage" below), and are cleared the
  moment the browser is closed.
- Never touches 3DS/OTP/ACS. It is not injected into those pages at all,
  so it has no way to.

## Why session storage, not local storage

`chrome.storage.local` is **not an encrypted vault** — it is plain,
unencrypted local storage that a third-party extension has no special
protection for beyond ordinary OS file permissions. It would be dishonest
to present it as a secure place for a raw card number and CVC to sit
indefinitely. `chrome.storage.session` (Chrome 102+) is the meaningfully
safer choice for this narrow case: it is held in memory only, is **never**
written to disk, and is wiped automatically when the browser closes. It
does not defend against something with access to this OS user's live
browser process memory — no browser-extension mechanism can promise that —
but it is an honest, real improvement over permanent local storage, which
is why this design asks the operator to re-enter the card once per browser
session rather than "once, forever."

If your organization needs the card to persist securely across browser
restarts, the right tool is the OS's own credential store (Windows
Credential Manager / macOS Keychain) via a native messaging host — a
materially bigger piece of software than this. That was deliberately not
built here; re-entering the card once per browser session was judged an
acceptable trade-off for a first version. See the delivery report's
ARCHITECTURE AUDIT for the full comparison.

## Installation (per operator machine)

1. Open `chrome://extensions` (or the Edge equivalent).
2. Enable "Developer mode".
3. Click "Load unpacked" and select this `browser-extension/bog-card-autofill/`
   folder.
4. Pin the extension icon for convenience.

There is no configuration file and nothing to fill in before installing —
the card is entered live, per session, via the popup itself.

## Using it

1. In `/admin/orders`, click **"Оплатить TPL"** as before — this still
   just opens the stored BOG payment URL in a new tab, exactly as the
   backend already did before this extension existed.
2. On the BOG tab, open the extension popup and enter the card once (per
   browser session) under "Сохранить на эту сессию браузера".
3. Click **"Заполнить на текущей странице"**. Review the filled fields.
4. Continue in the BOG page yourself: confirm/submit if the interface
   requires it, enter the SMS/OTP code you receive, and complete the
   payment exactly as you would without this extension.

## Known limitation — not yet verified against the live page

The exact rendered form on `mpi.gc.ge/page1` (input names/ids/DOM
structure) has never been captured — prior discovery always stopped
before or at this screen. Field detection here is built from BOG's own
confirmed i18n label strings ("Номер карты", "Срок" ×2, "CVV"/"CVC2"/etc.,
"Имя владельца"), not from an inspected DOM. The first real use should be
a supervised one: open the popup, click "Заполнить на текущей странице",
and confirm each field actually filled correctly *before* trusting it to
save time. If a field isn't found, the popup says so plainly rather than
guessing.
