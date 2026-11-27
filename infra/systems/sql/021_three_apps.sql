-- Three container apps (ADR 0034):
-- the operator app and the agents' tools app get their own database users; the workers keep
-- ARROW's user, id-airrec-ops-dev. As in every migration, the names are labels and each user is
-- bound to this environment's identity by its client ID. Grants to the workers' user are only
-- added here: revoking the ones it no longer needs (INSERT and UPDATE on ops.policy_change,
-- SELECT on audit.system_call) would break the single app before the cutover, so that is a
-- later clean-up. The intake state the one app kept in memory, the events that opened no case
-- and the last Test Bench Reset, moves here so the workers write it and the operator app reads it.
-- Idempotent. In Dev apply with tools\systems\apply_sql.py and OperatorClientId=, ToolsClientId=.
SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @operator_sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(OperatorClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-operator-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(OperatorClientId)' AS uniqueidentifier))
    THROW 50011, N'id-airrec-operator-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-operator-dev')
    EXEC(N'CREATE USER [id-airrec-operator-dev] WITH SID = ' + @operator_sid + N', TYPE = E;');

DECLARE @tools_sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(ToolsClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-tools-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(ToolsClientId)' AS uniqueidentifier))
    THROW 50012, N'id-airrec-tools-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-tools-dev')
    EXEC(N'CREATE USER [id-airrec-tools-dev] WITH SID = ' + @tools_sid + N', TYPE = E;');

-- The operator app opens typed reports, records decisions, chat turns and reviews, closes cases
-- on Reset, changes the rules under four-eyes and reads the systems' audit for the trail.
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.recovery_case TO [id-airrec-operator-dev];
GRANT SELECT, INSERT ON OBJECT::ops.decision_log TO [id-airrec-operator-dev];
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.policy_change TO [id-airrec-operator-dev];
GRANT SELECT ON OBJECT::audit.system_call TO [id-airrec-operator-dev];

-- The tools record readings, plans and submissions on existing cases and read the rules in force.
GRANT SELECT, UPDATE ON OBJECT::ops.recovery_case TO [id-airrec-tools-dev];
GRANT SELECT, INSERT ON OBJECT::ops.decision_log TO [id-airrec-tools-dev];
GRANT SELECT ON OBJECT::ops.policy_change TO [id-airrec-tools-dev];

IF OBJECT_ID(N'ops.rejected_event') IS NULL
CREATE TABLE ops.rejected_event (
    id bigint IDENTITY NOT NULL PRIMARY KEY,
    event_id nvarchar(100) NULL,
    received_at datetime2(3) NOT NULL,
    reason nvarchar(1000) NOT NULL,
    body nvarchar(max) NULL CHECK (body IS NULL OR ISJSON(body) = 1)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_rejected_event_received')
    EXEC(N'CREATE INDEX ix_rejected_event_received ON ops.rejected_event (received_at)');

IF OBJECT_ID(N'ops.day_reset') IS NULL
CREATE TABLE ops.day_reset (
    id tinyint NOT NULL PRIMARY KEY CHECK (id = 1),
    reset_at datetime2(3) NOT NULL
);

GRANT SELECT, INSERT ON OBJECT::ops.rejected_event TO [id-airrec-ops-dev];
GRANT SELECT ON OBJECT::ops.day_reset TO [id-airrec-ops-dev];
GRANT SELECT ON OBJECT::ops.rejected_event TO [id-airrec-operator-dev];
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.day_reset TO [id-airrec-operator-dev];
