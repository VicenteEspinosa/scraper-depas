-- The breakdown posted under each card, so a redraw can re-render it in place and a
-- verdict can cut it down the way the card itself is cut down. It lives wherever the
-- verdict keyboard lives -- in the card's Comments thread where there is one, and as a
-- reply to the card where there is not -- so it carries its own chat rather than
-- borrowing the card's.
ALTER TABLE card_messages ADD COLUMN detail_chat_id TEXT;
ALTER TABLE card_messages ADD COLUMN detail_message_id INTEGER;
