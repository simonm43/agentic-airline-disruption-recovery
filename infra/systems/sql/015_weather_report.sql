-- Case 5 (overnight plan, Task 4a): a forecast storm at Lisbon, recorded by Schedule from a
-- TAF. One row per case; the planning rate comes from the weather playbook in code.
-- Idempotent. Apply with tools\systems\apply_sql.py before the systems image that reads it.

IF OBJECT_ID(N'schedule.weather_report') IS NULL
CREATE TABLE schedule.weather_report (
    trace_id           char(32)      NOT NULL CONSTRAINT pk_weather_report PRIMARY KEY,
    operating_date     date          NOT NULL,
    airport            char(3)       NOT NULL CONSTRAINT ck_weather_airport CHECK (airport = 'LIS'),
    window_start_local datetime2(0)  NOT NULL,
    window_end_local   datetime2(0)  NOT NULL,
    hourly_rate        smallint      NOT NULL CONSTRAINT ck_weather_rate CHECK (hourly_rate > 0),
    hazard             varchar(16)   NOT NULL CONSTRAINT ck_weather_hazard
        CHECK (hazard IN ('thunderstorm', 'strong_wind', 'low_visibility')),
    document           nvarchar(max) NOT NULL CONSTRAINT ck_weather_document
        CHECK (ISJSON(document) = 1),
    created_at         datetime2(3)  NOT NULL CONSTRAINT df_weather_created
        DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_weather_window CHECK (
        window_end_local > window_start_local
        AND DATEDIFF(minute, window_start_local, window_end_local) BETWEEN 30 AND 360
    )
);

-- Schedule records storms; every system reads them to build the same grounded options.
GRANT SELECT, INSERT ON OBJECT::schedule.weather_report TO [id-airrec-systems-dev];
GRANT SELECT ON OBJECT::schedule.weather_report TO [id-airrec-maintenance-dev];
GRANT SELECT ON OBJECT::schedule.weather_report TO [id-airrec-crew-dev];
GRANT SELECT ON OBJECT::schedule.weather_report TO [id-airrec-booking-dev];
GRANT SELECT ON OBJECT::schedule.weather_report TO [id-airrec-slots-dev];
