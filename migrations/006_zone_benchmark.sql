-- Median published zone UF/m2 per commune. Only Portal Inmobiliario states one, so
-- listings from the other portals borrow their commune's figure to be graded on value.
CREATE TABLE IF NOT EXISTS zone_benchmark (
    commune   TEXT PRIMARY KEY,
    uf_per_m2 REAL NOT NULL
);
