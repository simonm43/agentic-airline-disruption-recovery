-- Rule versions (control tower and rules spec §3): the airline's editable autonomy limits change
-- only when a second OCC duty manager approves. Every proposal and its outcome is kept; the
-- approved row with the highest version is in force. Idempotent. Apply with
-- tools\systems\apply_sql.py before the ops image that reads rule versions.
SET NOCOUNT ON;
SET XACT_ABORT ON;

IF OBJECT_ID(N'ops.policy_change') IS NULL
CREATE TABLE ops.policy_change (
    change_id     int           IDENTITY NOT NULL CONSTRAINT pk_policy_change PRIMARY KEY,
    version       int           NULL,
    cost_cap_eur  int           NOT NULL CONSTRAINT ck_policy_change_cost CHECK (cost_cap_eur >= 0),
    rebook_limit  int           NOT NULL CONSTRAINT ck_policy_change_rebook CHECK (rebook_limit >= 0),
    base_version  int           NOT NULL,
    reason        nvarchar(300) NOT NULL CONSTRAINT ck_policy_change_reason CHECK (LEN(reason) >= 3),
    proposed_by   varchar(64)   NOT NULL,
    proposed_at   datetime2(3)  NOT NULL CONSTRAINT df_policy_change_proposed DEFAULT SYSUTCDATETIME(),
    status        varchar(12)   NOT NULL
        CONSTRAINT ck_policy_change_status
        CHECK (status IN ('proposed', 'approved', 'rejected', 'withdrawn')),
    decided_by    varchar(64)   NULL,
    decided_at    datetime2(3)  NULL,
    -- A decided row names its decider, and only an approved row has a version.
    CONSTRAINT ck_policy_change_decided
        CHECK (status = 'proposed' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    CONSTRAINT ck_policy_change_version
        CHECK ((status = 'approved' AND version IS NOT NULL)
               OR (status <> 'approved' AND version IS NULL)),
    -- Four-eyes. Only the version 1 seed (release values, proposed and approved by arrow-release)
    -- is exempt; a rejected row has no version, so the exemption is FALSE for it, not UNKNOWN.
    CONSTRAINT ck_policy_change_four_eyes
        CHECK (status NOT IN ('approved', 'rejected') OR decided_by <> proposed_by
               OR (status = 'approved' AND version = 1 AND proposed_by = 'arrow-release'))
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ux_policy_change_pending'
               AND object_id = OBJECT_ID(N'ops.policy_change'))
CREATE UNIQUE INDEX ux_policy_change_pending ON ops.policy_change (status)
    WHERE status = 'proposed';

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ux_policy_change_version'
               AND object_id = OBJECT_ID(N'ops.policy_change'))
CREATE UNIQUE INDEX ux_policy_change_version ON ops.policy_change (version)
    WHERE version IS NOT NULL;

IF NOT EXISTS (SELECT 1 FROM ops.policy_change)
INSERT INTO ops.policy_change
    (version, cost_cap_eur, rebook_limit, base_version, reason, proposed_by, status,
     decided_by, decided_at)
VALUES (1, 20000, 10, 1, N'ARROW release values', 'arrow-release', 'approved',
        'arrow-release', SYSUTCDATETIME());

-- No DELETE: every proposal and its outcome stays.
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.policy_change TO [id-airrec-ops-dev];
