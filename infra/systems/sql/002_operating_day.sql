-- Operating day for the Maintenance, Crew, Booking and Slots systems, plus the Schedule load marker,
-- in database airrec-ops. Idempotent: safe to run again. schedule.flight is unchanged.
-- The systems identity reads and writes only these schemas; DELETE serves the daily reload.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF SCHEMA_ID(N'maintenance') IS NULL EXEC(N'CREATE SCHEMA maintenance');
IF SCHEMA_ID(N'crew') IS NULL EXEC(N'CREATE SCHEMA crew');
IF SCHEMA_ID(N'booking') IS NULL EXEC(N'CREATE SCHEMA booking');
IF SCHEMA_ID(N'slots') IS NULL EXEC(N'CREATE SCHEMA slots');

-- One row per fully loaded operating day; written in the same transaction as the day's rows.
IF OBJECT_ID(N'schedule.operating_day') IS NULL
CREATE TABLE schedule.operating_day (
    flight_date   date         NOT NULL CONSTRAINT pk_operating_day PRIMARY KEY,
    flights       int          NOT NULL,
    loaded_at     datetime2(0) NOT NULL DEFAULT SYSUTCDATETIME()
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_flight_aircraft')
CREATE INDEX ix_flight_aircraft ON schedule.flight (flight_date, aircraft_reg, std_local);

-- Maintenance: fleet and today's airworthiness status.
IF OBJECT_ID(N'maintenance.aircraft') IS NULL
CREATE TABLE maintenance.aircraft (
    registration  varchar(8)   NOT NULL CONSTRAINT pk_aircraft PRIMARY KEY,
    fleet_type    varchar(16)  NOT NULL,
    seats         smallint     NOT NULL CONSTRAINT ck_aircraft_seats CHECK (seats > 0),
    base          char(3)      NOT NULL
);

IF OBJECT_ID(N'maintenance.aircraft_status') IS NULL
CREATE TABLE maintenance.aircraft_status (
    status_date   date         NOT NULL,
    registration  varchar(8)   NOT NULL
        CONSTRAINT fk_status_aircraft REFERENCES maintenance.aircraft (registration),
    status        varchar(16)  NOT NULL
        CONSTRAINT ck_status CHECK (status IN ('serviceable', 'unserviceable')),
    return_to_service_local datetime2(0) NULL,  -- Lisbon local time; NULL while serviceable
    CONSTRAINT pk_aircraft_status PRIMARY KEY (status_date, registration)
);

-- Crew: today's working and reserve crew, duties (FDP per EASA ORO.FTL.205) and assignments.
IF OBJECT_ID(N'crew.crew_member') IS NULL
CREATE TABLE crew.crew_member (
    crew_id       varchar(8)   NOT NULL CONSTRAINT pk_crew_member PRIMARY KEY,
    rank          varchar(3)   NOT NULL CONSTRAINT ck_crew_rank CHECK (rank IN ('CPT', 'FO', 'SC', 'CC')),
    base          char(3)      NOT NULL,
    qualification varchar(8)   NOT NULL
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_crew_member_base')
CREATE INDEX ix_crew_member_base ON crew.crew_member (base, qualification, rank);

IF OBJECT_ID(N'crew.duty') IS NULL
CREATE TABLE crew.duty (
    duty_date       date         NOT NULL,
    duty_id         varchar(16)  NOT NULL,
    base            char(3)      NOT NULL,
    start_airport   char(3)      NOT NULL,
    qualification   varchar(8)   NOT NULL,
    report_local    datetime2(0) NOT NULL,
    release_local   datetime2(0) NOT NULL,
    fdp_minutes     smallint     NOT NULL,
    max_fdp_minutes smallint     NOT NULL,
    pilots_required tinyint      NOT NULL,
    cabin_required  tinyint      NOT NULL,
    CONSTRAINT pk_duty PRIMARY KEY (duty_date, duty_id),
    CONSTRAINT ck_duty_fdp CHECK (fdp_minutes <= max_fdp_minutes),
    CONSTRAINT ck_duty_times CHECK (release_local > report_local)
);

IF OBJECT_ID(N'crew.duty_flight') IS NULL
CREATE TABLE crew.duty_flight (
    duty_date     date         NOT NULL,
    flight_no     varchar(8)   NOT NULL,
    duty_id       varchar(16)  NOT NULL,
    CONSTRAINT pk_duty_flight PRIMARY KEY (duty_date, flight_no),
    CONSTRAINT fk_duty_flight_duty FOREIGN KEY (duty_date, duty_id)
        REFERENCES crew.duty (duty_date, duty_id)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_duty_flight_duty')
CREATE INDEX ix_duty_flight_duty ON crew.duty_flight (duty_date, duty_id);

IF OBJECT_ID(N'crew.assignment') IS NULL
CREATE TABLE crew.assignment (
    duty_date     date         NOT NULL,
    crew_id       varchar(8)   NOT NULL
        CONSTRAINT fk_assignment_crew REFERENCES crew.crew_member (crew_id),
    duty_id       varchar(16)  NOT NULL,
    rank          varchar(3)   NOT NULL,
    CONSTRAINT pk_assignment PRIMARY KEY (duty_date, crew_id),  -- one duty per crew member a day
    CONSTRAINT fk_assignment_duty FOREIGN KEY (duty_date, duty_id)
        REFERENCES crew.duty (duty_date, duty_id)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_assignment_duty')
CREATE INDEX ix_assignment_duty ON crew.assignment (duty_date, duty_id);

IF OBJECT_ID(N'crew.reserve') IS NULL
CREATE TABLE crew.reserve (
    duty_date     date         NOT NULL,
    crew_id       varchar(8)   NOT NULL
        CONSTRAINT fk_reserve_crew REFERENCES crew.crew_member (crew_id),
    base          char(3)      NOT NULL,
    start_local   datetime2(0) NOT NULL,
    end_local     datetime2(0) NOT NULL,
    CONSTRAINT pk_reserve PRIMARY KEY (duty_date, crew_id),
    CONSTRAINT ck_reserve_times CHECK (end_local > start_local)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_reserve_base')
CREATE INDEX ix_reserve_base ON crew.reserve (duty_date, base, start_local);

-- Booking: seat capacity and EU261 distance per flight, PNRs with party size, segments.
IF OBJECT_ID(N'booking.flight') IS NULL
CREATE TABLE booking.flight (
    flight_date   date         NOT NULL,
    flight_no     varchar(8)   NOT NULL,
    seats         smallint     NOT NULL CONSTRAINT ck_booking_seats CHECK (seats > 0),
    distance_km   smallint     NOT NULL,
    CONSTRAINT pk_booking_flight PRIMARY KEY (flight_date, flight_no)
);

IF OBJECT_ID(N'booking.pnr') IS NULL
CREATE TABLE booking.pnr (
    travel_date   date         NOT NULL,
    locator       char(6)      NOT NULL,
    party_size    tinyint      NOT NULL CONSTRAINT ck_pnr_party CHECK (party_size BETWEEN 1 AND 9),
    CONSTRAINT pk_pnr PRIMARY KEY (travel_date, locator)
);

IF OBJECT_ID(N'booking.segment') IS NULL
CREATE TABLE booking.segment (
    travel_date   date         NOT NULL,
    locator       char(6)      NOT NULL,
    seq           tinyint      NOT NULL,
    flight_no     varchar(8)   NOT NULL,
    CONSTRAINT pk_segment PRIMARY KEY (travel_date, locator, seq),
    CONSTRAINT fk_segment_pnr FOREIGN KEY (travel_date, locator)
        REFERENCES booking.pnr (travel_date, locator)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_segment_flight')
CREATE INDEX ix_segment_flight ON booking.segment (travel_date, flight_no) INCLUDE (locator);

-- Slots: declared hourly capacity of coordinated airports and one slot per movement there.
IF OBJECT_ID(N'slots.airport') IS NULL
CREATE TABLE slots.airport (
    iata            char(3)    NOT NULL CONSTRAINT pk_slot_airport PRIMARY KEY,
    hourly_capacity smallint   NOT NULL CONSTRAINT ck_slot_capacity CHECK (hourly_capacity > 0)
);

IF OBJECT_ID(N'slots.slot') IS NULL
CREATE TABLE slots.slot (
    slot_date     date         NOT NULL,  -- operating day of the flight
    flight_no     varchar(8)   NOT NULL,
    movement      char(1)      NOT NULL CONSTRAINT ck_slot_movement CHECK (movement IN ('D', 'A')),
    airport       char(3)      NOT NULL CONSTRAINT fk_slot_airport REFERENCES slots.airport (iata),
    slot_local    datetime2(0) NOT NULL,  -- Lisbon local time; may fall after midnight
    CONSTRAINT pk_slot PRIMARY KEY (slot_date, flight_no, movement)
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_slot_airport_time')
CREATE INDEX ix_slot_airport_time ON slots.slot (airport, slot_local);

DECLARE @sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(SystemsClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-systems-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(SystemsClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-systems-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-systems-dev')
    EXEC(N'CREATE USER [id-airrec-systems-dev] WITH SID = ' + @sid + N', TYPE = E;');

GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::schedule TO [id-airrec-systems-dev];
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::maintenance TO [id-airrec-systems-dev];
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::crew TO [id-airrec-systems-dev];
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::booking TO [id-airrec-systems-dev];
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::slots TO [id-airrec-systems-dev];
