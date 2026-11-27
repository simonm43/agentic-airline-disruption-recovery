"""Azure SQL access with an Entra token (managed identity in Azure, no secrets)."""

from __future__ import annotations

import json
import struct
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import pyodbc
from azure.core.credentials import TokenCredential
from azure.identity import ManagedIdentityCredential

from airline_systems import telemetry
from airline_systems.actions import check_changes, describe, seats_gone, source_changed
from airline_systems.schedule import Flight, lisbon_today

if TYPE_CHECKING:
    from airline_systems.operating_day import OperatingDay

_SQL_SCOPE = "https://database.windows.net/.default"
_TOKEN_ATTR = 1256

# Child tables before parents. Every row of these tables belongs to the one loaded day; the OCC
# playbook's passenger notices (sql/017_playbook.sql) and a storm case's standby alerts
# (sql/019_storm_held.sql) go with it.
_DAY_TABLES = (
    "booking.passenger_notice",
    "crew.standby_alert",
    "booking.segment",
    "booking.pnr",
    "booking.flight",
    "slots.slot",
    "slots.airport",
    "crew.assignment",
    "crew.duty_flight",
    "crew.reserve",
    "crew.duty",
    "crew.crew_member",
    "maintenance.aircraft_status",
    "maintenance.aircraft",
    "schedule.flight",
    "schedule.operating_day",
)


