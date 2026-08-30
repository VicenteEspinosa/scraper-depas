-- A verdict given from the chat: 1 for /like, -1 for /dislike, NULL until somebody
-- says. Not part of the scrape's FIELDS, so a re-scrape never blanks it.
ALTER TABLE listings ADD COLUMN interest INTEGER;
ALTER TABLE listings ADD COLUMN rated_at TEXT;
ALTER TABLE listings ADD COLUMN rated_by TEXT;

CREATE INDEX IF NOT EXISTS idx_listings_rejected
    ON listings (portal) WHERE interest < 0;

-- Every card the bot has posted, so a command commented under one can be traced
-- back to the listing it is about, and the card itself edited in place to show
-- the verdict. A card posted to a channel is auto-forwarded into the linked
-- discussion group, and that copy's message id is the message_thread_id every
-- comment on the card carries: thread_id holds it, filled in when the bot sees
-- the forward, which is the only update where Telegram publishes that pairing.
CREATE TABLE IF NOT EXISTS card_messages (
    chat_id        TEXT NOT NULL,
    message_id     INTEGER NOT NULL,
    portal         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    -- A photo card carries its text as a caption, and the two are edited by
    -- different methods, so which one it was has to be remembered.
    is_photo       INTEGER NOT NULL DEFAULT 0,
    thread_chat_id TEXT,
    thread_id      INTEGER,
    posted_at      TEXT NOT NULL,

    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_card_messages_thread
    ON card_messages (thread_chat_id, thread_id);
