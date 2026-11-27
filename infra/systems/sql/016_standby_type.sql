-- Standby crews as airlines roster them (standby and story plan, Task 1). Each standby row says
-- whether the crew member waits at home (answers a call in 60 min, then reports) or at the
-- airport (reports at the call). Rows already loaded become home standby until the day is reloaded.
-- Idempotent. Apply with tools\systems\apply_sql.py before the systems image and the day-load
-- jobs that read and write the column: until then that image's Crew read and day load fail.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF COL_LENGTH(N'crew.reserve', N'standby_type') IS NULL
    ALTER TABLE crew.reserve ADD standby_type varchar(8) NOT NULL
        CONSTRAINT df_reserve_standby_type DEFAULT ('home');

-- Dynamic SQL: the batch is compiled before the column above exists.
IF OBJECT_ID(N'crew.ck_reserve_standby_type') IS NULL
    EXEC(N'ALTER TABLE crew.reserve ADD CONSTRAINT ck_reserve_standby_type
          CHECK (standby_type IN (''home'', ''airport''));');
