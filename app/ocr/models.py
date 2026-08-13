"""Data shapes shared by every OCR provider and by the vehicle-document
parser. Keeping these provider-agnostic is what makes the provider
swappable later (Google/Azure/local Tesseract) without touching
checkout_routes.py or the parser: whatever a provider is internally, it
must produce an OcrResult with exactly these five nullable text fields.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrResult:
    """The narrow structured extraction result for ONE uploaded vehicle
    document. Every field is the provider's best-effort raw text reading,
    not yet normalized and not yet matched against our catalog — that's
    the parser's job (see app.ocr.parser). None means "not found", never
    a guess.

    Deliberately excludes anything about the document's owner (full name,
    address, passport, etc.) -- the provider is only ever asked for these
    five vehicle fields (see OpenAIVisionOcrProvider's prompt) and MUST NOT be
    extended to carry personal fields.
    """

    provider: str
    registration_number: str | None
    vin: str | None
    chassis_number: str | None
    manufacturer: str | None
    model: str | None

    @property
    def fields_found_count(self) -> int:
        return sum(
            1
            for value in (self.registration_number, self.vin, self.chassis_number, self.manufacturer, self.model)
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
