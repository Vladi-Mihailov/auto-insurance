"""Data shapes shared by every OCR provider and by the vehicle-document
parser. Keeping these provider-agnostic is what makes the provider
swappable later (Google/Azure/local Tesseract) without touching
checkout_routes.py or the parser: whatever a provider is internally, it
must produce an OcrResult with exactly these ten nullable text fields.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrResult:
    """The narrow structured extraction result for one insurance case,
    from all of its uploaded document photos taken together in a single
    provider call (see app.ocr.provider.OcrProvider.recognize -- never one
    call per photo). Every field is the provider's best-effort raw text
    reading, not yet normalized and not yet matched against our catalog —
    that's the parser's job (see app.ocr.parser). None means "not found",
    never a guess.

    policyholder_full_name/driver_full_name/owner_full_name/passport_number/
    citizenship are the ONLY personal-data fields ever asked of the
    provider (see OpenAIVisionOcrProvider's prompt for the exact per-field
    source rule). The driver/owner "Идентификационный номер" fields on
    /policyholder are always typed by hand, never OCR-derived -- there is
    no reliable way to know a driver's/owner's own ID number belongs to
    them specifically rather than to the policyholder (see
    app.web.checkout_routes.get_policyholder). Each field below has its
    own single allowed source document and MUST NOT be extended/repurposed:
    - policyholder_full_name: the vehicle owner named on the tech passport/
      registration certificate ONLY. Never the passport/ID/driver's
      license/power-of-attorney holder's name.
    - driver_full_name: the passport/ID holder's name, falling back to the
      driver's license only if no passport/ID photo is present. Never the
      tech passport's owner name.
    - owner_full_name: the represented owner/principal named in a power of
      attorney ONLY, and only when confidently identifiable (never the
      attorney-in-fact/representative acting under it). Never the tech
      passport's owner name -- that would silently collapse two distinct
      business roles (see app.ocr.provider's module docstring).
    - passport_number: the policyholder's own passport/ID number ONLY.
      Never a driver's license number (a different, incompatible ID),
      never the tech passport's VIN/chassis/registration number, never
      anything from a power of attorney.
    - citizenship: read from that SAME passport/ID document passport_number
      came from -- never inferred from a name, document language, phone
      number, or any other document. Normalized to an English country
      name (see app.countries); may fail to safely match any entry in our
      fixed country list even when non-None, in which case
      app.web.checkout_routes never auto-selects anything.

    engine_power/model_year/date_of_birth (added for the AM/TR country-
    specific fields -- see app.web.checkout_routes/app.validation) are the
    same kind of best-effort raw reading, still subject to the SAME source
    isolation rules as everything else here:
    - engine_power: from the vehicle document ONLY, and ONLY when the
      document states it explicitly as horsepower/PS/л.с. -- never guessed
      from a kW-only figure (no deterministic kW->hp conversion exists in
      this codebase), and never confused with engine displacement/capacity
      (e.g. "1998 cm3"), a completely different figure.
    - model_year: from the vehicle document ONLY, and ONLY the actual
      model/manufacture year field -- never substituted with a first-
      registration year or the document's own issue date, even when
      engine_power/model_year is the only vehicle-document field missing.
    - date_of_birth: from the SAME passport/ID document passport_number/
      citizenship came from ONLY (i.e. the policyholder's own document) --
      never a driver's license, never a power of attorney, and never the
      document's issue/expiry date.
    All three are None whenever not confidently determinable -- never a
    guess -- and, unlike the ten fields above, are pure autofill
    convenience: see Order.engine_power/model_year/date_of_birth for how a
    missing value here simply falls back to manual entry on /vehicle or
    /policyholder, and see is_complete_for_checkout below, which
    deliberately does NOT depend on any of these three.
    """

    provider: str
    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None
    policyholder_full_name: str | None = None
    driver_full_name: str | None = None
    owner_full_name: str | None = None
    passport_number: str | None = None
    citizenship: str | None = None
    engine_power: int | None = None
    model_year: int | None = None
    date_of_birth: str | None = None

    @property
    def fields_found_count(self) -> int:
        return sum(
            1
            for value in (
                self.registration_number,
                self.vin,
                self.chassis_number,
                self.manufacturer,
                self.model,
                self.policyholder_full_name,
                self.driver_full_name,
                self.owner_full_name,
                self.passport_number,
                self.citizenship,
                self.engine_power,
                self.model_year,
                self.date_of_birth,
            )
            if value
        )

    @property
    def is_complete_for_checkout(self) -> bool:
        """True once registration_number, manufacturer, and model were all
        recognized AND at least one vehicle identifier was found. vin and
        chassis_number are ALTERNATIVE identifiers (see app/vehicle_form.html's
        VIN/chassis toggle) -- a document that clearly uses VIN and has no
        chassis_number is a complete read, not a partial one. This is a
        distinct notion from fields_found_count (a raw 0-5 tally used only
        for metrics, unchanged)."""
        return bool(
            self.registration_number and self.manufacturer and self.model and (self.vin or self.chassis_number)
        )


@dataclass(frozen=True)
class VehicleDataCandidates:
    """Output of app.ocr.parser.build_candidates(OcrResult) -- normalized
    and catalog-matched, ready to merge directly into the same draft keys
    the manual /vehicle form already writes. manufacturer_text/model_text
    are kept only as a transient review hint for the user when the
    catalog match came back empty; they are never written to the draft
    (see checkout_routes.py) and never used as a stand-in for a real
    manufacturer_id/model_id."""

    registration_number: str | None
    identifier_type: str | None  # "vin" | "chassis" | None
    identifier: str | None
    manufacturer_id: int | None
    manufacturer_text: str | None
    model_id: int | None
    model_text: str | None

    @property
    def fields_found_count(self) -> int:
        return sum(
            1
            for value in (self.registration_number, self.identifier, self.manufacturer_id, self.model_id)
            if value
        )
