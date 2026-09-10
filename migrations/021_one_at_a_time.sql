-- Nothing stopped two of the same stage running at once. In practice they did not
-- overlap much — a batch took two to four minutes inside a ten-minute window — but the
-- window is what changed: the detail read is parallel now and its budget went from 60 to
-- 250, and a stage that may run again while there is work left can outlast its slot on
-- purpose. Two writers meeting is not corruption, since WAL and `busy_timeout` handle
-- that, but it is one of them dying with «database is locked» and a pass silently lost.
--
-- One row per stage while it runs, and no row when it does not. `taken_at` is what makes
-- it safe rather than a new way to break: a lock nobody released — OOM, SIGKILL, the
-- container restarted mid-run — would wedge the stage forever, which is far worse than
-- the overlap it prevents. So a lock older than the staleness limit is not a lock.
CREATE TABLE IF NOT EXISTS stage_locks (
    stage    TEXT PRIMARY KEY,
    taken_at TEXT NOT NULL
);
