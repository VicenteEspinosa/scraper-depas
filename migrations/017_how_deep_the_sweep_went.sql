-- Cutting a sweep short once a page brings nothing new is only correct if the portal
-- returns the newest listings first. Portal Inmobiliario and Chilepropiedades appear to,
-- but neither says so, and the failure mode is silent: a listing sitting behind known
-- ones is never seen, with no error and nothing in the log to show it happened.
--
-- So the sweep records how deep it went and, of the listings it had never seen before,
-- the deepest page one of them turned up on. If the ordering holds, a new listing only
-- ever appears on the first page or two and `deepest_new_page` stays below the cutoff.
-- The day it does not, `cutoff_safety` says so instead of us finding out by missing a
-- flat. That is what turns the assumption into a measurement.
ALTER TABLE scrape_runs ADD COLUMN pages_read INTEGER;

-- A sweep that deliberately read every page rather than stopping early. One of these per
-- portal per DEPAS_DEEP_SWEEP_HOURS is the safety net: even with the ordering wrong, a
-- listing is found within a day rather than never.
ALTER TABLE scrape_runs ADD COLUMN deep INTEGER NOT NULL DEFAULT 0;

ALTER TABLE scrape_runs ADD COLUMN deepest_new_page INTEGER;

CREATE INDEX IF NOT EXISTS idx_scrape_runs_depth
    ON scrape_runs (portal, deep, started_at);
