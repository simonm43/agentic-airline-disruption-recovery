-- Decision trail redesign (owner): each system call keeps the endpoint it came
-- through (an MCP tool or a REST route), its inputs and a short answer, so "How ARROW decided"
-- shows caller, endpoint, request and response. The answer is a per-operation summary written by
-- code (counts, statuses, option ids; Booking's is counts only), never a copied body, and the
-- request holds no free text, token or booking data (src/airline_systems/audit.py).
-- Idempotent. Apply with tools\systems\apply_sql.py BEFORE the systems image that writes these
-- columns (its audit insert names them) and before the ARROW image that reads them. Rows written
-- earlier keep NULL and show as not recorded. The existing grants cover the new columns: the
-- systems' INSERT and ARROW's SELECT on audit.system_call (001, 005, 006, 011).
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'audit.system_call', N'endpoint') IS NULL
    ALTER TABLE audit.system_call ADD endpoint nvarchar(120) NULL;
IF COL_LENGTH(N'audit.system_call', N'request_json') IS NULL
    ALTER TABLE audit.system_call ADD request_json nvarchar(2000) NULL;
IF COL_LENGTH(N'audit.system_call', N'response_json') IS NULL
    ALTER TABLE audit.system_call ADD response_json nvarchar(2000) NULL;
