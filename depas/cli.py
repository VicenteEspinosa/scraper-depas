import argparse
import sqlite3
import time
from collections.abc import Iterator

from depas.bot import run as run_bot
from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.commute import as_text as commute_text
from depas.detail import infer_from_description
from depas.config import (alert_communes, chat_id, locations, max_rent, optional_int,
                          optional_text)
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.models import Listing, Query
from depas.portals import PORTALS
from depas.metro import nearest_station
from depas.store import (connect, mark_notified, refresh_commutes, refresh_zone_benchmarks,
                         save, save_detail)
from depas.telegram import chats, format_listing, send_listing
from depas.uf import normalize, stored_uf

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
        stored_uf(connection, fetcher)
        for name in args.portals or PORTALS:
            try:
                counts = save(connection, _matching(PORTALS[name].search(fetcher, query), fetcher, query))
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
    for listing in listings:
        normalize(listing, fetcher)
        if query.min_price is not None and listing.price_clp < query.min_price:
            continue
        if query.max_price is not None and listing.price_clp > query.max_price:
            continue
        if query.min_bedrooms is not None and (listing.bedrooms or 0) < query.min_bedrooms:
            continue
        if query.min_area_m2 is not None and (listing.area_m2 or 0) < query.min_area_m2:
            continue
        yield listing


def _infer_stored_descriptions(connection: sqlite3.Connection) -> int:
    """Fill columns a portal left empty from descriptions already in the database."""
    filled = 0
    for row in connection.execute(
        "SELECT * FROM listings WHERE description IS NOT NULL AND description != ''"
    ).fetchall():
        gaps = {column: value
                for column, value in infer_from_description(row["description"]).items()
                if row[column] is None}
        if gaps:
            save_detail(connection, row["portal"], row["external_id"], gaps)
            filled += 1
    return filled


def _enrich_one(connection: sqlite3.Connection, fetcher: Fetcher, row: sqlite3.Row) -> None:
    """Fetch one detail page, falling back to a computed walk when the portal omits one."""
    detail = PORTALS[row["portal"]].fetch_detail(fetcher, row["url"])
    description = detail.get("description")
    if description:
        # The portal's own spec table always wins; prose only fills what it left empty.
        detail = infer_from_description(str(description)) | detail
    if "nearest_station" not in detail and detail.get("lat") is not None:
        station, metres, minutes = nearest_station(detail["lat"], detail["lon"])
        detail |= {"nearest_station": station, "station_distance_m": metres,
                   "walk_minutes": minutes, "walk_source": "computed"}
    save_detail(connection, row["portal"], row["external_id"], detail)


def enrich(args: argparse.Namespace) -> None:
    connection = connect()
    pending = connection.execute(
        "SELECT portal, external_id, url FROM listings "
        "WHERE detail_fetched_at IS NULL LIMIT ?",
        (args.limit,),
    ).fetchall()

    fetcher = Fetcher()
    try:
        stored_uf(connection, fetcher)
        for index, row in enumerate(pending, start=1):
            _enrich_one(connection, fetcher, row)
            print(f"\r{index}/{len(pending)} enriched", end="", flush=True)
        refresh_commutes(connection, fetcher, args.limit)
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
    # An unenriched listing would be graded on two components and beat everything.
    conditions = ["is_project = 0", "detail_fetched_at IS NOT NULL"]
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

# Every requirement is re-checked here, including the ones the scrape already
# applied: enrichment overwrites card values (bedrooms among them) with the
# detail page's, so a listing can stop qualifying after it was stored.
# Floor is deliberately absent: it grades rather than excludes, because the
# portals that publish the most listings never publish a floor number at all.
ALERT_REQUIREMENTS = (
    ("DEPAS_ALERT_MAX_COST", "net_monthly_clp <= ?", optional_int),
    ("DEPAS_ALERT_MIN_BEDROOMS", "bedrooms >= ?", optional_int),
    ("DEPAS_ALERT_MAX_WALK", "walk_minutes <= ?", optional_int),
    ("DEPAS_ALERT_MIN_AREA", "(area IS NULL OR area >= ?)", optional_int),
)


def _requirement_clauses() -> tuple[list[str], list[object]]:
    """Only the requirements actually configured become WHERE conditions."""
    conditions, parameters = [], []
    for name, condition, read in ALERT_REQUIREMENTS:
        value = read(name)
        if value is not None:
            conditions.append(condition)
            parameters.append(value)
    reach = optional_int("DEPAS_ALERT_MAX_COMMUTE")
    if reach is not None:
        for place in locations():
            conditions.append(f"json_extract(commute, '$.{place.name}') <= ?")
            parameters.append(reach)
    communes = alert_communes()
    if communes:
        conditions.append(f"commune IN ({', '.join('?' * len(communes))})")
        parameters.extend(communes)
    return conditions, parameters


