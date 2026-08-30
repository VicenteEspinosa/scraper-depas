-- Minutes from the listing to each DEPAS_LOCATIONS entry, as {"name": 32}. Which places
-- matter is configuration, so they cannot each be a column.
ALTER TABLE listings ADD COLUMN commute TEXT;
