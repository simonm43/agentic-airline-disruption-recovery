-- Published timetable (review). A retime moves std_local/sta_local, the current
-- estimate, and never the published times, so a passenger's delay is measured from the timetable
-- they booked (EU261) even after an earlier case retimed the rotation. The day load writes both;
-- no governed action may write the published columns (actions.WRITABLE_COLUMNS).
-- Idempotent. Apply with tools\systems\apply_sql.py, then move every systems app and both
-- day-load jobs to the image that reads and writes these columns, then Reset the day: the
-- back-fill copies the loaded day's current times, which may already be retimed. Until the jobs
-- run that image, their day load fails on the NOT NULL columns instead of loading without them.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'schedule.flight', N'published_std_local') IS NULL
    ALTER TABLE schedule.flight ADD published_std_local datetime2(0) NULL;
IF COL_LENGTH(N'schedule.flight', N'published_sta_local') IS NULL
    ALTER TABLE schedule.flight ADD published_sta_local datetime2(0) NULL;

-- Dynamic SQL: the batch is compiled before the columns above exist.
EXEC(N'UPDATE schedule.flight SET published_std_local = std_local
      WHERE published_std_local IS NULL;
      UPDATE schedule.flight SET published_sta_local = sta_local
      WHERE published_sta_local IS NULL;');

IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'schedule.flight')
           AND name = N'published_std_local' AND is_nullable = 1)
    EXEC(N'ALTER TABLE schedule.flight ALTER COLUMN published_std_local datetime2(0) NOT NULL;');
IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID(N'schedule.flight')
           AND name = N'published_sta_local' AND is_nullable = 1)
    EXEC(N'ALTER TABLE schedule.flight ALTER COLUMN published_sta_local datetime2(0) NOT NULL;');

IF OBJECT_ID(N'schedule.ck_flight_published_times') IS NULL
    EXEC(N'ALTER TABLE schedule.flight ADD CONSTRAINT ck_flight_published_times
          CHECK (published_sta_local > published_std_local);');
