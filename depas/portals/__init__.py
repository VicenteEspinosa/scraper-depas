from collections.abc import Callable, Iterator

from depas.fetch import Fetcher
from depas.models import Listing, Query
from depas.portals import goplaceit, houm, portalinmobiliario, toctoc

SearchFunction = Callable[[Fetcher, Query], Iterator[Listing]]

PORTALS: dict[str, SearchFunction] = {
    module.NAME: module.search
    for module in (portalinmobiliario, houm, goplaceit, toctoc)
}
