import argparse
import sqlite3
from collections.abc import Iterator

from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.models import Listing, Query
from depas.portals import PORTALS, portalinmobiliario
from depas.metro import nearest_station
from depas.store import connect, save, save_detail
from depas.uf import to_clp, uf_in_clp

TOP_QUERY = """
SELECT * FROM listings_ranked
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
            if "nearest_station" not in detail and detail.get("lat") is not None:
                station, metres, minutes = nearest_station(detail["lat"], detail["lon"])
                detail |= {"nearest_station": station, "station_distance_m": metres,
                           "walk_minutes": minutes, "walk_source": "computed"}
            save_detail(connection, row["portal"], row["external_id"], detail)
            print(f"\r{index}/{len(pending)} enriched", end="", flush=True)
    finally:
        fetcher.close()
        connection.close()
    print(f"\n{len(pending)} listings enriched")


FILTERS = (
    ("max_cost", "net_monthly_clp <= ?"),
    ("max_walk", "walk_minutes <= ?"),
    ("min_floor", "floor >= ?"),
    ("min_bedrooms", "bedrooms >= ?"),
    ("min_area", "area >= ?"),
    ("security", "security_type = ?"),
)


def _build_query(args: argparse.Namespace) -> tuple[str, tuple[object, ...]]:
    """Assemble the ranked query from whichever filters were actually given."""
    conditions = ["is_project = 0"]
    parameters: list[object] = []
    for name, condition in FILTERS:
        value = getattr(args, name)
        if value is not None:
            conditions.append(condition)
            parameters.append(value)
    if args.commune:
        conditions.append(f"commune IN ({', '.join('?' * len(args.commune))})")
        parameters.extend(commune.value for commune in args.commune)

    return f"{TOP_QUERY}\nWHERE {' AND '.join(conditions)}", tuple(parameters)


def show(args: argparse.Namespace) -> None:
    connection = connect()
    query, parameters = (args.sql, ()) if args.sql else _build_query(args)
    rows = connection.execute(query, parameters).fetchall()
    if args.sql:
        _print_table(rows)
        return
    pool = connection.execute(
        "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL"
    ).fetchall()
    scale = Scale([dict(row) for row in pool])
    # grading ranks against the whole pool, so the limit can only be applied afterwards
    graded = sorted((_summarise(row, scale) for row in rows),
                    key=lambda row: row["score"], reverse=True)
    _print_table([{k: v for k, v in row.items() if k != "score"} for row in graded[:args.limit]])
    if any(row["grade"].endswith("*") for row in graded[:args.limit]):
        print("\n* graded on partial data — see the 'on' column for how many of 5 components")
    connection.close()


SUMMARY_COLUMNS = ("commune", "bedrooms", "area", "floor", "gastos", "est", "bod",
                   "net", "nearest_station", "walk", "url")


def _summarise(row: sqlite3.Row, scale: Scale) -> dict[str, object]:
    """One display row: the fields worth scanning, led by the grade."""
    scored = scale.grade(dict(row))
    return {
        "grade": f"{scored.letter} {scored.score}" + ("*" if scored.missing else ""),
        "score": scored.score,
        "on": f"{len(scored.parts)}/5",
        "commune": row["commune"], "bedrooms": row["bedrooms"], "area": row["area"],
        "floor": row["floor"], "rent": round(row["price_clp"]),
        "gastos": row["common_expenses"], "est": row["parking_spaces"],
        "bod": row["storage_units"], "net": round(row["net_monthly_clp"]),
        "metro": row["nearest_station"], "walk": row["walk_minutes"], "url": row["url"],
    }


def _print_table(rows: list[sqlite3.Row] | list[dict[str, object]]) -> None:
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
    viewer.add_argument("--max-walk", type=int, help="max walking minutes to a metro station")
    viewer.add_argument("--max-cost", type=int, help="max net monthly cost in CLP")
    viewer.add_argument("--min-floor", type=int)
    viewer.add_argument("--min-bedrooms", type=int)
    viewer.add_argument("--min-area", type=float, help="minimum useful m2")
    viewer.add_argument("--security", help='e.g. "24 horas"')
    viewer.add_argument("--commune", action="append", default=[], type=Commune,
                        choices=list(Commune), metavar="SLUG")
    viewer.set_defaults(func=show)

    args = parser.parse_args()
    args.func(args)
