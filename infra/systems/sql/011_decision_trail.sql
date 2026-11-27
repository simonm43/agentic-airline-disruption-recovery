-- Decision trail (business UI and free-text spec §4.1): each system call keeps its non-personal
-- arguments, and ARROW's own identity may read the call log to show which agent read which
-- system for a case.
-- Idempotent. Apply with tools\systems\apply_sql.py before the systems image that writes detail.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'audit.system_call', N'detail') IS NULL
    ALTER TABLE audit.system_call ADD detail nvarchar(400) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_system_call_trace'
               AND object_id = OBJECT_ID(N'audit.system_call'))
    CREATE INDEX ix_system_call_trace ON audit.system_call (trace_id, call_id);

GRANT SELECT ON OBJECT::audit.system_call TO [id-airrec-ops-dev];
