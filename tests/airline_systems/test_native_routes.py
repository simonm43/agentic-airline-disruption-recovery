"""The airline systems at each recipient's own interface (native dispatch spec §3): operations
control, the crew and passenger systems, ground handling, EUROCONTROL, Lisbon A-CDM and the slot
coordinator, each played in Dev by one sample system."""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any

from arrow_scenario import ScenarioStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems import event_ids
from airline_systems.app import create_app

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
EVENT = event_ids.event_id(TRACE)
OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # ARROW's identity, the only writer
AGENT = "11111111-2222-3333-4444-555555555555"
RETIME = "4bf92f3577b3-0001-retime-ar101"


def principal(oid: str = OPERATOR) -> dict[str, str]:
    claims = {
        "claims": [
            {"typ": "oid", "val": oid},
            *role_claims(ARROW if oid == OPERATOR else READS),
        ]
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


def system(store: ScenarioStore, name: str) -> TestClient:
    return TestClient(
        create_app(
            store,
            frozenset({OPERATOR, AGENT}),
            system=name,
            writer_oids=frozenset({OPERATOR}),
            mock_confirm_seconds=0,
        )
    )


def at(store: ScenarioStore, hhmm: str) -> str:
    return f"{store.day.isoformat()}T{hhmm}:00"


def version_of(schedule: TestClient, flight_no: str, aircraft_reg: str = "CS-TQA") -> str:
    """The version operations control serves with the flight, as ARROW reads it."""
    rotation = schedule.get(f"/api/aircraft/{aircraft_reg}/rotation", headers=principal())
    assert rotation.status_code == 200, rotation.text
    version: str = next(
        f["version"] for f in rotation.json()["flights"] if f["flight_no"] == flight_no
    )
    return version


def patch(
    schedule: TestClient,
    flight_no: str,
    body: dict[str, Any],
    *,
    key: str,
    if_match: str,
    oid: str = OPERATOR,
) -> Any:
    return schedule.patch(
        f"/api/flights/{flight_no}",
        json=body,
        headers={
            **principal(oid),
            "Idempotency-Key": key,
            "X-Request-Id": key,
            "If-Match": if_match,
        },
    )


def post(client: TestClient, path: str, body: dict[str, Any], request_id: str) -> Any:
    return client.post(path, json=body, headers={**principal(), "X-Request-Id": request_id})


def check_operations_control_retimes_a_flight_once_and_refuses_a_stale_version() -> None:
    store = ScenarioStore(TRACE)
    times = {"std_local": at(store, "13:45"), "sta_local": at(store, "14:40")}
    later = {"std_local": at(store, "14:00"), "sta_local": at(store, "14:55")}
    with system(store, "schedule") as schedule:
        validated = version_of(schedule, "AR101")
        first = patch(schedule, "AR101", times, key=RETIME, if_match=validated)
        again = patch(schedule, "AR101", times, key=RETIME, if_match=validated)
        stale = patch(
            schedule, "AR101", later, key="4bf92f3577b3-0002-retime-ar101", if_match=validated
        )
        served = version_of(schedule, "AR101")

    assert first.status_code == 200, first.text
    assert first.json() == {
        "flight_no": "AR101",
        **times,
        "status": "scheduled",
        "aircraft_reg": "CS-TQA",
        "version": served,
    }
    assert served != validated
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert stale.status_code == 412, stale.text
    assert stale.json() == {"detail": "Source changed: flight AR101"}
    assert store.flights["AR101"]["std_local"] == times["std_local"]
    assert list(store.actions) == [RETIME]


def check_a_reused_idempotency_key_with_another_body_is_refused() -> None:
    store = ScenarioStore(TRACE)
    times = {"std_local": at(store, "13:45"), "sta_local": at(store, "14:40")}
    with system(store, "schedule") as schedule:
        first = patch(schedule, "AR101", times, key=RETIME, if_match=version_of(schedule, "AR101"))
        reused = patch(
            schedule,
            "AR101",
            {"std_local": at(store, "14:00"), "sta_local": at(store, "14:55")},
            key=RETIME,
            if_match=version_of(schedule, "AR101"),
        )

    assert first.status_code == 200, first.text
    assert reused.status_code == 409, reused.text
    assert store.flights["AR101"]["std_local"] == times["std_local"]
    assert list(store.actions) == [RETIME]


def check_operations_control_cancels_a_flight_and_changes_an_aircraft() -> None:
    store = ScenarioStore(TRACE)
    with system(store, "schedule") as schedule:
        cancelled = patch(
            schedule,
            "AR131",
            {"status": "cancelled"},
            key="4bf92f3577b3-0003-cancel-ar131",
            if_match=version_of(schedule, "AR131"),
        )
        swapped = patch(
            schedule,
            "AR101",
            {"aircraft_reg": "CS-TQB"},
            key="4bf92f3577b3-0004-tail-ar101",
            if_match=version_of(schedule, "AR101"),
        )
        moved = version_of(schedule, "AR101", "CS-TQB")

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert swapped.status_code == 200, swapped.text
    assert (swapped.json()["aircraft_reg"], swapped.json()["version"]) == ("CS-TQB", moved)
    assert store.flights["AR131"]["status"] == "cancelled"
    assert store.flights["AR101"]["aircraft_reg"] == "CS-TQB"


def check_network_manager_acknowledges_a_delay_message_and_rejects_nonsense() -> None:
    store = ScenarioStore(TRACE)
    dof = store.day.strftime("%y%m%d")
    delayed, changed = "4bf92f3577b3-0005-dla-ar101", "4bf92f3577b3-0006-chg-ar101"
    with system(store, "slots") as slots:
        ok = post(slots, "/nm/messages", {"text": f"(DLA-AR101-LPPT1050-LPPR-DOF/{dof})"}, delayed)
        tail = post(
            slots,
            "/nm/messages",
            {"text": f"(CHG-AR101-LPPT1030-LPPR-DOF/{dof}-18/REG/CSTQB)"},
            changed,
        )
        bad = post(slots, "/nm/messages", {"text": "hello"}, "4bf92f3577b3-0007-dla")
        nowhere = post(
            slots,
            "/nm/messages",
            {"text": f"(DLA-AR101-ZZZZ1050-LPPR-DOF/{dof})"},
            "4bf92f3577b3-0008-dla",
        )
        unknown = post(
            slots,
            "/nm/messages",
            {"text": f"(DLA-AR999-LPPT1050-LPPR-DOF/{dof})"},
            "4bf92f3577b3-0009-dla",
        )

    assert ok.status_code == 200 and ok.json() == {"status": "ACK"}, ok.text
    assert tail.status_code == 200 and tail.json() == {"status": "ACK"}, tail.text
    for rejected in (bad, nowhere, unknown):
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "REJ" and rejected.json()["reason"], rejected.text
    assert "ZZZZ" in nowhere.json()["reason"], nowhere.text
    assert "AR999" in unknown.json()["reason"], unknown.text
    assert sorted(store.actions) == sorted([delayed, changed])


def check_slot_coordinator_answers_a_change_request_later() -> None:
    store = ScenarioStore(TRACE)
    change, release = "4bf92f3577b3-0010-slot-ar163", "4bf92f3577b3-0011-slot-ar131"
    with system(store, "slots") as slots:
        sent = post(
            slots,
            "/slot-coordinator/requests",
            {
                "airport": "LIS",
                "flight_no": "AR163",
                "type": "change",
                "request": "LIS night quota for AR163",
            },
            change,
        )
        answer = slots.get(f"/slot-coordinator/requests/{change}", headers=principal())
        released = post(
            slots,
            "/slot-coordinator/requests",
            {"airport": "LIS", "flight_no": "AR131", "type": "non_use", "request": "AR131"},
            release,
        )

    assert sent.status_code == 202, sent.text
    assert sent.json() == {"request_id": change, "status": "pending"}
    assert answer.status_code == 200 and answer.json()["status"] == "confirmed", answer.text
    assert released.status_code == 200, released.text
    assert released.json() == {"request_id": release, "status": "confirmed"}


def check_the_airport_and_the_ground_handler_take_their_orders() -> None:
    store = ScenarioStore(TRACE)
    transfer = {
        "from_flight_no": "AR132",
        "to_flight_no": "AR161",
        "passengers": 4,
        "minutes": 40,
        "passport_control": True,
    }
    with system(store, "slots") as slots, system(store, "booking") as booking:
        tobt = post(
            slots,
            "/acdm/flights/AR101/tobt",
            {"airport": "LIS", "tobt": at(store, "10:50")},
            "4bf92f3577b3-0012-tobt-ar101",
        )
        cars = post(
            booking,
            "/ground-handling/transfer-orders",
            {"transfers": [transfer]},
            "4bf92f3577b3-0013-transfer",
        )

    assert tobt.status_code == 200 and tobt.json() == {"status": "accepted"}, tobt.text
    assert cars.status_code == 200, cars.text
    assert cars.json()["status"] == "acknowledged"
    assert "4 passengers" in cars.json()["message"], cars.json()


def check_the_generic_action_endpoint_is_gone_and_writes_need_the_role_and_an_id() -> None:
    store = ScenarioStore(TRACE)
    times = {"std_local": at(store, "13:45"), "sta_local": at(store, "14:40")}
    with system(store, "schedule") as schedule:
        version = version_of(schedule, "AR101")
        generic = schedule.post(
            "/api/actions",
            json={"action_id": RETIME, "kind": "retime_flights", "payload": {}},
            headers=principal(),
        )
        status = schedule.get(f"/api/actions/{RETIME}", headers=principal())
        agent = patch(schedule, "AR101", times, key=RETIME, if_match=version, oid=AGENT)
        unnamed = schedule.patch(
            "/api/flights/AR101", json=times, headers={**principal(), "If-Match": version}
        )

    assert generic.status_code in (404, 405), generic.text
    assert status.status_code in (404, 405), status.text
    assert agent.status_code == 403, agent.text
    assert unnamed.status_code == 400, unnamed.text
    assert store.actions == {}
    assert store.flights["AR101"]["std_local"] == at(store, "10:30")


def check_each_call_is_recorded_with_its_request_id() -> None:
    # The trail joins ARROW's call and the recipient's record by the call's X-Request-Id.
    store = ScenarioStore(TRACE)
    times = {"std_local": at(store, "13:45"), "sta_local": at(store, "14:40")}
    text = f"(DLA-AR101-LPPT1345-LPPR-DOF/{store.day.strftime('%y%m%d')})"
    message = "4bf92f3577b3-0014-dla-ar101"
    with system(store, "schedule") as schedule, system(store, "slots") as slots:
        validated = version_of(schedule, "AR101")
        assert patch(schedule, "AR101", times, key=RETIME, if_match=validated).status_code == 200
        stale = "4bf92f3577b3-0015-retime-ar101"
        assert patch(schedule, "AR101", times, key=stale, if_match=validated).status_code == 412
        assert post(slots, "/nm/messages", {"text": text}, message).status_code == 200

    rows = {
        json.loads(row["request"])["request_id"]: row
        for row in store.calls
        if row["operation"] in {"update_flight", "send_nm_message"}
    }
    assert set(rows) == {RETIME, stale, message}, store.calls
    retimed = rows[RETIME]
    assert (retimed["endpoint"], retimed["outcome"], retimed["caller_oid"]) == (
        "PATCH /api/flights/{flight_no}",
        "ok",
        OPERATOR,
    )
    assert json.loads(retimed["request"]) == {
        "event_id": EVENT,
        "request_id": RETIME,
        "flight_no": "AR101",
        **times,
    }
    assert json.loads(retimed["response"])["std_local"] == times["std_local"]
    assert rows[stale]["outcome"] == "failed"
    assert json.loads(rows[stale]["response"]) == {"error": "Source changed: flight AR101"}
    sent = rows[message]
    assert sent["endpoint"] == "POST /nm/messages"
    assert json.loads(sent["request"]) == {"event_id": EVENT, "request_id": message, "text": text}
    assert json.loads(sent["response"]) == {"status": "ACK"}


class NativeRouteTests(unittest.TestCase):
    def test_operations_control_retimes_a_flight_once_and_refuses_a_stale_version(self) -> None:
        check_operations_control_retimes_a_flight_once_and_refuses_a_stale_version()

    def test_a_reused_idempotency_key_with_another_body_is_refused(self) -> None:
        check_a_reused_idempotency_key_with_another_body_is_refused()

    def test_operations_control_cancels_a_flight_and_changes_an_aircraft(self) -> None:
        check_operations_control_cancels_a_flight_and_changes_an_aircraft()

    def test_network_manager_acknowledges_a_delay_message_and_rejects_nonsense(self) -> None:
        check_network_manager_acknowledges_a_delay_message_and_rejects_nonsense()

    def test_slot_coordinator_answers_a_change_request_later(self) -> None:
        check_slot_coordinator_answers_a_change_request_later()

    def test_the_airport_and_the_ground_handler_take_their_orders(self) -> None:
        check_the_airport_and_the_ground_handler_take_their_orders()

    def test_the_generic_action_endpoint_is_gone_and_writes_need_the_role_and_an_id(
        self,
    ) -> None:
        check_the_generic_action_endpoint_is_gone_and_writes_need_the_role_and_an_id()

    def test_each_call_is_recorded_with_its_request_id(self) -> None:
        check_each_call_is_recorded_with_its_request_id()
