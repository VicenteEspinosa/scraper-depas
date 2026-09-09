import argparse
import sqlite3
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import ModuleType

from curl_cffi.requests.exceptions import HTTPError

from depas import shortlist
from depas.bot import post_breakdown, refresh_card
from depas.bot import run as run_bot
from depas.communes import SANTIAGO_PROVINCE, Commune
from depas.commute import as_text as commute_text
from depas.commute import resolve_locations
from depas.config import DEFAULT_COMMON_EXPENSES
from depas.detail import INFERRED_VERSION, infer_from_description
from depas.fetch import Fetcher
from depas.grade import Scale
from depas.metro import nearest_station
from depas.models import Listing, Query
from depas.portals import PORTALS
from depas.preferences import (
    DEFAULTED,
    SET,
    Preferences,
    check_environment,
    described,
    seed_from_env,
    setting,
)
from depas.store import (
    KEPT,
    Subscriber,
    add_subscriber,
    clear_notified,
    connect,
    cutoff_safety,
    due_a_deep_sweep,
    fill_gaps,
    forget_preference,
    known_ids,
    mark_delisted,
    mark_notified,
    pending_detail,
    pool_query,
    quiet_portals,
    refresh_commutes,
    refresh_zone_benchmarks,
    remember_card,
    remember_sweep,
    remember_validators,
    remember_watch,
    remove_subscriber,
    save,
    save_detail,
    stale_stages,
    store_preference,
    stored_watch,
    subscribers,
    sweep_delisted,
    sync_lease_income,
    validator_coverage,
)
from depas.telegram import (
    chat_type,
    chats,
    escape,
    format_listing,
    hides_comments,
    reply,
    send_listing,
    verdict_buttons,
)
from depas.traits import EXCLUDE
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
        chosen = {name: PORTALS[name] for name in args.portals} or PORTALS
        # No prefs: run by hand it reads every page, since the point is usually to see
        # everything a portal has rather than only what is new.
        counts = _discover(connection, stored_uf(connection, fetcher), query, chosen)
    finally:
        fetcher.close()
        connection.close()
    print(f"{counts['new']} new, {counts['price_changed']} price changed, "
          f"{counts['portals']} sweeps ok, {counts['failed']} failed")


def _matching(
    listings: Iterator[Listing], uf_value: float, query: Query
) -> Iterator[Listing]:
    """Normalize UF prices to CLP; portal-side filters are unreliable, so re-check them here."""
    for listing in listings:
        normalize(listing, uf_value)
        if query.min_price is not None and listing.price_clp < query.min_price:
            continue
        if query.max_price is not None and listing.price_clp > query.max_price:
            continue
        if query.min_bedrooms is not None and (listing.bedrooms or 0) < query.min_bedrooms:
            continue
        if query.min_area_m2 is not None and (listing.area_m2 or 0) < query.min_area_m2:
            continue
        yield listing


@dataclass(slots=True)
class Swept:
    """One (portal, comuna) sweep as it came back, before anything has been written."""

    portal: str
    commune: Commune | None
    started_at: str
    listings: list[Listing] = field(default_factory=list)
    error: str | None = None
    deep: bool = False

    def pages_read(self) -> int | None:
        """How many pages this sweep got through, for the portals that paginate at all."""
        pages = [one.extra["page"] for one in self.listings if "page" in one.extra]
        return max(pages) + 1 if pages else None

    def deepest_new_page(self, known: frozenset[str]) -> int | None:
        """The deepest page a listing we had never seen turned up on.

        The evidence for or against the ordering the cutoff assumes: while this stays
        under DEPAS_SWEEP_QUIET_PAGES, stopping early cannot have dropped anything.
        """
        pages = [one.extra["page"] for one in self.listings
                 if "page" in one.extra and one.external_id not in known]
        return max(pages) if pages else None


