SET NOCOUNT ON;
SET XACT_ABORT ON;

DECLARE @booking_sid uniqueidentifier = CAST('$(BookingClientId)' AS uniqueidentifier);

IF NOT EXISTS (
    SELECT 1
    FROM sys.database_principals
    WHERE name = N'id-airrec-booking-dev'
      AND CONVERT(uniqueidentifier, sid) = @booking_sid
)
    THROW 50010, N'id-airrec-booking-dev does not match BookingClientId', 1;

IF COL_LENGTH(N'booking.pnr', N'synthetic_recovery_opt_in') IS NULL
    ALTER TABLE booking.pnr
        ADD synthetic_recovery_opt_in bit NOT NULL
            CONSTRAINT df_booking_pnr_synthetic_recovery_opt_in DEFAULT (0);

IF OBJECT_ID(N'booking.synthetic_recovery_hold', N'U') IS NULL
    CREATE TABLE booking.synthetic_recovery_hold (
        action_id uniqueidentifier NOT NULL
            CONSTRAINT df_synthetic_recovery_hold_action_id DEFAULT (NEWSEQUENTIALID()),
        travel_date date NOT NULL,
        trace_id char(32) NOT NULL,
        option_id varchar(32) NOT NULL,
        source_flight_no varchar(8) NOT NULL,
        replacement_flight_no varchar(8) NOT NULL,
        locator char(6) NOT NULL,
        party_size tinyint NOT NULL
            CONSTRAINT ck_synthetic_recovery_hold_party CHECK (party_size BETWEEN 1 AND 9),
        assessment_version int NOT NULL
            CONSTRAINT ck_synthetic_recovery_hold_version CHECK (assessment_version >= 0),
        idempotency_key varchar(128) NOT NULL,
        created_at datetime2(7) NOT NULL
            CONSTRAINT df_synthetic_recovery_hold_created_at DEFAULT (SYSUTCDATETIME()),
        expires_at datetime2(7) NOT NULL
            CONSTRAINT df_synthetic_recovery_hold_expires_at
            DEFAULT (DATEADD(MINUTE, 15, SYSUTCDATETIME())),
        CONSTRAINT pk_synthetic_recovery_hold PRIMARY KEY (action_id),
        CONSTRAINT uq_synthetic_recovery_hold_idempotency
            UNIQUE (travel_date, trace_id, idempotency_key)
    );

IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'booking.synthetic_recovery_hold')
      AND name = N'ix_synthetic_recovery_hold_capacity'
)
    CREATE INDEX ix_synthetic_recovery_hold_capacity
        ON booking.synthetic_recovery_hold (travel_date, replacement_flight_no, expires_at)
        INCLUDE (party_size);

GRANT SELECT, INSERT ON OBJECT::booking.synthetic_recovery_hold TO [id-airrec-booking-dev];
