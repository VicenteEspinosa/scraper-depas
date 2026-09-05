"""The pool at a glance: what you are choosing between, before you look at any one of it."""
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

from depas.commute import SANTIAGO
from depas.grade import LETTERS, Scale
from depas.preferences import Preferences
from depas.store import DISLIKE, LIKE, pool_query
from depas.telegram import (
    GRADE_EMOJI,
    bar,
    clp,
    escape,
    price_change,
    reply,
)

COMMAND = "/resumen"
# Every listing carries one of these; the order is the one a reader expects, best first.
GRADE_LETTERS = tuple(letter for _, letter in LETTERS) + ("E", "?")
# What counts as new, and how far back a price move is still news.
RECENT_DAYS = 7
BEST_SHOWN = 3
COMMUNES_SHOWN = 8

EMPTY = ("📊 <b>Tu pool</b>\n\nNo hay nada enriquecido todavía. Corre <code>depas watch</code> "
         "o espera a la próxima pasada.")
FOOTER = "Míralos uno por uno con /top."


def _pool(connection: sqlite3.Connection, prefs: Preferences) -> list[tuple[dict, object]]:
    """The same pool the alerts draw from, graded, best first."""
    scale = Scale(prefs)
    rows = [dict(row) for row in connection.execute(pool_query(prefs))]
    return sorted(((row, scale.grade(row)) for row in rows),
                  key=lambda pair: pair[1].score, reverse=True)


def _grades(graded: list[tuple[dict, object]]) -> list[str]:
    """The spread of grades as bars, so a pool of C's cannot be mistaken for a good week."""
    counted = Counter(grade.letter for _, grade in graded)
    most = max(counted.values(), default=0)
    return [f"{GRADE_EMOJI.get(letter, '⚪')} {letter}  {bar(counted[letter], most)} "
            f"{counted[letter]:>3}"
            for letter in GRADE_LETTERS if counted[letter]]


def _costs(graded: list[tuple[dict, object]], prefs: Preferences) -> list[str]:
    """The band the pool actually spans, and how much of it you could afford."""
    costs = sorted(row["net_monthly_clp"] for row, _ in graded
                   if row.get("net_monthly_clp") is not None)
    if not costs:
        return []
    lines = ["", "💰 <b>Neto al mes</b>",
             f"    más barato · {clp(costs[0])}",
             f"    mediana · {clp(median(costs))}",
             f"    más caro · {clp(costs[-1])}"]
    target = prefs.cost.target
    if target is not None:
        within = sum(1 for cost in costs if cost <= target)
        lines.append(f"    {within} de {len(costs)} bajo tu objetivo de {clp(target)}")
    return lines


def _communes(graded: list[tuple[dict, object]]) -> list[str]:
    """One row per commune: how many, the best grade in it, and what the middle one costs.

    Where to look is a decision the pool can answer and no single card can."""
    by_commune: dict[str, list[tuple[dict, object]]] = defaultdict(list)
    for row, grade in graded:
        by_commune[(row.get("commune") or "").replace("-", " ").title() or "—"].append(
            (row, grade))
    if len(by_commune) < 2:
        return []  # a single commune is the header restated, not a comparison

    ranked = sorted(by_commune.items(), key=lambda pair: len(pair[1]), reverse=True)
    shown = ranked[:COMMUNES_SHOWN]
    width = max(len(name) for name, _ in shown)
    rows = []
    for name, found in shown:
        costs = [row["net_monthly_clp"] for row, _ in found
                 if row.get("net_monthly_clp") is not None]
        best = max((grade for _, grade in found), key=lambda grade: grade.score)
        rows.append(f"{name.ljust(width)}  {len(found):>3}  {f'{best.letter} {best.score}':<6}"
                    f"{clp(median(costs)) if costs else '—':>10}")
    left_out = len(ranked) - len(shown)
    table = escape("\n".join(rows))
    return ["", f"📍 <b>Por comuna</b>{f' · y {left_out} más' if left_out else ''}",
            f"<pre>{table}</pre>"]


def _movement(connection: sqlite3.Connection, graded: list[tuple[dict, object]]) -> list[str]:
    """What changed since you last looked: arrivals, markdowns, and what you have judged."""
    cutoff = (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat()
    fresh = sum(1 for row, _ in graded if (row.get("first_seen") or "") >= cutoff)
    dropped = sum(1 for row, _ in graded
                  if (change := price_change(row)) and change[0] < 0)
    starred = sum(1 for row, _ in graded if row.get("interest") == LIKE)
    discarded = connection.execute(
        "SELECT COUNT(*) FROM listings WHERE interest = ?", (DISLIKE,)).fetchone()[0]

    parts = [f"🆕 {fresh} de los últimos {RECENT_DAYS} días" if fresh else None,
             f"📉 {dropped} baj{'aron' if dropped != 1 else 'ó'} de precio" if dropped else None,
             f"⭐ {starred} marcado{'s' if starred != 1 else ''}" if starred else None,
             f"🚫 {discarded} descartado{'s' if discarded != 1 else ''}" if discarded else None]
    kept = [part for part in parts if part]
    return ["", " · ".join(kept)] if kept else []


def _headline(row: dict, grade: object) -> str:
    """One listing as a single clickable line: the grade, where, what it costs."""
    commune = (row.get("commune") or "").replace("-", " ").title()
    facts = " · ".join(part for part in (
        f"{GRADE_EMOJI.get(grade.letter, '⚪')} {grade.letter} {grade.score}",
        escape(commune) or None,
        clp(row.get("net_monthly_clp")),
        f"{row['area']:.0f} m²" if row.get("area") else None,
    ) if part)
    return f'<a href="{escape(row["url"])}">{facts}</a>'


def _best(graded: list[tuple[dict, object]]) -> list[str]:
    return ["", "🏆 <b>Los mejores</b>",
            *(_headline(row, grade) for row, grade in graded[:BEST_SHOWN])]


def format_summary(connection: sqlite3.Connection, prefs: Preferences) -> str:
    """Render the whole pool as one message: its shape first, its best three last."""
    graded = _pool(connection, prefs)
    if not graded:
        return EMPTY
    when = datetime.now(SANTIAGO).strftime("%d/%m %H:%M")
    lines = [f"📊 <b>Tu pool</b> · {len(graded)} depto{'s' if len(graded) != 1 else ''} · {when}",
             "", *_grades(graded)]
    lines += _costs(graded, prefs)
    lines += _communes(graded)
    lines += _movement(connection, graded)
    lines += _best(graded)
    lines += ["", f"<i>{FOOTER}</i>"]
    return "\n".join(lines)


def answer(connection: sqlite3.Connection, message: dict, prefs: Preferences) -> None:
    """Answer /resumen wherever it was asked.

    Unlike /top it carries no keyboard, so there is nothing here a stranger in the
    discussion group could press — and the alert chat is exactly where the question
    «¿qué tengo?» gets asked."""
    reply(str(message["chat"]["id"]), format_summary(connection, prefs),
          message.get("message_thread_id"), message["message_id"])
