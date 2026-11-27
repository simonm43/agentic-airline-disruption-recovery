-- Drops what 017_playbook.sql added for the storm's "Act now" plan, which the owner dropped
-- (D125); the owner approved the cleanup ("you can clean up the stuff we dont need").
-- No code in src, tests or tools reads or writes these since D125: crew.crew_notice,
-- booking.hotel_hold and slots.slot_swap (with their indexes), schedule.flight.booked_passengers
-- and crew.reserve.airport_start_local/airport_end_local. booking.passenger_notice stays (used).
-- Each grant 017 added for them is revoked first, including UPDATE on crew.reserve: Crew only
-- deletes a called standby's row (DELETE, 010), it never updates one.
-- Destructive for the dropped tables' rows (generated, per operating day). Idempotent. Apply with
-- tools\systems\apply_sql.py only once the deployed systems image and both day-load jobs are
-- built from code at or after D125, as they no longer name these tables or columns.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-crew-dev')
BEGIN
    IF OBJECT_ID(N'crew.crew_notice') IS NOT NULL
        REVOKE SELECT, INSERT ON OBJECT::crew.crew_notice FROM [id-airrec-crew-dev];
    REVOKE UPDATE ON OBJECT::crew.reserve FROM [id-airrec-crew-dev];
END;

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-booking-dev')
    AND OBJECT_ID(N'booking.hotel_hold') IS NOT NULL
    REVOKE SELECT, INSERT ON OBJECT::booking.hotel_hold FROM [id-airrec-booking-dev];

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-slots-dev')
    AND OBJECT_ID(N'slots.slot_swap') IS NOT NULL
    REVOKE SELECT, INSERT ON OBJECT::slots.slot_swap FROM [id-airrec-slots-dev];

IF OBJECT_ID(N'crew.crew_notice') IS NOT NULL
    DROP TABLE crew.crew_notice;

IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'ix_hotel_hold_day' AND object_id = OBJECT_ID(N'booking.hotel_hold')
)
    DROP INDEX ix_hotel_hold_day ON booking.hotel_hold;

IF OBJECT_ID(N'booking.hotel_hold') IS NOT NULL
    DROP TABLE booking.hotel_hold;

IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'ix_slot_swap_day' AND object_id = OBJECT_ID(N'slots.slot_swap')
)
    DROP INDEX ix_slot_swap_day ON slots.slot_swap;

IF OBJECT_ID(N'slots.slot_swap') IS NOT NULL
    DROP TABLE slots.slot_swap;

IF COL_LENGTH(N'schedule.flight', N'booked_passengers') IS NOT NULL
    ALTER TABLE schedule.flight DROP COLUMN booked_passengers;

IF COL_LENGTH(N'crew.reserve', N'airport_start_local') IS NOT NULL
    ALTER TABLE crew.reserve DROP COLUMN airport_start_local;

IF COL_LENGTH(N'crew.reserve', N'airport_end_local') IS NOT NULL
    ALTER TABLE crew.reserve DROP COLUMN airport_end_local;
