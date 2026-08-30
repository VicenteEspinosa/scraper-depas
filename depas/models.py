from dataclasses import dataclass, field
from typing import Literal

from depas.communes import Commune

Currency = Literal["CLP", "UF"]


@dataclass(slots=True)
class Listing:
    """One apartment as published by one portal."""

    portal: str
    external_id: str
    url: str
    price: float
    currency: Currency
    title: str | None = None
    common_expenses: int | None = None
    is_project: bool = False
    price_clp: float | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    area_m2: float | None = None
    commune: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class Query:
    """Portal-agnostic search filters; each portal maps these onto its own params."""

    operation: Literal["rent", "sale"] = "rent"
    communes: list[Commune] = field(default_factory=list)
    min_price: int | None = None
    max_price: int | None = None
    min_bedrooms: int | None = None
    min_area_m2: float | None = None
    max_pages: int = 5
