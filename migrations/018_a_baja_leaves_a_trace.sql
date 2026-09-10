-- `save` clears `delisted_at` unconditionally, which is what makes a portal outage or a
-- comuna dropped and restored heal itself — and it is also why a listing could leave the
-- pool and come back with nothing anywhere saying it had happened. Asked why a card for
-- an apartment first seen weeks ago turned up today, the database had no answer: the
-- column holds a state, and this is the history behind it.
--
-- Both directions are recorded because both are worth telling a reader about. "El aviso
-- que te mandé ya no está" is the more useful of the two, and "volvió a estar publicado"
-- is what a false positive looks like from the outside — a sweep that could not see the
-- listing rather than a flat that came off the market.
CREATE TABLE IF NOT EXISTS delisting_events (
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    -- Only ever one of two things, and a typo in the writer must not read as the other.
    event       TEXT NOT NULL CHECK (event IN ('delisted', 'relisted')),
    -- Why the baja was concluded: the portal answering 404 on the detail page is direct
    -- evidence, while the sweeps failing to turn it up is an absence being counted.
    reason      TEXT,
    happened_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delisting_events_listing
    ON delisting_events (portal, external_id, happened_at);
