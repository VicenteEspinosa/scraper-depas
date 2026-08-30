-- One row per listing per portal, upserted on every scrape; price_history keeps
-- the trail of what it used to cost. Detail-page columns stay NULL until the
-- listing is enriched, so `detail_fetched_at IS NULL` is the enrichment queue.
CREATE TABLE IF NOT EXISTS listings (
    portal          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    price           REAL NOT NULL,
    currency        TEXT NOT NULL,
    common_expenses INTEGER,
    is_project      INTEGER NOT NULL DEFAULT 0,
    price_clp       REAL,
    bedrooms        INTEGER,
    bathrooms       INTEGER,
    area_m2         REAL,
    commune         TEXT,
    address         TEXT,
    lat             REAL,
    lon             REAL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,

    -- detail page
    area_total_m2        REAL,
    area_useful_m2       REAL,
    terrace_m2           REAL,
    rooms                INTEGER,
    parking_spaces       INTEGER,
    storage_units        INTEGER,
    floor                INTEGER,
    building_floors      INTEGER,
    units_per_floor      INTEGER,
    age_years            INTEGER,
    orientation          TEXT,
    available_from       TEXT,
    furnished            INTEGER,
    pets_allowed         INTEGER,
    has_elevator         INTEGER,
    has_concierge        INTEGER,
    security_type        TEXT,
    gated_community      INTEGER,
    has_heating          INTEGER,
    has_air_conditioning INTEGER,
    has_pool             INTEGER,
    has_gym              INTEGER,
    has_terrace          INTEGER,
    description          TEXT,
    published_label      TEXT,
    published_days_ago   INTEGER,
    features             TEXT,
    nearest_station      TEXT,
    station_distance_m   INTEGER,
    walk_minutes         INTEGER,
    walk_source          TEXT,
    transit              TEXT,
    broker               TEXT,
    price_per_m2_uf      REAL,
    zone_price_per_m2_uf REAL,
    detail_fetched_at    TEXT,

    PRIMARY KEY (portal, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    seen_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON price_history (portal, external_id, seen_at);

CREATE INDEX IF NOT EXISTS idx_listings_pending_detail
    ON listings (portal) WHERE detail_fetched_at IS NULL;

-- Lease income is mirrored here from the environment on every connect so the
-- ranked view can read it from SQL.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('parking_income', 0), ('storage_income', 0);
