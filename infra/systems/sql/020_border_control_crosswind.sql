-- Final owner review: two values the sample operations control system records.
-- A flight delay's cause border_control: border-control queues (Entry/Exit System checks), IATA
-- delay code 86 (immigration, customs, health). A weather forecast's hazard crosswind: a wind
-- across Lisbon's single runway 02/20, which reduces its rate and never closes it. strong_wind,
-- which crosswind replaces, stays allowed so rows recorded before still check.
-- Idempotent: each constraint is dropped and added again with its full list. Apply with
-- tools\systems\apply_sql.py before the systems image built from this change.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'schedule.ck_delay_cause') IS NOT NULL
    ALTER TABLE schedule.delay_report DROP CONSTRAINT ck_delay_cause;
ALTER TABLE schedule.delay_report ADD CONSTRAINT ck_delay_cause
    CHECK (cause IN ('staff_shortage', 'baggage', 'late_inbound', 'border_control', 'other'));

IF OBJECT_ID(N'schedule.ck_weather_hazard') IS NOT NULL
    ALTER TABLE schedule.weather_report DROP CONSTRAINT ck_weather_hazard;
ALTER TABLE schedule.weather_report ADD CONSTRAINT ck_weather_hazard
    CHECK (hazard IN ('thunderstorm', 'crosswind', 'strong_wind', 'low_visibility'));
