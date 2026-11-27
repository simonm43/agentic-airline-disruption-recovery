-- Governed recovery actions: checked writes and airport rules.
-- Idempotent. Each system identity gets only the writes its own system performs.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'audit.recovery_action') IS NULL
CREATE TABLE audit.recovery_action (
    action_id    varchar(80)   NOT NULL CONSTRAINT pk_recovery_action PRIMARY KEY,
    system_name  varchar(16)   NOT NULL,
    kind         varchar(48)   NOT NULL,
    trace_id     char(32)      NOT NULL,
    plan_hash    char(64)      NOT NULL,
    proposed_by  varchar(64)   NOT NULL,
    status       varchar(12)   NOT NULL
        CONSTRAINT ck_recovery_action_status
        CHECK (status IN ('confirmed', 'pending', 'refused', 'failed')),
    request      nvarchar(max) NOT NULL CONSTRAINT ck_recovery_action_request CHECK (ISJSON(request) = 1),
    receipt      nvarchar(max) NOT NULL CONSTRAINT ck_recovery_action_receipt CHECK (ISJSON(receipt) = 1),
    created_at   datetime2(3)  NOT NULL CONSTRAINT df_recovery_action_created DEFAULT SYSUTCDATETIME(),
    updated_at   datetime2(3)  NOT NULL CONSTRAINT df_recovery_action_updated DEFAULT SYSUTCDATETIME()
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_recovery_action_trace')
CREATE INDEX ix_recovery_action_trace ON audit.recovery_action (trace_id, created_at);

-- Airport rules are station data, not code. The published LIS night period, as a demo rule.
IF OBJECT_ID(N'slots.airport_rule') IS NULL
CREATE TABLE slots.airport_rule (
    iata                  char(3)       NOT NULL CONSTRAINT pk_airport_rule PRIMARY KEY,
    coordination_level    tinyint       NULL,
    a_cdm                 bit           NOT NULL,
    night_departure_from  time(0)       NULL,
    night_departure_until time(0)       NULL,
    night_arrival_from    time(0)       NULL,
    night_arrival_until   time(0)       NULL,
    source                nvarchar(200) NOT NULL
);

IF NOT EXISTS (SELECT 1 FROM slots.airport_rule WHERE iata = 'LIS')
INSERT INTO slots.airport_rule
    (iata, coordination_level, a_cdm, night_departure_from, night_departure_until,
     night_arrival_from, night_arrival_until, source)
VALUES ('LIS', 3, 1, '23:55', '05:50', '00:05', '06:00',
        N'LIS night period per airline-ops KB §12.2');

INSERT INTO slots.airport_rule (iata, coordination_level, a_cdm, source)
SELECT a.iata, NULL, 0, N'no rule modelled'
FROM slots.airport a
WHERE NOT EXISTS (SELECT 1 FROM slots.airport_rule r WHERE r.iata = a.iata);

-- Schedule (ops control) already updates schedule.flight; it records its own actions.
GRANT SELECT, INSERT, UPDATE ON OBJECT::audit.recovery_action TO [id-airrec-systems-dev];
GRANT SELECT, INSERT, UPDATE ON OBJECT::audit.recovery_action TO [id-airrec-crew-dev];
GRANT UPDATE ON OBJECT::crew.duty TO [id-airrec-crew-dev];
GRANT SELECT, INSERT, UPDATE ON OBJECT::audit.recovery_action TO [id-airrec-booking-dev];
GRANT UPDATE ON OBJECT::booking.segment TO [id-airrec-booking-dev];
GRANT SELECT, INSERT, UPDATE ON OBJECT::audit.recovery_action TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::slots.airport_rule TO [id-airrec-slots-dev];