def _sweep_portal(portal: ModuleType, query: Query, uf_value: float) -> list[Swept]:
    """Scrape one portal comuna by comuna, in this thread, touching no database.

    The workers fetch and parse; the caller writes. That keeps SQLite to the one writer
    it is happiest with and means nothing here has to think about transactions — the
    listings come back as plain objects.

    Comuna by comuna because that is the unit the evidence is recorded at: one comuna's
    markup breaking must not make the portal's others look swept.
    """
    fetcher = Fetcher()
    swept: list[Swept] = []
    try:
        for commune in query.communes or [None]:
            one = replace(query, communes=[commune] if commune else [])
            started_at = datetime.now(UTC).isoformat()
            try:
                found = list(_matching(portal.search(fetcher, one), uf_value, one))
            except NotImplementedError:
                return []  # this portal does not do this operation at all
            except Exception as error:
                # Recorded rather than raised: the comunas already swept are real
                # findings, and the other five portals have nothing to do with this.
                swept.append(Swept(portal.NAME, commune, started_at,
                                   error=f"{type(error).__name__}: {error}",
                                   deep=query.quiet_pages == 0))
                return swept
            swept.append(Swept(portal.NAME, commune, started_at, found,
                               deep=query.quiet_pages == 0))
        return swept
    finally:
        fetcher.close()


def _discover(connection: sqlite3.Connection, uf_value: float, query: Query,
              portals: Mapping[str, ModuleType] | None = None,
              prefs: Preferences | None = None) -> dict[str, int]:
    """Sweep every portal at once, then write what they found.

    One worker per portal: they are six different hosts, so this costs no host more
    requests per second than the sequential version did — `Fetcher` keeps its own polite
    delay inside each thread — and the pass stops taking the sum of six portals.
    """
    chosen = PORTALS if portals is None else portals
    quiet = 0 if prefs is None else prefs.value("DEPAS_SWEEP_QUIET_PAGES")
    hours = 0 if prefs is None else prefs.value("DEPAS_DEEP_SWEEP_HOURS")
    # Both read here rather than inside a worker, which never touches the database: what
    # each portal has already stored, and whether it is its turn to be read to the bottom.
    known = {name: known_ids(connection, name) for name in chosen}
    plans = {name: replace(query, known=known[name],
                           quiet_pages=0 if due_a_deep_sweep(connection, name, hours)
                           else quiet)
             for name in chosen}

    totals = {"new": 0, "price_changed": 0, "unchanged": 0, "portals": 0, "failed": 0,
              "deep": 0}
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        sweeps = pool.map(lambda name: _sweep_portal(chosen[name], plans[name], uf_value),
                          list(chosen))
        for swept in [one for portal in sweeps for one in portal]:
            counts = save(connection, swept.listings)
            remember_sweep(connection, swept.portal, _slug(swept.commune),
                           swept.started_at, len(swept.listings), swept.error,
                           pages_read=swept.pages_read(), deep=swept.deep,
                           deepest_new_page=swept.deepest_new_page(known[swept.portal]))
            for name in ("new", "price_changed", "unchanged"):
                totals[name] += counts[name]
            totals["failed" if swept.error else "portals"] += 1
            totals["deep"] += int(swept.deep and not swept.error)
            if swept.error:
                print(f"WARNING {swept.portal} "
                      f"{_slug(swept.commune) or 'todas'}: {swept.error}")
    # One portal being down is not worth every other portal's alerts, which is what
    # raising here used to cost. All of them failing is a different thing.
    if totals["portals"] == 0 and totals["failed"] > 0:
        raise RuntimeError(f"every sweep failed ({totals['failed']} of them)")
    return totals


def _slug(commune: Commune | None) -> str | None:
    return commune.value if commune is not None else None


def _budget(override: int | None, prefs: Preferences, name: str) -> int:
    """How much work this pass may do: the flag if one was given, else the setting."""
    return override if override is not None else prefs.value(name)


HAS_DESCRIPTION = "description IS NOT NULL AND description != ''"


def _infer_stored_descriptions(connection: sqlite3.Connection) -> int:
    """Fill columns a portal left empty from descriptions this version has not read."""
    filled = 0
    rows = connection.execute(
        f"SELECT * FROM listings WHERE {HAS_DESCRIPTION} AND inferred_version < ?",
        (INFERRED_VERSION,),
    ).fetchall()
    for row in rows:
        gaps = {column: value
                for column, value in infer_from_description(row["description"]).items()
                if row[column] is None}
        if gaps:
            fill_gaps(connection, row["portal"], row["external_id"], gaps)
            filled += 1
    # Stamped after the reading, so a pass that dies half way scans those rows again
    # rather than marking them read on the strength of work it never did.
    connection.execute(
        f"UPDATE listings SET inferred_version = ? WHERE {HAS_DESCRIPTION} "
        "AND inferred_version < ?",
        (INFERRED_VERSION, INFERRED_VERSION),
    )
    connection.commit()
    return filled


