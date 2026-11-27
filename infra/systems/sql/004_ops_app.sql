SET NOCOUNT ON;
SET XACT_ABORT ON;

IF SCHEMA_ID(N'ops') IS NULL EXEC(N'CREATE SCHEMA ops');
IF OBJECT_ID(N'ops.recovery_case') IS NULL
CREATE TABLE ops.recovery_case (
    trace_id char(32) NOT NULL PRIMARY KEY,
    operating_date date NOT NULL,
    aircraft_reg varchar(8) NOT NULL,
    created_at datetime2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    document nvarchar(max) NOT NULL CHECK (ISJSON(document)=1)
);

IF OBJECT_ID(N'ops.decision_log') IS NULL
CREATE TABLE ops.decision_log (
    sequence bigint IDENTITY NOT NULL PRIMARY KEY,
    trace_id char(32) NOT NULL,
    at datetime2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    document nvarchar(max) NOT NULL CHECK (ISJSON(document)=1)
) WITH (LEDGER=ON (APPEND_ONLY=ON));

DECLARE @sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(OpsClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-ops-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(OpsClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-ops-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-ops-dev')
    EXEC(N'CREATE USER [id-airrec-ops-dev] WITH SID = ' + @sid + N', TYPE = E;');
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.recovery_case TO [id-airrec-ops-dev];
GRANT SELECT, INSERT ON OBJECT::ops.decision_log TO [id-airrec-ops-dev];
