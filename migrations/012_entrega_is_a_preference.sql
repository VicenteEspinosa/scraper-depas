-- The move-in date used to be a bound: a listing that only freed up after
-- DEPAS_AVAILABLE_BY was never announced. It grades now, on how far the entrega lands
-- from the date you want in either direction, so the slot in its name changed with it
-- -- TARGET is the one that only costs score.
--
-- The value travels with the name rather than being re-seeded, so whatever was set
-- from the chat is what the new component scores against.
UPDATE preferences SET name = 'DEPAS_AVAILABILITY_TARGET' WHERE name = 'DEPAS_AVAILABLE_BY';
