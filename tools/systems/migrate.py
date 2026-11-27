"""Apply the database migrations in order, inside the environment.

The migration job (infra/systems/migration-job.bicep) runs this as the SQL server's Entra admin
identity, from inside the workload network: a new environment has no developer network that
could reach its private SQL. Every file is idempotent and runs as one batch in its own
transaction, in name order; its $(Name) placeholders are the client IDs of the environment's
identities, read from the MIGRATION_<Name> settings. Every file is rendered before the first one
runs, and a failed file is rolled back and stops the run. Running it again is safe; after the
first day load, a second run declares the airport rules of the stations that day flies (008, 009).

tools/systems/build.ps1 packages this file and infra/systems/sql into the airrec-systems image as
airline_systems.migrate and airline_systems/sql.

    python -m airline_systems.migrate               # the job's command
    python tools/systems/migrate.py --plan          # offline: the files and their placeholders
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

FILE = re.compile(r"[0-9]{3}_[a-z0-9_]+\.sql")
PLACEHOLDER = re.compile(r"\$\((\w+)\)")
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SETTING = "MIGRATION_"
SCOPE = "https://database.windows.net/.default"
TOKEN_ATTRIBUTE = 1256  # SQL_COPT_SS_ACCESS_TOKEN
CHECK = (
    "SELECT (SELECT COUNT(*) FROM sys.tables),"
    " (SELECT COUNT(*) FROM sys.database_principals WHERE type = 'E'),"
    " (SELECT COUNT(*) FROM slots.airport_rule),"
    " (SELECT COUNT(*) FROM schedule.flight"
    " WHERE flight_date = (SELECT MAX(flight_date) FROM schedule.flight)),"
    " (SELECT COUNT(*) FROM (SELECT origin AS iata FROM schedule.flight"
    " UNION SELECT destination FROM schedule.flight) AS flown"
    " WHERE NOT EXISTS (SELECT 1 FROM slots.airport_rule r WHERE r.iata = flown.iata))"
)


def sql_directory() -> Path:
    """The packaged files in the image, else infra/systems/sql in the repository."""
    packaged = Path(__file__).with_name("sql")
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "infra" / "systems" / "sql"


def migrations(directory: Path) -> list[Path]:
    """The migration files in the order they run."""
    files = sorted(path for path in directory.iterdir() if FILE.fullmatch(path.name))
    if not files:
        raise ValueError(f"no migrations in {directory}")
    return files


def placeholders(sql: str) -> set[str]:
    return set(PLACEHOLDER.findall(sql))


def render(sql: str, values: Mapping[str, str]) -> str:
    """The batch with every $(Name) replaced by its client ID; ValueError names a missing or
    malformed one."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        given = values.get(name)
        if given is None:
            raise ValueError(f"missing setting {SETTING}{name}")
        if not GUID.fullmatch(given):
            raise ValueError(f"{SETTING}{name} is not a GUID")
        return given

    return PLACEHOLDER.sub(value, sql)


def settings(environ: Mapping[str, str]) -> dict[str, str]:
    return {key[len(SETTING) :]: item for key, item in environ.items() if key.startswith(SETTING)}


def connect(server: str, database: str, client_id: str) -> Any:
    """A connection signed in as the job's managed identity (Entra only, private endpoint)."""
    import pyodbc
    from azure.identity import ManagedIdentityCredential

    token = ManagedIdentityCredential(client_id=client_id).get_token(SCOPE).token
    encoded = token.encode("utf-16-le")
    packed = struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)
    return pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;"
        f"Database={database};Encrypt=yes;TrustServerCertificate=no",
        attrs_before={TOKEN_ATTRIBUTE: packed},
        autocommit=False,
        timeout=120,
    )


def apply(connection: Any, batches: Sequence[tuple[str, str]]) -> bool:
    """Run each batch in its own transaction; stop at the first failure, rolled back."""
    for name, batch in batches:
        started = time.monotonic()
        cursor = connection.cursor()
        try:
            cursor.execute(batch)
            # A later statement's error surfaces only when its result is read.
            while cursor.nextset():
                pass
            connection.commit()
        except Exception as error:
            connection.rollback()
            print(f"FAILED {name}: {type(error).__name__}: {error}", flush=True)
            return False
        print(f"applied {name} in {time.monotonic() - started:.1f} s", flush=True)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    files = migrations(sql_directory())
    if arguments == ["--plan"]:
        for path in files:
            print(path.name, " ".join(sorted(placeholders(path.read_text(encoding="utf-8")))))
        return 0
    if arguments:
        print("usage: python -m airline_systems.migrate [--plan]", file=sys.stderr)
        return 2
    values = settings(os.environ)
    try:
        batches = [(path.name, render(path.read_text(encoding="utf-8"), values)) for path in files]
    except ValueError as error:
        print(f"FAILED before the first file: {error}", flush=True)
        return 1
    connection = connect(
        os.environ["SQL_SERVER"], os.environ["SQL_DATABASE"], os.environ["AZURE_CLIENT_ID"]
    )
    try:
        if not apply(connection, batches):
            return 1
        tables, users, rules, flights, unruled = connection.cursor().execute(CHECK).fetchone()
    finally:
        connection.close()
    # flights: the latest loaded day's flights (0 before the first day load); stations_without_rule:
    # flown stations with no airport rule, 0 once a run after the day load declared them.
    summary = {
        "files": len(batches),
        "tables": tables,
        "entra_users": users,
        "airport_rules": rules,
        "flights": flights,
        "stations_without_rule": unruled,
    }
    print("MIGRATED " + json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
