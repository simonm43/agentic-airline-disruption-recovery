"""Load today's Lisbon operating day into airrec-ops as the signed-in az user (Dev only).

Usage from the repository root (needs the private SQL endpoint and the venv packages):
    $env:PYTHONPATH='src'; python tools\\systems\\load_day.py

Replaces the loaded day in all five systems, then prints verification counts read back from SQL.
"""

from __future__ import annotations

import time

from azure.identity import AzureCliCredential

from airline_systems.operating_day import build_operating_day
from airline_systems.schedule import lisbon_today
from airline_systems.sql import SqlScheduleStore

SERVER = "sql-airrec-dev-bslgt7jwkw2zu.database.windows.net"
DATABASE = "airrec-ops"

CHECKS = (
    ("flights", "SELECT COUNT(*) FROM schedule.flight WHERE flight_date = ?"),
    (
        "flights per fleet",
        "SELECT a.fleet_type + ' ' + CAST(COUNT(*) AS varchar(8)) FROM schedule.flight f"
        " JOIN maintenance.aircraft a ON a.registration = f.aircraft_reg"
        " WHERE f.flight_date = ? GROUP BY a.fleet_type ORDER BY a.fleet_type",
    ),
    (
        "LIS share %",
        "SELECT CAST(100.0 * SUM(CASE WHEN 'LIS' IN (origin, destination) THEN 1 ELSE 0 END)"
        " / COUNT(*) AS decimal(5, 1)) FROM schedule.flight WHERE flight_date = ?",
    ),
    (
        "LIS max movements/hour vs capacity",
        "SELECT CAST(MAX(n) AS varchar(8)) + ' / ' + CAST(MAX(c) AS varchar(8)) FROM ("
        " SELECT COUNT(*) AS n, MAX(a.hourly_capacity) AS c FROM slots.slot s"
        " JOIN slots.airport a ON a.iata = s.airport"
        " WHERE s.slot_date = ? AND s.airport = 'LIS' GROUP BY DATEPART(hour, s.slot_local)) h",
    ),
    (
        "airport-hours over declared capacity",
        "SELECT COUNT(*) FROM (SELECT s.airport FROM slots.slot s"
        " JOIN slots.airport a ON a.iata = s.airport WHERE s.slot_date = ?"
        " GROUP BY s.airport, DATEPART(hour, s.slot_local), a.hourly_capacity"
        " HAVING COUNT(*) > a.hourly_capacity) x",
    ),
    (
        "flights at coordinated airports without their slots",
        "SELECT COUNT(*) FROM schedule.flight f WHERE f.flight_date = ? AND ("
        " (f.origin IN (SELECT iata FROM slots.airport) AND NOT EXISTS (SELECT 1 FROM slots.slot s"
        "  WHERE s.slot_date = f.flight_date AND s.flight_no = f.flight_no AND s.movement = 'D'))"
        " OR (f.destination IN (SELECT iata FROM slots.airport) AND NOT EXISTS (SELECT 1"
        "  FROM slots.slot s WHERE s.slot_date = f.flight_date AND s.flight_no = f.flight_no"
        "  AND s.movement = 'A')))",
    ),
    (
        "passengers / seats",
        "SELECT CAST(SUM(p.party_size) AS varchar(10)) + ' / '"
        " + CAST((SELECT SUM(seats) FROM booking.flight WHERE flight_date = ?) AS varchar(10))"
        " FROM booking.segment s JOIN booking.pnr p"
        " ON p.travel_date = s.travel_date AND p.locator = s.locator WHERE s.travel_date = ?",
    ),
    (
        "flights with passengers above seats",
        "SELECT COUNT(*) FROM booking.flight b WHERE b.flight_date = ? AND b.seats < ("
        " SELECT COALESCE(SUM(p.party_size), 0) FROM booking.segment s JOIN booking.pnr p"
        " ON p.travel_date = s.travel_date AND p.locator = s.locator"
        " WHERE s.travel_date = b.flight_date AND s.flight_no = b.flight_no)",
    ),
    (
        "connecting PNRs (2 segments)",
        "SELECT COUNT(*) FROM (SELECT locator FROM booking.segment WHERE travel_date = ?"
        " GROUP BY locator HAVING COUNT(*) > 1) x",
    ),
    (
        "flights without a legal crew",
        "SELECT COUNT(*) FROM schedule.flight f"
        " LEFT JOIN crew.duty_flight df"
        " ON df.duty_date = f.flight_date AND df.flight_no = f.flight_no"
        " LEFT JOIN crew.duty d ON d.duty_date = df.duty_date AND d.duty_id = df.duty_id"
        " WHERE f.flight_date = ? AND (d.duty_id IS NULL OR d.fdp_minutes > d.max_fdp_minutes"
        " OR (SELECT COUNT(*) FROM crew.assignment a WHERE a.duty_date = d.duty_date"
        "  AND a.duty_id = d.duty_id AND a.rank IN ('CPT', 'FO')) < d.pilots_required"
        " OR (SELECT COUNT(*) FROM crew.assignment a WHERE a.duty_date = d.duty_date"
        "  AND a.duty_id = d.duty_id AND a.rank IN ('SC', 'CC')) < d.cabin_required)",
    ),
    ("duties", "SELECT COUNT(*) FROM crew.duty WHERE duty_date = ?"),
    ("crew members on duty", "SELECT COUNT(*) FROM crew.assignment WHERE duty_date = ?"),
    ("reserve crew", "SELECT COUNT(*) FROM crew.reserve WHERE duty_date = ?"),
    (
        "aircraft serviceable / fleet",
        "SELECT CAST(SUM(CASE WHEN s.status = 'serviceable' THEN 1 ELSE 0 END) AS varchar(8))"
        " + ' / ' + CAST(COUNT(*) AS varchar(8)) FROM maintenance.aircraft a"
        " LEFT JOIN maintenance.aircraft_status s"
        " ON s.registration = a.registration AND s.status_date = ?",
    ),
    (
        "CS-TQA rotation",
        "SELECT STRING_AGG(flight_no + ' ' + origin + '-' + destination + ' '"
        " + FORMAT(std_local, 'HH:mm') + '-' + FORMAT(sta_local, 'HH:mm'), ', ')"
        " WITHIN GROUP (ORDER BY std_local) FROM schedule.flight"
        " WHERE flight_date = ? AND aircraft_reg = 'CS-TQA'",
    ),
)


def main() -> None:
    day = lisbon_today()
    store = SqlScheduleStore(SERVER, DATABASE, None, "load-day", credential=AzureCliCredential())
    started = time.perf_counter()
    op_day = build_operating_day(day)
    built = time.perf_counter()
    store.load_operating_day(day, op_day, force=True)
    loaded = time.perf_counter()
    print(
        f"operating day {day}: built in {built - started:.1f} s, loaded in {loaded - built:.1f} s"
    )
    with store.connect() as conn:
        for label, query in CHECKS:
            rows = conn.execute(query, *([day] * query.count("?"))).fetchall()
            print(f"{label}: {'; '.join(str(row[0]) for row in rows)}")


if __name__ == "__main__":
    main()
