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

-- ── and it starts today ────────────────────────────────────────────────────────
-- Without this the first pass after the deploy would report every change ever recorded
-- about every card ever posted: months of price movements drip-fed ten an hour, and a
-- «ya no está» for each of the flats that came off the market back in July. True, all of
-- it, and none of it news.
--
-- So a card already posted starts on what happens next, which is the same decision
-- `add_subscriber` makes for a chat and for the same reason. Only the pairs that have a
-- card, because those are the only ones this can ever report on: a listing stamped
-- without being posted — below the bar — has no message to correct and no reader who
-- ever saw it.
--
-- Written in the format the code compares against (an ISO timestamp with its zone) and
-- not SQLite's `datetime('now')`, whose space instead of a T sorts below every timestamp
-- of the same day and would let the day's own changes through.
INSERT OR IGNORE INTO update_notifications (chat_id, portal, external_id, through, reported_at)
SELECT told.chat_id, told.portal, told.external_id,
       REPLACE(datetime('now'), ' ', 'T') || '+00:00',
       REPLACE(datetime('now'), ' ', 'T') || '+00:00'
  FROM subscriber_notifications AS told
 WHERE EXISTS (SELECT 1 FROM card_messages AS card
                WHERE card.chat_id = told.chat_id AND card.portal = told.portal
                  AND card.external_id = told.external_id);

-- The grade a card went out with, so a change notice can say the nota moved rather than
-- only restating today's. It only ever holds what this version wrote: cards posted before
-- it keep a NULL, and a notice about one of those says what the grade is and not what it
-- was, which is the honest reading of "we did not record it".
ALTER TABLE subscriber_notifications ADD COLUMN grade_letter TEXT;
ALTER TABLE subscriber_notifications ADD COLUMN grade_score INTEGER;
