-- Stamped when a listing has been posted to Telegram, so each one is announced
-- exactly once no matter how often the watch runs.
ALTER TABLE listings ADD COLUMN notified_at TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_unnotified
    ON listings (portal) WHERE notified_at IS NULL;
