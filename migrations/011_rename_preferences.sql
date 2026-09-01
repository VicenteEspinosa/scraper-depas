-- Settings are named DEPAS_<PARAMETER>_<SLOT> now, so every knob for one parameter
-- sorts together and the suffix says what it does: MIN/MAX exclude a listing, TARGET
-- only costs it score, WEIGHT moves the grade, WANTED/TIERS are matched against.
--
-- The old names said ALERT_ or TARGET_ up front, which put a parameter's knobs in two
-- places and misdescribed three of them: ALERT_SECURITY never excluded anything,
-- ALERT_MIN_AREA both excluded and graded, and ALERT_MIN_GRADE bounded the composite
-- rather than any component.
--
-- Renaming the rows here rather than re-seeding keeps whatever was edited from the
-- chat: the value travels with the name. .env is not consulted -- it was only ever the
-- starting point, and the seed is long done.
UPDATE preferences SET name = 'DEPAS_COMMUNES'        WHERE name = 'DEPAS_ALERT_COMMUNES';
UPDATE preferences SET name = 'DEPAS_BEDROOMS_MIN'    WHERE name = 'DEPAS_ALERT_MIN_BEDROOMS';
UPDATE preferences SET name = 'DEPAS_GRADE_MIN'       WHERE name = 'DEPAS_ALERT_MIN_GRADE';

UPDATE preferences SET name = 'DEPAS_COST_MAX'        WHERE name = 'DEPAS_ALERT_MAX_COST';
UPDATE preferences SET name = 'DEPAS_COST_TARGET'     WHERE name = 'DEPAS_TARGET_COST';
UPDATE preferences SET name = 'DEPAS_WALK_MAX'        WHERE name = 'DEPAS_ALERT_MAX_WALK';
UPDATE preferences SET name = 'DEPAS_WALK_TARGET'     WHERE name = 'DEPAS_TARGET_WALK';
UPDATE preferences SET name = 'DEPAS_AREA_MIN'        WHERE name = 'DEPAS_ALERT_MIN_AREA';
UPDATE preferences SET name = 'DEPAS_AREA_TARGET'     WHERE name = 'DEPAS_TARGET_AREA';
UPDATE preferences SET name = 'DEPAS_COMMUTE_MAX'     WHERE name = 'DEPAS_ALERT_MAX_COMMUTE';
UPDATE preferences SET name = 'DEPAS_COMMUTE_TARGET'  WHERE name = 'DEPAS_TARGET_COMMUTE';
UPDATE preferences SET name = 'DEPAS_FLOOR_TARGET'    WHERE name = 'DEPAS_TARGET_FLOOR';
UPDATE preferences SET name = 'DEPAS_AGE_TARGET'      WHERE name = 'DEPAS_TARGET_AGE';
UPDATE preferences SET name = 'DEPAS_SECURITY_WANTED' WHERE name = 'DEPAS_ALERT_SECURITY';
UPDATE preferences SET name = 'DEPAS_METRO_TIERS'     WHERE name = 'DEPAS_LINE_PREFERENCE';

UPDATE preferences SET name = 'DEPAS_VALUE_WEIGHT'     WHERE name = 'DEPAS_WEIGHT_VALUE';
UPDATE preferences SET name = 'DEPAS_COST_WEIGHT'      WHERE name = 'DEPAS_WEIGHT_COST';
UPDATE preferences SET name = 'DEPAS_WALK_WEIGHT'      WHERE name = 'DEPAS_WEIGHT_LOCATION';
UPDATE preferences SET name = 'DEPAS_AREA_WEIGHT'      WHERE name = 'DEPAS_WEIGHT_SIZE';
UPDATE preferences SET name = 'DEPAS_AMENITIES_WEIGHT' WHERE name = 'DEPAS_WEIGHT_AMENITIES';
UPDATE preferences SET name = 'DEPAS_SECURITY_WEIGHT'  WHERE name = 'DEPAS_WEIGHT_SECURITY';
UPDATE preferences SET name = 'DEPAS_FLOOR_WEIGHT'     WHERE name = 'DEPAS_WEIGHT_FLOOR';
UPDATE preferences SET name = 'DEPAS_METRO_WEIGHT'     WHERE name = 'DEPAS_WEIGHT_METRO';
UPDATE preferences SET name = 'DEPAS_COMMUTE_WEIGHT'   WHERE name = 'DEPAS_WEIGHT_COMMUTE';
UPDATE preferences SET name = 'DEPAS_AGE_WEIGHT'       WHERE name = 'DEPAS_WEIGHT_AGE';
