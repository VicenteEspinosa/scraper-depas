import argparse
import sqlite3
import time
from collections.abc import Iterator

from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.config import alert_communes, chat_id, optional_int
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.models import Listing, Query
from depas.portals import PORTALS, portalinmobiliario
from depas.metro import nearest_station
from depas.store import connect, mark_notified, save, save_detail
from depas.telegram import chats, format_listing, send_listing
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


def _enrich_one(connection: sqlite3.Connection, fetcher: Fetcher, row: sqlite3.Row) -> None:
    """Fetch one detail page, falling back to a computed walk when the portal omits one."""
    detail = portalinmobiliario.fetch_detail(fetcher, row["url"])
    if "nearest_station" not in detail and detail.get("lat") is not None:
        station, metres, minutes = nearest_station(detail["lat"], detail["lon"])
        detail |= {"nearest_station": station, "station_distance_m": metres,
                   "walk_minutes": minutes, "walk_source": "computed"}
    save_detail(connection, row["portal"], row["external_id"], detail)


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
            _enrich_one(connection, fetcher, row)
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


ALERT_DELAY_SECONDS = 3


def _announce(connection: sqlite3.Connection, limit: int) -> int:
    """Post enriched, un-announced listings that clear DEPAS_ALERT_MIN_GRADE."""
    candidates = connection.execute(
        "SELECT * FROM listings_ranked "
        "WHERE notified_at IS NULL AND detail_fetched_at IS NOT NULL AND is_project = 0"
    ).fetchall()
    if not candidates:
        return 0

    pool = connection.execute(
        "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL"
    ).fetchall()
    scale = Scale([dict(row) for row in pool])
    minimum = optional_int("DEPAS_ALERT_MIN_GRADE") or 0

    graded = sorted(((row, scale.grade(dict(row))) for row in candidates),
                    key=lambda pair: pair[1].score, reverse=True)
    posted = 0
    for row, grade in graded:
        if posted >= limit:
            break
        # Below the bar still gets stamped, so it is never reconsidered later.
        if grade.score >= minimum:
            send_listing(chat_id(), format_listing(dict(row), grade))
            posted += 1
            time.sleep(ALERT_DELAY_SECONDS)  # group sends are rate-limited by Telegram
        mark_notified(connection, row["portal"], row["external_id"])
    return posted


def watch(args: argparse.Namespace) -> None:
    """One scheduled pass: scrape the configured communes, then enrich what is new."""
    communes = [Commune(slug) for slug in alert_communes()]
    if not communes:
        raise ValueError("set DEPAS_ALERT_COMMUNES to the commune slugs you want watched")

    query = Query(
        operation="rent",
        communes=communes,
        max_price=optional_int("DEPAS_ALERT_MAX_PRICE"),
        min_bedrooms=optional_int("DEPAS_ALERT_MIN_BEDROOMS"),
    )
    fetcher = Fetcher()
    connection = connect()
    try:
        counts = save(connection, _matching(portalinmobiliario.search(fetcher, query), fetcher, query))
        print(f"scrape: {counts['new']} new, {counts['price_changed']} price changed")

        pending = connection.execute(
            "SELECT portal, external_id, url FROM listings WHERE detail_fetched_at IS NULL LIMIT ?",
            (args.enrich_limit,),
        ).fetchall()
        for row in pending:
            _enrich_one(connection, fetcher, row)
        print(f"enrich: {len(pending)} listings")
        print(f"alerts: {_announce(connection, args.max_alerts)} posted")
    finally:
        fetcher.close()
        connection.close()


def telegram_chats(args: argparse.Namespace) -> None:
    """List the chats the bot can see, so the group id can be copied into TELEGRAM_CHAT_ID."""
    found = chats()
    if not found:
        print("No chats yet. Add the bot to the group and send /start there, then rerun.\n"
              "Bots only see commands until privacy mode is disabled in @BotFather.")
        return
    for chat in found:
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or "-"
        print(f"{chat['id']:>16}  {chat.get('type'):12}  {name}")


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

    watcher = subparsers.add_parser("watch", help="scheduled pass: scrape then enrich new listings")
    watcher.add_argument("--enrich-limit", type=int, default=60)
    watcher.add_argument("--max-alerts", type=int, default=10)
    watcher.set_defaults(func=watch)

    chatter = subparsers.add_parser("chats", help="list Telegram chats the bot can see")
    chatter.set_defaults(func=telegram_chats)

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
