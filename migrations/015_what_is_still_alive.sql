-- `last_seen` was written on every scrape and read by nothing, so nothing ever noticed a
-- listing going away: an apartment rented three weeks ago stayed in the pool, kept voting
-- in its comuna's median, and kept sitting in the pinned ⭐ list. This is the column that
-- was missing, and `save` clears it again the moment a sweep sees the listing — a portal
-- outage, a comuna dropped from the config and then restored, all heal themselves.
ALTER TABLE listings ADD COLUMN delisted_at TEXT;

-- One row per (portal, comuna) sweep attempt. Delisting needs to know the difference
-- between "we looked and it was gone" and "we never got to look", and until now a pass
-- that scraped nothing at all still stamped watch_completed_at and read as healthy.
CREATE TABLE IF NOT EXISTS scrape_runs (
    portal      TEXT NOT NULL,
    commune     TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    cards_seen  INTEGER NOT NULL DEFAULT 0,
    -- Both are required before a sweep counts as evidence: a sweep that raised proves
    -- nothing, and neither does one that came back empty, which is what a portal whose
    -- markup moved looks like from here.
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_evidence
    ON scrape_runs (portal, started_at) WHERE ok = 1 AND cards_seen > 0;

-- The price the listing carried when its detail page was read. `price` is refreshed from
-- the search card every hour but `price_per_m2_uf` comes from the detail page and was
-- written once, so a listing whose rent moved was being ranked on a UF/m2 figure computed
-- from the old one — while every other listing used today's. This is how that is noticed.
ALTER TABLE listings ADD COLUMN price_at_detail REAL;

-- A digest of the parsed detail, not of the HTML: a page carries CSRF tokens, view
-- counters and render timestamps that change on every request without any datum moving.
ALTER TABLE listings ADD COLUMN detail_hash TEXT;
-- How many refreshes in a row found nothing new, which is what spaces the next one out.
ALTER TABLE listings ADD COLUMN detail_unchanged_count INTEGER NOT NULL DEFAULT 0;

-- When the detail page is next worth reading. Empty string rather than NULL so it is
-- always comparable and sorts before every timestamp: a row that has never been enriched
-- is due now, which is what the default gives every existing row.
ALTER TABLE listings ADD COLUMN detail_due_at TEXT NOT NULL DEFAULT '';

-- The queue moved from "never fetched" to "due", so its index moves with it. Same
-- eligibility predicate as before plus the delisting, so SQLite can still use it.
DROP INDEX IF EXISTS idx_listings_pending_detail;

CREATE INDEX IF NOT EXISTS idx_listings_detail_due
    ON listings (detail_due_at)
    WHERE delisted_at IS NULL AND is_project = 0 AND COALESCE(interest, 0) >= 0;

-- One row per field that actually moved, rather than a snapshot per fetch: a listing
-- revisited twenty times with one changed gasto comun is twenty reads and one row here,
-- where snapshots would be twenty near-identical copies of forty columns.
CREATE TABLE IF NOT EXISTS detail_changes (
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_detail_changes_listing
    ON detail_changes (portal, external_id, changed_at);

-- Two history tables is a storage detail, not something a reader should have to know:
-- price_history predates this one and migrating it buys nothing.
DROP VIEW IF EXISTS listing_changes;
CREATE VIEW listing_changes AS
    SELECT portal, external_id, 'price' AS field,
           NULL AS old_value, CAST(price AS TEXT) AS new_value, seen_at AS changed_at
      FROM price_history
    UNION ALL
    SELECT portal, external_id, field, old_value, new_value, changed_at
      FROM detail_changes;

-- Validators only, never a response body: an ETag and a Last-Modified are ~60 bytes for a
-- page that costs hundreds of kilobytes. Kept in its own table so it can be emptied in
-- one statement without touching anything real, which is the whole safety story here.
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    status        INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL
);
