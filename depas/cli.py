import argparse
import sqlite3
from collections.abc import Iterator

from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.fetch import Fetcher
from depas.models import Listing, Query
from depas.portals import PORTALS, portalinmobiliario
from depas.store import connect, save, save_detail
from depas.uf import to_clp, uf_in_clp

TOP_QUERY = """
SELECT commune, bedrooms, area, ROUND(price_clp) AS rent, common_expenses,
       ROUND(total_monthly_clp) AS total, ROUND(total_clp_per_m2) AS total_per_m2,
       floor, has_elevator, published_days_ago, url
FROM listings_ranked
WHERE area > 0 AND is_project = 0
ORDER BY COALESCE(total_clp_per_m2, clp_per_m2)
LIMIT ?
"""


def scrape(args: argparse.Namespace) -> None:
    query = Query(
        operation=args.operation,
        communes=sorted(SANTIAGO_PROVINCE) if args.santiago else args.commune,
        max_price=args.max_price,
        min_bedrooms=args.min_bedrooms,
        min_area_m2=args.min_area_m2,
    )
    fetcher = Fetcher()
    connection = connect()
    try:
        for name in args.portals or PORTALS:
            try:
                counts = save(connection, _matching(PORTALS[name](fetcher, query), fetcher, query))
            except NotImplementedError as error:
                print(f"{name}: skipped ({error})")
                continue
            print(f"{name}: {counts['new']} new, {counts['price_changed']} price changed")
    finally:
        fetcher.close()
        connection.close()


def _matching(
    listings: Iterator[Listing], fetcher: Fetcher, query: Query
) -> Iterator[Listing]:
    """Normalize UF prices to CLP; portal-side filters are unreliable, so re-check them here."""
    uf_value = uf_in_clp(fetcher)
    for listing in listings:
        listing.price_clp = to_clp(listing.price, listing.currency, uf_value)
        if query.min_price is not None and listing.price_clp < query.min_price:
            continue
        if query.max_price is not None and listing.price_clp > query.max_price:
            continue
        if query.min_bedrooms is not None and (listing.bedrooms or 0) < query.min_bedrooms:
            continue
        if query.min_area_m2 is not None and (listing.area_m2 or 0) < query.min_area_m2:
            continue
        yield listing


def enrich(args: argparse.Namespace) -> None:
    connection = connect()
    pending = connection.execute(
        "SELECT portal, external_id, url FROM listings "
        "WHERE detail_fetched_at IS NULL AND portal = ? LIMIT ?",
        (portalinmobiliario.NAME, args.limit),
    ).fetchall()

    fetcher = Fetcher()
    try:
        for index, row in enumerate(pending, start=1):
            detail = portalinmobiliario.fetch_detail(fetcher, row["url"])
            save_detail(connection, row["portal"], row["external_id"], detail)
            print(f"\r{index}/{len(pending)} enriched", end="", flush=True)
    finally:
        fetcher.close()
        connection.close()
    print(f"\n{len(pending)} listings enriched")


def show(args: argparse.Namespace) -> None:
    connection = connect()
    rows = connection.execute(args.sql or TOP_QUERY, () if args.sql else (args.limit,)).fetchall()
    _print_table(rows)
    connection.close()


def _print_table(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("no rows")
        return
    columns = rows[0].keys()
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print("  ".join(str(row[c]).ljust(w) for c, w in zip(columns, widths)))


def main() -> None:
    parser = argparse.ArgumentParser(prog="depas")
    subparsers = parser.add_subparsers(required=True)

    scraper = subparsers.add_parser("scrape", help="fetch listings into depas.db")
    scraper.add_argument("portals", nargs="*", choices=[*PORTALS, []], default=[])
    scraper.add_argument("--operation", choices=["rent", "sale"], default="rent")
    scraper.add_argument("--commune", action="append", default=[], type=Commune,
                         choices=list(Commune), metavar="SLUG")
    scraper.add_argument("--santiago", action="store_true",
                         help="all 32 communes of Provincia de Santiago")
    scraper.add_argument("--max-price", type=int)
    scraper.add_argument("--min-bedrooms", type=int)
    scraper.add_argument("--min-area-m2", type=float)
    scraper.set_defaults(func=scrape)

    enricher = subparsers.add_parser("enrich", help="fetch detail pages for listings missing them")
    enricher.add_argument("--limit", type=int, default=50)
    enricher.set_defaults(func=enrich)

    viewer = subparsers.add_parser("show", help="best price per m2, or your own SQL")
    viewer.add_argument("sql", nargs="?")
    viewer.add_argument("--limit", type=int, default=20)
    viewer.set_defaults(func=show)

    args = parser.parse_args()
    args.func(args)
