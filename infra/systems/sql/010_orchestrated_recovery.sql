-- Orchestrated recovery (orchestrated recovery plan): flight delay reports and the crew reserve
-- write. Idempotent. Apply with tools\systems\apply_sql.py.

IF OBJECT_ID(N'schedule.delay_report') IS NULL
CREATE TABLE schedule.delay_report (
    trace_id       char(32)      NOT NULL CONSTRAINT pk_delay_report PRIMARY KEY,
    operating_date date          NOT NULL,
    flight_no      varchar(8)    NOT NULL,
    delay_minutes  smallint      NOT NULL CONSTRAINT ck_delay_minutes
        CHECK (delay_minutes BETWEEN 5 AND 180 AND delay_minutes % 5 = 0),
    cause          varchar(24)   NOT NULL CONSTRAINT ck_delay_cause
        CHECK (cause IN ('staff_shortage', 'baggage', 'late_inbound', 'other')),
    document       nvarchar(max) NOT NULL CONSTRAINT ck_delay_document CHECK (ISJSON(document) = 1),
    created_at     datetime2(3)  NOT NULL CONSTRAINT df_delay_created DEFAULT SYSUTCDATETIME()
);

-- Schedule records delays; every system reads them to build the same grounded options.
GRANT SELECT, INSERT ON OBJECT::schedule.delay_report TO [id-airrec-systems-dev];
GRANT SELECT ON OBJECT::schedule.delay_report TO [id-airrec-maintenance-dev];
GRANT SELECT ON OBJECT::schedule.delay_report TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::schedule.delay_report TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::schedule.delay_report TO [id-airrec-slots-dev];

-- Crew reads reserve ranks and qualifications to offer a legal reserve crew.
GRANT SELECT ON OBJECT::crew.crew_member TO [id-airrec-crew-dev];

-- assign_reserve_crew: a reserve duty, its assignments, the moved legs and the used reserves.
GRANT INSERT ON OBJECT::crew.duty TO [id-airrec-crew-dev];
GRANT INSERT ON OBJECT::crew.assignment TO [id-airrec-crew-dev];
GRANT UPDATE ON OBJECT::crew.duty_flight TO [id-airrec-crew-dev];
GRANT DELETE ON OBJECT::crew.reserve TO [id-airrec-crew-dev];

-- Ops cases: a delay is reported by flight, so the aircraft is known only after Schedule
-- records it; a repeated disruption event (double click, retry) maps to its existing case.
ALTER TABLE ops.recovery_case ALTER COLUMN aircraft_reg varchar(8) NULL;
IF COL_LENGTH(N'ops.recovery_case', N'event_id') IS NULL
    ALTER TABLE ops.recovery_case ADD event_id uniqueidentifier NULL;
-- The file runs as one batch: create the index on the new column through dynamic SQL.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ux_recovery_case_event')
    EXEC(N'CREATE UNIQUE INDEX ux_recovery_case_event ON ops.recovery_case (event_id)
        WHERE event_id IS NOT NULL;');
