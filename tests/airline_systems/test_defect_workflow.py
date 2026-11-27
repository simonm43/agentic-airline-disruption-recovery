"""Defect-to-candidate business flow through the real REST and MCP boundaries."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from system_callers import ARROW
from test_schedule_system import AIRCRAFT_AGENT, OTHER, TRACE, MemoryStore, principal

from airline_systems import case_ids, event_ids
from airline_systems.app import create_app
from airline_systems.operating_day import OperatingDay
from airline_systems.schedule import ScheduleService, lisbon_today

APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CREW_AGENT = "22222222-2222-2222-2222-222222222222"
BOOKING_AGENT = "33333333-3333-3333-3333-333333333333"
SLOT_AGENT = "44444444-4444-4444-4444-444444444444"
# Maintenance's own reference for the defect it records, never ARROW's case.
EVENT = event_ids.event_id(TRACE)
CASE = case_ids.case_id(TRACE, lisbon_today())


def defect_facts(unavailable_minutes: int = 240) -> dict[str, Any]:
    """The defect as ARROW and the agents name it to the systems: CS-TQA out of service from
    the 09:45 clock for maintenance's estimate."""
    return {
        "type": "aircraft_defect",
        "aircraft_reg": "CS-TQA",
        "unavailable_minutes": unavailable_minutes,
    }


class OperationStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.op: OperatingDay | None = None
        self.defects: dict[str, dict[str, Any]] = {}

    def load_operating_day(self, day: date, op_day: OperatingDay, *, force: bool = False) -> bool:
        self.op = op_day
        return super().load_operating_day(day, op_day, force=force)

    def _day(self, day: date) -> OperatingDay:
        """The day as the day job loads it before any system reads it."""
        if self.op is None:
            ScheduleService(self).ensure_day(day)
        assert self.op
        return self.op

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        existing = self.defects.get(defect["trace_id"])
        if existing and existing != defect:
            raise ValueError("A different defect already uses this trace")
        self.defects[defect["trace_id"]] = defect
        return defect

    def get_defect(self, trace: str) -> dict[str, Any]:
        if trace not in self.defects:
            raise ValueError("Defect not found")
        return self.defects[trace]

    def get_delay(self, trace: str) -> dict[str, Any] | None:
        return None

    def get_weather(self, trace: str) -> dict[str, Any] | None:
        return None

    def event_traces(self, day: date, digits: str) -> list[str]:
        return [trace for trace in self.defects if trace[6:12] == digits]

    def recovery_inventory(self, day: date) -> dict[str, Any]:
        op = self._day(day)
        return {
            "flights": [f.as_json() for f in self.flights if f.flight_date == day],
            "aircraft": [
                {
                    "registration": a.registration,
                    "fleet_type": a.fleet_type,
                    "seats": a.seats,
                    "base": a.base,
                    "status": (
                        "unserviceable"
                        if any(d["aircraft_reg"] == a.registration for d in self.defects.values())
                        else "serviceable"
                    ),
                }
                for a in op.aircraft
            ],
        }

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        self._day(day)
        assert self.op
        if domain == "crew":
            members = {member.crew_id: member for member in self.op.crew}
            return {
                "duties": [
                    {
                        "duty_id": duty.duty_id,
                        "base": duty.base,
                        "qualification": duty.qualification,
                        "report_local": duty.report_local.isoformat(),
                        "release_local": duty.release_local.isoformat(),
                        "flight_nos": list(duty.flight_nos),
                        "pilots_required": duty.pilots_required,
                        "cabin_required": duty.cabin_required,
                        "start_airport": duty.start_airport,
                        "fdp_minutes": duty.fdp_minutes,
                        "max_fdp_minutes": duty.max_fdp_minutes,
                        "members": [
                            {"crew_id": member.crew_id, "rank": member.rank}
                            for member in duty.members
                        ],
                    }
                    for duty in self.op.duties
                ],
                "reserves": [
                    {
                        "crew_id": reserve.crew_id,
                        "base": reserve.base,
                        "start_local": reserve.start_local.isoformat(),
                        "end_local": reserve.end_local.isoformat(),
                        "rank": members[reserve.crew_id].rank,
                        "qualification": members[reserve.crew_id].qualification,
                        "standby_type": reserve.standby_type,
                    }
                    for reserve in self.op.reserves
                ],
            }
        if domain == "booking":
            return {
                "pnrs": [
                    {
                        "locator": pnr.locator,
                        "party_size": pnr.party_size,
                        "flight_nos": list(pnr.flight_nos),
                    }
                    for pnr in self.op.pnrs
                ],
                "inventory": [
                    {
                        "flight_no": inventory.flight_no,
                        "seats": inventory.seats,
                        "distance_km": inventory.distance_km,
                        "passengers": inventory.passengers,
                    }
                    for inventory in self.op.inventory
                ],
            }
        if domain == "slots":
            return {
                "capacity": self.op.slot_capacity,
                "slots": [
                    {
                        "airport": slot.airport,
                        "flight_no": slot.flight_no,
                        "movement": slot.movement,
                        "slot_local": slot.slot_local.isoformat(),
                    }
                    for slot in self.op.slots
                ],
            }
        raise AssertionError(f"Unknown domain: {domain}")


