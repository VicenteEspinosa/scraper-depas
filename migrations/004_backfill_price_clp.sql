-- Links pasted into the bot were stored without price_clp, and the ranked view
-- turns that NULL into a NULL net cost. The UF rate is the one published the day
-- this shipped; anything the crawler re-scrapes overwrites it with the live one.
UPDATE listings
   SET price_clp = CASE WHEN currency = 'UF' THEN price * 40872.45 ELSE price END
 WHERE price_clp IS NULL;
