-- `interest` and `notified_at` were columns of `listings`, which made them everybody's:
-- one person's /dislike took the flat out of every reader's pool, and "already announced"
-- was a property of the listing rather than of the chat it was announced in. With one
-- reader those are the same thing. With two they are not, and the second reader cannot be
-- added until they come apart.
--
-- They come apart along two different keys, which is the whole point:
--   * a verdict belongs to a PERSON — it is an opinion, and two people may disagree
--   * an announcement belongs to a DESTINATION — a card posted in a channel has been
--     posted, and posting it again per subscriber would repeat it in the same chat

-- One row per place cards are posted: a private conversation, or a channel (whose linked
-- discussion group carries the comments, which is how this repo's own instance runs).
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id       TEXT PRIMARY KEY,
    -- NULL means shared: anybody's verdict counts for it, which is what a couple reading
    -- one channel together already gets today. Set for a private chat, where only its
    -- owner's opinion decides what that chat's pool holds.
    owner_user_id INTEGER,
    added_at      TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_interest (
    user_id     INTEGER NOT NULL,
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    interest    INTEGER NOT NULL,
    -- The display name, for the card that says who said it; the id is what identifies.
    rated_by    TEXT,
    rated_at    TEXT NOT NULL,

    PRIMARY KEY (user_id, portal, external_id)
);

CREATE INDEX IF NOT EXISTS idx_user_interest_listing
    ON user_interest (portal, external_id);

CREATE TABLE IF NOT EXISTS subscriber_notifications (
    chat_id     TEXT NOT NULL,
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    notified_at TEXT NOT NULL,

    PRIMARY KEY (chat_id, portal, external_id)
);

CREATE INDEX IF NOT EXISTS idx_subscriber_notifications_when
    ON subscriber_notifications (notified_at);

-- The pinned ⭐ list was two integers in `settings`, so there could only ever be one.
CREATE TABLE IF NOT EXISTS subscriber_shortlist (
    chat_id    TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL
);

-- ── carrying the single reader across ──────────────────────────────────────────
-- Whatever TELEGRAM_CHAT_ID says becomes the first subscriber, shared, so the box this
-- lands on keeps posting exactly where it posted before and to nowhere else.
INSERT OR IGNORE INTO subscribers (chat_id, owner_user_id, added_at)
SELECT value, NULL, datetime('now') FROM preferences WHERE name = 'TELEGRAM_CHAT_ID';

-- Verdicts already given have a display name in `rated_by` and no id at all, so they land
-- under user 0: nobody's account, and the one user id Telegram never issues. A shared
-- subscriber counts anybody's verdict, so they keep working exactly as they did; a private
-- subscriber added later starts with a clean opinion, which is the honest answer since we
-- genuinely do not know whose these were.
INSERT OR IGNORE INTO user_interest (user_id, portal, external_id, interest, rated_by, rated_at)
SELECT 0, portal, external_id, interest, rated_by, COALESCE(rated_at, datetime('now'))
  FROM listings WHERE interest IS NOT NULL;

INSERT OR IGNORE INTO subscriber_notifications (chat_id, portal, external_id, notified_at)
SELECT (SELECT value FROM preferences WHERE name = 'TELEGRAM_CHAT_ID'),
       portal, external_id, notified_at
  FROM listings
 WHERE notified_at IS NOT NULL
   AND EXISTS (SELECT 1 FROM preferences WHERE name = 'TELEGRAM_CHAT_ID');

INSERT OR IGNORE INTO subscriber_shortlist (chat_id, message_id)
SELECT (SELECT CAST(value AS TEXT) FROM settings WHERE key = 'shortlist_chat_id'),
       (SELECT value FROM settings WHERE key = 'shortlist_message_id')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'shortlist_message_id');

-- ── and out of `listings` ───────────────────────────────────────────────────────
-- Left in place they would be state nobody writes and something eventually reads, and
-- `listings_ranked` is SELECT *, so the per-reader columns could not even be named
-- without colliding with them. The indexes go first: SQLite refuses to drop a column an
-- index mentions.
DROP INDEX IF EXISTS idx_listings_rejected;
DROP INDEX IF EXISTS idx_listings_unnotified;
DROP INDEX IF EXISTS idx_listings_detail_due;

ALTER TABLE listings DROP COLUMN interest;
ALTER TABLE listings DROP COLUMN rated_at;
ALTER TABLE listings DROP COLUMN rated_by;
ALTER TABLE listings DROP COLUMN notified_at;

-- Rebuilt without the interest clause. Whether anybody has turned a listing down is now
-- a subquery, and a partial index may not contain one — so it moves to the query, where
-- it also has to mean "turned down by whoever this subscriber listens to" anyway.
CREATE INDEX IF NOT EXISTS idx_listings_detail_due
    ON listings (detail_due_at)
    WHERE delisted_at IS NULL AND is_project = 0;
