-- A change reaches the reader twice, and the two halves no longer share a clock: the
-- card is edited in place by the pass that saw the change, so whoever opens it reads the
-- price it is actually asking, while the message that names every flat that moved goes
-- out once a day. One watermark cannot say both — moved on the edit it would swallow the
-- resumen, moved on the resumen it would re-edit the same card and re-comment its thread
-- every five minutes for a day — so the resumen gets its own.
--
-- `through` keeps its meaning of the two: the newest change already drawn on the card.
ALTER TABLE update_notifications ADD COLUMN digested_through TEXT;

-- Copied rather than left NULL, because a NULL here means "never named in a resumen" and
-- is owed everything since the card went out. Until this migration both halves moved
-- together, so what is on the card is exactly what was in the last resumen; a fresh
-- column would post months of accumulated history in the first resumen after the deploy.
UPDATE update_notifications SET digested_through = through;
