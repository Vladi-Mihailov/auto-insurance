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
    contact_type: str | None
    contact_value: str | None
    full_name: str | None
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

    # Legacy free-text vehicle fields — kept only so orders created before
    # this migration keep reading back correctly (see summary.html's
    # fallback). New orders leave these NULL; never write to them.
    vehicle_make: str | None
    vehicle_model: str | None
    vin: str | None
    car_number: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Order":
        return cls(
            id=row["id"],
            public_number=row["public_number"],
            country_code=row["country_code"],
            status=row["status"],
            session_id=row["session_id"],
            contact_type=row["contact_type"],
            contact_value=row["contact_value"],
            full_name=row["full_name"],
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
            vehicle_make=row["vehicle_make"],
            vehicle_model=row["vehicle_model"],
            vin=row["vin"],
            car_number=row["car_number"],
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
