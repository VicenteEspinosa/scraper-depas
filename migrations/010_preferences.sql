-- Every tunable the bot has, one row per setting, holding exactly the text that
-- used to sit on the right-hand side of an `=` in .env. Storing the raw string
-- rather than a typed column keeps one parser for both sources, and means a value
-- shown in the chat is the same value that could be pasted back into a file.
--
-- The table is seeded from the environment once, on the first connect that finds
-- it empty, so an existing deployment keeps its configuration without being
-- touched. After that the database wins and .env is only a starting point: see
-- `depas config import-env` for pulling the environment back in on purpose.
CREATE TABLE IF NOT EXISTS preferences (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
