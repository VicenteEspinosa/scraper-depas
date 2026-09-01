import argparse
import sqlite3
import time
from collections.abc import Iterator

from depas.bot import refresh_card, run as run_bot
from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.commute import as_text as commute_text
from depas.detail import infer_from_description
from depas.config import DEFAULT_COMMON_EXPENSES
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.models import Listing, Query
from depas.portals import PORTALS
from depas.metro import nearest_station
from depas.preferences import (DEFAULTED, SET, Preferences, check_environment, described,
                               seed_from_env, setting)
from depas.store import (NOT_FURNISHED, NOT_REJECTED, POOL_QUERY, clear_notified, connect,
                        forget_preference, mark_notified, refresh_commutes,
                        refresh_zone_benchmarks, remember_card, save, save_detail,
                        store_preference, sync_lease_income)
from depas.telegram import chat_type, chats, format_listing, send_listing, verdict_buttons
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
    prefs = Preferences.load(connection)
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
        refresh_commutes(connection, fetcher, prefs, args.limit)
    finally:
        fetcher.close()
        connection.close()
    print(f"\n{len(pending)} listings enriched")


# Most listings never state when they free up, and an undeclared date must not be read
# as "never": it is the portals that are silent, not the apartment.
AVAILABLE_BY = "(available_from IS NULL OR available_from <= ?)"

FILTERS = (
    ("max_cost", "net_monthly_clp <= ?"),
    ("max_walk", "walk_minutes <= ?"),
    ("min_floor", "floor >= ?"),
    ("min_bedrooms", "bedrooms >= ?"),
    ("min_area", "area >= ?"),
    ("max_age", "age <= ?"),
    ("security", "security_type = ?"),
    ("available_by", AVAILABLE_BY),
)


def _build_query(args: argparse.Namespace) -> tuple[str, tuple[object, ...]]:
    """Assemble the ranked query from whichever filters were actually given."""
    # An unenriched listing would be graded on two components and beat everything,
    # amoblado is never on the table however good the rest of the row looks, and a
    # listing already turned down in the chat is not worth scanning past again.
    conditions = ["is_project = 0", "detail_fetched_at IS NOT NULL", NOT_FURNISHED,
                  NOT_REJECTED]
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
    ("DEPAS_COST_MAX", "net_monthly_clp <= ?"),
    ("DEPAS_BEDROOMS_MIN", "bedrooms >= ?"),
    ("DEPAS_WALK_MAX", "walk_minutes <= ?"),
    ("DEPAS_AREA_MIN", "(area IS NULL OR area >= ?)"),
    ("DEPAS_AVAILABLE_BY", AVAILABLE_BY),
)


def _requirement_clauses(prefs: Preferences) -> tuple[list[str], list[object]]:
    """Only the requirements actually configured become WHERE conditions."""
    conditions, parameters = [], []
    for name, condition in ALERT_REQUIREMENTS:
        value = prefs.value(name)
        if value is not None:
            conditions.append(condition)
            parameters.append(value)
    reach = prefs.commute.maximum
    if reach is not None:
        for place in prefs.locations():
            conditions.append(f"json_extract(commute, '$.{place.name}') <= ?")
            parameters.append(reach)
    communes = prefs.communes()
    if communes:
        conditions.append(f"commune IN ({', '.join('?' * len(communes))})")
        parameters.extend(communes)
    return conditions, parameters


def _announce(connection: sqlite3.Connection, prefs: Preferences, limit: int) -> int:
    """Post enriched, un-announced listings that clear DEPAS_GRADE_MIN."""
    conditions, parameters = _requirement_clauses(prefs)
    candidates = connection.execute(
        f"{POOL_QUERY} AND notified_at IS NULL"
        + "".join(f" AND {condition}" for condition in conditions),
        parameters,
    ).fetchall()
    if not candidates:
        return 0

    pool = connection.execute(POOL_QUERY).fetchall()
    scale = Scale([dict(row) for row in pool], prefs)
    minimum = prefs.value("DEPAS_GRADE_MIN") or 0

    graded = sorted(((row, scale.grade(dict(row))) for row in candidates),
                    key=lambda pair: pair[1].score, reverse=True)
    destination = prefs.chat_id()
    # Cards only get comment threads in a channel, and the id alone cannot say which
    # this is: channels and discussion groups share the -100 prefix.
    print(f"alerts: posting to a {chat_type(destination)}")
    posted = 0
    for row, grade in graded:
        if posted >= limit:
            break
        # Below the bar still gets stamped, so it is never reconsidered later.
        if grade.score >= minimum:
            sent = send_listing(destination, format_listing(dict(row), grade, prefs),
                                row["image_url"],
                                buttons=verdict_buttons(row["id"], row["interest"]))
            # Recorded so a /like or /dislike commented under the card knows which
            # listing it is about, and so the card can be redrawn with the verdict.
            remember_card(connection, sent["chat"]["id"], sent["message_id"],
                          row["portal"], row["external_id"], "photo" in sent)
            posted += 1
            time.sleep(ALERT_DELAY_SECONDS)  # Telegram rate-limits how fast a chat is posted to
        mark_notified(connection, row["portal"], row["external_id"])
    return posted


