-- The storm held until confirmed (case summary and held storm plan, Task 1; owner-approved).
-- Booking's passenger notice gains a kind: possible_delay (a heads-up when OCC approves a storm
-- plan, no new times), new_times (after the retime, as before) and no_change (the storm stood
-- down). One notice of each kind per flight and day, so the primary key moves to
-- (travel_date, flight_no, kind); rows already recorded are new_times.
-- Crew records standby alerts: a case's standby crew put on alert with its call-by time (report
-- minus 60 minutes) and its members, then called or released. An alert is not a call; neither
-- changes a duty or a standby row. Each alert belongs to one operating day: the day load deletes
-- them with the day, so a Reset clears them. No free text is stored.
-- Idempotent. Apply with tools\systems\apply_sql.py before the systems image and both day-load
-- jobs built from this change: their notice insert names the kind and their day load deletes
-- crew.standby_alert. Images built before it keep working against it (kind defaults to
-- new_times).
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'booking.passenger_notice', N'kind') IS NULL
    ALTER TABLE booking.passenger_notice ADD kind varchar(16) NOT NULL
        CONSTRAINT df_passenger_notice_kind DEFAULT ('new_times');

-- Dynamic SQL: the batch is compiled before the column above exists.
IF OBJECT_ID(N'booking.ck_passenger_notice_kind') IS NULL
    EXEC(N'ALTER TABLE booking.passenger_notice ADD CONSTRAINT ck_passenger_notice_kind
          CHECK (kind IN (''possible_delay'', ''new_times'', ''no_change''));');

IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes i
    JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    WHERE i.object_id = OBJECT_ID(N'booking.passenger_notice') AND i.is_primary_key = 1
        AND c.name = N'kind'
)
BEGIN
    IF OBJECT_ID(N'booking.pk_passenger_notice') IS NOT NULL
        ALTER TABLE booking.passenger_notice DROP CONSTRAINT pk_passenger_notice;
    EXEC(N'ALTER TABLE booking.passenger_notice ADD CONSTRAINT pk_passenger_notice
          PRIMARY KEY (travel_date, flight_no, kind);');
END;

IF OBJECT_ID(N'crew.standby_alert') IS NULL
CREATE TABLE crew.standby_alert (
    duty_date          date          NOT NULL,
    -- The alerting action's id and the crew's option (R1 to R99).
    alert_id           varchar(84)   NOT NULL CONSTRAINT pk_standby_alert PRIMARY KEY,
    trace_id           char(32)      NOT NULL,
    reserve_option_id  varchar(3)    NOT NULL,
    -- The crew's members (crew.crew_member ids, comma-separated): a called crew is not counted
    -- among the standby crews left.
    crew_ids           varchar(200)  NOT NULL CONSTRAINT ck_standby_alert_crew
        CHECK (crew_ids <> ''),
    call_by_local      datetime2(0)  NOT NULL,
    status             varchar(8)    NOT NULL CONSTRAINT ck_standby_alert_status
        CHECK (status IN ('alerted', 'called', 'released')),
    created_at         datetime2(3)  NOT NULL CONSTRAINT df_standby_alert_created
        DEFAULT SYSUTCDATETIME()
);

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'ix_standby_alert_day' AND object_id = OBJECT_ID(N'crew.standby_alert')
)
CREATE INDEX ix_standby_alert_day ON crew.standby_alert (duty_date, trace_id);

-- Crew only: it records, calls and releases alerts. Booking already holds SELECT and INSERT on
-- booking.passenger_notice (017); the day-load identity deletes both with the day (002's schema
-- grants). No other grant changes.
GRANT SELECT, INSERT, UPDATE ON OBJECT::crew.standby_alert TO [id-airrec-crew-dev];
