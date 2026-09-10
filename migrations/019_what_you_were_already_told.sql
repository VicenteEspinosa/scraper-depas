-- A change is news exactly once. `subscriber_notifications` says a chat has had a card,
-- which was enough while a card was a one-off; a card that gets corrected every time the
-- rent moves needs the second half of the sentence — what about it has already been told.
--
-- One row per (chat, listing) rather than one per change reported: what a reader needs
-- is a watermark, and `through` is the newest change already in a message. Everything
-- after it is what the next pass has to say, which makes a pass that dies half way
-- repeat a notice at worst and never swallow one.
CREATE TABLE IF NOT EXISTS update_notifications (
    chat_id     TEXT NOT NULL,
    portal      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    -- The timestamp of the newest change this chat has been told about.
    through     TEXT NOT NULL,
    reported_at TEXT NOT NULL,

    PRIMARY KEY (chat_id, portal, external_id)
);

-- The grade a card went out with, so a change notice can say the nota moved rather than
-- only restating today's. It only ever holds what this version wrote: cards posted before
-- it keep a NULL, and a notice about one of those says what the grade is and not what it
-- was, which is the honest reading of "we did not record it".
ALTER TABLE subscriber_notifications ADD COLUMN grade_letter TEXT;
ALTER TABLE subscriber_notifications ADD COLUMN grade_score INTEGER;
