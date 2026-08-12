import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class VehicleCategory:
    id: int
    external_id: int
    code: str
    name: str
    icon: str | None
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "VehicleCategory":
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            code=row["code"],
            name=row["name"],
            icon=row["icon"],
            active=bool(row["active"]),
        )


@dataclass(frozen=True)
class Manufacturer:
    id: int
    external_id: int
    name: str
    is_popular: bool
    active: bool
    # None = this manufacturer's models have never been synced (distinct
    # from "synced, genuinely zero models") — see app/catalog/sync.py.
    models_synced_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Manufacturer":
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            name=row["name"],
            is_popular=bool(row["is_popular"]),
            active=bool(row["active"]),
            models_synced_at=datetime.fromisoformat(row["models_synced_at"]) if row["models_synced_at"] else None,
        )

    @property
    def models_never_synced(self) -> bool:
        return self.models_synced_at is None


@dataclass(frozen=True)
class VehicleModel:
    id: int
    external_id: int
    manufacturer_id: int
    name: str
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "VehicleModel":
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            manufacturer_id=row["manufacturer_id"],
            name=row["name"],
            active=bool(row["active"]),
        )
