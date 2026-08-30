from collections.abc import Iterator

from depas.fetch import Fetcher
from depas.models import Listing, Query

NAME = "goplaceit"


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    raise NotImplementedError("endpoint not mapped yet: api.goplaceit.com is live but the search path is unknown")
