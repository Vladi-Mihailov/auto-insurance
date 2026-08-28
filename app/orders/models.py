import sqlite3
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Order:
    id: int
    public_number: str
    country_code: str
    status: str
    session_id: str | None
    full_name: str | None
    identification_number: str | None
    citizenship: str | None
    contact_email: str | None
    contact_telegram: str | None
    contact_phone: str | None
    contact_max: str | None
    contact_other: str | None
    period_code: str | None
    start_date: date | None
    end_date: date | None
    customer_currency: str
    purchase_currency: str
    price_customer_minor: int | None
    resume_token: str
    created_at: datetime
    updated_at: datetime

    # New (post-TPL-alignment) vehicle data — see app/catalog/.
    vehicle_category_code: str | None
    manufacturer_id: int | None
    model_id: int | None
    identifier_type: str | None
    identifier: str | None
    data_entry_method: str | None

    # Country-specific vehicle/policyholder fields (AM/TR only — see
    # app.web.checkout_routes/app.validation). Always None for Georgia and
    # for any order created before this migration; never required there.
    engine_power: int | None  # horsepower — AM + TR
    model_year: int | None  # TR only
    date_of_birth: date | None  # policyholder's own DOB — TR only

    # Legacy free-text vehicle fields — kept only so orders created before
    # this migration keep reading back correctly (see summary.html's
    # fallback). New orders leave these NULL; never write to them.
    vehicle_make: str | None
    vehicle_model: str | None
    vin: str | None
    car_number: str | None

    # Legacy single contact_type/contact_value radio-select columns — kept
    # only so orders created before the multi-field contact migration keep
    # reading back correctly (see contact_rows below). New orders leave
    # these NULL; never write to them.
    contact_type: str | None
    contact_value: str | None

    # Driver/owner ("Водитель"/"Владелец" — tpl.ge parity, see /policyholder
    # and app.validation.validate_driver_form/validate_owner_form).
    # *_same_as_policyholder is never None (DB column defaults to 1/true --
    # see app.db._ORDER_COLUMN_MIGRATIONS): a pre-existing order that never
    # had this concept at all reads back as "same as policyholder", the
    # ordinary case, not a broken/unknown state. The rest stay None for such
    # an order, same as any other field never collected from it.
    driver_same_as_policyholder: bool
    driver_full_name: str | None
    driver_identifier: str | None
    driver_citizenship: str | None
    driver_phone: str | None
    driver_email: str | None
    owner_same_as_policyholder: bool
    owner_entity_type: str | None  # "individual" | "legal" | None (None means same_as_policyholder)
    owner_full_name: str | None  # individual's name, or the company name for a legal entity
    owner_identifier: str | None  # individual's ID number, or the company's identification code
    owner_citizenship: str | None  # individual only; always None for a legal entity
    owner_phone: str | None
    owner_email: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Order":
        return cls(
            id=row["id"],
            public_number=row["public_number"],
            country_code=row["country_code"],
            status=row["status"],
            session_id=row["session_id"],
            full_name=row["full_name"],
            identification_number=row["identification_number"],
            citizenship=row["citizenship"],
            contact_email=row["contact_email"],
            contact_telegram=row["contact_telegram"],
            contact_phone=row["contact_phone"],
            contact_max=row["contact_max"],
            contact_other=row["contact_other"],
            contact_type=row["contact_type"],
            contact_value=row["contact_value"],
            period_code=row["period_code"],
            start_date=date.fromisoformat(row["start_date"]) if row["start_date"] else None,
            end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
            customer_currency=row["customer_currency"],
            purchase_currency=row["purchase_currency"],
            price_customer_minor=row["price_customer_minor"],
            resume_token=row["resume_token"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            vehicle_category_code=row["vehicle_category_code"],
            manufacturer_id=row["manufacturer_id"],
            model_id=row["model_id"],
            identifier_type=row["identifier_type"],
            identifier=row["identifier"],
            data_entry_method=row["data_entry_method"],
            engine_power=row["engine_power"],
            model_year=row["model_year"],
            date_of_birth=date.fromisoformat(row["date_of_birth"]) if row["date_of_birth"] else None,
            vehicle_make=row["vehicle_make"],
            vehicle_model=row["vehicle_model"],
            vin=row["vin"],
            car_number=row["car_number"],
            driver_same_as_policyholder=bool(row["driver_same_as_policyholder"]),
            driver_full_name=row["driver_full_name"],
            driver_identifier=row["driver_identifier"],
            driver_citizenship=row["driver_citizenship"],
            driver_phone=row["driver_phone"],
            driver_email=row["driver_email"],
            owner_same_as_policyholder=bool(row["owner_same_as_policyholder"]),
            owner_entity_type=row["owner_entity_type"],
            owner_full_name=row["owner_full_name"],
            owner_identifier=row["owner_identifier"],
            owner_citizenship=row["owner_citizenship"],
            owner_phone=row["owner_phone"],
            owner_email=row["owner_email"],
        )

    @property
    def price_customer_rub(self) -> float | None:
        if self.price_customer_minor is None:
            return None
        return self.price_customer_minor / 100

    @property
    def display_identifier_type(self) -> str | None:
        """New identifier_type, or 'vin' for legacy orders that only have the old vin column."""
        if self.identifier_type:
            return self.identifier_type
        return "vin" if self.vin else None

    @property
    def display_identifier(self) -> str | None:
        return self.identifier or self.vin

    @property
    def display_registration_number(self) -> str | None:
        return self.car_number

    @property
    def contact_rows(self) -> list[tuple[str, str]]:
        """(label, value) pairs for every populated contact -- multiple can
        coexist since only email is required. Falls back to the legacy
        single contact_type/contact_value for orders created before the
        multi-field contact migration (see Order.contact_type docstring
        above), which never had the new columns populated at all."""
        rows = [
            (label, value)
            for label, value in (
                ("Email", self.contact_email),
                ("Telegram", self.contact_telegram),
                ("Телефон", self.contact_phone),
                ("MAX", self.contact_max),
                ("Другое", self.contact_other),
            )
            if value
        ]
        if rows:
            return rows
        if self.contact_value:
            legacy_labels = {"telegram": "Telegram", "max": "MAX", "phone": "Телефон", "other": "Другое"}
            return [(legacy_labels.get(self.contact_type, "Контакт"), self.contact_value)]
        return []

    @property
    def contact_form_values(self) -> dict[str, str]:
        """The 5 editable contact fields, for pre-filling /edit-policyholder.
        A legacy contact_type/contact_value order (no new columns populated)
        maps its single value into the matching new field; email is left
        blank since it never existed as a concept before this migration --
        the user must supply it once to save further edits, same as any
        other now-required field on an old record."""
        values = {
            "contact_email": self.contact_email or "",
            "contact_telegram": self.contact_telegram or "",
            "contact_phone": self.contact_phone or "",
            "contact_max": self.contact_max or "",
            "contact_other": self.contact_other or "",
        }
        if not any(values.values()) and self.contact_value:
            legacy_keys = {"telegram": "contact_telegram", "max": "contact_max", "phone": "contact_phone", "other": "contact_other"}
            key = legacy_keys.get(self.contact_type)
            if key:
                values[key] = self.contact_value
        return values
