import sqlite3
from datetime import date
from functools import cache

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
    value = uf_in_clp(fetcher)
    connection.execute("INSERT INTO uf_daily (day, value) VALUES (?, ?)", (today, value))
    connection.commit()
    return value


def to_clp(price: float, currency: str, uf_value: float) -> float:
    return price * uf_value if currency == "UF" else price


def normalize(listing: Listing, fetcher: Fetcher) -> Listing:
    """Fill price_clp so UF- and CLP-priced listings compare; every save path needs it."""
    listing.price_clp = to_clp(listing.price, listing.currency, uf_in_clp(fetcher))
    return listing
