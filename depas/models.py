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
    image_url: str | None = None
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
    # The external ids this portal has already stored, so a paginating portal can tell a
    # page of nothing new from a page of finds. Plain data rather than a callback: a
    # portal has no database and should not grow one.
    known: frozenset[str] = field(default_factory=frozenset)
    # Consecutive pages of nothing new before the sweep stops. 0 reads every page, which
    # is what a deep sweep passes and what a portal whose ordering we do not trust gets.
    quiet_pages: int = 0

    def nothing_new(self, page: list["Listing"]) -> bool:
        """Whether a whole page of results was already stored.

        A method rather than a helper in `depas.portals` because that package imports
        every portal module, so a portal importing back out of it is a cycle.
        """
        return bool(page) and all(one.external_id in self.known for one in page)