def watch(args: argparse.Namespace) -> None:
    """One scheduled pass: scrape the configured communes, then enrich what is new."""
    fetcher = Fetcher()
    connection = connect()
    try:
        # Inside the try: the settings are read from the database now, so everything
        # that decides what this pass even scrapes happens after both are open.
        prefs = Preferences.load(connection)
        communes = [Commune(slug) for slug in prefs.communes()]
        if not communes:
            raise ValueError("set DEPAS_COMMUNES to the commune slugs you want watched")

        query = Query(
            operation="rent",
            communes=communes,
            max_price=prefs.max_rent(),  # derived from the budget, not configured
            min_bedrooms=prefs.value("DEPAS_BEDROOMS_MIN"),
        )
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
        routed = refresh_commutes(connection, fetcher, prefs, args.commute_limit)
        print(f"commutes: {routed} routed")
        print(f"zone benchmarks: {refresh_zone_benchmarks(connection)} communes")
        print(f"alerts: {_announce(connection, prefs, args.max_alerts)} posted")
    finally:
        fetcher.close()
        connection.close()


def telegram_chats(args: argparse.Namespace) -> None:
    """List the chats the bot can see, so the right id can be copied into TELEGRAM_CHAT_ID."""
    found = chats()
    if not found:
        print("No chats yet. Add the bot to the channel or group and post there, then rerun.\n"
              "Bots only see commands until privacy mode is disabled in @BotFather.")
        return
    for chat in found:
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or "-"
        print(f"{chat['id']:>16}  {chat.get('type'):12}  {name}")


def test_alert(args: argparse.Namespace) -> None:
    """Post the best-graded listing to Telegram, marked as a test rather than a find."""
    connection = connect()
    prefs = Preferences.load(connection)
    try:
        pool = [dict(row) for row in connection.execute(POOL_QUERY)]
        if not pool:
            raise ValueError("nothing enriched to post; run `depas enrich` first")
        scale = Scale(pool, prefs)
        row, grade = max(((row, scale.grade(row)) for row in pool), key=lambda pair: pair[1].score)
        sent = send_listing(prefs.chat_id(), format_listing(row, grade, prefs, is_test=True),
                            row["image_url"],
                            buttons=verdict_buttons(row["id"], row["interest"]))
        # Recorded like any other card, so /like and /dislike can be tried on it.
        remember_card(connection, sent["chat"]["id"], sent["message_id"],
                      row["portal"], row["external_id"], "photo" in sent)
        print(f"test alert posted: {grade.letter} {grade.score} {row['url']}")
    finally:
        connection.close()


def resend(args: argparse.Namespace) -> None:
    """Un-stamp recent alerts so the next watch pass posts them again, to wherever it posts now."""
    connection = connect()
    try:
        cleared = clear_notified(connection, args.hours)
    finally:
        connection.close()
    print(f"{cleared} listings un-stamped; `depas watch` will announce them again")


def redraw(args: argparse.Namespace) -> None:
    """Re-render cards already posted, newest first, with today's grades and today's rules.

    Which is how a card posted with the keyboard that hid its «Comentarios» button
    gets that button back: the redraw withholds the keyboard wherever it would cost
    the comments, and an edit that omits reply_markup drops what is there.
    """
    connection = connect()
    prefs = Preferences.load(connection)
    try:
        cards = connection.execute(
            "SELECT * FROM card_messages ORDER BY posted_at DESC LIMIT ?", (args.limit,)
        ).fetchall()
        redrawn = 0
        for card in cards:
            if refresh_card(connection, dict(card), prefs):
                redrawn += 1
            time.sleep(ALERT_DELAY_SECONDS)  # Telegram rate-limits edits like anything else
        print(f"redraw: {redrawn} of {len(cards)} cards re-rendered")
    finally:
        connection.close()


