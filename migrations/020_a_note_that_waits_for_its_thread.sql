-- Why a card is arriving now belongs under the card, and in a channel with a discussion
-- group the card has no thread at the moment it is posted: Telegram auto-forwards the
-- post into the linked group and the bot only learns the thread when that copy reaches
-- it, in a different process from the pass that posted the card.
--
-- So the note waits here for its thread, the way the verdict keyboard already does. The
-- alternative was posting it into the channel itself, which is the one place the card's
-- own explanation must not go: the channel is the feed, and a note about crawler queues
-- read as a second announcement.
ALTER TABLE card_messages ADD COLUMN arrival_note TEXT;
