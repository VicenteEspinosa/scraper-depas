"""Things a listing either is or is not, and that you either will not take or dislike.

A trait is not a range, so it has no MIN/MAX/TARGET: what varies is only what it does
to a listing that has it. That is the setting -- `exclude` drops the listing, `penalise`
only costs it score, `ignore` does neither -- because whether amoblado is a deal-breaker
or a mild dislike is a preference, not something the code should decide.

Excluding is the heavier of the two: it takes the listing out of the pool everything else
is ranked against, so it moves other listings' grades. Penalising moves only its own.
"""
from collections.abc import Callable
from dataclasses import dataclass

EXCLUDE, PENALISE, IGNORE = "exclude", "penalise", "ignore"
DISPOSITIONS = (EXCLUDE, PENALISE, IGNORE)


@dataclass(frozen=True, slots=True)
class Trait:
    """One yes/no property, with both ways of spotting it and what it does by default.

    `keeps` and `holds` are the same question asked of SQL and of a row, because the two
    dispositions read it in different places: excluding is a WHERE clause over the pool,
    penalising is a component scored per listing, and the two must agree on every row:
    a listing excluded for a trait is the same listing penalised for it.

    `component` is where a penalty lands. Most traits have no natural home and share
    `traits`, where they count equally -- a percentile only sees the order, so a size
    would do nothing. One that belongs to an existing component says so and brings a
    `penalty`, which there competes against a continuous score and does mean something.
    """

    setting: str
    keeps: str
    holds: Callable[[dict], bool | None]
    help: str
    default: str
    component: str = "traits"
    penalty: float = 1.0


def _is_furnished(row: dict) -> bool:
    """Reads exactly as `keeps` does, so excluding and penalising never disagree on a row."""
    # The portals that publish no spec row for amoblado still say it in the title.
    return bool(row.get("furnished")) or "amoblad" in (row.get("title") or "").lower()


def _is_top_floor(row: dict) -> bool | None:
    floor, storeys = row.get("floor"), row.get("building_floors")
    return None if floor is None or storeys is None else floor == storeys


TRAITS: tuple[Trait, ...] = (
    Trait("DEPAS_FURNISHED",
          keeps="COALESCE(furnished, 0) = 0"
                " AND (title IS NULL OR lower(title) NOT LIKE '%amoblad%')",
          holds=_is_furnished,
          help="Qué hacer con un depto amoblado. Un aviso que no lo declara no cuenta "
               "como amoblado, pero un título que dice amoblado sí.",
          default=EXCLUDE),
    Trait("DEPAS_TOP_FLOOR",
          keeps="floor IS NULL OR building_floors IS NULL OR floor <> building_floors",
          holds=_is_top_floor,
          help="Qué hacer con el último piso, que se lleva el calor y las filtraciones "
               "del techo. Un aviso sin piso declarado nunca cuenta como último.",
          default=PENALISE,
          # Docked inside `floor`, on top of whatever the height already cost: a
          # penthouse stays worse than the identical unit one floor down.
          component="floor", penalty=0.5),
)

BY_SETTING = {trait.setting: trait for trait in TRAITS}
