"""Mocked airline systems at their REST boundary: rebooking plans and checked writes."""

from __future__ import annotations

import base64
import copy
import json
import unittest
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems.app import create_app
from airline_systems.defects import DefectService, recovery_options
from airline_systems.facts import facts_of, reported
from airline_systems.recovery import RebookingService

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AGENT = "11111111-2222-3333-4444-555555555555"


def principal(oid: str, roles: tuple[str, ...] | None = None) -> dict[str, str]:
    granted = roles if roles is not None else ARROW if oid == OPERATOR else READS
    claims = {"claims": [{"typ": "oid", "val": oid}, *role_claims(granted)]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def defect_facts(unavailable_minutes: int = 240) -> dict[str, Any]:
    """CS-TQA's defect as ARROW and the agents name it to the systems: its facts."""
    return {
        "type": "aircraft_defect",
        "aircraft_reg": "CS-TQA",
        "unavailable_minutes": unavailable_minutes,
    }


def defect_options(store: ScenarioStore) -> dict[str, Any]:
    """Schedule's options for CS-TQA out of service for 240 min from the 09:45 clock."""
    return DefectService(store).options(reported(store, facts_of(defect_facts())))


def client_for(store: ScenarioStore, system: str) -> TestClient:
    return TestClient(
        create_app(
            store,
            frozenset({OPERATOR, AGENT}),
            system=system,
            writer_oids=frozenset({OPERATOR}),
        )
    )


def retime(std: str = "13:45", sta: str = "14:40") -> dict[str, str]:
    day = ScenarioStore(TRACE).day.isoformat()
    return {"std_local": f"{day}T{std}:00", "sta_local": f"{day}T{sta}:00"}


def served_version(schedule: TestClient, flight_no: str) -> str:
    """The flight's version as operations control serves it with the rotation ARROW reads."""
    rotation = schedule.get("/api/aircraft/CS-TQA/rotation", headers=principal(OPERATOR))
    assert rotation.status_code == 200, rotation.text
    version: str = next(
        f["version"] for f in rotation.json()["flights"] if f["flight_no"] == flight_no
    )
    return version


def patch_flight(
    schedule: TestClient,
    flight_no: str,
    body: dict[str, Any],
    key: str,
    if_match: str | None,
    headers: dict[str, str] | None = None,
) -> Any:
    sent = {**(headers or principal(OPERATOR)), "Idempotency-Key": key, "X-Request-Id": key}
    if if_match is not None:
        sent["If-Match"] = if_match
    return schedule.patch(f"/api/flights/{flight_no}", json=body, headers=sent)


def submit(client: TestClient, path: str, body: dict[str, Any], key: str, digest: str) -> Any:
    """A change to the crew or passenger system, bound to the plan digest ARROW validated."""
    headers = {
        **principal(OPERATOR),
        "Idempotency-Key": key,
        "X-Request-Id": key,
        "If-Match": digest,
    }
    return client.post(path, json=body, headers=headers)


def check_rebooking_plan_places_every_disrupted_passenger_and_returns_counts_only() -> None:
    # Delay 120: AR102 lands after AR151 leaves (3); a car keeps the London connection (D108).
    for option_id, minutes, disrupted in (("delay", 240, 7), ("cancel", 240, 5), ("delay", 120, 3)):
        store = ScenarioStore(TRACE, unavailable_minutes=minutes)
        with client_for(store, "booking") as client:
            response = client.get(
                f"/api/recovery-options/{option_id}/rebooking-plan",
                params={**defect_facts(minutes), "additional_delay_minutes": 0},
                headers=principal(AGENT),
            )

        assert response.status_code == 200, response.text
        plan: dict[str, Any] = response.json()
        assert plan["disrupted_passengers"] == disrupted, (option_id, minutes, plan)
        assert plan["rebooked_passengers"] + plan["unplaced_passengers"] == disrupted
        assert plan["unplaced_passengers"] == 0
        assert len(plan["plan_digest"]) == 64
        assert "SYN0" not in response.text and "assignments" not in plan
        assert "journeys" not in plan  # agents get counts only


def check_arrow_gets_each_journey_and_agents_get_counts_only() -> None:
    # ARROW's cost estimate (passenger cost spec) needs each affected booking's waits, arrivals and
    # distances; agents still get counts only. No booking reference leaves Booking.
    def plan_as(oid: str, option_id: str) -> dict[str, Any]:
        store = ScenarioStore(TRACE, unavailable_minutes=240)
        with client_for(store, "booking") as client:
            response = client.get(
                f"/api/recovery-options/{option_id}/rebooking-plan",
                params={**defect_facts(240), "additional_delay_minutes": 0},
                headers=principal(oid),
            )
        assert response.status_code == 200, response.text
        assert "SYN0" not in response.text
        result: dict[str, Any] = response.json()
        return result

    def hhmm(value: str | None) -> str:
        return (value or "")[11:16]

    delay = plan_as(OPERATOR, "delay")
    assert delay["seats"] == {"AR101": 174, "AR102": 174, "AR131": 174, "AR132": 174}
    assert [
        (
            j["party_size"],
            j["first_flight"],
            j["rebooked"],
            hhmm(j["departs"]),
            hhmm(j["departs_new"]),
            hhmm(j["arrives_new"]),
            j["flight_km"],
            j["journey_km"],
        )
        for j in delay["journeys"]
    ] == [
        (2, True, False, "10:30", "13:45", "14:40", 277, 277),
        (3, True, True, "12:00", "12:05", "16:15", 277, 439),
        (1, True, False, "14:10", "16:45", "17:30", 221, 221),
        (4, False, True, "17:30", "21:30", "23:55", 1565, 1714),
    ], delay["journeys"]
    lhr = delay["journeys"][3]
    assert lhr["flight_eu"] is False and lhr["journey_eu"] is False
    assert not any(j["cancelled"] for j in delay["journeys"])

    cancel = plan_as(OPERATOR, "cancel")
    assert [
        (
            j["party_size"],
            j["cancelled"],
            j["rebooked"],
            hhmm(j["departs"]),
            hhmm(j["departs_new"]),
            hhmm(j["arrives"]),
            hhmm(j["arrives_new"]),
        )
        for j in cancel["journeys"]
    ] == [
        (2, True, True, "10:30", "10:40", "11:25", "11:35"),
        (3, True, True, "12:00", "12:05", "16:15", "16:15"),
    ], cancel["journeys"]

    assert "journeys" not in plan_as(AGENT, "delay")


def check_arrows_tools_app_gets_journeys_without_writing() -> None:
    # Three container apps: the tools app prices plans (build_plan, what_if, the Passenger
    # agent's exposure) with its own identity, which reads Booking but never writes. Booking gives
    # it each journey, by its own list; an agent still gets counts, and neither may write.
    tools = "66666666-7777-8888-9999-000000000000"
    store = ScenarioStore(TRACE, unavailable_minutes=240)
    app = create_app(
        store,
        frozenset({OPERATOR, AGENT, tools}),
        system="booking",
        writer_oids=frozenset({OPERATOR}),
        journey_reader_oids=frozenset({tools}),
    )
    with TestClient(app) as client:

        def plan_as(oid: str) -> dict[str, Any]:
            response = client.get(
                "/api/recovery-options/delay/rebooking-plan",
                params={**defect_facts(240), "additional_delay_minutes": 0},
                headers=principal(oid, ("Booking.Read",) if oid == tools else None),
            )
            assert response.status_code == 200, response.text
            assert "SYN0" not in response.text
            result: dict[str, Any] = response.json()
            return result

        assert len(plan_as(tools)["journeys"]) == 4
        assert plan_as(tools)["journeys"] == plan_as(OPERATOR)["journeys"]
        assert "journeys" not in plan_as(AGENT)
        notice = client.post(
            "/api/passenger-notices",
            json={"trace_id": TRACE, "flights": ["AR101"], "kind": "delay"},
            headers={**principal(tools, ("Booking.Read",)), "X-Request-Id": "tools-write"},
        )
    assert notice.status_code == 403, notice.text


def check_a_flight_that_leaves_before_the_passenger_lands_is_not_a_later_flight() -> None:
    # Final review: SYN002 (OPO-LIS-MAD) misses AR151 and cannot be rebooked (AR112 and AR153
    # full). AR153 at 19:00 is the last LIS-MAD flight; it counts as a later flight (whatever
    # its seats) only if the passenger lands at least 60 minutes before it.
    def journey_after(landing: tuple[str, str]) -> dict[str, Any]:
        store = ScenarioStore(TRACE, seats={"AR112": 0, "AR153": 0})
        day = store.day.isoformat()
        flights = [
            {
                **store.flights["AR102"],
                "std_local": f"{day}T{landing[0]}:00",
                "sta_local": f"{day}T{landing[1]}:00",
            }
        ]
        plan = RebookingService(store).plan_for(flights)
        [syn002] = [j for j in plan["journeys"] if j["journey_km"] == 439]
        return syn002

    late = journey_after(("18:35", "19:30"))
    assert (late["rebooked"], late["first_flight"], late["next_departure"]) == (
        False,
        False,
        None,
    ), late
    assert late["departs"].endswith("T14:00:00")  # the missed AR151
    early = journey_after(("16:35", "17:30"))
    assert early["next_departure"].endswith("T19:00:00"), early  # AR153, 90 min after landing


def check_longer_connection_is_not_a_disruption() -> None:
    # SYN005 connects from AR112 onto the delayed AR131: the layover grows but still works.
    store = ScenarioStore(TRACE, unavailable_minutes=330)
    store.segments["SYN005"] = ["AR112", "AR131"]
    store.party["SYN005"] = 2
    params = {**defect_facts(330), "additional_delay_minutes": 0}
    with client_for(store, "booking") as client:
        plan = client.get(
            "/api/recovery-options/delay/rebooking-plan",
            params=params,
            headers=principal(AGENT),
        )
        assessed = client.get(
            "/api/recovery-options/delay/impact", params=params, headers=principal(AGENT)
        )

    assert plan.status_code == 200 and assessed.status_code == 200, (plan.text, assessed.text)
    misconnected = assessed.json()["misconnected_passengers"]
    assert misconnected == 7, assessed.json()
    assert plan.json()["disrupted_passengers"] == misconnected, plan.json()
    assert plan.json()["rebooked_passengers"] + plan.json()["unplaced_passengers"] == misconnected


def check_a_car_keeps_a_connection_the_delay_shortened() -> None:
    # Owner (D108): at Lisbon a car keeps a connection a delay left at least 15 minutes,
    # 20 through passport control. 120 min out of service: AR102 lands after AR151 to Madrid
    # leaves (SYN002, 3 rebooked); AR132 lands 40 min before AR161 to London (SYN004, 4 by car).
    store = ScenarioStore(TRACE, unavailable_minutes=120)
    params = {**defect_facts(120), "additional_delay_minutes": 0}
    with client_for(store, "booking") as client:
        plan = client.get(
            "/api/recovery-options/delay/rebooking-plan",
            params=params,
            headers=principal(AGENT),
        )
        assessed = client.get(
            "/api/recovery-options/delay/impact", params=params, headers=principal(AGENT)
        )

    assert plan.status_code == 200 and assessed.status_code == 200, (plan.text, assessed.text)
    body = plan.json()
    assert (
        body["disrupted_passengers"],
        body["rebooked_passengers"],
        body["transferred_passengers"],
    ) == (3, 3, 4), body
    assert body["transfers"] == [
        {
            "from_flight_no": "AR132",
            "to_flight_no": "AR161",
            "passengers": 4,
            "minutes": 40,
            "passport_control": True,
        }
    ]
    assert assessed.json()["misconnected_passengers"] == 3, assessed.json()
    assert "SYN0" not in plan.text


def check_a_car_needs_15_minutes_or_20_through_passport_control() -> None:
    def after(flight_no: str, std: str, sta: str) -> tuple[int, int]:
        store = ScenarioStore(TRACE)
        day = store.day.isoformat()
        flights = [
            {
                **store.flights[flight_no],
                "std_local": f"{day}T{std}:00",
                "sta_local": f"{day}T{sta}:00",
            }
        ]
        plan = RebookingService(store).plan_for(flights)
        return plan["disrupted_passengers"], plan["transferred_passengers"]

    # SYN002 (3) Porto-Lisbon-Madrid stays inside Schengen; AR151 leaves at 14:00.
    assert after("AR102", "12:47", "13:42") == (0, 3)  # 18 min
    assert after("AR102", "12:51", "13:46") == (3, 0)  # 14 min
    # SYN004 (4) Faro-Lisbon-London crosses passport control; AR161 leaves at 17:30.
    assert after("AR132", "16:23", "17:08") == (0, 4)  # 22 min
    assert after("AR132", "16:28", "17:13") == (4, 0)  # 17 min


def check_arrow_needs_the_write_role_to_change_a_system_or_see_journeys() -> None:
    # Being the listed writer is not enough: ARROW's token must also carry Actions.Write.
    reads_only = tuple(role for role in ARROW if role != "Actions.Write")
    store = ScenarioStore(TRACE)
    with client_for(store, "schedule") as client:
        refused = patch_flight(
            client,
            "AR101",
            retime(),
            "4bf92f3577b3-0001-retime-ar101",
            served_version(client, "AR101"),
            principal(OPERATOR, reads_only),
        )
    assert refused.status_code == 403, refused.text
    assert "Actions.Write" in refused.text
    assert store.actions == {}
    assert store.flights["AR101"]["std_local"].endswith("T10:30:00")

    with client_for(ScenarioStore(TRACE, unavailable_minutes=240), "booking") as client:
        plan = client.get(
            "/api/recovery-options/delay/rebooking-plan",
            params={**defect_facts(240), "additional_delay_minutes": 0},
            headers=principal(OPERATOR, reads_only),
        )
    assert plan.status_code == 200, plan.text
    assert "journeys" not in plan.json()


def check_a_flight_changed_after_its_version_was_checked_is_refused_without_writing() -> None:
    # The version check and the write are two steps; the conditional write still refuses a
    # flight someone else changed in between (native dispatch spec §3).
    class RacingStore(ScenarioStore):
        def apply_action(self, request: Any, changes: Any, receipt: Any) -> Any:
            self.flights["AR101"]["std_local"] = self.flights["AR101"]["std_local"].replace(
                "10:30", "11:00"
            )
            return super().apply_action(request, changes, receipt)

    store = RacingStore(TRACE)
    with client_for(store, "schedule") as client:
        response = patch_flight(
            client,
            "AR101",
            retime(),
            "4bf92f3577b3-0002-retime-ar101",
            served_version(client, "AR101"),
        )

    assert response.status_code == 412, response.text
    assert response.json()["detail"] == "Source changed: flight AR101", response.text
    assert store.flights["AR101"]["std_local"].endswith("T11:00:00")


def check_changed_booking_is_refused_without_exposing_its_locator() -> None:
    class RacingStore(ScenarioStore):
        def apply_action(self, request: Any, changes: Any, receipt: Any) -> Any:
            self.segments["SYN002"] = ["AR112", "AR151"]  # someone else rebooked it meanwhile
            return super().apply_action(request, changes, receipt)

    store = RacingStore(TRACE)
    body, digest = rebook(store)
    with client_for(store, "booking") as client:
        response = submit(client, "/api/rebookings", body, "4bf92f3577b3-0009-rebook", digest)

    assert response.status_code == 412, response.text
    assert "booking" in response.json()["detail"], response.json()
    assert "SYN0" not in response.text


def rebook(store: ScenarioStore) -> tuple[dict[str, Any], str]:
    """The delay option's rebooking as ARROW sends it, and the plan digest it validated."""
    delay = next(o for o in defect_options(store)["options"] if o["option_id"] == "delay")
    originals = [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]
    plan = RebookingService(store).plan_for(delay["flights"], originals=originals)
    body = {
        "option_id": "delay",
        "additional_delay_minutes": 0,
        "flights": delay["flights"],
        "originals": originals,
    }
    return body, plan["plan_digest"]


def check_two_cases_cannot_both_take_the_last_seats() -> None:
    # Review: two cases execute at once (D110). Both validated AR112's last 3 seats for
    # their passengers; the other case's rebooking (2 passengers) was written first. This one is
    # refused and AR112 is not overbooked.
    class RacingStore(ScenarioStore):
        def apply_action(self, request: Any, changes: Any, receipt: Any) -> Any:
            if request["kind"] == "rebook_passengers":
                self.party["SYN006"] = 2
                self.segments["SYN006"] = ["AR112"]
            return super().apply_action(request, changes, receipt)

    store = RacingStore(TRACE, seats={"AR112": 3})
    body, digest = rebook(store)
    with client_for(store, "booking") as client:
        response = submit(client, "/api/rebookings", body, "4bf92f3577b3-0020-rebook", digest)

    assert response.status_code == 412, response.text
    assert "AR112" in response.json()["detail"], response.json()
    assert "SYN0" not in response.text
    assert store.segments["SYN002"] == ["AR102", "AR151"]
    on_ar112 = sum(store.party[pnr] for pnr, legs in store.segments.items() if "AR112" in legs)
    assert on_ar112 <= 3, store.segments


def check_a_write_compares_only_known_columns_and_never_writes_blind() -> None:
    # Review: request keys must never become SQL column names, and a write must not
    # happen unconditionally: a flight change without the version ARROW validated is refused.
    store = ScenarioStore(TRACE)
    with client_for(store, "schedule") as client:
        version = served_version(client, "AR101")
        injected = patch_flight(
            client,
            "AR101",
            {"status = status OR 1 = 1 --": "scheduled"},
            "4bf92f3577b3-0021-retime-ar101",
            version,
        )
        blind = patch_flight(client, "AR101", retime(), "4bf92f3577b3-0022-retime-ar101", None)

    assert injected.status_code == 400, injected.text
    assert blind.status_code == 412, blind.text
    assert store.flights["AR101"]["std_local"].endswith("T10:30:00")
    assert store.actions == {}


def check_a_malformed_change_is_refused_with_its_missing_field() -> None:
    # A 409 is final for ARROW's executor; a 500 would be retried as a temporary failure.
    store = ScenarioStore(TRACE)
    delay = next(o for o in defect_options(store)["options"] if o["option_id"] == "delay")
    with client_for(store, "crew") as client:
        response = submit(
            client,
            "/api/duty-changes",
            {"flights": delay["flights"]},
            "4bf92f3577b3-0023-duties",
            "a" * 64,
        )

    assert response.status_code == 409, response.text
    assert "originals" in response.json()["detail"], response.text
    assert store.actions == {}


def check_a_system_reads_and_advances_only_its_own_receipts() -> None:
    store = ScenarioStore(TRACE)
    request_id = "4bf92f3577b3-0024-slot-request"
    retimed = "4bf92f3577b3-0024-retime-ar101"

    def app(system: str) -> TestClient:
        return TestClient(
            create_app(
                store,
                frozenset({OPERATOR}),
                system=system,
                writer_oids=frozenset({OPERATOR}),
                mock_confirm_seconds=0,
            )
        )

    with app("slots") as slots, app("schedule") as schedule:
        sent = slots.post(
            "/slot-coordinator/requests",
            json={
                "airport": "LIS",
                "flight_no": "AR163",
                "type": "change",
                "request": "LIS night quota for AR163",
            },
            headers={**principal(OPERATOR), "X-Request-Id": request_id},
        )
        assert sent.json()["status"] == "pending", sent.text
        elsewhere = schedule.get(
            f"/slot-coordinator/requests/{request_id}", headers=principal(OPERATOR)
        )
        assert elsewhere.status_code == 404, elsewhere.text
        assert store.actions[request_id]["receipt"]["status"] == "pending"
        moved = patch_flight(
            schedule, "AR101", retime(), retimed, served_version(schedule, "AR101")
        )
        assert moved.status_code == 200, moved.text
        foreign = slots.get(f"/slot-coordinator/requests/{retimed}", headers=principal(OPERATOR))
        own = slots.get(f"/slot-coordinator/requests/{request_id}", headers=principal(OPERATOR))

    assert foreign.status_code == 404, foreign.text
    assert own.status_code == 200 and own.json()["status"] == "confirmed", own.text


def check_a_later_delay_is_measured_from_the_published_schedule() -> None:
    # Review: a retime moves the estimate, never the published timetable. The defect case
    # moved AR101 10:30-11:25 to 13:45-14:40; a later 60-minute delay lands SYN001 (2) at 15:40,
    # 255 minutes after its published arrival (EU261 3 h), not 60 minutes.
    store = ScenarioStore(TRACE)
    delay = next(o for o in defect_options(store)["options"] if o["option_id"] == "delay")
    with client_for(store, "schedule") as schedule:
        versions = {
            f["flight_no"]: served_version(schedule, f["flight_no"]) for f in delay["flights"]
        }
        for flight in delay["flights"]:
            retimed = patch_flight(
                schedule,
                flight["flight_no"],
                {"std_local": flight["std_local"], "sta_local": flight["sta_local"]},
                f"4bf92f3577b3-0025-retime-{flight['flight_no'].lower()}",
                versions[flight["flight_no"]],
            )
            assert retimed.status_code == 200, retimed.text
        rotation = schedule.get("/api/aircraft/CS-TQA/rotation", headers=principal(AGENT)).json()
    ar101 = next(f for f in rotation["flights"] if f["flight_no"] == "AR101")
    assert (ar101["std_local"][11:16], ar101["published_std_local"][11:16]) == ("13:45", "10:30")

    # Another case's delay, as it names it to Booking: AR101, already retimed, 60 min later.
    later = {
        "type": "flight_delay",
        "flight_no": "AR101",
        "delay_minutes": 60,
        "cause": "staff_shortage",
    }
    with client_for(store, "booking") as booking:
        plan = booking.get(
            "/api/recovery-options/delay/rebooking-plan",
            params={**later, "additional_delay_minutes": 0},
            headers=principal(OPERATOR),
        )

    assert plan.status_code == 200, plan.text
    [syn001] = [j for j in plan.json()["journeys"] if j["party_size"] == 2 and j["first_flight"]]
    assert (
        syn001["departs"][11:16],
        syn001["departs_new"][11:16],
        syn001["arrives"][11:16],
        syn001["arrives_new"][11:16],
    ) == ("10:30", "14:45", "11:25", "15:40"), syn001


def check_crew_update_is_refused_when_the_duty_changed_after_validation() -> None:
    store = ScenarioStore(TRACE)
    delay = next(o for o in defect_options(store)["options"] if o["option_id"] == "delay")
    originals = [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]
    with client_for(store, "crew") as client:
        plan = client.get(
            "/api/recovery-options/delay/duty-plan",
            params={**defect_facts(240), "additional_delay_minutes": 0},
            headers=principal(AGENT),
        )
        assert plan.status_code == 200, plan.text
        assert len(plan.json()["plan_digest"]) == 64
        changed = store.duties["D-LIS-001"]["release_local"].replace("16:50", "17:20")
        store.duties["D-LIS-001"]["release_local"] = changed  # crew control re-planned meanwhile
        response = submit(
            client,
            "/api/duty-changes",
            {"flights": delay["flights"], "originals": originals},
            "4bf92f3577b3-0010-duties",
            plan.json()["plan_digest"],
        )

    assert response.status_code == 412, response.text
    assert response.json()["detail"].startswith("Source changed"), response.json()
    assert store.duties["D-LIS-001"]["release_local"] == changed


# The reserve case's delay, as ARROW and the agents name it to the systems.
RESERVE_DELAY = {
    "type": "flight_delay",
    "flight_no": "AR131",
    "delay_minutes": 60,
    "cause": "staff_shortage",
}


def reserve_case(store: ScenarioStore) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # AR131 +60 pushes D-LIS-001 (limit overridden to 450) to FDP 465; R1 takes AR131/AR132.
    store.duties["D-LIS-001"]["max_fdp_minutes"] = 450
    delay = recovery_options(store, reported(store, facts_of(RESERVE_DELAY)))["options"][0]
    return delay, [dict(store.flights[f["flight_no"]]) for f in delay["flights"]]


def assign_reserve(delay: dict[str, Any], originals: list[dict[str, Any]]) -> dict[str, Any]:
    return {"flights": delay["flights"], "originals": originals, "reserve_option_id": "R1"}


def reserve_digest(client: TestClient) -> str:
    plan = client.get(
        "/api/recovery-options/delay/duty-plan",
        params={**RESERVE_DELAY, "reserve_option_id": "R1"},
        headers=principal(AGENT),
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["breaches_unresolved"] == 0, plan.json()
    digest: str = plan.json()["plan_digest"]
    return digest


def check_reserve_assignment_moves_the_late_legs_to_the_reserve_crew() -> None:
    store = ScenarioStore(TRACE)
    delay, originals = reserve_case(store)
    with client_for(store, "crew") as client:
        response = submit(
            client,
            "/api/duty-changes",
            assign_reserve(delay, originals),
            "4bf92f3577b3-0011-reserve",
            reserve_digest(client),
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted", response.json()
    reserve = store.duties["D-LIS-001-R1"]
    assert sorted(reserve["flight_nos"]) == ["AR131", "AR132"], reserve
    assert sorted(m["crew_id"] for m in reserve["members"]) == [f"R00{n}" for n in range(1, 7)]
    original = store.duties["D-LIS-001"]
    assert sorted(original["flight_nos"]) == ["AR101", "AR102"], original
    assert original["release_local"].endswith("T13:25:00"), original
    assert original["fdp_minutes"] <= original["max_fdp_minutes"], original
    assert store.reserves == []


def check_used_reserve_is_refused() -> None:
    store = ScenarioStore(TRACE)
    delay, originals = reserve_case(store)
    with client_for(store, "crew") as client:
        digest = reserve_digest(client)
        body = assign_reserve(delay, originals)
        first = submit(client, "/api/duty-changes", body, "4bf92f3577b3-0011-reserve", digest)
        assert first.json()["status"] == "accepted", first.json()
        after_first = copy.deepcopy((store.duties, store.reserves))
        second = submit(client, "/api/duty-changes", body, "4bf92f3577b3-0012-reserve", digest)

    assert second.status_code == 412, second.text
    assert second.json()["detail"].startswith("Source changed"), second.json()
    assert (store.duties, store.reserves) == after_first


def check_each_system_declares_its_integration_capabilities() -> None:
    for system in ("schedule", "crew", "booking", "slots"):
        with client_for(ScenarioStore(TRACE), system) as client:
            response = client.get("/api/capabilities", headers=principal(AGENT))

        assert response.status_code == 200, response.text
        capabilities = response.json()["capabilities"]
        assert capabilities
        for declared in capabilities.values():
            assert set(declared) == {
                "query",
                "subscription",
                "conditional_write",
                "rate_limit_per_minute",
            }


def call_tool(client: TestClient, oid: str, name: str, arguments: dict[str, Any]) -> None:
    response = client.post(
        "/mcp",
        headers={**principal(oid), "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["result"].get("isError"), response.json()


def check_booking_calls_are_audited_with_their_request_and_counts_only() -> None:
    """The decision trail shows each system call's request and answer (owner). Booking's
    recorded answer is counts only, even for ARROW's read that returns each journey and for the
    rebooking ARROW carries out: no booking reference or journey reaches the audit."""
    store = ScenarioStore(TRACE, unavailable_minutes=240)
    option = {"option_id": "delay", "additional_delay_minutes": 0}
    asked = {"disruption": defect_facts(), **option}
    with client_for(store, "booking") as client:
        call_tool(client, AGENT, "assess_passenger_recovery", asked)
        call_tool(client, AGENT, "get_rebooking_plan", asked)
        journeys = client.get(
            "/api/recovery-options/delay/rebooking-plan",
            params={**defect_facts(), "additional_delay_minutes": 0},
            headers=principal(OPERATOR),
        )
        assert journeys.status_code == 200 and journeys.json()["journeys"], journeys.text
        body, digest = rebook(store)
        rebooking = "4bf92f3577b3-0030-rebook"
        done = submit(client, "/api/rebookings", body, rebooking, digest)
        assert done.status_code == 200 and done.json()["status"] == "rebooked", done.text

    calls = {(call["endpoint"], call["caller_oid"]): call for call in store.calls}
    rest = "GET /api/recovery-options/{option_id}/rebooking-plan"
    assert set(calls) == {
        ("MCP assess_passenger_recovery", AGENT),
        ("MCP get_rebooking_plan", AGENT),
        (rest, OPERATOR),
        ("POST /api/rebookings", OPERATOR),
    }, list(calls)
    counts = {
        "disrupted_passengers": 7,
        "rebooked_passengers": 7,
        "unplaced_passengers": 0,
        "transferred_passengers": 0,
        "transfers": 0,
    }
    # Agents and ARROW alike name the disruption by its facts, recorded with the option asked
    # about; each call is filed under the trace it was made in.
    for planned in (("MCP get_rebooking_plan", AGENT), (rest, OPERATOR)):
        assert json.loads(calls[planned]["request"]) == {**defect_facts(), **option}, calls[planned]
        assert json.loads(calls[planned]["response"]) == counts, calls[planned]
        assert calls[planned]["trace_id"] == TRACE, calls[planned]
    assessed = json.loads(calls[("MCP assess_passenger_recovery", AGENT)]["response"])
    assert assessed["constraint_status"] in {"pass", "fail"}, assessed
    assert all(isinstance(v, int) for k, v in assessed.items() if k != "constraint_status")
    rebooked = calls[("POST /api/rebookings", OPERATOR)]
    assert json.loads(rebooked["request"])["request_id"] == rebooking, rebooked
    assert json.loads(rebooked["response"])["status"] == "rebooked", rebooked
    for call in store.calls:
        for recorded in (call["request"], call["response"]):
            assert recorded and len(recorded) <= 2000, call
            for personal in ("SYN0", "locator", "journeys", "assignments", "party_size"):
                assert personal not in recorded, (personal, call)


class RecoveryActionApiTests(unittest.TestCase):
    def test_booking_calls_are_audited_with_their_request_and_counts_only(self) -> None:
        check_booking_calls_are_audited_with_their_request_and_counts_only()

    def test_rebooking_plan_places_every_disrupted_passenger_and_returns_counts_only(self) -> None:
        check_rebooking_plan_places_every_disrupted_passenger_and_returns_counts_only()

    def test_arrow_gets_each_journey_and_agents_get_counts_only(self) -> None:
        check_arrow_gets_each_journey_and_agents_get_counts_only()

    def test_arrows_tools_app_gets_journeys_without_writing(self) -> None:
        check_arrows_tools_app_gets_journeys_without_writing()

    def test_a_flight_that_leaves_before_the_passenger_lands_is_not_a_later_flight(self) -> None:
        check_a_flight_that_leaves_before_the_passenger_lands_is_not_a_later_flight()

    def test_longer_connection_is_not_a_disruption(self) -> None:
        check_longer_connection_is_not_a_disruption()

    def test_a_car_keeps_a_connection_the_delay_shortened(self) -> None:
        check_a_car_keeps_a_connection_the_delay_shortened()

    def test_a_car_needs_15_minutes_or_20_through_passport_control(self) -> None:
        check_a_car_needs_15_minutes_or_20_through_passport_control()

    def test_arrow_needs_the_write_role_to_change_a_system_or_see_journeys(self) -> None:
        check_arrow_needs_the_write_role_to_change_a_system_or_see_journeys()

    def test_a_flight_changed_after_its_version_was_checked_is_refused_without_writing(
        self,
    ) -> None:
        check_a_flight_changed_after_its_version_was_checked_is_refused_without_writing()

    def test_changed_booking_is_refused_without_exposing_its_locator(self) -> None:
        check_changed_booking_is_refused_without_exposing_its_locator()

    def test_two_cases_cannot_both_take_the_last_seats(self) -> None:
        check_two_cases_cannot_both_take_the_last_seats()

    def test_a_write_compares_only_known_columns_and_never_writes_blind(self) -> None:
        check_a_write_compares_only_known_columns_and_never_writes_blind()

    def test_a_malformed_change_is_refused_with_its_missing_field(self) -> None:
        check_a_malformed_change_is_refused_with_its_missing_field()

    def test_a_system_reads_and_advances_only_its_own_receipts(self) -> None:
        check_a_system_reads_and_advances_only_its_own_receipts()

    def test_a_later_delay_is_measured_from_the_published_schedule(self) -> None:
        check_a_later_delay_is_measured_from_the_published_schedule()

    def test_crew_update_is_refused_when_the_duty_changed_after_validation(self) -> None:
        check_crew_update_is_refused_when_the_duty_changed_after_validation()

    def test_reserve_assignment_moves_the_late_legs_to_the_reserve_crew(self) -> None:
        check_reserve_assignment_moves_the_late_legs_to_the_reserve_crew()

    def test_used_reserve_is_refused(self) -> None:
        check_used_reserve_is_refused()

    def test_each_system_declares_its_integration_capabilities(self) -> None:
        check_each_system_declares_its_integration_capabilities()
