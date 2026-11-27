"""Gate A business checks for the Schedule system, run in the cloud image build."""

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
from airline_systems.schedule import Flight, lisbon_today

AIRCRAFT_AGENT = "f3ab4bd3-1ee2-4667-9f75-f918105784b9"
OTHER = "11111111-2222-3333-4444-555555555555"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


class MemoryStore:
    def __init__(self) -> None:
        self.flights: list[Flight] = []
        self.calls: list[tuple[str, str | None, str | None, str]] = []

    def has_day(self, day: date) -> bool:
        return any(f.flight_date == day for f in self.flights)

    def load_operating_day(self, day: date, op_day: OperatingDay, *, force: bool = False) -> bool:
        self.flights.extend(op_day.flights)
        return True

    def flights_for_aircraft(self, day: date, aircraft_reg: str) -> list[Flight]:
        return [f for f in self.flights if f.flight_date == day and f.aircraft_reg == aircraft_reg]

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
        self.calls.append((operation, caller_oid, trace_id, outcome))


def principal(oid: str, roles: tuple[str, ...] = READS) -> dict[str, str]:
    claims = {
        "claims": [
            {"typ": "oid", "val": oid},
            {"typ": "appid", "val": "agent"},
            *role_claims(roles),
        ]
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def mcp_call(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={**headers, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_aircraft_rotation", "arguments": {"aircraft_reg": "cs-tqa"}},
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class ScheduleSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.client = TestClient(create_app(self.store, frozenset({AIRCRAFT_AGENT})))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_aircraft_agent_reads_todays_rotation_and_is_logged(self) -> None:
        body = mcp_call(self.client, principal(AIRCRAFT_AGENT))
        result = body["result"]
        self.assertFalse(result.get("isError"), result)
        rotation = result["structuredContent"]
        self.assertEqual(rotation["aircraft_reg"], "CS-TQA")
        self.assertEqual(rotation["date"], lisbon_today().isoformat())
        self.assertEqual(rotation["as_of_local"], f"{lisbon_today().isoformat()}T09:45:00")
        self.assertEqual(
            [f["flight_no"] for f in rotation["flights"]], ["AR101", "AR102", "AR131", "AR132"]
        )
        self.assertEqual(self.store.calls, [("get_aircraft_rotation", AIRCRAFT_AGENT, TRACE, "ok")])

    def test_anonymous_call_is_refused_and_logged(self) -> None:
        body = mcp_call(self.client, {})
        self.assertTrue(body["result"]["isError"])
        self.assertEqual(self.store.calls, [("get_aircraft_rotation", None, None, "refused")])
        self.assertEqual(self.store.flights, [])

    def test_unlisted_identity_is_refused(self) -> None:
        body = mcp_call(self.client, principal(OTHER))
        self.assertTrue(body["result"]["isError"])
        self.assertIn("not allowed", body["result"]["content"][0]["text"])
        self.assertEqual(self.store.calls[-1][3], "refused")

    def test_listed_agent_without_the_schedule_role_is_refused(self) -> None:
        # Owner-approved app roles: a listed caller also needs this system's role in its token.
        crew_only = principal(AIRCRAFT_AGENT, ("Crew.Read",))
        body = mcp_call(self.client, crew_only)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("Schedule.Read", body["result"]["content"][0]["text"])
        rest = self.client.get("/api/aircraft/CS-TQA/rotation", headers=crew_only)
        self.assertEqual(rest.status_code, 403)
        self.assertEqual([call[3] for call in self.store.calls], ["refused", "refused"])
        self.assertEqual(self.store.flights, [])

    def test_rest_returns_same_rotation_and_refuses_anonymous(self) -> None:
        ok = self.client.get("/api/aircraft/CS-TQA/rotation", headers=principal(AIRCRAFT_AGENT))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["flights"]), 4)
        self.assertEqual(self.client.get("/api/aircraft/CS-TQA/rotation").status_code, 403)

    def test_day_is_seeded_once(self) -> None:
        mcp_call(self.client, principal(AIRCRAFT_AGENT))
        mcp_call(self.client, principal(AIRCRAFT_AGENT))
        self.assertEqual(len(self.store.flights), 900)


if __name__ == "__main__":
    unittest.main()
