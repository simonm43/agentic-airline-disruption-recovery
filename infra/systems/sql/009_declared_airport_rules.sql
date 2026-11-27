-- Declare airport rules for every station the generated network flies (review fix).
-- The validator treats a moved flight's station without a rule row as unknown and blocks the
-- option (unknown is not clearance). Stations with no modelled night rule therefore get an
-- explicit, labelled row instead of being silently treated as unrestricted. Idempotent.
INSERT INTO slots.airport_rule (iata, coordination_level, a_cdm, source)
SELECT flown.iata, NULL, 0, N'no rule modelled (declared for every flown station)'
FROM (
    SELECT origin AS iata FROM schedule.flight
    UNION
    SELECT destination FROM schedule.flight
) AS flown
WHERE NOT EXISTS (SELECT 1 FROM slots.airport_rule r WHERE r.iata = flown.iata);