class SqlScheduleStore:
    def __init__(
        self,
        server: str,
        database: str,
        client_id: str | None,
        system: str,
        credential: TokenCredential | None = None,
        *,
        passenger_holds_enabled: bool = False,
    ) -> None:
        self._dsn = (
            f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;"
            f"Database={database};Encrypt=yes;TrustServerCertificate=no"
        )
        self._server, self._database = server, database
        self._credential = credential or ManagedIdentityCredential(client_id=client_id)
        self._system = system
        self._passenger_holds_enabled = passenger_holds_enabled

    @contextmanager
    def connect(self) -> Iterator[pyodbc.Connection]:
        token = self._credential.get_token(_SQL_SCOPE).token.encode("utf-16-le")
        packed = struct.pack(f"<I{len(token)}s", len(token), token)
        traced = {"db.system": "mssql", "db.name": self._database, "server.address": self._server}
        # ODBC ignores "Connection Timeout" in the string; the login timeout allows a slow first
        # login (a database resuming took about a minute when Dev SQL could pause).
        with telemetry.sql_span("CONNECT", traced) if telemetry.enabled() else nullcontext():
            conn = pyodbc.connect(self._dsn, attrs_before={_TOKEN_ATTR: packed}, timeout=90)
        try:
            yield telemetry.sql_connection(conn, self._server, self._database)
            conn.commit()
        finally:
            conn.close()

    def has_day(self, day: date) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM schedule.operating_day WHERE flight_date = ?", day
            ).fetchone()
        return bool(row and row[0])

    def load_operating_day(self, day: date, op_day: OperatingDay, *, force: bool = False) -> bool:
        """Replace every system's day with ``op_day`` in one transaction; False if loaded."""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DECLARE @rc int; EXEC @rc = sp_getapplock @Resource = N'airrec-operating-day',"
                " @LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = 300000;"
                " IF @rc < 0 THROW 50020, N'operating day load lock not granted', 1;"
            )
            loaded = cursor.execute(
                "SELECT COUNT(*) FROM schedule.operating_day WHERE flight_date = ?", day
            ).fetchone()
            if loaded and loaded[0] and not force:
                return False
            for table in _DAY_TABLES:
                cursor.execute(f"DELETE FROM {table}")
            cursor.fast_executemany = True
            for statement, rows in _day_rows(
                day, op_day, passenger_holds_enabled=self._passenger_holds_enabled
            ):
                if rows:
                    cursor.executemany(statement, rows)
            cursor.execute(
                "INSERT INTO schedule.operating_day (flight_date, flights) VALUES (?, ?)",
                day,
                len(op_day.flights),
            )
        return True

    def flights_for_aircraft(self, day: date, aircraft_reg: str) -> list[Flight]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT flight_date, flight_no, origin, destination, std_local, sta_local,"
                " aircraft_reg, status, published_std_local, published_sta_local"
                " FROM schedule.flight WHERE flight_date = ? AND aircraft_reg = ?",
                day,
                aircraft_reg,
            ).fetchall()
        return [Flight(*row) for row in rows]

    def booking_flight_summary(self, day: date, flight_no: str) -> dict[str, Any] | None:
        normalized = flight_no.strip().upper()
        if not normalized:
            raise ValueError("Flight number is required")
        with self.connect() as conn:
            scheduled = conn.execute(
                "SELECT 1 FROM schedule.flight WHERE flight_date=? AND flight_no=?",
                day,
                normalized,
            ).fetchone()
            if scheduled is None:
                return None
            inventory = conn.execute(
                "SELECT seats FROM booking.flight WHERE flight_date=? AND flight_no=?",
                day,
                normalized,
            ).fetchone()
            if inventory is None:
                return {
                    "flight_no": normalized,
                    "status": "unavailable",
                    "reason": "Booking inventory is not recorded for this flight.",
                    "passenger_count": None,
                    "seat_capacity": None,
                    "held_seat_count": None,
                    "available_seats": None,
                    "connecting_passenger_count": None,
                    "connections": None,
                }
            passenger_row = conn.execute(
                "SELECT COALESCE(SUM(p.party_size), 0) FROM booking.segment s"
                " JOIN booking.pnr p ON p.travel_date=s.travel_date AND p.locator=s.locator"
                " WHERE s.travel_date=? AND s.flight_no=?",
                day,
                normalized,
            ).fetchone()
            connection_rows = conn.execute(
                "SELECT onward.flight_no, SUM(p.party_size) AS passenger_count"
                " FROM booking.segment current_leg"
                " JOIN booking.segment onward ON onward.travel_date=current_leg.travel_date"
                " AND onward.locator=current_leg.locator AND onward.seq=current_leg.seq+1"
                " JOIN booking.pnr p ON p.travel_date=current_leg.travel_date"
                " AND p.locator=current_leg.locator"
                " WHERE current_leg.travel_date=? AND current_leg.flight_no=?"
                " GROUP BY onward.flight_no ORDER BY onward.flight_no",
                day,
                normalized,
            ).fetchall()
            held_row = None
            if self._passenger_holds_enabled:
                held_row = conn.execute(
                    "SELECT COALESCE(SUM(party_size), 0) FROM booking.synthetic_recovery_hold"
                    " WHERE travel_date=? AND replacement_flight_no=?"
                    " AND expires_at>SYSUTCDATETIME()",
                    day,
                    normalized,
                ).fetchone()
        passenger_count = (
            int(passenger_row[0]) if passenger_row and passenger_row[0] is not None else 0
        )
        held_seat_count = int(held_row[0]) if held_row and held_row[0] is not None else 0
        connections = [
            {"flight_no": row[0], "passenger_count": int(row[1])} for row in connection_rows
        ]
        return {
            "flight_no": normalized,
            "status": "available",
            "passenger_count": passenger_count,
            "seat_capacity": int(inventory[0]),
            "held_seat_count": held_seat_count,
            "available_seats": int(inventory[0]) - passenger_count - held_seat_count,
            "connecting_passenger_count": sum(
                connection["passenger_count"] for connection in connections
            ),
            "connections": connections,
        }

    def find_recovery_hold(
        self, day: date, trace_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        if not self._passenger_holds_enabled:
            raise ValueError("Seat holds are disabled in this increment")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT CONVERT(varchar(36), action_id), option_id, source_flight_no,"
                " replacement_flight_no, party_size, assessment_version, created_at, expires_at,"
                " CASE WHEN expires_at>SYSUTCDATETIME() THEN 'held' ELSE 'expired' END"
                " FROM booking.synthetic_recovery_hold"
                " WHERE travel_date=? AND trace_id=? AND idempotency_key=?",
                day,
                trace_id,
                idempotency_key,
            ).fetchone()
        if row is None:
            return None
        return {
            "option_id": row[1],
            "source_flight_no": row[2],
            "assessment_version": row[5],
            "receipt": _recovery_hold_receipt(
                (row[0], row[3], row[4], row[6], row[7], row[8]), trace_id
            ),
        }

    def create_recovery_hold(
        self,
        *,
        day: date,
        trace_id: str,
        option_id: str,
        source_flight_no: str,
        replacement_flight_no: str,
        locator: str,
        party_size: int,
        expected_flight_nos: tuple[str, ...],
        assessment_version: int,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        if not self._passenger_holds_enabled:
            raise ValueError("Seat holds are disabled in this increment")
        with self.connect() as conn:
            cursor = conn.cursor()
            _acquire_transaction_lock(cursor, f"airrec-hold-key:{day}:{trace_id}:{idempotency_key}")
            existing = cursor.execute(
                "SELECT CONVERT(varchar(36), action_id), option_id, source_flight_no,"
                " replacement_flight_no, locator, party_size, assessment_version, created_at,"
                " expires_at,"
                " CASE WHEN expires_at>SYSUTCDATETIME() THEN 'held' ELSE 'expired' END"
                " FROM booking.synthetic_recovery_hold"
                " WHERE travel_date=? AND trace_id=? AND idempotency_key=?",
                day,
                trace_id,
                idempotency_key,
            ).fetchone()
            if existing is not None:
                if (
                    existing[1] != option_id
                    or existing[2] != source_flight_no
                    or existing[3] != replacement_flight_no
                    or existing[4].strip() != locator
                    or existing[5] != party_size
                    or existing[6] != assessment_version
                ):
                    raise ValueError("Idempotency key was already used for a different hold")
                return (
                    _recovery_hold_receipt(
                        (
                            existing[0],
                            existing[3],
                            existing[5],
                            existing[7],
                            existing[8],
                            existing[9],
                        ),
                        trace_id,
                    ),
                    False,
                )

            _acquire_transaction_lock(cursor, f"airrec-hold-flight:{day}:{replacement_flight_no}")
            pnr = cursor.execute(
                "SELECT party_size, synthetic_recovery_opt_in FROM booking.pnr"
                " WITH (UPDLOCK, HOLDLOCK) WHERE travel_date=? AND locator=?",
                day,
                locator,
            ).fetchone()
            if pnr is None or pnr[0] != party_size or pnr[1] is not True:
                raise ValueError("The booking is no longer eligible for a hold")
            segments = cursor.execute(
                "SELECT flight_no FROM booking.segment WITH (UPDLOCK, HOLDLOCK)"
                " WHERE travel_date=? AND locator=? ORDER BY seq",
                day,
                locator,
            ).fetchall()
            if tuple(row[0] for row in segments) != expected_flight_nos:
                raise ValueError("The booking itinerary changed before the hold")
            if source_flight_no not in expected_flight_nos:
                raise ValueError("The source flight is not part of the opted-in booking")

            source = cursor.execute(
                "SELECT origin, destination FROM schedule.flight"
                " WHERE flight_date=? AND flight_no=?",
                day,
                source_flight_no,
            ).fetchone()
            alternative = cursor.execute(
                "SELECT f.origin, f.destination, f.status, s.status"
                " FROM schedule.flight f"
                " JOIN maintenance.aircraft_status s"
                " ON s.status_date=f.flight_date AND s.registration=f.aircraft_reg"
                " WHERE f.flight_date=? AND f.flight_no=?",
                day,
                replacement_flight_no,
            ).fetchone()
            if (
                source is None
                or alternative is None
                or (source[0], source[1]) != (alternative[0], alternative[1])
                or alternative[2] != "scheduled"
                or alternative[3] != "serviceable"
            ):
                raise ValueError("The same-day alternative flight is no longer available")

            replacement_itinerary = tuple(
                replacement_flight_no if flight_no == source_flight_no else flight_no
                for flight_no in expected_flight_nos
            )
            itinerary_flights: list[tuple[str, str, datetime, datetime, str]] = []
            for flight_no in replacement_itinerary:
                flight = cursor.execute(
                    "SELECT origin, destination, std_local, sta_local, status"
                    " FROM schedule.flight WHERE flight_date=? AND flight_no=?",
                    day,
                    flight_no,
                ).fetchone()
                if flight is None or flight[4] != "scheduled":
                    raise ValueError("The replacement itinerary is incomplete")
                itinerary_flights.append((flight[0], flight[1], flight[2], flight[3], flight[4]))
            for current, following in zip(itinerary_flights, itinerary_flights[1:]):
                connection = following[2] - current[3]
                if (
                    current[1] != following[0]
                    or connection.total_seconds() < 60 * 60
                    or connection.total_seconds() > 300 * 60
                ):
                    raise ValueError("The replacement itinerary violates the scenario rules")

            seats = cursor.execute(
                "SELECT seats FROM booking.flight WITH (UPDLOCK, HOLDLOCK)"
                " WHERE flight_date=? AND flight_no=?",
                day,
                replacement_flight_no,
            ).fetchone()
            if seats is None:
                raise ValueError("Alternative-flight booking inventory is missing")
            booked = cursor.execute(
                "SELECT COALESCE(SUM(p.party_size), 0) FROM booking.segment s"
                " JOIN booking.pnr p ON p.travel_date=s.travel_date AND p.locator=s.locator"
                " WHERE s.travel_date=? AND s.flight_no=?",
                day,
                replacement_flight_no,
            ).fetchone()
            held = cursor.execute(
                "SELECT COALESCE(SUM(party_size), 0) FROM booking.synthetic_recovery_hold"
                " WITH (UPDLOCK, HOLDLOCK) WHERE travel_date=? AND replacement_flight_no=?"
                " AND expires_at>SYSUTCDATETIME()",
                day,
                replacement_flight_no,
            ).fetchone()
            if int(seats[0]) - int(booked[0]) - int(held[0]) < party_size:
                raise ValueError("Alternative-flight seats are no longer available")

            cursor.execute(
                "INSERT INTO booking.synthetic_recovery_hold"
                " (travel_date, trace_id, option_id, source_flight_no, replacement_flight_no,"
                " locator, party_size, assessment_version, idempotency_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                day,
                trace_id,
                option_id,
                source_flight_no,
                replacement_flight_no,
                locator,
                party_size,
                assessment_version,
                idempotency_key,
            )
            created = cursor.execute(
                "SELECT CONVERT(varchar(36), action_id), replacement_flight_no, party_size,"
                " created_at, expires_at, CASE WHEN expires_at>SYSUTCDATETIME()"
                " THEN 'held' ELSE 'expired' END FROM booking.synthetic_recovery_hold"
                " WHERE travel_date=? AND trace_id=? AND idempotency_key=?",
                day,
                trace_id,
                idempotency_key,
            ).fetchone()
            if created is None:
                raise RuntimeError("The committed hold receipt is missing")
            receipt = _recovery_hold_receipt(created, trace_id)
        return receipt, True

    def find_action(self, action_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT request, receipt FROM audit.recovery_action WHERE action_id=?", action_id
            ).fetchone()
        if row is None:
            return None
        return {"request": json.loads(row[0]), "receipt": json.loads(row[1])}

    def apply_action(
        self, request: dict[str, Any], changes: list[dict[str, Any]], receipt: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Apply one action atomically with its audit row; compare-and-set on every changed row.

        Column names come only from each table's allow-list (actions.WRITABLE_COLUMNS); booking
        segments move onto a flight only while its seats still hold everyone (two cases can
        execute at once, D110)."""
        check_changes(changes)
        day = lisbon_today()
        with self.connect() as conn:
            cursor = conn.cursor()
            _acquire_transaction_lock(cursor, f"airrec-action:{request['action_id']}")
            existing = cursor.execute(
                "SELECT request, receipt FROM audit.recovery_action WHERE action_id=?",
                request["action_id"],
            ).fetchone()
            if existing is not None:
                if json.loads(existing[0]) != request:
                    raise ValueError("Action id was already used for a different request")
                return json.loads(existing[1]), False
            # Lock each flight a segment moves onto before its segments, in one order, so
            # concurrent rebookings onto the same flight run one after the other.
            moves = [change for change in changes if change["table"] == "segment"]
            seats: dict[str, int | None] = {}
            for flight_no in sorted({move["values"]["flight_no"] for move in moves}):
                row = cursor.execute(
                    "SELECT seats FROM booking.flight WITH (UPDLOCK, HOLDLOCK)"
                    " WHERE flight_date=? AND flight_no=?",
                    day,
                    flight_no,
                ).fetchone()
                seats[flight_no] = int(row[0]) if row else None
            for change in changes:
                table, keys, date_column = _ACTION_TABLES[change["table"]]
                columns = sorted(change["expected"])
                where = " AND ".join(f"{key}=?" for key in keys)
                row = cursor.execute(
                    f"SELECT {', '.join(columns) or '1'} FROM {table} WITH (UPDLOCK, HOLDLOCK)"
                    f" WHERE {date_column}=? AND {where}",
                    day,
                    *[change["key"][key] for key in keys],
                ).fetchone()
                if change.get("op", "update") == "insert":
                    conflict = row is not None
                else:
                    current = dict(zip(columns, (_plain(value) for value in row))) if row else None
                    conflict = current is None or any(
                        current[column] != change["expected"][column] for column in columns
                    )
                if conflict:
                    receipt = {
                        **receipt,
                        "status": "refused",
                        "message": source_changed(change["table"], change["key"]),
                    }
                    changes = []
                    break
            full = self._full_flight(cursor, day, moves, seats) if changes else None
            if full is not None:
                receipt = {**receipt, "status": "refused", "message": seats_gone(full)}
                changes = []
            for change in changes:
                table, keys, date_column = _ACTION_TABLES[change["table"]]
                values = change["values"]
                key_values = [change["key"][key] for key in keys]
                op = change.get("op", "update")
                if op == "insert":
                    names = [date_column, *keys, *values]
                    cursor.execute(
                        f"INSERT INTO {table} ({', '.join(names)})"
                        f" VALUES ({', '.join(['?'] * len(names))})",
                        day,
                        *key_values,
                        *values.values(),
                    )
                    continue
                if op == "delete":
                    expected = change["expected"]
                    match = " AND ".join(f"{name}=?" for name in [*keys, *expected])
                    deleted = cursor.execute(
                        f"DELETE FROM {table} WHERE {date_column}=? AND {match}",
                        day,
                        *key_values,
                        *expected.values(),
                    ).rowcount
                    if deleted != 1:
                        raise ValueError(
                            f"Could not delete {describe(change['table'], change['key'])}"
                        )
                    continue
                assignments = ", ".join(f"{column}=?" for column in values)
                guard: list[Any] = []
                if change["table"] == "duty" and "release_local" in values:
                    # FDP runs to on-blocks; the plan computes it and the row must stay legal.
                    if "fdp_minutes" not in values:
                        raise ValueError(
                            f"A release change must carry its FDP: "
                            f"{describe(change['table'], change['key'])}"
                        )
                    guard = [values["fdp_minutes"]]
                extra = " AND ? <= max_fdp_minutes" if guard else ""
                params = [*values.values(), day, *key_values, *guard]
                updated = cursor.execute(
                    f"UPDATE {table} SET {assignments}"
                    f" WHERE {date_column}=? AND {' AND '.join(f'{key}=?' for key in keys)}{extra}",
                    *params,
                ).rowcount
                if updated != 1:
                    raise ValueError(f"Could not update {describe(change['table'], change['key'])}")
            # A native call carries no ARROW plan hash or proposer; the columns stay NOT NULL.
            cursor.execute(
                "INSERT INTO audit.recovery_action (action_id, system_name, kind, trace_id,"
                " plan_hash, proposed_by, status, request, receipt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                request["action_id"],
                self._system,
                request["kind"],
                request["trace_id"],
                request.get("plan_hash") or "",
                request.get("proposed_by") or "",
                receipt["status"],
                json.dumps(request),
                json.dumps(receipt),
            )
        return receipt, True

    def _full_flight(
        self,
        cursor: Any,
        day: date,
        moves: list[dict[str, Any]],
        seats: dict[str, int | None],
    ) -> str | None:
        """The first flight (rows locked by the caller) these segment moves would overbook."""
        if not moves:
            return None
        locators = sorted({move["key"]["locator"] for move in moves})
        rows = cursor.execute(
            "SELECT locator, party_size FROM booking.pnr WHERE travel_date=?"
            f" AND locator IN ({', '.join('?' * len(locators))})",
            day,
            *locators,
        ).fetchall()
        party = {str(row[0]).strip(): int(row[1]) for row in rows}
        for flight_no, capacity in sorted(seats.items()):
            if capacity is None:
                return flight_no
            booked = cursor.execute(
                "SELECT COALESCE(SUM(p.party_size), 0) FROM booking.segment s"
                " JOIN booking.pnr p ON p.travel_date=s.travel_date AND p.locator=s.locator"
                " WHERE s.travel_date=? AND s.flight_no=?",
                day,
                flight_no,
            ).fetchone()
            held = (
                cursor.execute(
                    "SELECT COALESCE(SUM(party_size), 0) FROM booking.synthetic_recovery_hold"
                    " WITH (UPDLOCK, HOLDLOCK) WHERE travel_date=? AND replacement_flight_no=?"
                    " AND expires_at>SYSUTCDATETIME()",
                    day,
                    flight_no,
                ).fetchone()
                if self._passenger_holds_enabled
                else (0,)
            )
            arriving = sum(
                party[move["key"]["locator"]]
                for move in moves
                if move["values"]["flight_no"] == flight_no
            )
            leaving = sum(
                party[move["key"]["locator"]]
                for move in moves
                if move["key"]["flight_no"] == flight_no
            )
            if int(booked[0]) + int(held[0]) - leaving + arriving > capacity:
                return flight_no
        return None

    def passenger_notices(self, day: date, kind: str = "new_times") -> dict[str, str]:
        """Today's flights notified of ``kind`` and the notice (its action id) that notified
        each."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT flight_no, notice_id FROM booking.passenger_notice"
                " WHERE travel_date=? AND kind=?",
                day,
                kind,
            ).fetchall()
        return {str(row[0]).strip(): str(row[1]) for row in rows}

    def standby_alerts(self, day: date) -> list[dict[str, Any]]:
        """Today's standby alerts (sql/019): each crew a case put on alert, called or released."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT alert_id, trace_id, reserve_option_id, crew_ids, call_by_local, status"
                " FROM crew.standby_alert WHERE duty_date=? ORDER BY created_at, alert_id",
                day,
            ).fetchall()
        return [
            {
                "alert_id": str(row[0]),
                "trace_id": str(row[1]).strip(),
                "reserve_option_id": str(row[2]).strip(),
                "crew_ids": str(row[3]).split(","),
                "call_by_local": row[4].isoformat(),
                "status": str(row[5]).strip(),
            }
            for row in rows
        ]

    def update_action(self, action_id: str, receipt: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE audit.recovery_action SET status=?, receipt=?,"
                " updated_at=SYSUTCDATETIME() WHERE action_id=? AND system_name=?",
                receipt["status"],
                json.dumps(receipt),
                action_id,
                self._system,
            )

    def airport_rules(self) -> list[dict[str, Any]]:
        names = (
            "iata",
            "coordination_level",
            "a_cdm",
            "night_departure_from",
            "night_departure_until",
            "night_arrival_from",
            "night_arrival_until",
            "source",
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(names)} FROM slots.airport_rule ORDER BY iata"
            ).fetchall()
        return [
            {
                name: value.strftime("%H:%M")
                if hasattr(value, "strftime")
                else bool(value)
                if name == "a_cdm"
                else value
                for name, value in zip(names, row)
            }
            for row in rows
        ]

    def log_call(
        self,
        operation: str,
        caller_oid: str | None,
        caller_name: str | None,
        trace_id: str | None,
        outcome: str,
        detail: str | None = None,
        *,
        endpoint: str | None = None,
        request: str | None = None,
        response: str | None = None,
    ) -> None:
        """One audit row; endpoint, request and response need sql/014_audit_payloads.sql."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit.system_call (system_name, operation, caller_oid, caller_name,"
                " trace_id, outcome, detail, endpoint, request_json, response_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._system,
                operation,
                caller_oid,
                caller_name,
                trace_id,
                outcome,
                detail[:400] if detail else None,
                endpoint[:120] if endpoint else None,
                request if request and len(request) <= 2000 else None,
                response if response and len(response) <= 2000 else None,
            )

    def record_delay(self, delay: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT document FROM schedule.delay_report WITH (UPDLOCK, HOLDLOCK)"
                " WHERE trace_id = ?",
                delay["trace_id"],
            ).fetchone()
            if existing:
                stored: dict[str, Any] = json.loads(existing[0])
                if stored != delay:
                    raise ValueError("A different delay already uses this trace")
                return stored
            conn.execute(
                "INSERT INTO schedule.delay_report (trace_id, operating_date, flight_no,"
                " delay_minutes, cause, document) VALUES (?, ?, ?, ?, ?, ?)",
                delay["trace_id"],
                delay["operating_date"],
                delay["flight_no"],
                delay["delay_minutes"],
                delay["cause"],
                json.dumps(delay),
            )
        return delay

    def get_delay(self, trace: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM schedule.delay_report WHERE trace_id=?", trace
            ).fetchone()
        return json.loads(row[0]) if row else None

    def event_traces(self, day: date, digits: str) -> list[str]:
        """Traces of the day's recorded delays and defects whose seventh to twelfth characters
        are ``digits``: the record behind an operations event id."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT trace_id FROM schedule.delay_report WHERE operating_date=?"
                " AND SUBSTRING(trace_id, 7, 6)=? UNION SELECT trace_id FROM maintenance.defect"
                " WHERE operating_date=? AND SUBSTRING(trace_id, 7, 6)=?",
                day.isoformat(),
                digits,
                day.isoformat(),
                digits,
            ).fetchall()
        return [str(row[0]) for row in rows]

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT document FROM maintenance.defect WITH (UPDLOCK, HOLDLOCK)"
                " WHERE trace_id = ?",
                defect["trace_id"],
            ).fetchone()
            if existing:
                stored: dict[str, Any] = json.loads(existing[0])
                if stored != defect:
                    raise ValueError("A different defect already uses this trace")
                return stored
            changed = conn.execute(
                "UPDATE maintenance.aircraft_status SET status='unserviceable',"
                " return_to_service_local=? WHERE status_date=? AND registration=?",
                defect["unavailable_until"],
                defect["operating_date"],
                defect["aircraft_reg"],
            ).rowcount
            if changed != 1:
                raise ValueError("Aircraft status is absent for this operating day")
            conn.execute(
                "INSERT INTO maintenance.defect (trace_id, operating_date, aircraft_reg, document)"
                " VALUES (?, ?, ?, ?)",
                defect["trace_id"],
                defect["operating_date"],
                defect["aircraft_reg"],
                json.dumps(defect),
            )
        return defect

    def get_defect(self, trace: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM maintenance.defect WHERE trace_id=?", trace
            ).fetchone()
        if row is None:
            raise ValueError("Defect not found")
        result: dict[str, Any] = json.loads(row[0])
        return result

    def recovery_inventory(self, day: date) -> dict[str, Any]:
        with self.connect() as conn:
            flights = conn.execute(
                "SELECT flight_date, flight_no, origin, destination, std_local, sta_local,"
                " aircraft_reg, status, published_std_local, published_sta_local"
                " FROM schedule.flight WHERE flight_date=?",
                day,
            ).fetchall()
            aircraft = conn.execute(
                "SELECT a.registration,a.fleet_type,a.seats,a.base,s.status"
                " FROM maintenance.aircraft a JOIN maintenance.aircraft_status s"
                " ON a.registration=s.registration WHERE s.status_date=?",
                day,
            ).fetchall()
        return {
            "flights": [Flight(*row).as_json() for row in flights],
            "aircraft": [
                dict(zip(("registration", "fleet_type", "seats", "base", "status"), row))
                for row in aircraft
            ],
        }

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        if domain != self._system:
            raise ValueError(f"{self._system} identity cannot read {domain} inventory")
        with self.connect() as conn:
            if domain == "crew":
                duty_rows = conn.execute(
                    "SELECT duty_id, base, qualification, report_local, release_local,"
                    " pilots_required, cabin_required, start_airport, fdp_minutes,"
                    " max_fdp_minutes FROM crew.duty WHERE duty_date=? ORDER BY duty_id",
                    day,
                ).fetchall()
                flight_rows = conn.execute(
                    "SELECT duty_id, flight_no FROM crew.duty_flight WHERE duty_date=?"
                    " ORDER BY duty_id, flight_no",
                    day,
                ).fetchall()
                member_rows = conn.execute(
                    "SELECT duty_id, crew_id, rank FROM crew.assignment WHERE duty_date=?"
                    " ORDER BY duty_id, crew_id",
                    day,
                ).fetchall()
                reserve_rows = conn.execute(
                    "SELECT r.crew_id, r.base, r.start_local, r.end_local, m.rank,"
                    " m.qualification, r.standby_type FROM crew.reserve r JOIN crew.crew_member m"
                    " ON m.crew_id = r.crew_id WHERE r.duty_date=? ORDER BY r.crew_id",
                    day,
                ).fetchall()
                flights_by_duty: dict[str, list[str]] = defaultdict(list)
                for duty_id, flight_no in flight_rows:
                    flights_by_duty[duty_id].append(flight_no)
                members_by_duty: dict[str, list[dict[str, str]]] = defaultdict(list)
                for duty_id, crew_id, rank in member_rows:
                    members_by_duty[duty_id].append({"crew_id": crew_id, "rank": rank})
                return {
                    "duties": [
                        {
                            "duty_id": row[0],
                            "base": row[1],
                            "qualification": row[2],
                            "report_local": row[3].isoformat(),
                            "release_local": row[4].isoformat(),
                            "pilots_required": row[5],
                            "cabin_required": row[6],
                            "start_airport": row[7],
                            "fdp_minutes": row[8],
                            "max_fdp_minutes": row[9],
                            "flight_nos": flights_by_duty[row[0]],
                            "members": members_by_duty[row[0]],
                        }
                        for row in duty_rows
                    ],
                    "reserves": [
                        {
                            "crew_id": row[0],
                            "base": row[1],
                            "start_local": row[2].isoformat(),
                            "end_local": row[3].isoformat(),
                            "rank": row[4],
                            "qualification": row[5],
                            "standby_type": row[6],
                        }
                        for row in reserve_rows
                    ],
                }
            if domain == "booking":
                opt_in_column = (
                    "p.synthetic_recovery_opt_in"
                    if self._passenger_holds_enabled
                    else "CAST(0 AS bit)"
                )
                pnr_rows = conn.execute(
                    "SELECT p.locator, p.party_size, s.flight_no,"
                    f" {opt_in_column} FROM booking.pnr p"
                    " JOIN booking.segment s ON p.travel_date=s.travel_date"
                    " AND p.locator=s.locator WHERE p.travel_date=?"
                    " ORDER BY p.locator, s.seq",
                    day,
                ).fetchall()
                inventory_rows = conn.execute(
                    "SELECT flight_no, seats, distance_km FROM booking.flight"
                    " WHERE flight_date=? ORDER BY flight_no",
                    day,
                ).fetchall()
                hold_rows = []
                if self._passenger_holds_enabled:
                    hold_rows = conn.execute(
                        "SELECT replacement_flight_no, COALESCE(SUM(party_size), 0)"
                        " FROM booking.synthetic_recovery_hold"
                        " WHERE travel_date=? AND expires_at>SYSUTCDATETIME()"
                        " GROUP BY replacement_flight_no",
                        day,
                    ).fetchall()
                passengers_by_flight: dict[str, int] = defaultdict(int)
                holds_by_flight = {row[0]: int(row[1]) for row in hold_rows}
                pnrs: dict[str, dict[str, Any]] = {}
                for locator, party_size, flight_no, opted_in in pnr_rows:
                    pnr = pnrs.setdefault(
                        locator,
                        {
                            "locator": locator,
                            "party_size": party_size,
                            "flight_nos": [],
                            "synthetic_recovery_opt_in": bool(opted_in),
                        },
                    )
                    pnr["flight_nos"].append(flight_no)
                    passengers_by_flight[flight_no] += party_size
                return {
                    "pnrs": list(pnrs.values()),
                    "inventory": [
                        {
                            "flight_no": row[0],
                            "seats": row[1],
                            "distance_km": row[2],
                            "passengers": passengers_by_flight[row[0]],
                            "held_seats": holds_by_flight.get(row[0], 0),
                        }
                        for row in inventory_rows
                    ],
                }
            if domain == "slots":
                capacity_rows = conn.execute(
                    "SELECT iata, hourly_capacity FROM slots.airport ORDER BY iata"
                ).fetchall()
                slot_rows = conn.execute(
                    "SELECT airport, flight_no, movement, slot_local FROM slots.slot"
                    " WHERE slot_date=? ORDER BY flight_no, movement",
                    day,
                ).fetchall()
                return {
                    "capacity": {row[0]: row[1] for row in capacity_rows},
                    "slots": [
                        {
                            "airport": row[0],
                            "flight_no": row[1],
                            "movement": row[2],
                            "slot_local": row[3].isoformat(),
                        }
                        for row in slot_rows
                    ],
                }
        raise ValueError(f"Unknown domain: {domain}")


# Table, key columns and operating-day column per action table. The columns an action may
# compare or change are actions.WRITABLE_COLUMNS, checked by check_changes before any SQL is built.
_ACTION_TABLES = {
    "flight": ("schedule.flight", ("flight_no",), "flight_date"),
    "duty": ("crew.duty", ("duty_id",), "duty_date"),
    "duty_flight": ("crew.duty_flight", ("flight_no",), "duty_date"),
    "assignment": ("crew.assignment", ("crew_id",), "duty_date"),
    "reserve": ("crew.reserve", ("crew_id",), "duty_date"),
    "segment": ("booking.segment", ("locator", "flight_no"), "travel_date"),
    "passenger_notice": ("booking.passenger_notice", ("flight_no", "kind"), "travel_date"),
    "standby_alert": ("crew.standby_alert", ("alert_id",), "duty_date"),
}


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _acquire_transaction_lock(cursor: Any, resource: str) -> None:
    row = cursor.execute(
        "DECLARE @lock_result int;"
        " EXEC @lock_result=sys.sp_getapplock @Resource=?, @LockMode='Exclusive',"
        " @LockOwner='Transaction', @LockTimeout=5000;"
        " SELECT @lock_result;",
        resource,
    ).fetchone()
    if row is None or int(row[0]) < 0:
        raise ValueError("A concurrent Booking action prevented the hold from being created")


def _recovery_hold_receipt(row: Sequence[Any], trace_id: str) -> dict[str, Any]:
    action_id, flight_no, party_size, created_at, expires_at, status = row

    def as_utc(value: datetime) -> str:
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat()

    return {
        "action_id": str(action_id),
        "trace_id": trace_id,
        "flight_no": flight_no,
        "party_count": 1,
        "seat_count": int(party_size),
        "created_at": as_utc(created_at),
        "expires_at": as_utc(expires_at),
        "status": status,
    }


def _day_rows(
    day: date, op: OperatingDay, *, passenger_holds_enabled: bool = False
) -> list[tuple[str, Sequence[tuple[Any, ...]]]]:
    """Parents before children, matching infra/systems/sql/002_operating_day.sql."""
    return [
        (
            "INSERT INTO maintenance.aircraft (registration, fleet_type, seats, base)"
            " VALUES (?, ?, ?, ?)",
            [(a.registration, a.fleet_type, a.seats, a.base) for a in op.aircraft],
        ),
        (
            "INSERT INTO maintenance.aircraft_status (status_date, registration, status)"
            " VALUES (?, ?, ?)",
            [(day, s.registration, s.status) for s in op.statuses],
        ),
        (
            "INSERT INTO schedule.flight (flight_date, flight_no, origin, destination,"
            " std_local, sta_local, aircraft_reg, status, published_std_local,"
            " published_sta_local) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f.flight_date,
                    f.flight_no,
                    f.origin,
                    f.destination,
                    f.std_local,
                    f.sta_local,
                    f.aircraft_reg,
                    f.status,
                    f.published_std_local,
                    f.published_sta_local,
                )
                for f in op.flights
            ],
        ),
        (
            "INSERT INTO crew.crew_member (crew_id, rank, base, qualification) VALUES (?, ?, ?, ?)",
            [(c.crew_id, c.rank, c.base, c.qualification) for c in op.crew],
        ),
        (
            "INSERT INTO crew.duty (duty_date, duty_id, base, start_airport, qualification,"
            " report_local, release_local, fdp_minutes, max_fdp_minutes, pilots_required,"
            " cabin_required) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    day,
                    d.duty_id,
                    d.base,
                    d.start_airport,
                    d.qualification,
                    d.report_local,
                    d.release_local,
                    d.fdp_minutes,
                    d.max_fdp_minutes,
                    d.pilots_required,
                    d.cabin_required,
                )
                for d in op.duties
            ],
        ),
        (
            "INSERT INTO crew.duty_flight (duty_date, flight_no, duty_id) VALUES (?, ?, ?)",
            [(day, no, d.duty_id) for d in op.duties for no in d.flight_nos],
        ),
        (
            "INSERT INTO crew.assignment (duty_date, crew_id, duty_id, rank) VALUES (?, ?, ?, ?)",
            [(day, m.crew_id, d.duty_id, m.rank) for d in op.duties for m in d.members],
        ),
        (
            "INSERT INTO crew.reserve (duty_date, crew_id, base, start_local, end_local,"
            " standby_type) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (day, r.crew_id, r.base, r.start_local, r.end_local, r.standby_type)
                for r in op.reserves
            ],
        ),
        (
            "INSERT INTO booking.flight (flight_date, flight_no, seats, distance_km)"
            " VALUES (?, ?, ?, ?)",
            [(day, i.flight_no, i.seats, i.distance_km) for i in op.inventory],
        ),
        (
            "INSERT INTO booking.pnr"
            + (
                " (travel_date, locator, party_size, synthetic_recovery_opt_in) VALUES (?, ?, ?, ?)"
                if passenger_holds_enabled
                else " (travel_date, locator, party_size) VALUES (?, ?, ?)"
            ),
            [
                (day, p.locator, p.party_size, p.synthetic_recovery_opt_in)
                if passenger_holds_enabled
                else (day, p.locator, p.party_size)
                for p in op.pnrs
            ],
        ),
        (
            "INSERT INTO booking.segment (travel_date, locator, seq, flight_no)"
            " VALUES (?, ?, ?, ?)",
            [(day, p.locator, i, no) for p in op.pnrs for i, no in enumerate(p.flight_nos, 1)],
        ),
        (
            "INSERT INTO slots.airport (iata, hourly_capacity) VALUES (?, ?)",
            sorted(op.slot_capacity.items()),
        ),
        (
            "INSERT INTO slots.slot (slot_date, flight_no, movement, airport, slot_local)"
            " VALUES (?, ?, ?, ?, ?)",
            [(day, s.flight_no, s.movement, s.airport, s.slot_local) for s in op.slots],
        ),
    ]
