import sqlite3
from datetime import date
from functools import cache

from curl_cffi.requests.exceptions import RequestException

from depas.fetch import Fetcher
from depas.models import Listing

UF_API = "https://mindicador.cl/api/uf"


@cache
def uf_in_clp(fetcher: Fetcher) -> float:
    """Today's UF value in CLP, so UF- and CLP-priced listings can be compared."""
    return float(fetcher.get(UF_API).json()["serie"][0]["valor"])


def stored_uf(connection: sqlite3.Connection, fetcher: Fetcher) -> float:
    """Today's UF, fetched once a day and kept in the database for later passes."""
    today = date.today().isoformat()
    row = connection.execute("SELECT value FROM uf_daily WHERE day = ?", (today,)).fetchone()
    if row:
        return float(row[0])
    try:
        value = uf_in_clp(fetcher)
    except (RequestException, KeyError, IndexError, ValueError) as error:
        # The indicator being down is not worth the whole sweep: the UF moves by a
        # fraction of a percent a day, so yesterday's is a fine price for today. Not
        # stored under today, so tomorrow's pass asks again.
        last = connection.execute(
            "SELECT day, value FROM uf_daily ORDER BY day DESC LIMIT 1").fetchone()
        if last is None:
            raise
        print(f"WARNING UF indicator unavailable ({error}); using the UF of {last[0]}")
        return float(last[1])
    connection.execute("INSERT INTO uf_daily (day, value) VALUES (?, ?)", (today, value))
    connection.commit()
    return value


def to_clp(price: float, currency: str, uf_value: float) -> float:
    return price * uf_value if currency == "UF" else price


def normalize(listing: Listing, uf_value: float) -> Listing:
    """Fill price_clp so UF- and CLP-priced listings compare; every save path needs it.

    Takes the value rather than the fetcher so that normalising is arithmetic and not a
    request: `uf_in_clp` caches per Fetcher, so a pass with one Fetcher per portal used
    to ask the indicator once per portal, and could not be run off the main thread
    without each worker doing it again.
    """
    listing.price_clp = to_clp(listing.price, listing.currency, uf_value)
    return listing
