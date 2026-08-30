from collections.abc import Iterator

from depas.fetch import Fetcher
from depas.models import Listing, Query

NAME = "toctoc"


def search(fetcher: Fetcher, query: Query) -> Iterator[Listing]:
    raise NotImplementedError("endpoint not mapped yet: www.toctoc.com serves HTML, parser not written")
