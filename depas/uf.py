from functools import cache

from depas.fetch import Fetcher

UF_API = "https://mindicador.cl/api/uf"


@cache
def uf_in_clp(fetcher: Fetcher) -> float:
    """Today's UF value in CLP, so UF- and CLP-priced listings can be compared."""
    return float(fetcher.get(UF_API).json()["serie"][0]["valor"])


def to_clp(price: float, currency: str, uf_value: float) -> float:
    return price * uf_value if currency == "UF" else price