def _enrich_one(connection: sqlite3.Connection, fetcher: Fetcher, row: sqlite3.Row) -> bool:
    """Fetch one detail page, falling back to a computed walk when the portal omits one."""
    try:
        detail = PORTALS[row["portal"]].fetch_detail(fetcher, row["url"])
    except HTTPError as error:
        if error.response.status_code != 404:  # anything else is the portal, not this listing
            raise
        # The strongest delisting signal there is: the portal itself says the page is
        # gone. Before `delisted_at` existed, leaving the row unenriched was the only
        # way to keep it out of the pool — which did nothing for a row already in it.
        mark_delisted(connection, row["portal"], row["external_id"])
        print(f"gone: {row['url']}")
        return False
    description = detail.get("description")
    if description:
        # The portal's own spec table always wins; prose only fills what it left empty.
        detail = infer_from_description(str(description)) | detail
    if "nearest_station" not in detail and detail.get("lat") is not None:
        station, metres, minutes = nearest_station(detail["lat"], detail["lon"])
        detail |= {"nearest_station": station, "station_distance_m": metres,
                   "walk_minutes": minutes, "walk_source": "computed"}
    save_detail(connection, row["portal"], row["external_id"], detail)
    return True


FILTERS = (
    ("max_cost", "net_monthly_clp <= ?"),
    ("max_walk", "walk_minutes <= ?"),
    ("min_floor", "floor >= ?"),
    ("min_bedrooms", "bedrooms >= ?"),
    ("min_area", "area >= ?"),
    ("max_age", "age <= ?"),
    ("security", "security_type = ?"),
)


def _build_query(args: argparse.Namespace,
                 prefs: Preferences) -> tuple[str, tuple[object, ...]]:
    """Assemble the ranked query from whichever filters were actually given."""
    # The same pool the grading uses, so browsing and alerting agree on what is a candidate.
    conditions = [KEPT, *(f"({trait.keeps})" for trait in prefs.traits(EXCLUDE))]
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

