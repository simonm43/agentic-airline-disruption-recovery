SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'maintenance.defect') IS NULL
CREATE TABLE maintenance.defect (
    trace_id       char(32) NOT NULL PRIMARY KEY,
    operating_date date NOT NULL,
    aircraft_reg   varchar(8) NOT NULL,
    document       nvarchar(max) NOT NULL CHECK (ISJSON(document)=1),
    created_at     datetime2(3) NOT NULL DEFAULT SYSUTCDATETIME()
);
