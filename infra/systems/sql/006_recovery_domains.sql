SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @crew_sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(CrewClientId)' AS uniqueidentifier)), 1);
DECLARE @booking_sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(BookingClientId)' AS uniqueidentifier)), 1);
DECLARE @slots_sid nvarchar(max) = CONVERT(varchar(max), CONVERT(varbinary(16),
    CAST('$(SlotsClientId)' AS uniqueidentifier)), 1);

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-crew-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(CrewClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-crew-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-crew-dev')
    EXEC(N'CREATE USER [id-airrec-crew-dev] WITH SID = ' + @crew_sid + N', TYPE = E;');

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-booking-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(BookingClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-booking-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-booking-dev')
    EXEC(N'CREATE USER [id-airrec-booking-dev] WITH SID = ' + @booking_sid + N', TYPE = E;');

IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-slots-dev'
           AND CONVERT(uniqueidentifier, sid) <> CAST('$(SlotsClientId)' AS uniqueidentifier))
    THROW 50010, N'id-airrec-slots-dev exists with another SID', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'id-airrec-slots-dev')
    EXEC(N'CREATE USER [id-airrec-slots-dev] WITH SID = ' + @slots_sid + N', TYPE = E;');

GRANT SELECT ON OBJECT::schedule.flight TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft_status TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::maintenance.defect TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::crew.duty TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::crew.duty_flight TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::crew.assignment TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::crew.reserve TO [id-airrec-crew-dev];
GRANT INSERT ON OBJECT::audit.system_call TO [id-airrec-crew-dev];

GRANT SELECT ON OBJECT::schedule.flight TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft_status TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::maintenance.defect TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::booking.flight TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::booking.pnr TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::booking.segment TO [id-airrec-booking-dev];
GRANT INSERT ON OBJECT::audit.system_call TO [id-airrec-booking-dev];

GRANT SELECT ON OBJECT::schedule.flight TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::maintenance.aircraft_status TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::maintenance.defect TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::slots.airport TO [id-airrec-slots-dev];
GRANT SELECT ON OBJECT::slots.slot TO [id-airrec-slots-dev];
GRANT INSERT ON OBJECT::audit.system_call TO [id-airrec-slots-dev];