# Re-applied after enrichment, which overwrites card values and can disqualify a listing.
ALERT_REQUIREMENTS = (
    ("DEPAS_COST_MAX", "net_monthly_clp <= ?"),
    ("DEPAS_BEDROOMS_MIN", "bedrooms >= ?"),
    ("DEPAS_WALK_MAX", "walk_minutes <= ?"),
    ("DEPAS_AREA_MIN", "(area IS NULL OR area >= ?)"),
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
            # The path is bound, not interpolated, and quoted so a label with a dot or
            # a space in it still names its key instead of silently matching nothing.
            conditions.append("json_extract(commute, ?) <= ?")
            parameters.extend((f'$."{place.name}"', reach))
    communes = prefs.communes()
    if communes:
        conditions.append(f"commune IN ({', '.join('?' * len(communes))})")
        parameters.extend(communes)
    return conditions, parameters


def _post_card(connection: sqlite3.Connection, prefs: Preferences, destination: str,
               row: dict, text: str) -> None:
    """Post one card, record it, and explain its grade underneath where nothing else will."""
    sent = send_listing(destination, text, row["image_url"],
                        buttons=verdict_buttons(row["id"], row["interest"]))
    # Recorded so a command left under the card finds its listing, and can redraw it.
    remember_card(connection, sent["chat"]["id"], sent["message_id"],
                  row["portal"], row["external_id"], "photo" in sent)
    # Where the card gets a Comments thread the bot posts the breakdown into it instead,
    # under the keyboard, once Telegram's copy of the card tells it where the thread is.
    if hides_comments(destination):
        return
    card = {"chat_id": str(sent["chat"]["id"]), "message_id": sent["message_id"],
            "portal": row["portal"], "external_id": row["external_id"]}
    post_breakdown(connection, card, prefs)
    time.sleep(ALERT_DELAY_SECONDS)  # a second message spends a second slice of the rate limit


def _announce_to(connection: sqlite3.Connection, prefs: Preferences,
                 subscriber: Subscriber, limit: int) -> int:
    """Post what this subscriber has not been shown yet and that clears DEPAS_GRADE_MIN."""
    conditions, parameters = _requirement_clauses(prefs)
    candidates = connection.execute(
        f"{pool_query(prefs, subscriber)} AND notified_at IS NULL"
        + "".join(f" AND {condition}" for condition in conditions),
        parameters,
    ).fetchall()
    if not candidates:
        return 0

    scale = Scale(prefs)
    minimum = prefs.value("DEPAS_GRADE_MIN") or 0

    graded = sorted(((row, scale.grade(dict(row))) for row in candidates),
                    key=lambda pair: pair[1].score, reverse=True)
    destination = subscriber.chat_id
    # Not `chat_type` any more: it asks Telegram, and per subscriber per pass that is a
    # request bought for a log line. `depas subscribers list` says what each chat is.
    print(f"alerts: posting to {destination}")
    posted = 0
    for row, grade in graded:
        if posted >= limit:
            break
        # Below the bar still gets stamped, so it is never reconsidered later.
        if grade.score >= minimum:
            _post_card(connection, prefs, destination, dict(row),
                       format_listing(dict(row), grade, prefs))
            posted += 1
            time.sleep(ALERT_DELAY_SECONDS)  # Telegram rate-limits how fast a chat is posted to
        mark_notified(connection, destination, row["portal"], row["external_id"])
    return posted


def _announce(connection: sqlite3.Connection, prefs: Preferences, limit: int) -> int:
    """Post to every subscriber, each with its own budget and its own idea of the pool.

    Per subscriber rather than per listing: the budget is there to keep one chat from
    being flooded, and two chats are not one chat. One destination refusing a card — a
    bot removed from a channel, say — must not cost the others theirs.
    """
    posted = 0
    for subscriber in subscribers(connection, prefs):
        try:
            posted += _announce_to(connection, prefs, subscriber, limit)
        except (RuntimeError, ValueError) as error:
            print(f"WARNING could not post to {subscriber.chat_id}: {error}")
    return posted


def _watched_query(prefs: Preferences) -> Query:
    """What the scheduled work looks for, read from the settings rather than the flags."""
    communes = [Commune(slug) for slug in prefs.communes()]
    if not communes:
        raise ValueError("set DEPAS_COMMUNES to the commune slugs you want watched")
    return Query(
        operation="rent",
        communes=communes,
        max_price=prefs.max_rent(),  # derived from the budget, not configured
        min_bedrooms=prefs.value("DEPAS_BEDROOMS_MIN"),
    )


@contextmanager
def _stage(name: str) -> Iterator[tuple[sqlite3.Connection, Fetcher, Preferences]]:
    """One stage of the scheduled work, stamped only if it ran the whole way through.

    The stamp goes on at the end on purpose: a 404 in the enrichment once got past every
    freshness signal there was — listings minutes old, the UF cache current, both
    containers up for days — while no alert had been posted for 44 hours. Only finishing
    proves it finished. What is new is that each stage says so for itself, so a stalled
    enrichment is no longer hidden behind a scrape that keeps succeeding.
    """
    fetcher = Fetcher()
    connection = connect()
    try:
        yield connection, fetcher, Preferences.load(connection)
        remember_validators(connection, fetcher.validators)
        remember_watch(connection, None, name)
    except Exception as error:
        # Re-raised: supercronic still logs it and the exit code still says it failed.
        remember_watch(connection, f"{type(error).__name__}: {error}", name)
        raise
    finally:
        fetcher.close()
        connection.close()


def discover(args: argparse.Namespace) -> None:
    """Sweep every configured comuna on every portal, all six at once."""
    with _stage("discover") as (connection, fetcher, prefs):
        # The query first: a misconfigured comuna list must fail before anything is
        # fetched, and naming `stored_uf` inside the call would have it evaluated first.
        query = _watched_query(prefs)
        # The ranked view prices per m2 straight from this, so cache it before reading it.
        counts = _discover(connection, stored_uf(connection, fetcher), query,
                           prefs=prefs)
        print(f"scrape: {counts['new']} new, {counts['price_changed']} price changed, "
              f"{counts['portals']} sweeps ok, {counts['failed']} failed, "
              f"{counts['deep']} read to the bottom")
        # The cutoff assumes the portal returns the newest first. This is the check.
        for risky in cutoff_safety(connection, prefs.value("DEPAS_SWEEP_QUIET_PAGES")):
            print(f"WARNING {risky['portal']}: a listing we had never seen turned up on "
                  f"page {risky['deepest'] + 1}, past where the cutoff stops — its pages "
                  f"are not newest-first, so set DEPAS_SWEEP_QUIET_PAGES to 0")
        gone = sweep_delisted(connection, prefs.value("DEPAS_DELIST_AFTER"))
        print(f"delisted: {gone} listings no sweep has turned up")
        for quiet in quiet_portals(connection):
            print(f"WARNING {quiet['portal']}: last sweep saw no listings at all, "
                  f"where an earlier one saw {quiet['cards_at_best']} — parser or portal?")


def enrich(args: argparse.Namespace) -> None:
    """Read the detail pages that are due: the new ones first, then the re-reads."""
    with _stage("enrich") as (connection, fetcher, prefs):
        # The ranked view prices per m2 straight from this, so cache it before reading it.
        stored_uf(connection, fetcher)
        pending = pending_detail(
            connection,
            _budget(args.limit, prefs, "DEPAS_ENRICH_LIMIT"),
            _budget(args.refresh_limit, prefs, "DEPAS_REFRESH_LIMIT"))
        enriched = sum(_enrich_one(connection, fetcher, row) for row in pending)
        print(f"enrich: {enriched} of {len(pending)} listings")
        print(f"from descriptions: {_infer_stored_descriptions(connection)} listings filled")
        urls, offered = validator_coverage(connection)
        print(f"http: {offered} of {urls} urls offer a cache validator")


def route(args: argparse.Namespace) -> None:
    """Travel times and the zone benchmarks, both of which the grading reads."""
    with _stage("route") as (connection, fetcher, prefs):
        routed = refresh_commutes(
            connection, fetcher, prefs,
            _budget(args.limit, prefs, "DEPAS_COMMUTE_LIMIT"))
        print(f"commutes: {routed} routed")
        print(f"zone benchmarks: {refresh_zone_benchmarks(connection)} communes")


def announce(args: argparse.Namespace) -> None:
    """Post what is enriched, un-announced and over the bar, then restate the ⭐ list."""
    with _stage("announce") as (connection, _fetcher, prefs):
        alerts = _budget(args.limit, prefs, "DEPAS_ALERTS_LIMIT")
        print(f"alerts: {_announce(connection, prefs, alerts)} posted")
        # Grades move with the pool, so the pinned list is restated once a pass.
        print(f"lista: {'actualizada' if shortlist.sync(connection, prefs) else 'sin cambios'}")


# Every stage in the order they feed each other, which is what one hourly crontab entry
# runs. Split them across entries and each keeps its own heartbeat; leave it as one and
# nothing about the old behaviour changes.
PASS_STAGES = (discover, enrich, route, announce)


def watch(args: argparse.Namespace) -> None:
    """One scheduled pass: every stage in order, stamped as a whole as well as apart."""
    connection = connect()
    try:
        for stage in PASS_STAGES:
            stage(args)
        remember_watch(connection, None)
    except Exception as error:
        remember_watch(connection, f"{type(error).__name__}: {error}")
        raise
    finally:
        connection.close()


STAGE_LABEL = {"watch": "la pasada horaria", "discover": "el barrido de portales",
               "enrich": "la lectura de fichas", "route": "el ruteo de viajes",
               "announce": "la publicación de alertas"}


def healthcheck(args: argparse.Namespace) -> None:
    """Warn the admins when a stage has gone too long without completing."""
    connection = connect()
    try:
        prefs = Preferences.load(connection)
        stale = stale_stages(connection, args.stale_hours)
        if not stale:
            completed, _ = stored_watch(connection)
            print(f"watch healthy: last completed {completed}")
            return

        lines = []
        for stage, completed, error in stale:
            # Read on a phone, so the minute rather than the microsecond it carries.
            since = (f"la última terminó el {completed[:16].replace('T', ' ')} UTC"
                     if completed else "nunca ha terminado una")
            lines.append(f"• <b>{STAGE_LABEL.get(stage, stage)}</b>: {since}.")
            if error:
                lines.append(f"  <code>{escape(error)}</code>")
        warning = "⚠️ <b>Hay etapas que no están corriendo</b>\n" + "\n".join(lines)
        for admin in prefs.admins():
            reply(str(admin), warning)
        print(f"watch stale: warned {len(prefs.admins())} admins about "
              f"{', '.join(stage for stage, _, _ in stale)}")
    finally:
        connection.close()


def subscribers_list(args: argparse.Namespace) -> None:
    """Every chat cards go to, and whose opinion shapes what each one is shown."""
    connection = connect()
    try:
        prefs = Preferences.load(connection)
        found = subscribers(connection, prefs)
        stored = {row["chat_id"] for row in connection.execute(
            "SELECT chat_id FROM subscribers WHERE enabled = 1")}
    finally:
        connection.close()
    if not found:
        print("nobody is subscribed. `depas subscribers add CHAT_ID` starts one, and "
              "`depas chats` lists what the bot can see.")
        return
    for one in found:
        shared = "compartido" if one.owner is None else f"de {one.owner}"
        # A chat standing in for the setting is not stored, and says so: it disappears
        # the moment a real subscriber is added.
        source = "" if one.chat_id in stored else "  (desde TELEGRAM_CHAT_ID)"
        print(f"{one.chat_id:>16}  {chat_type(one.chat_id):12}  {shared}{source}")


def subscribers_add(args: argparse.Namespace) -> None:
    """Start posting to a chat: a private conversation, or a channel with its group."""
    connection = connect()
    try:
        written_off = add_subscriber(connection, args.chat_id, args.owner,
                                     catch_up=args.catch_up)
    finally:
        connection.close()
    whose = "compartido: cuenta el veredicto de cualquiera" if args.owner is None \
        else f"privado de {args.owner}: solo su veredicto lo moldea"
    print(f"{args.chat_id} suscrito ({whose})")
    if written_off:
        print(f"  {written_off} avisos ya guardados quedan por vistos; este chat empieza "
              "en lo que venga (--catch-up para recibirlos)")


def subscribers_remove(args: argparse.Namespace) -> None:
    """Stop posting to a chat, keeping what it was already told."""
    connection = connect()
    try:
        removed = remove_subscriber(connection, args.chat_id)
    finally:
        connection.close()
    print(f"{args.chat_id} " + ("dado de baja" if removed else "no estaba suscrito"))


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
        first = subscribers(connection, prefs)
        if not first:
            raise ValueError("nobody is subscribed; set TELEGRAM_CHAT_ID or "
                             "`depas subscribers add`")
        pool = [dict(row) for row in connection.execute(pool_query(prefs, first[0]))]
        if not pool:
            raise ValueError("nothing enriched to post; run `depas enrich` first")
        scale = Scale(prefs)
        row, grade = max(((row, scale.grade(row)) for row in pool), key=lambda pair: pair[1].score)
        # Posted like any other card, so /like, /dislike and the breakdown can be tried on it.
        _post_card(connection, prefs, first[0].chat_id, row,
                   format_listing(row, grade, prefs, is_test=True))
        print(f"test alert posted: {grade.letter} {grade.score} {row['url']}")
    finally:
        connection.close()


def resend(args: argparse.Namespace) -> None:
    """Un-stamp recent alerts so the next watch pass posts them again, to wherever it posts now."""
    connection = connect()
    try:
        cleared = clear_notified(connection, args.hours, args.chat)
    finally:
        connection.close()
    print(f"{cleared} listings un-stamped; `depas watch` will announce them again")


def redraw(args: argparse.Namespace) -> None:
    """Re-render cards already posted, newest first, with today's grades and today's rules."""
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


def pinned_list(args: argparse.Namespace) -> None:
    """Re-post or re-render the pinned ⭐ list, which verdicts otherwise keep current."""
    connection = connect()
    try:
        prefs = Preferences.load(connection)
        starred = shortlist.starred(connection, prefs)
        print(f"lista: {len(starred)} marcados · "
              f"{'actualizada' if shortlist.sync(connection, prefs) else 'sin publicar'}")
    finally:
        connection.close()


def show(args: argparse.Namespace) -> None:
    connection = connect()
    prefs = Preferences.load(connection)
    query, parameters = (args.sql, ()) if args.sql else _build_query(args, prefs)
    rows = connection.execute(query, parameters).fetchall()
    if args.sql:
        _print_table(rows)
        return
    scale = Scale(prefs)
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
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    for row in rows:
        print("  ".join(str(row[c]).ljust(w) for c, w in zip(columns, widths, strict=True)))


# Every write goes through `store_preference`, the one validated path the chat shares.
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
    written = " ".join(args.value)
    # The only setting you can give in words: an address is geocoded here, once.
    if args.name == "DEPAS_LOCATIONS":
        fetcher = Fetcher()
        try:
            written, matched = resolve_locations(fetcher, written)
        finally:
            fetcher.close()
        for match in matched:
            print(f"  {match}")
    connection = connect()
    try:
        value = store_preference(connection, args.name, written)
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
    """Validate .env against the registry, touching nothing."""
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


# What every stage reads off its args. `watch` calls the stages directly, so its own
# namespace has to carry the same names — None everywhere, meaning "use the setting".
_STAGE_DEFAULTS = {"limit": None, "refresh_limit": None}


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

    # One stage each, so they can run on their own schedules; `watch` is all four in
    # order and is what a single crontab entry still gets.
    discoverer = subparsers.add_parser(
        "discover", help="sweep every configured comuna on every portal")
    discoverer.set_defaults(func=discover, **_STAGE_DEFAULTS)

    enricher = subparsers.add_parser("enrich", help="read the detail pages that are due")
    enricher.add_argument("--limit", type=int,
                          help="detail pages this run; default DEPAS_ENRICH_LIMIT")
    enricher.add_argument("--refresh-limit", type=int,
                          help="detail pages re-read; default DEPAS_REFRESH_LIMIT")
    enricher.set_defaults(func=enrich, **{**_STAGE_DEFAULTS, "limit": None})

    router = subparsers.add_parser("route", help="travel times and the zone benchmarks")
    router.add_argument("--limit", type=int,
                        help="listings routed this run; default DEPAS_COMMUTE_LIMIT")
    router.set_defaults(func=route, **{**_STAGE_DEFAULTS, "limit": None})

    announcer = subparsers.add_parser("announce", help="post what is over the bar")
    announcer.add_argument("--limit", type=int,
                           help="cards posted this run; default DEPAS_ALERTS_LIMIT")
    announcer.set_defaults(func=announce, **{**_STAGE_DEFAULTS, "limit": None})

    watcher = subparsers.add_parser(
        "watch", help="scheduled pass: every stage in order")
    # No numbers here: the standing budgets live in the settings, where they can be moved
    # without a redeploy. The stages read `limit` and `refresh_limit`, so a pass that
    # overrides nothing passes None for both and each stage falls back to its setting.
    watcher.set_defaults(func=watch, **_STAGE_DEFAULTS)

    checker = subparsers.add_parser(
        "healthcheck", help="warn the admins if the hourly pass has stopped completing")
    checker.add_argument("--stale-hours", type=int, default=4,
                         help="hours without a completed pass before the admins are warned")
    checker.set_defaults(func=healthcheck)

    bot = subparsers.add_parser("bot", help="reply to portal links posted in the chat")
    bot.set_defaults(func=lambda _: run_bot())

    chatter = subparsers.add_parser("chats", help="list Telegram chats the bot can see")
    chatter.set_defaults(func=telegram_chats)

    subs = subparsers.add_parser("subscribers", help="where cards get posted")
    subs.set_defaults(func=subscribers_list)
    sub_actions = subs.add_subparsers()

    sub_lister = sub_actions.add_parser("list", help="every chat cards go to")
    sub_lister.set_defaults(func=subscribers_list)

    sub_adder = sub_actions.add_parser("add", help="start posting to a chat")
    sub_adder.add_argument("chat_id")
    sub_adder.add_argument("--owner", type=int,
                           help="Telegram user id whose verdicts shape this chat's pool; "
                                "omit for a shared chat, where anybody's count")
    sub_adder.add_argument("--catch-up", action="store_true",
                           help="also post the backlog; without it the chat starts on "
                                "what is found from now on")
    sub_adder.set_defaults(func=subscribers_add)

    sub_remover = sub_actions.add_parser("remove", help="stop posting to a chat")
    sub_remover.add_argument("chat_id")
    sub_remover.set_defaults(func=subscribers_remove)

    tester = subparsers.add_parser("test-alert", help="post the top listing as a test card")
    tester.set_defaults(func=test_alert)

    resender = subparsers.add_parser(
        "resend", help="re-announce recently alerted listings on the next watch pass")
    resender.add_argument("--hours", type=int, default=6,
                          help="how far back to un-stamp; older alerts are left alone")
    resender.add_argument("--chat", help="only this subscriber; default every one of them")
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

    pinner = subparsers.add_parser(
        "shortlist", help="re-post or re-render the pinned list of what you starred")
    pinner.set_defaults(func=pinned_list)

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
    viewer.add_argument("--commune", action="append", default=[], type=Commune,
                        choices=list(Commune), metavar="SLUG")
    viewer.set_defaults(func=show)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as error:
        # Under `config` a ValueError is somebody's typo; everywhere else it is a bug.
        if not getattr(args, "refuses_politely", False):
            raise
        raise SystemExit(f"depas: {error}") from None
