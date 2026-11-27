"""A new environment's database is built by the migration job from inside its network
(infra/systems/migration-job.bicep runs tools/systems/migrate.py): every SQL file runs, in order,
with every identity it grants passed to the job."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.systems import migrate

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "infra" / "systems" / "migration-job.bicep"


def test_the_job_passes_every_identity_the_migrations_grant() -> None:
    files = migrate.migrations(ROOT / "infra" / "systems" / "sql")
    numbers = [int(path.name[:3]) for path in files]
    assert numbers == list(range(1, len(files) + 1))
    used = set().union(*(migrate.placeholders(path.read_text("utf-8")) for path in files))
    passed = set(re.findall(r"name: 'MIGRATION_(\w+)'", JOB.read_text(encoding="utf-8")))
    assert (
        used
        == passed
        == {
            "SystemsClientId",
            "OpsClientId",
            "MaintenanceClientId",
            "CrewClientId",
            "BookingClientId",
            "SlotsClientId",
            "OperatorClientId",
            "ToolsClientId",
        }
    )


def test_a_missing_or_malformed_client_id_stops_the_run_before_any_file() -> None:
    sql = "CREATE USER [x] WITH SID = $(OpsClientId)"
    assert migrate.render(sql, {"OpsClientId": "00000000-0000-4000-8000-000000000003"}) == (
        "CREATE USER [x] WITH SID = 00000000-0000-4000-8000-000000000003"
    )
    with pytest.raises(ValueError, match="MIGRATION_OpsClientId"):
        migrate.render(sql, {})
    with pytest.raises(ValueError, match="not a GUID"):
        migrate.render(sql, {"OpsClientId": "x'; DROP TABLE y; --"})
