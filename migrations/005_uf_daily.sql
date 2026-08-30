-- Today's UF, cached so an hourly pass hits the indicator API once a day rather
-- than every run, and so the ranked view can price listings per m2 in SQL.
CREATE TABLE IF NOT EXISTS uf_daily (
    day   TEXT PRIMARY KEY,
    value REAL NOT NULL
);