class MissingCrewDutyStore(OperationStore):
    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        inventory = super().domain_inventory(day, domain)
        if domain == "crew":
            inventory["duties"] = [
                {**duty, "flight_nos": ["AR131", "AR132"]}
                for duty in inventory["duties"]
                if "AR101" in duty["flight_nos"]
            ]
        return inventory


class IncompleteCrewStore(OperationStore):
    def __init__(self, missing: str) -> None:
        super().__init__()
        self.missing = missing

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        inventory = super().domain_inventory(day, domain)
        if domain == "crew":
            for duty in inventory["duties"]:
                if "AR101" not in duty["flight_nos"]:
                    continue
                if self.missing == "report":
                    duty["report_local"] = None
                elif self.missing == "all_members":
                    duty["members"] = []
                else:
                    duty["members"] = [
                        member for member in duty["members"] if member["rank"] != self.missing
                    ]
        return inventory


def tool(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = call(client, name, arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def tool_error(client: TestClient, name: str, arguments: dict[str, Any]) -> str:
    """The text of a tool call the system refused."""
    result = call(client, name, arguments)
    assert result.get("isError"), result
    return str(result["content"][0]["text"])


def call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={**principal(AIRCRAFT_AGENT), "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


class DefectWorkflowTests(unittest.TestCase):
    def test_long_delay_retains_time_already_spent_on_crew_duty(self) -> None:
        store = OperationStore()
        with TestClient(create_app(store, frozenset({CREW_AGENT}), system="crew")) as crew:
            response = crew.get(
                "/api/recovery-options/delay/impact",
                params=defect_facts(480),
                headers=principal(CREW_AGENT),
            )
        self.assertEqual(response.status_code, 200, response.text)
        assessment = response.json()
        duty = next(d for d in assessment["duties"] if d["duty_id"] == "D-LIS-079")
        # Recorded report 09:30 to proposed last arrival 22:50; no new reporting agreement.
        self.assertEqual(duty["modeled_fdp_minutes"], 800)
        self.assertEqual(duty["modeled_fdp_limit_minutes"], 720)
        self.assertEqual(duty["status"], "fail")
        self.assertEqual(assessment["constraint_status"], "fail")
        self.assertFalse(assessment["approval_ready"])
        self.assertTrue(all(f.status == "scheduled" for f in store.flights))

    def test_incomplete_crew_assignments_or_report_time_cannot_pass(self) -> None:
        for missing in ("all_members", "CPT", "FO", "CC", "report"):
            with self.subTest(missing=missing):
                store = IncompleteCrewStore(missing)
                with TestClient(create_app(store, frozenset({CREW_AGENT}), system="crew")) as crew:
                    response = crew.get(
                        "/api/recovery-options/delay/impact",
                        params=defect_facts(),
                        headers=principal(CREW_AGENT),
                    )
                self.assertEqual(response.status_code, 200, response.text)
                assessment = response.json()
                self.assertEqual(assessment["constraint_status"], "unknown")
                duty = next(d for d in assessment["duties"] if d["duty_id"] == "D-LIS-079")
                self.assertEqual(duty["status"], "unknown")
                self.assertIsNone(duty["modeled_fdp_limit_minutes"])
                self.assertFalse(assessment["approval_ready"])

    def test_missing_crew_duty_coverage_is_unknown_not_pass(self) -> None:
        store = MissingCrewDutyStore()
        with TestClient(create_app(store, frozenset({CREW_AGENT}), system="crew")) as crew:
            response = crew.get(
                "/api/recovery-options/delay/impact",
                params=defect_facts(),
                headers=principal(CREW_AGENT),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["constraint_status"], "unknown")

    def test_domain_agents_get_grounded_read_only_assessments_from_their_systems(self) -> None:
        store = OperationStore()
        domains = {
            "crew": (CREW_AGENT, "assess_crew_recovery"),
            "booking": (BOOKING_AGENT, "assess_passenger_recovery"),
            "slots": (SLOT_AGENT, "assess_slot_recovery"),
        }
        for domain, (agent, tool_name) in domains.items():
            with TestClient(create_app(store, frozenset({agent}), system=domain)) as system:
                response = system.get(
                    "/api/recovery-options/delay/impact",
                    params=defect_facts(),
                    headers=principal(agent),
                )
                self.assertEqual(response.status_code, 200, response.text)
                assessment = response.json()
                self.assertEqual(assessment["domain"], domain)
                self.assertEqual(assessment["option_id"], "delay")
                self.assertTrue(assessment["assessment_id"])
                self.assertIn(assessment["constraint_status"], {"pass", "fail", "unknown"})
                self.assertIsNone(assessment["total_eur"])
                self.assertFalse(assessment["approval_ready"])
                self.assertIn("limitations", assessment)
                self.assertIsNone(assessment["operational_feasible"])
                self.assertTrue(assessment["changed_flight_ids"])
                if domain == "booking":
                    self.assertEqual(
                        assessment["constraint_status"],
                        "fail" if assessment["misconnected_passengers"] else "pass",
                    )
                if domain == "slots":
                    self.assertEqual(assessment["constraint_status"], "fail")
                    self.assertEqual(assessment["required_additional_delay_minutes"], 15)

                tool_response = system.post(
                    "/mcp",
                    headers={
                        **principal(agent),
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": {
                                "disruption": defect_facts(),
                                "option_id": "delay",
                                "additional_delay_minutes": 0,
                            },
                        },
                    },
                )
                self.assertEqual(tool_response.status_code, 200, tool_response.text)
                tool_result = tool_response.json()["result"]
                self.assertFalse(tool_result.get("isError"), tool_result)
                self.assertEqual(
                    tool_result["structuredContent"]["assessment_id"], assessment["assessment_id"]
                )
                self.assertIn(
                    (f"assess_{domain}", agent, TRACE, "ok"),
                    store.calls,
                )

                adjusted = system.get(
                    "/api/recovery-options/delay/impact",
                    params={**defect_facts(), "additional_delay_minutes": 15},
                    headers=principal(agent),
                )
                self.assertEqual(adjusted.status_code, 200, adjusted.text)
                adjusted_assessment = adjusted.json()
                self.assertEqual(adjusted_assessment["additional_delay_minutes"], 15)
                self.assertEqual(
                    adjusted_assessment["changed_flight_ids"],
                    ["AR101", "AR102", "AR131", "AR132"],
                )
                if domain == "slots":
                    self.assertEqual(adjusted_assessment["constraint_status"], "pass")
                    self.assertTrue(adjusted_assessment["capacity_fit"])
                    bounded = system.get(
                        "/api/recovery-options/delay/impact",
                        params={**defect_facts(), "additional_delay_minutes": 180},
                        headers=principal(agent),
                    )
                    self.assertEqual(bounded.status_code, 200, bounded.text)
                    required = bounded.json()["required_additional_delay_minutes"]
                    self.assertTrue(required is None or required <= 180)

        self.assertEqual(len(store.flights), 900)
        self.assertTrue(all(f.status == "scheduled" for f in store.flights))
        # The assessments only read: nothing was recorded in any system.
        self.assertEqual({event for event, *_ in store.calls if event.startswith("record")}, set())

    def test_operator_reads_the_complete_network_without_changing_the_timetable(self) -> None:
        store = OperationStore()
        with TestClient(create_app(store, frozenset({APP}))) as schedule:
            response = schedule.get("/api/network", headers=principal(APP, ARROW))
            self.assertEqual(response.status_code, 200, response.text)
            network = response.json()
            self.assertEqual(len(network["flights"]), 900)
            self.assertEqual(len(network["aircraft"]), 180)
            self.assertEqual(network["timezone"], "Europe/Lisbon")
            lisbon = next(a for a in network["airports"] if a["iata"] == "LIS")
            self.assertEqual(lisbon["position"], [-9.13, 38.77])
            self.assertTrue(all(f["status"] == "scheduled" for f in network["flights"]))
            self.assertEqual(
                [f["flight_no"] for f in network["flights"] if f["aircraft_reg"] == "CS-TQA"],
                ["AR101", "AR102", "AR131", "AR132"],
            )
            locations = {a["iata"] for a in network["airports"]}
            self.assertTrue(
                all({f["origin"], f["destination"]} <= locations for f in network["flights"])
            )
            self.assertEqual(store.defects, {})
            self.assertIn(("get_network", APP, TRACE, "ok"), store.calls)
            self.assertEqual(schedule.get("/api/network").status_code, 403)
            self.assertEqual(
                schedule.get("/api/network", headers=principal(OTHER)).status_code, 403
            )

    def test_short_defect_cancellation_preserves_aircraft_position(self) -> None:
        store = OperationStore()
        with TestClient(create_app(store, frozenset({AIRCRAFT_AGENT}))) as schedule:
            candidates = tool(schedule, "get_recovery_options", {"disruption": defect_facts(60)})
            self.assertEqual(candidates["affected_flight_ids"], ["AR101"])
            cancel = next(o for o in candidates["options"] if o["kind"] == "cancel")
            self.assertEqual([f["flight_no"] for f in cancel["flights"]], ["AR101", "AR102"])
            retained = [
                f
                for f in store.flights
                if f.aircraft_reg == "CS-TQA" and f.flight_no not in {"AR101", "AR102"}
            ]
            self.assertEqual(retained[0].origin, cancel["flights"][0]["origin"])

    def test_operator_logs_defect_agents_read_it_and_get_grounded_candidates(self) -> None:
        store = OperationStore()
        with (
            TestClient(
                create_app(
                    store,
                    frozenset({APP, AIRCRAFT_AGENT}),
                    system="maintenance",
                    writer_oids=frozenset({APP}),
                )
            ) as maintenance,
            TestClient(create_app(store, frozenset({AIRCRAFT_AGENT}))) as schedule,
        ):
            payload = {
                "trace_id": TRACE,
                "aircraft_reg": "CS-TQA",
                "description": "Fuel-pump defect",
                "unavailable_minutes": 240,
                "user_oid": OTHER,
            }
            response = maintenance.post("/api/defects", json=payload, headers=principal(APP, ARROW))
            self.assertEqual(response.status_code, 201, response.text)
            # Maintenance issues its own operations event id; ARROW's trace stays out of answers.
            self.assertEqual(response.json()["event_id"], EVENT)
            self.assertNotIn("trace_id", response.json())
            defect = tool(maintenance, "get_defect", {"event_id": EVENT})
            self.assertEqual(defect["user_oid"], OTHER)
            self.assertTrue(defect["unavailable_until"].endswith("T13:45:00"))
            self.assertEqual((defect["event_id"], "trace_id" in defect), (EVENT, False))
            candidates = tool(schedule, "get_recovery_options", {"disruption": defect_facts()})
            # Read by the defect's facts: the answer names no record and no case.
            self.assertEqual(("event_id" in candidates, "trace_id" in candidates), (False, False))
            self.assertEqual(candidates["affected_flight_ids"], ["AR101", "AR102"])
            delay = next(o for o in candidates["options"] if o["kind"] == "delay")
            self.assertEqual(
                [f["flight_no"] for f in delay["flights"]], ["AR101", "AR102", "AR131", "AR132"]
            )
            self.assertTrue(delay["flights"][0]["std_local"].endswith("T13:45:00"))
            self.assertTrue(delay["flights"][2]["std_local"].endswith("T16:45:00"))
            self.assertIsNone(delay["feasible"])  # Other domains have not assessed it.
            self.assertIsNone(delay["total_eur"])
            self.assertTrue(all(o["approval_ready"] is False for o in candidates["options"]))
            # If nothing changes, the defect makes CS-TQA's four flights late, by up to 195 min.
            disruption = schedule.get(
                "/api/disruption", params=defect_facts(), headers=principal(AIRCRAFT_AGENT)
            )
            self.assertEqual(disruption.status_code, 200, disruption.text)
            knock_on = disruption.json()["knock_on"]
            self.assertEqual(
                [(f["flight_no"], f["minutes_late"]) for f in knock_on["flights"]],
                [("AR101", 195), ("AR102", 195), ("AR131", 155), ("AR132", 150)],
            )
            self.assertEqual(
                (knock_on["flights_affected"], knock_on["max_minutes_late"], knock_on["aircraft"]),
                (4, 195, ["CS-TQA"]),
            )
            # ARROW's case id is not an operations event: Maintenance refuses it, and an event it
            # never issued.
            refused = tool_error(maintenance, "get_defect", {"event_id": CASE})
            self.assertIn("operations event id", refused)
            unknown = tool_error(maintenance, "get_defect", {"event_id": "EV-000000"})
            self.assertIn("No event EV-000000 is recorded today", unknown)
            self.assertEqual(len(store.flights), 900)
            self.assertTrue(store.flights[0].status == "scheduled")
            self.assertIn(("get_defect", AIRCRAFT_AGENT, TRACE, "ok"), store.calls)

    def test_arrow_records_defects_but_cannot_read_maintenance(self) -> None:
        # ARROW holds Actions.Write but not Maintenance.Read: it records a defect, and reading
        # Maintenance is refused although ARROW is a listed caller (least privilege).
        store = OperationStore()
        with TestClient(
            create_app(store, frozenset({APP}), system="maintenance", writer_oids=frozenset({APP}))
        ) as maintenance:
            recorded = maintenance.post(
                "/api/defects",
                headers=principal(APP, ARROW),
                json={
                    "trace_id": TRACE,
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                    "user_oid": OTHER,
                },
            )
            self.assertEqual(recorded.status_code, 201, recorded.text)
            read = maintenance.get("/api/capabilities", headers=principal(APP, ARROW))
            self.assertEqual(read.status_code, 403, read.text)
            self.assertIn("Maintenance.Read", read.text)
        self.assertEqual(list(store.defects), [TRACE])

    def test_agents_cannot_log_defects_or_forge_operator_actions(self) -> None:
        store = OperationStore()
        with TestClient(
            create_app(
                store,
                frozenset({APP, AIRCRAFT_AGENT}),
                system="maintenance",
                writer_oids=frozenset({APP}),
            )
        ) as client:
            for headers in ({}, principal(AIRCRAFT_AGENT), principal(OTHER)):
                response = client.post(
                    "/api/defects",
                    headers=headers,
                    json={
                        "trace_id": TRACE,
                        "aircraft_reg": "CS-TQA",
                        "description": "Defect",
                        "unavailable_minutes": 240,
                        "user_oid": OTHER,
                    },
                )
                self.assertEqual(response.status_code, 403)
            self.assertEqual(store.defects, {})


if __name__ == "__main__":
    unittest.main()
