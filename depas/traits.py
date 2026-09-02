"""Things a listing either is or is not, and what having one does to it (docs/DESIGN.md)."""
from collections.abc import Callable
from dataclasses import dataclass

EXCLUDE, PENALISE, IGNORE = "exclude", "penalise", "ignore"
DISPOSITIONS = (EXCLUDE, PENALISE, IGNORE)


@dataclass(frozen=True, slots=True)
class Trait:
    """One yes/no property, with both ways of spotting it and what it does by default."""

    setting: str
    # The same question asked of SQL and of a row; the two must agree on every listing.
    keeps: str
    holds: Callable[[dict], bool | None]
    help: str
    default: str
    component: str = "traits"
    penalty: float = 20.0


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
          # Docked inside `floor`, so a penthouse stays worse than the same unit lower down.
          component="floor"),
)

BY_SETTING = {trait.setting: trait for trait in TRAITS}
