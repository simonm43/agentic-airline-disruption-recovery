"""Booking flight-summary API workflows expose aggregates, never booking records."""

from __future__ import annotations

import base64
import json
import unittest
from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from system_callers import READS, role_claims

from airline_systems.app import create_app
from airline_systems.operating_day import OperatingDay
from airline_systems.schedule import Flight

BOOKING_AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "11111111-2222-3333-4444-555555555555"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


class MemoryStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None, str | None, str]] = []
        self.lookups: list[tuple[date, str]] = []
        self.summaries: dict[str, dict[str, Any]] = {
            "AR101": {
                "flight_no": "AR101",
                "status": "available",
                "passenger_count": 126,
                "seat_capacity": 180,
                "held_seat_count": 0,
                "available_seats": 54,
                "connecting_passenger_count": 18,
                "connections": [{"flight_no": "AR131", "passenger_count": 18}],
            },
            "AR102": {
                "flight_no": "AR102",
                "status": "unavailable",
                "reason": "Booking inventory is not recorded for this flight.",
                "passenger_count": None,
                "seat_capacity": None,
                "held_seat_count": None,
                "available_seats": None,
                "connecting_passenger_count": None,
                "connections": None,
            },
        }

    def has_day(self, day: date) -> bool:
        return True

    def load_operating_day(self, day: date, op_day: OperatingDay, *, force: bool = False) -> bool:
        return False

    def flights_for_aircraft(self, day: date, aircraft_reg: str) -> list[Flight]:
        return []

    def booking_flight_summary(self, day: date, flight_no: str) -> dict[str, Any] | None:
        self.lookups.append((day, flight_no))
        return self.summaries.get(flight_no)

    def log_call(
        self,
        operation: str,
        caller_oid: str | None,
        caller_name: str | None,
        trace_id: str | None,
        outcome: str,
        detail: str | None = None,
        **recorded: str | None,
    ) -> None:
        self.calls.append((operation, caller_oid, caller_name, trace_id, outcome))


def principal(oid: str) -> dict[str, str]:
    claims = {
        "claims": [
            {"typ": "oid", "val": oid},
            {"typ": "appid", "val": "operator"},
            *role_claims(READS),
        ]
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


class BookingFlightSummaryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.client = TestClient(
            create_app(self.store, frozenset({BOOKING_AGENT}), system="booking")
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_returns_only_aggregate_passenger_seat_and_connection_data(self) -> None:
        response = self.client.get("/api/flights/AR101/summary", headers=principal(BOOKING_AGENT))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "flight_no": "AR101",
                "status": "available",
                "passenger_count": 126,
                "seat_capacity": 180,
                "held_seat_count": 0,
                "available_seats": 54,
                "connecting_passenger_count": 18,
                "connections": [{"flight_no": "AR131", "passenger_count": 18}],
            },
        )
        self.assertNotIn("locator", response.text.lower())
        self.assertNotIn("pnr", response.text.lower())
        self.assertEqual(
            self.store.calls,
            [("get_flight_summary", BOOKING_AGENT, "operator", TRACE, "ok")],
        )

    def test_missing_booking_data_is_explicit_and_unknown_flight_is_not_found(self) -> None:
        missing = self.client.get("/api/flights/AR102/summary", headers=principal(BOOKING_AGENT))
        unknown = self.client.get("/api/flights/AR999/summary", headers=principal(BOOKING_AGENT))
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertEqual(missing.json()["status"], "unavailable")
        self.assertIsNone(missing.json()["passenger_count"])
        self.assertIsNone(missing.json()["connections"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual([flight_no for _, flight_no in self.store.lookups], ["AR102", "AR999"])

    def test_rejects_anonymous_and_unlisted_callers_without_reading_booking_data(self) -> None:
        anonymous = self.client.get("/api/flights/AR101/summary")
        unlisted = self.client.get("/api/flights/AR101/summary", headers=principal(OTHER))
        self.assertEqual(anonymous.status_code, 403)
        self.assertEqual(unlisted.status_code, 403)
        self.assertEqual(self.store.lookups, [])
        self.assertEqual(
            [call[-1] for call in self.store.calls],
            ["refused", "refused"],
        )


if __name__ == "__main__":
    unittest.main()
