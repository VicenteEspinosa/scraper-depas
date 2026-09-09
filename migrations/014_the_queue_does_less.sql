-- The enrichment queue used to be "every listing without a detail page", which spent
-- requests on rows the pool can never accept: `KEPT` wants an actual unit nobody turned
-- down, so a project or a /dislike is a detail page fetched for nothing. The index moves
-- with the query — same predicate, so SQLite can still use it — and carries `first_seen`
-- because the queue is now ordered newest first rather than by insertion order.
DROP INDEX IF EXISTS idx_listings_pending_detail;

CREATE INDEX IF NOT EXISTS idx_listings_pending_detail
    ON listings (first_seen DESC)
    WHERE detail_fetched_at IS NULL AND is_project = 0 AND COALESCE(interest, 0) >= 0;

-- Which release of `infer_from_description` has already read this listing's prose.
-- Re-reading a description the current code has read finds exactly what it found last
-- time, so the hourly pass stopped doing it: only a bumped INFERRED_VERSION is worth a
-- second look. Existing rows start at 0 and are scanned once more after this lands.
ALTER TABLE listings ADD COLUMN inferred_version INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_listings_uninferred
    ON listings (inferred_version)
    WHERE description IS NOT NULL AND description != '';

-- `commune` filters both the ranked view and the alert query; `last_seen` is what the
-- delisting work reads next, and both were full scans.
CREATE INDEX IF NOT EXISTS idx_listings_commune ON listings (commune);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings (last_seen);

-- A series with UF and CLP stretches in it cannot be compared without knowing the UF of
-- each day, so the trail records what the price was worth when it was seen.
ALTER TABLE price_history ADD COLUMN price_clp REAL;

UPDATE price_history SET price_clp = price WHERE currency = 'CLP';

-- Backfilled only where the UF of that day was cached; the rest stay NULL rather than
-- carry a figure converted at the wrong rate.
UPDATE price_history
   SET price_clp = price * (SELECT value FROM uf_daily
                             WHERE day = substr(price_history.seen_at, 1, 10))
 WHERE currency = 'UF'
   AND price_clp IS NULL
   AND EXISTS (SELECT 1 FROM uf_daily WHERE day = substr(price_history.seen_at, 1, 10));
