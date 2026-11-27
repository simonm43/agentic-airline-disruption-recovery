-- The OCC playbook (OCC playbook plan, Task 2): what the sample systems record of
-- the playbook's writes, beside each write's row in audit.recovery_action (request and receipt).
-- Crew: delayed-report notices (crew consent needed) and, on crew.reserve, the airport window of
-- a home standby moved to the airport (it stays on home standby outside it). Booking: one
-- passenger notice per flight and day, and hotel holds against the day's allotment. Slots (the
-- sample Network Manager): accepted slot swaps. Schedule: each flight's start-of-day booked
-- count, written by the day load, which ranks THIN. No free text is stored.
-- Each record belongs to one operating day: the day load deletes them with the day, so a Reset
-- frees the hotel allotment, the notices and the slot swaps.
-- Idempotent. Apply with tools\systems\apply_sql.py before the systems image and both day-load
-- jobs that read and write these tables and columns: until then that image's reads and day load
-- fail. Then Reset the day so the booked counts load.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'schedule.flight', N'booked_passengers') IS NULL
    ALTER TABLE schedule.flight ADD booked_passengers smallint NULL;

IF COL_LENGTH(N'crew.reserve', N'airport_start_local') IS NULL
    ALTER TABLE crew.reserve ADD airport_start_local datetime2(0) NULL,
        airport_end_local datetime2(0) NULL;

IF OBJECT_ID(N'crew.crew_notice') IS NULL
CREATE TABLE crew.crew_notice (
    duty_date           date          NOT NULL,
    notice_id           varchar(80)   NOT NULL CONSTRAINT pk_crew_notice PRIMARY KEY,
    trace_id            char(32)      NOT NULL,
    duty_id             varchar(16)   NOT NULL,
    report_before_local datetime2(0)  NOT NULL,
    report_local        datetime2(0)  NOT NULL,
    status              varchar(24)   NOT NULL CONSTRAINT ck_crew_notice_status
        CHECK (status IN ('crew consent needed')),
    created_at          datetime2(3)  NOT NULL CONSTRAINT df_crew_notice_created
        DEFAULT SYSUTCDATETIME()
);

IF OBJECT_ID(N'booking.passenger_notice') IS NULL
CREATE TABLE booking.passenger_notice (
    travel_date   date          NOT NULL,
    flight_no     varchar(8)    NOT NULL,
    notice_id     varchar(80)   NOT NULL,
    trace_id      char(32)      NOT NULL,
    passengers    smallint      NOT NULL CONSTRAINT ck_passenger_notice_passengers
        CHECK (passengers >= 0),
    moved_early   smallint      NOT NULL CONSTRAINT ck_passenger_notice_moved
        CHECK (moved_early >= 0),
    created_at    datetime2(3)  NOT NULL CONSTRAINT df_passenger_notice_created
        DEFAULT SYSUTCDATETIME(),
    -- A flight is notified, and its passengers offered an earlier flight, once a day.
    CONSTRAINT pk_passenger_notice PRIMARY KEY (travel_date, flight_no)
);

IF OBJECT_ID(N'booking.hotel_hold') IS NULL
CREATE TABLE booking.hotel_hold (
    travel_date      date          NOT NULL,
    hold_id          varchar(80)   NOT NULL CONSTRAINT pk_hotel_hold PRIMARY KEY,
    trace_id         char(32)      NOT NULL,
    rooms_requested  smallint      NOT NULL CONSTRAINT ck_hotel_hold_requested
        CHECK (rooms_requested > 0),
    rooms_held       smallint      NOT NULL,
    rate_eur         smallint      NOT NULL CONSTRAINT ck_hotel_hold_rate CHECK (rate_eur > 0),
    created_at       datetime2(3)  NOT NULL CONSTRAINT df_hotel_hold_created
        DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_hotel_hold_rooms CHECK (rooms_held > 0 AND rooms_held <= rooms_requested)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_hotel_hold_day')
CREATE INDEX ix_hotel_hold_day ON booking.hotel_hold (travel_date) INCLUDE (rooms_held);

IF OBJECT_ID(N'slots.slot_swap') IS NULL
CREATE TABLE slots.slot_swap (
    slot_date   date          NOT NULL,
    swap_id     varchar(84)   NOT NULL CONSTRAINT pk_slot_swap PRIMARY KEY,
    trace_id    char(32)      NOT NULL,
    flight_a    varchar(8)    NOT NULL,
    flight_b    varchar(8)    NOT NULL,
    created_at  datetime2(3)  NOT NULL CONSTRAINT df_slot_swap_created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_slot_swap_pair CHECK (flight_a <> flight_b)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_slot_swap_day')
CREATE INDEX ix_slot_swap_day ON slots.slot_swap (slot_date) INCLUDE (flight_a, flight_b);

-- Each system identity gets only the writes its own system performs. Schedule (the day-load
-- identity) already holds SELECT, INSERT, UPDATE and DELETE on these schemas (002); every system
-- already reads schedule.flight (006), now with its booked count. No grant is widened beyond the
-- new records.
-- Crew: notices, the duty's new report (UPDATE crew.duty, 008) and a standby's airport window.
GRANT SELECT, INSERT ON OBJECT::crew.crew_notice TO [id-airrec-crew-dev];
GRANT UPDATE ON OBJECT::crew.reserve TO [id-airrec-crew-dev];
-- Booking: notices and holds; early moves change segments (UPDATE booking.segment, 008).
GRANT SELECT, INSERT ON OBJECT::booking.passenger_notice TO [id-airrec-booking-dev];
GRANT SELECT, INSERT ON OBJECT::booking.hotel_hold TO [id-airrec-booking-dev];
-- Slots: the sample Network Manager's accepted swaps.
GRANT SELECT, INSERT ON OBJECT::slots.slot_swap TO [id-airrec-slots-dev];
