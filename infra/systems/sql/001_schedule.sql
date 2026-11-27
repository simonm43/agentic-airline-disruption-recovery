-- Schedule system (Gate A) and the shared system-call log in database airrec-ops.
-- Idempotent: safe to run again. The systems identity gets read/write on its own schemas only.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF SCHEMA_ID(N'schedule') IS NULL EXEC(N'CREATE SCHEMA schedule');
IF SCHEMA_ID(N'audit') IS NULL EXEC(N'CREATE SCHEMA audit');

IF OBJECT_ID(N'schedule.flight') IS NULL
CREATE TABLE schedule.flight (
    flight_date   date         NOT NULL,
    flight_no     varchar(8)   NOT NULL,
    origin        char(3)      NOT NULL,
    destination   char(3)      NOT NULL,
    std_local     datetime2(0) NOT NULL,  -- Lisbon local time of the origin
    sta_local     datetime2(0) NOT NULL,
    aircraft_reg  varchar(8)   NOT NULL,
    status        varchar(16)  NOT NULL DEFAULT 'scheduled',
    CONSTRAINT pk_flight PRIMARY KEY (flight_date, flight_no),
    CONSTRAINT ck_flight_times CHECK (sta_local > std_local)
);

IF OBJECT_ID(N'audit.system_call') IS NULL
CREATE TABLE audit.system_call (
    call_id       bigint IDENTITY PRIMARY KEY,
    called_at     datetime2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    system_name   varchar(16)   NOT NULL,
    operation     varchar(64)   NOT NULL,
    caller_oid    uniqueidentifier NULL,  -- from the validated token, never from the request body
    caller_name   nvarchar(128) NULL,
    trace_id      char(32)      NULL,
    outcome       varchar(16)   NOT NULL
);

DECLARE @sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(SystemsClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-systems-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(SystemsClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-systems-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-systems-dev')
    EXEC(N'CREATE USER [id-airrec-systems-dev] WITH SID = ' + @sid + N', TYPE = E;');

GRANT SELECT, INSERT, UPDATE ON SCHEMA::schedule TO [id-airrec-systems-dev];
GRANT INSERT, SELECT ON SCHEMA::audit TO [id-airrec-systems-dev];
