SET NOCOUNT ON;
SET XACT_ABORT ON;
DECLARE @sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(MaintenanceClientId)' AS uniqueidentifier)), 1);
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-maintenance-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(MaintenanceClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-maintenance-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-maintenance-dev')
    EXEC(N'CREATE USER [id-airrec-maintenance-dev] WITH SID = ' + @sid + N', TYPE = E;');
GRANT SELECT, INSERT, UPDATE ON SCHEMA::maintenance TO [id-airrec-maintenance-dev];
GRANT SELECT ON OBJECT::schedule.flight TO [id-airrec-maintenance-dev];
GRANT SELECT ON OBJECT::schedule.operating_day TO [id-airrec-maintenance-dev];
GRANT INSERT ON OBJECT::audit.system_call TO [id-airrec-maintenance-dev];