def show(args: argparse.Namespace) -> None:
    connection = connect()
    prefs = Preferences.load(connection)
    query, parameters = (args.sql, ()) if args.sql else _build_query(args)
    rows = connection.execute(query, parameters).fetchall()
    if args.sql:
        _print_table(rows)
        return
    pool = connection.execute(POOL_QUERY).fetchall()
    scale = Scale([dict(row) for row in pool], prefs)
    # grading ranks against the whole pool, so the limit can only be applied afterwards
    graded = sorted((_summarise(row, scale) for row in rows),
                    key=lambda row: row["score"], reverse=True)
    _print_table([{k: v for k, v in row.items() if k != "score"} for row in graded[:args.limit]])
    if any(row["grade"].endswith("*") for row in graded[:args.limit]):
        print("\n* graded on partial data — see the 'on' column for how many components scored")
    connection.close()


SUMMARY_COLUMNS = ("commune", "bedrooms", "area", "floor", "age", "gastos", "est", "bod",
                   "net", "nearest_station", "walk", "commute", "url")


def _gastos(published: int | None) -> str:
    """The figure the net cost was built from, marked when it is the assumed default."""
    return str(published) if published else f"{DEFAULT_COMMON_EXPENSES} (def)"


def _summarise(row: sqlite3.Row, scale: Scale) -> dict[str, object]:
    """One display row: the fields worth scanning, led by the grade."""
    scored = scale.grade(dict(row))
    return {
        "grade": f"{scored.letter} {scored.score}" + ("*" if scored.missing else ""),
        "score": scored.score,
        "on": f"{len(scored.parts)}/{len(scored.parts) + len(scored.missing)}",
        "commune": row["commune"], "bedrooms": row["bedrooms"], "area": row["area"],
        "floor": row["floor"], "age": row["age"], "rent": round(row["price_clp"]),
        "gastos": _gastos(row["common_expenses"]), "est": row["parking_spaces"],
        "bod": row["storage_units"], "net": round(row["net_monthly_clp"]),
        "metro": row["nearest_station"], "walk": row["walk_minutes"],
        "commute": commute_text(row["commute"]) or "—",
        "desde": row["available_from"] or "—", "url": row["url"],
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


# The settings live in the database, so these are how they are read and written from a
# shell -- and the same calls the chat commands will make. Every write goes through
# `store_preference`, which refuses a value that would otherwise only fail later.
VALUE_WIDTH = 46
SOURCE_LABEL = {SET: "configured", DEFAULTED: "default"}


def _shorten(value: str | None) -> str:
    if value is None:
        return "—"
    return value if len(value) <= VALUE_WIDTH else f"{value[:VALUE_WIDTH - 1]}…"


def config_list(args: argparse.Namespace) -> None:
    """Every setting, what it is set to, and whether anybody actually set it."""
    connection = connect()
    try:
        rows = described(Preferences.load(connection))
    finally:
        connection.close()
    width = max(len(declared.name) for declared, _, _ in rows)
    for declared, value, source in rows:
        label = SOURCE_LABEL.get(source, "unset")
        print(f"{declared.name.ljust(width)}  {_shorten(value).ljust(VALUE_WIDTH)}  {label}")
    print(f"\n{len(rows)} settings · `depas config get NAME` explains one")


def config_get(args: argparse.Namespace) -> None:
    """One setting in full: what it means, what it holds, and what that parses to."""
    declared = setting(args.name)
    connection = connect()
    try:
        prefs = Preferences.load(connection)
        raw, value = prefs.raw(declared.name), prefs.value(declared.name)
    finally:
        connection.close()
    print(f"{declared.name}\n{declared.help}")
    if declared.example:
        print(f"example: {declared.example}")
    print(f"\nvalue:   {raw if raw is not None else '(unset)'}")
    if raw is None and declared.default is not None:
        print(f"default: {declared.default}")
    print(f"parses to: {value!r}")


def config_set(args: argparse.Namespace) -> None:
    """Write one setting, refusing anything that does not parse."""
    connection = connect()
    try:
        value = store_preference(connection, args.name, " ".join(args.value))
    finally:
        connection.close()
    print(f"{args.name} = {value!r}")


def config_unset(args: argparse.Namespace) -> None:
    """Forget one setting, so it falls back to its default or simply stops applying."""
    connection = connect()
    try:
        value = forget_preference(connection, args.name)
    finally:
        connection.close()
    print(f"{args.name} unset; it now means {value!r}")


def config_check(args: argparse.Namespace) -> None:
    """Validate .env against the registry without opening the database.

    Deliberately touches nothing: this is what a deploy runs after building the image
    and before restarting anything, so a .env the new parsers refuse fails the deploy
    while the old containers are still serving.
    """
    checked, problems = check_environment()
    for problem in problems:
        print(f"  {problem}")
    if problems:
        raise SystemExit(f"{len(problems)} problem(s) in the environment; nothing was changed")
    print(f"{checked} settings in the environment, all valid")


def config_import_env(args: argparse.Namespace) -> None:
    """Pull .env back into the table, which is otherwise only ever done once."""
    connection = connect()
    try:
        seeded = seed_from_env(connection, force=args.force)
        sync_lease_income(connection, Preferences.load(connection))
    finally:
        connection.close()
    if not seeded:
        print("nothing imported: the table was already seeded (pass --force to redo it)")
        return
    print(f"imported {len(seeded)} settings from the environment: {', '.join(seeded)}")


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

    bot = subparsers.add_parser("bot", help="reply to portal links posted in the chat")
    bot.set_defaults(func=lambda _: run_bot())

    chatter = subparsers.add_parser("chats", help="list Telegram chats the bot can see")
    chatter.set_defaults(func=telegram_chats)

    tester = subparsers.add_parser("test-alert", help="post the top listing as a test card")
    tester.set_defaults(func=test_alert)

    resender = subparsers.add_parser(
        "resend", help="re-announce recently alerted listings on the next watch pass")
    resender.add_argument("--hours", type=int, default=6,
                          help="how far back to un-stamp; older alerts are left alone")
    resender.set_defaults(func=resend)

    redrawer = subparsers.add_parser(
        "redraw", help="re-render cards already posted, newest first")
    redrawer.add_argument("--limit", type=int, default=25,
                          help="how many cards back to re-render")
    redrawer.set_defaults(func=redraw)

    configurer = subparsers.add_parser(
        "config", help="read and edit the settings, which live in the database")
    # Set on the parser, so every action under it inherits both defaults.
    configurer.set_defaults(func=config_list,  # bare `depas config` lists everything
                            refuses_politely=True)
    actions = configurer.add_subparsers()

    lister = actions.add_parser("list", help="every setting and what it is set to")
    lister.set_defaults(func=config_list)

    getter = actions.add_parser("get", help="one setting, with what it means")
    getter.add_argument("name")
    getter.set_defaults(func=config_get)

    setter = actions.add_parser("set", help="write one setting, checked before it is stored")
    setter.add_argument("name")
    # nargs="+": DEPAS_SECURITY_WANTED is "24 horas" and the home JSON has spaces in it.
    setter.add_argument("value", nargs="+")
    setter.set_defaults(func=config_set)

    unsetter = actions.add_parser("unset", help="forget one setting, back to its default")
    unsetter.add_argument("name")
    unsetter.set_defaults(func=config_unset)

    checker = actions.add_parser(
        "check", help="validate .env against the registry, touching nothing")
    checker.set_defaults(func=config_check)

    importer = actions.add_parser(
        "import-env", help="pull .env into the database again, after the initial seed")
    importer.add_argument("--force", action="store_true",
                          help="overwrite settings already stored with what .env says")
    importer.set_defaults(func=config_import_env)

    viewer = subparsers.add_parser("show", help="best price per m2, or your own SQL")
    viewer.add_argument("sql", nargs="?")
    viewer.add_argument("--limit", type=int, default=20)
    viewer.add_argument("--max-walk", type=int, help="max walking minutes to a metro station")
    viewer.add_argument("--max-cost", type=int, help="max net monthly cost in CLP")
    viewer.add_argument("--min-floor", type=int)
    viewer.add_argument("--min-bedrooms", type=int)
    viewer.add_argument("--min-area", type=float, help="minimum useful m2")
    viewer.add_argument("--max-age", type=int,
                        help="max years since the building went up; alerts never filter on it")
    viewer.add_argument("--security", help='e.g. "24 horas"')
    viewer.add_argument("--available-by", help="latest move-in date, as 2026-11-01")
    viewer.add_argument("--commune", action="append", default=[], type=Commune,
                        choices=list(Commune), metavar="SLUG")
    viewer.set_defaults(func=show)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as error:
        # Under `config` a ValueError is always somebody's typo, and a traceback is the
        # wrong way to say a commune does not exist. Everywhere else it is a bug, and
        # the traceback is the point.
        if not getattr(args, "refuses_politely", False):
            raise
        raise SystemExit(f"depas: {error}") from None
