-- available_from used to hold the portal's own free text ("Inmediata", "15 julio");
-- it now holds the parsed ISO date, so anything else is stale and unfilterable.
UPDATE listings SET available_from = NULL
WHERE available_from NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]';
