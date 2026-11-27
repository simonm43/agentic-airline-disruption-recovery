"""Run one idempotent SQL file against airrec-ops as the signed-in Entra admin (Dev only).

Usage from the repository root:
    python tools\\systems\\apply_sql.py infra\\systems\\sql\\001_schedule.sql SystemsClientId=<guid>

``$(Name)`` placeholders are replaced by the given values. The file runs as one batch.
"""

from __future__ import annotations

import re
import struct
import subprocess
import sys
from pathlib import Path

import pyodbc

SERVER = "sql-airrec-dev-bslgt7jwkw2zu.database.windows.net"
DATABASE = "airrec-ops"
SUBSCRIPTION = "00000000-0000-4000-8000-000000000013"


def connect() -> pyodbc.Connection:
    token = (
        subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--subscription",
                SUBSCRIPTION,
                "--resource",
                "https://database.windows.net",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
            shell=sys.platform == "win32",
        )
        .stdout.strip()
        .encode("utf-16-le")
    )
    packed = struct.pack(f"<I{len(token)}s", len(token), token)
    return pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{SERVER},1433;Database={DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no",
        attrs_before={1256: packed},
        autocommit=False,
        timeout=120,
    )


def render(sql: str, values: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise SystemExit(f"missing value for $({name})")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", values[name]):
            raise SystemExit(f"$({name}) must be a GUID")
        return values[name]

    return re.sub(r"\$\((\w+)\)", sub, sql)


def main() -> None:
    path = Path(sys.argv[1])
    values = dict(arg.split("=", 1) for arg in sys.argv[2:])
    batch = render(path.read_text(encoding="utf-8"), values)
    with connect() as conn:
        conn.cursor().execute(batch)
        conn.commit()
    print(f"applied {path.name} to {DATABASE}")


if __name__ == "__main__":
    main()
