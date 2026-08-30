from collections.abc import Iterator

from depas.fetch import Fetcher
from depas.models import Listing, Query

NAME = "houm"


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    raise NotImplementedError("endpoint not mapped yet: api.houm.com returns 403 on the guessed path")