def _announce(connection: sqlite3.Connection, limit: int) -> int:
    """Post enriched, un-announced listings that clear DEPAS_ALERT_MIN_GRADE."""
    conditions, parameters = _requirement_clauses()
    candidates = connection.execute(
        "SELECT * FROM listings_ranked "
        "WHERE notified_at IS NULL AND detail_fetched_at IS NOT NULL AND is_project = 0"
        + "".join(f" AND {condition}" for condition in conditions),
        parameters,
    ).fetchall()
    if not candidates:
        return 0

    pool = connection.execute(
        "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL AND is_project = 0"
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
            send_listing(chat_id(), format_listing(dict(row), grade), row["image_url"])
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
        max_price=max_rent(),  # derived from the budget, not configured
        min_bedrooms=optional_int("DEPAS_ALERT_MIN_BEDROOMS"),
    )
    fetcher = Fetcher()
    connection = connect()
    try:
        # The ranked view prices listings per m2 straight from this, so cache it before
        # anything reads the view.
        stored_uf(connection, fetcher)
        for name, portal in PORTALS.items():
            try:
                counts = save(connection, _matching(portal.search(fetcher, query), fetcher, query))
            except NotImplementedError:
                continue
            print(f"scrape {name}: {counts['new']} new, {counts['price_changed']} price changed")

        pending = connection.execute(
            "SELECT portal, external_id, url FROM listings WHERE detail_fetched_at IS NULL LIMIT ?",
            (args.enrich_limit,),
        ).fetchall()
        for row in pending:
            _enrich_one(connection, fetcher, row)
        print(f"enrich: {len(pending)} listings")
        print(f"from descriptions: {_infer_stored_descriptions(connection)} listings filled")
        print(f"commutes: {refresh_commutes(connection, fetcher, args.commute_limit)} routed")
        print(f"zone benchmarks: {refresh_zone_benchmarks(connection)} communes")
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


def test_alert(args: argparse.Namespace) -> None:
    """Post the best-graded listing to Telegram, marked as a test rather than a find."""
    connection = connect()
    try:
        pool = [dict(row) for row in connection.execute(
            "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL AND is_project = 0")]
        if not pool:
            raise ValueError("nothing enriched to post; run `depas enrich` first")
        scale = Scale(pool)
        row, grade = max(((row, scale.grade(row)) for row in pool), key=lambda pair: pair[1].score)
        send_listing(chat_id(), format_listing(row, grade, is_test=True), row["image_url"])
        print(f"test alert posted: {grade.letter} {grade.score} {row['url']}")
    finally:
        connection.close()


def show(args: argparse.Namespace) -> None:
    connection = connect()
    query, parameters = (args.sql, ()) if args.sql else _build_query(args)
    rows = connection.execute(query, parameters).fetchall()
    if args.sql:
        _print_table(rows)
        return
    pool = connection.execute(
        "SELECT * FROM listings_ranked WHERE detail_fetched_at IS NOT NULL AND is_project = 0"
    ).fetchall()
    scale = Scale([dict(row) for row in pool])
    # grading ranks against the whole pool, so the limit can only be applied afterwards
    graded = sorted((_summarise(row, scale) for row in rows),
                    key=lambda row: row["score"], reverse=True)
    _print_table([{k: v for k, v in row.items() if k != "score"} for row in graded[:args.limit]])
    if any(row["grade"].endswith("*") for row in graded[:args.limit]):
        print("\n* graded on partial data — see the 'on' column for how many components scored")
    connection.close()


SUMMARY_COLUMNS = ("commune", "bedrooms", "area", "floor", "gastos", "est", "bod",
                   "net", "nearest_station", "walk", "commute", "url")


def _summarise(row: sqlite3.Row, scale: Scale) -> dict[str, object]:
    """One display row: the fields worth scanning, led by the grade."""
    scored = scale.grade(dict(row))
    return {
        "grade": f"{scored.letter} {scored.score}" + ("*" if scored.missing else ""),
        "score": scored.score,
        "on": f"{len(scored.parts)}/{len(scored.parts) + len(scored.missing)}",
        "commune": row["commune"], "bedrooms": row["bedrooms"], "area": row["area"],
        "floor": row["floor"], "rent": round(row["price_clp"]),
        "gastos": row["common_expenses"], "est": row["parking_spaces"],
        "bod": row["storage_units"], "net": round(row["net_monthly_clp"]),
        "metro": row["nearest_station"], "walk": row["walk_minutes"],
        "commute": commute_text(row["commute"]) or "—", "url": row["url"],
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
    watcher.add_argument("--commute-limit", type=int, default=40,
                         help="listings routed per pass; Transitous is somebody else's server")
    watcher.add_argument("--max-alerts", type=int, default=10)
    watcher.set_defaults(func=watch)

    bot = subparsers.add_parser("bot", help="reply to portal links posted in the group")
    bot.set_defaults(func=lambda _: run_bot())

    chatter = subparsers.add_parser("chats", help="list Telegram chats the bot can see")
    chatter.set_defaults(func=telegram_chats)

    tester = subparsers.add_parser("test-alert", help="post the top listing as a test card")
    tester.set_defaults(func=test_alert)

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
