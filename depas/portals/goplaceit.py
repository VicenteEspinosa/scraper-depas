from collections.abc import Iterator

from depas.fetch import Fetcher
import re
from typing import Any

from depas.models import Listing, Query

LISTING_URL = re.compile(r"(?!x)x")  # no links recognised yet


def listing_id(url: str) -> str | None:
    return None


NAME = "goplaceit"


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    raise NotImplementedError("endpoint not mapped yet: api.goplaceit.com is live but the search path is unknown")


def fetch_detail(fetcher: Fetcher, url: str) -> dict[str, Any]:
    raise NotImplementedError(f"{NAME} detail pages not mapped yet")
