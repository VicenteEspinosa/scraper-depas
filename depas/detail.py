import json
import re
import unicodedata

NUMBER = re.compile(r"-?\d[\d.]*(?:,\d+)?")
YES_NO = {"Sí": True, "No": False}

# Publishers sometimes type "160" meaning 160.000. Real gastos comunes are never
# this low, and a wrong figure quietly understates the net cost, so treat it as
# undeclared rather than guess at the intended magnitude.
MIN_PLAUSIBLE_COMMON_EXPENSES = 10_000

# Spec rows worth filtering on get their own column; everything else lands in `features`.
SPEC_COLUMNS: dict[str, tuple[str, str]] = {
    "Superficie total": ("area_total_m2", "REAL"),
    "Superficie útil": ("area_useful_m2", "REAL"),
    "Superficie de terraza": ("terrace_m2", "REAL"),
    "Dormitorios": ("bedrooms", "INTEGER"),
    "Baños": ("bathrooms", "INTEGER"),
    "Ambientes": ("rooms", "INTEGER"),
    "Estacionamientos": ("parking_spaces", "INTEGER"),
    "Bodegas": ("storage_units", "INTEGER"),
    "Número de piso de la unidad": ("floor", "INTEGER"),
    "Cantidad de pisos": ("building_floors", "INTEGER"),
    "Departamentos por piso": ("units_per_floor", "INTEGER"),
    "Antigüedad": ("age_years", "INTEGER"),
    "Gastos comunes": ("common_expenses", "INTEGER"),
    "Orientación": ("orientation", "TEXT"),
    "Disponible desde": ("available_from", "TEXT"),
    "Amoblado": ("furnished", "INTEGER"),
    "Admite mascotas": ("pets_allowed", "INTEGER"),
    "Ascensor": ("has_elevator", "INTEGER"),
    "Conserjería": ("has_concierge", "INTEGER"),
    "Tipo de seguridad": ("security_type", "TEXT"),
    "En condominio cerrado": ("gated_community", "INTEGER"),
    "Calefacción": ("has_heating", "INTEGER"),
    "Aire acondicionado": ("has_air_conditioning", "INTEGER"),
    "Piscina": ("has_pool", "INTEGER"),
    "Gimnasio": ("has_gym", "INTEGER"),
    "Terraza": ("has_terrace", "INTEGER"),
}

DETAIL_COLUMNS: dict[str, str] = {
    **{column: sql_type for column, sql_type in SPEC_COLUMNS.values()},
    "description": "TEXT",
    "published_label": "TEXT",
    "published_days_ago": "INTEGER",
    "features": "TEXT",
    "commute": "TEXT",
    "nearest_station": "TEXT",
    "station_distance_m": "INTEGER",
    "walk_minutes": "INTEGER",
    "walk_source": "TEXT",
    "transit": "TEXT",
    "broker": "TEXT",
    "price_per_m2_uf": "REAL",
    "zone_price_per_m2_uf": "REAL",
    "detail_fetched_at": "TEXT",
}


# TocToc and Chilepropiedades publish no spec table worth the name, but their prose
# names the same features outright. Only ever used for fields the portal left empty.
DESCRIPTION_HINTS = {
    "has_elevator": re.compile(r"ascensor", re.I),
    "has_concierge": re.compile(r"conserj\w*|porter[ií]a", re.I),
    "has_pool": re.compile(r"piscina", re.I),
    "has_gym": re.compile(r"gimnasio|\bgym\b", re.I),
    "has_heating": re.compile(r"calefacci[óo]n", re.I),
    "has_air_conditioning": re.compile(r"aire acondicionado", re.I),
    "has_terrace": re.compile(r"terraza", re.I),
}
# "piso 8" is the unit's floor; "piso flotante" is the flooring, and never matches
# because a digit is required.
FLOOR_IN_TEXT = re.compile(r"\bpiso\s+(\d{1,2})\b", re.I)
SECURITY_IN_TEXT = re.compile(r"24\s*(?:horas|hrs)", re.I)
# A denial only counts when it is right up against the feature ("sin ascensor",
# "no tiene ascensor") — in "sin piscina y gimnasio" the gym is not being denied.
DENIAL = re.compile(r"\b(?:sin|no)\s+(?:\w+\s+)?$", re.I)


def _claimed(text: str, pattern: re.Pattern[str]) -> bool | None:
    """True when the prose claims the feature, False when it denies it, None when silent."""
    match = pattern.search(text)
    if match is None:
        return None
    return DENIAL.search(text[:match.start()]) is None


def infer_from_description(text: str) -> dict[str, object]:
    """Read off the fields a portal omitted from its spec table but stated in prose."""
    inferred: dict[str, object] = {}
    for column, pattern in DESCRIPTION_HINTS.items():
        claimed = _claimed(text, pattern)
        if claimed is not None:
            inferred[column] = int(claimed)
    floor = FLOOR_IN_TEXT.search(text)
    if floor:
        inferred["floor"] = int(floor.group(1))
    if _claimed(text, SECURITY_IN_TEXT):
        inferred["security_type"] = "24 horas"
    return inferred


def parse_specs(rows: list[tuple[str, str]]) -> dict[str, object]:
    """Split a detail page's spec rows into promoted columns plus a `features` JSON blob."""
    parsed: dict[str, object] = {}
    features: dict[str, object] = {}

    for label, raw in rows:
        if label in SPEC_COLUMNS:
            column, sql_type = SPEC_COLUMNS[label]
            value = _coerce(raw, sql_type)
            if column == "common_expenses" and value is not None and value < MIN_PLAUSIBLE_COMMON_EXPENSES:
                value = None
            # keep the raw text rather than lose a value we could not parse or trust
            if value is None:
                features[_slug(label)] = raw
            else:
                parsed[column] = value
        else:
            features[_slug(label)] = YES_NO.get(raw, raw)

    parsed["features"] = json.dumps(features, ensure_ascii=False, sort_keys=True)
    return parsed


def _coerce(raw: str, sql_type: str) -> object | None:
    if sql_type == "TEXT":
        return raw or None
    if raw in YES_NO:
        return int(YES_NO[raw])
    match = NUMBER.search(raw)
    if match is None:
        return None
    number = float(match.group().replace(".", "").replace(",", "."))
    return number if sql_type == "REAL" else int(number)


def _slug(label: str) -> str:
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_label.lower()).strip("_")


AGE = re.compile(r"hace\s+(\d+)\s+(d[ií]a|semana|mes|a[ñn]o)", re.I)
AGE_IN_DAYS = {"dia": 1, "día": 1, "semana": 7, "mes": 30, "año": 365, "ano": 365}
FRESH_LABELS = {"hoy": 0, "ayer": 1, "esta semana": 3}


def published_days_ago(label: str) -> int | None:
    """Turn the portal's relative label ('hace 39 días', 'esta semana') into a day count."""
    normalized = label.strip().lower()
    if normalized in FRESH_LABELS:
        return FRESH_LABELS[normalized]
    match = AGE.search(normalized)
    return int(match.group(1)) * AGE_IN_DAYS[match.group(2)] if match else None
