"""Booking holds are bounded actions over deterministic recovery evidence."""

from __future__ import annotations

import base64
import json
import unittest
from datetime import date, datetime, time, timedelta
from typing import Any

from day_store import OperatingDayStore
from fastapi.testclient import TestClient
from system_callers import ARROW, READS, role_claims

from airline_systems import event_ids
from airline_systems.actions import TRANSPARENCY
from airline_systems.app import create_app
from airline_systems.schedule import Flight, lisbon_today

OPERATOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "11111111-2222-3333-4444-555555555555"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
ACTION_KEY = "recovery-hold-ar101"


def principal(oid: str) -> dict[str, str]:
    roles = role_claims(ARROW if oid == OPERATOR else READS)
    claims = {"claims": [{"typ": "oid", "val": oid}, {"typ": "appid", "val": "operator"}, *roles]}
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode(),
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }


class BookingActionStore:
    def __init__(
        self,
        *,
        opted_in: bool = True,
        seats: int = 174,
        passengers: int = 150,
        connected: bool = False,
        replacement_present: bool = True,
    ) -> None:
        day = lisbon_today()

        def at(hour: int, minute: int) -> datetime:
            return datetime.combine(day, time(hour, minute))

        self.flights = [
            Flight(day, "AR101", "LIS", "OPO", at(10, 30), at(11, 25), "CS-TQA"),
            Flight(day, "AR102", "OPO", "LIS", at(12, 0), at(12, 55), "CS-TQA"),
            Flight(day, "AR131", "LIS", "FAO", at(14, 10), at(14, 55), "CS-TQA"),
            Flight(day, "AR132", "FAO", "LIS", at(15, 35), at(16, 20), "CS-TQA"),
            Flight(day, "AR111", "LIS", "OPO", at(11, 30), at(12, 25), "CS-TQB"),
        ]
        if not replacement_present:
            self.flights = [flight for flight in self.flights if flight.flight_no != "AR111"]
        itinerary = ["AR101", "AR102"] if connected else ["AR101"]
        self.pnrs = [
            {
                "locator": "SYN001",
                "party_size": 2,
                "flight_nos": itinerary,
                "synthetic_recovery_opt_in": opted_in,
            }
        ]
        self.booking_inventory = [
            {"flight_no": "AR101", "seats": 174, "passengers": 150, "held_seats": 0},
            {"flight_no": "AR102", "seats": 174, "passengers": 90, "held_seats": 0},
            {
                "flight_no": "AR111",
                "seats": seats,
                "passengers": passengers,
                "held_seats": 0,
            },
        ]
        self.defect = {
            "trace_id": TRACE,
            "aircraft_reg": "CS-TQA",
            "operating_date": day.isoformat(),
            "reported_at": datetime.combine(day, time(9, 45)).isoformat(),
            "unavailable_until": datetime.combine(day, time(13, 45)).isoformat(),
        }
        self.holds: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str | None, str | None, str | None, str]] = []

    def recovery_inventory(self, day: date) -> dict[str, Any]:
        return {
            "flights": [flight.as_json() for flight in self.flights if flight.flight_date == day],
            "aircraft": [
                {
                    "registration": "CS-TQA",
                    "fleet_type": "A320neo",
                    "seats": 174,
                    "base": "LIS",
                    "status": "unserviceable",
                },
                {
                    "registration": "CS-TQB",
                    "fleet_type": "A320neo",
                    "seats": 174,
                    "base": "LIS",
                    "status": "serviceable",
                },
            ],
        }

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        if domain != "booking":
            raise AssertionError(f"Unexpected domain: {domain}")
        return {"pnrs": self.pnrs, "inventory": self.booking_inventory}

    def get_defect(self, trace: str) -> dict[str, Any]:
        if trace != TRACE:
            raise ValueError("Defect not found")
        return self.defect

    def get_delay(self, trace: str) -> dict[str, Any] | None:
        return None

    def get_weather(self, trace: str) -> dict[str, Any] | None:
        return None

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        self.defect = defect
        return defect

    def find_recovery_hold(
        self, day: date, trace_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        existing = next(
            (
                item
                for item in self.holds
                if item["trace_id"] == trace_id and item["idempotency_key"] == idempotency_key
            ),
            None,
        )
        if existing is None:
            return None
        return {
            "option_id": existing["option_id"],
            "source_flight_no": existing["source_flight_no"],
            "assessment_version": existing["assessment_version"],
            "receipt": existing["receipt"],
        }

    def create_recovery_hold(self, **hold: Any) -> tuple[dict[str, Any], bool]:
        existing = next(
            (
                item
                for item in self.holds
                if item["trace_id"] == hold["trace_id"]
                and item["idempotency_key"] == hold["idempotency_key"]
            ),
            None,
        )
        if existing is not None:
            if existing != hold:
                raise ValueError("Idempotency key was already used for a different hold")
            return existing["receipt"], False
        created_at = datetime.now().astimezone()
        receipt = {
            "action_id": "hold-0001",
            "trace_id": hold["trace_id"],
            "flight_no": hold["replacement_flight_no"],
            "party_count": 1,
            "seat_count": hold["party_size"],
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=15)).isoformat(),
            "status": "held",
        }
        self.holds.append({**hold, "receipt": receipt})
        return receipt, True

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


class BookingActionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BookingActionStore()
        self.client = TestClient(
            create_app(
                self.store,
                frozenset({OPERATOR, OTHER}),
                system="booking",
                writer_oids=frozenset({OPERATOR}),
                passenger_holds_enabled=True,
            )
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def test_default_booking_app_refuses_holds_even_for_an_allowed_writer(self) -> None:
        with TestClient(
            create_app(
                self.store,
                frozenset({OPERATOR}),
                system="booking",
                writer_oids=frozenset({OPERATOR}),
            )
        ) as client:
            response = client.post(
                "/api/recovery/holds",
                headers=principal(OPERATOR),
                json={
                    "trace_id": TRACE,
                    "option_id": "cancel",
                    "source_flight_no": "AR101",
                    "assessment_version": 12,
                    "idempotency_key": ACTION_KEY,
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.holds, [])

    def request_hold(self, **overrides: Any) -> Any:
        request = {
            "trace_id": TRACE,
            "option_id": "cancel",
            "source_flight_no": "AR101",
            "assessment_version": 12,
            "idempotency_key": ACTION_KEY,
        }
        request.update(overrides)
        return self.client.post(
            "/api/recovery/holds",
            headers=principal(OPERATOR),
            json=request,
        )

    def test_commits_one_opted_in_party_hold_without_changing_confirmed_bookings(self) -> None:
        flights_before = list(self.store.flights)
        pnrs_before = json.loads(json.dumps(self.store.pnrs))
        response = self.request_hold()

        self.assertEqual(response.status_code, 201, response.text)
        receipt = response.json()
        self.assertEqual(receipt["status"], "held")
        self.assertEqual(receipt["flight_no"], "AR111")
        self.assertEqual(receipt["party_count"], 1)
        self.assertEqual(receipt["seat_count"], 2)
        created_at = datetime.fromisoformat(receipt["created_at"])
        expires_at = datetime.fromisoformat(receipt["expires_at"])
        self.assertEqual(expires_at - created_at, timedelta(minutes=15))
        self.assertEqual(len(self.store.holds), 1)
        self.assertEqual(self.store.flights, flights_before)
        self.assertEqual(self.store.pnrs, pnrs_before)
        self.assertNotIn("locator", response.text.lower())
        self.assertNotIn("pnr", response.text.lower())

    def test_reuses_the_original_receipt_for_an_idempotent_retry(self) -> None:
        first = self.request_hold()
        retry = self.request_hold()

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(len(self.store.holds), 1)

    def test_refuses_a_caller_that_is_not_the_operator_writer(self) -> None:
        response = self.client.post(
            "/api/recovery/holds",
            headers=principal(OTHER),
            json={
                "trace_id": TRACE,
                "option_id": "cancel",
                "source_flight_no": "AR101",
                "assessment_version": 12,
                "idempotency_key": ACTION_KEY,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.holds, [])

    def test_blocks_party_without_explicit_synthetic_opt_in(self) -> None:
        self.store.pnrs[0]["synthetic_recovery_opt_in"] = False
        response = self.request_hold()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.holds, [])

    def test_blocks_when_the_alternative_has_no_available_seats(self) -> None:
        self.store.booking_inventory[-1]["passengers"] = self.store.booking_inventory[-1]["seats"]
        response = self.request_hold()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.holds, [])

    def test_blocks_an_alternative_that_breaks_an_existing_connection(self) -> None:
        self.store.pnrs[0]["flight_nos"] = ["AR101", "AR102"]
        response = self.request_hold()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.holds, [])

    def test_blocks_when_booking_or_alternative_flight_data_is_missing(self) -> None:
        self.store.booking_inventory = []
        response = self.request_hold()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.holds, [])

    def test_blocks_when_no_same_day_alternative_flight_is_recorded(self) -> None:
        self.store.flights = [
            flight for flight in self.store.flights if flight.flight_no != "AR111"
        ]
        response = self.request_hold()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.holds, [])


# The OCC playbook's passenger notice (OCC playbook plan; now a notice of new times only): after the
# retime ARROW asks Booking to tell the passengers of flights yet to leave their new times. Booking
# records one notice per flight and day and changes no booking.
NOTICE = "7a1b2c3d4e5f60718293a4b5c6d7e8f9"


def booking(store: OperatingDayStore) -> TestClient:
    return TestClient(
        create_app(
            store, frozenset({OPERATOR, OTHER}), system="booking", writer_oids=frozenset({OPERATOR})
        )
    )


def notice(client: TestClient, body: dict[str, Any], key: str, oid: str = OPERATOR) -> Any:
    return client.post(
        "/api/passenger-notices",
        json={"trace_id": NOTICE, **body},
        headers={**principal(oid), "Idempotency-Key": key, "X-Request-Id": key},
    )


def booked_on(store: OperatingDayStore, flights: set[str]) -> int:
    """Passengers booked on any of the flights, each booking once; a booking with a cancelled
    flight is rebooked, not told a time."""
    return sum(
        store.party[locator]
        for locator, legs in store.segments.items()
        if flights & set(legs)
        and all(store.flights[number]["status"] != "cancelled" for number in legs)
    )


class PassengerNoticeTests(unittest.TestCase):
    def test_passengers_are_told_their_new_times_once_a_day(self) -> None:
        store = OperatingDayStore()
        noon = f"{store.day.isoformat()}T12:00:00"
        later = sorted(n for n, f in store.flights.items() if f["std_local"] > noon)
        first, second = later[:3], later[2:5]
        left = min(store.flights, key=lambda number: store.flights[number]["std_local"])
        # A flight on a booking with one of the first three, cancelled: that booking is rebooked.
        cancelled = next(
            number
            for legs in store.segments.values()
            if set(legs) & set(first)
            for number in legs
            if number not in later[:6]
        )
        store.flights[cancelled]["status"] = "cancelled"
        before = {locator: list(legs) for locator, legs in store.segments.items()}
        with booking(store) as client:
            told = notice(client, {"flights": first}, "7a1b2c3d4e5f-0006-pax-notice")
            retry = notice(client, {"flights": first}, "7a1b2c3d4e5f-0006-pax-notice")
            more = notice(client, {"flights": second}, "7a1b2c3d4e5f-0007-pax-notice")
            again = notice(client, {"flights": first}, "7a1b2c3d4e5f-0008-pax-notice")
            unknown = notice(client, {"flights": ["AR9999"]}, "7a1b2c3d4e5f-0009-pax-notice")
            departed = notice(client, {"flights": [left]}, "7a1b2c3d4e5f-0010-pax-notice")
            dropped = notice(client, {"flights": [cancelled]}, "7a1b2c3d4e5f-0011-pax-notice")
            agent = notice(client, {"flights": later[5:6]}, "7a1b2c3d4e5f-0012-pax-notice", OTHER)
            # The earlier-flight offer is gone: a body that asks for it is not taken.
            offer = notice(
                client,
                {"flights": later[5:6], "offer_early_moves": True, "note": "free text"},
                "7a1b2c3d4e5f-0013-pax-notice",
            )

        self.assertEqual(told.status_code, 200, told.text)
        self.assertEqual(
            {key: told.json()[key] for key in told.json() if key != "message"},
            {
                "status": "accepted",
                "notified_flights": 3,
                "already_notified": [],
                "notified_passengers": booked_on(store, set(first)),
            },
        )
        self.assertIn("their new times", told.json()["message"])
        everyone = sum(
            store.party[locator]
            for locator, legs in store.segments.items()
            if set(first) & set(legs)
        )
        self.assertLess(told.json()["notified_passengers"], everyone)
        self.assertEqual(retry.json(), told.json())
        # A flight notified today is not notified again, and the answer says so.
        self.assertEqual(
            {key: more.json()[key] for key in ("notified_flights", "already_notified")},
            {"notified_flights": 2, "already_notified": [first[2]]},
        )
        self.assertEqual(more.json()["notified_passengers"], booked_on(store, set(second[1:])))
        self.assertEqual(
            {key: again.json()[key] for key in again.json()},
            {
                "status": "accepted",
                "notified_flights": 0,
                "already_notified": first,
                "notified_passengers": 0,
                "message": "3 flights already told today: nobody told twice",
            },
        )
        self.assertEqual(
            store.passenger_notices(store.day),
            {
                **dict.fromkeys(first, "7a1b2c3d4e5f-0006-pax-notice"),
                **dict.fromkeys(second[1:], "7a1b2c3d4e5f-0007-pax-notice"),
            },
        )
        # A notice changes no booking and moves nobody to another flight.
        self.assertEqual(store.segments, before)
        self.assertEqual(
            {row["moved_early"] for row in store.records["passenger_notice"].values()}, {0}
        )
        self.assertEqual(
            (unknown.status_code, unknown.json()["detail"]), (409, "No flight AR9999 today")
        )
        self.assertEqual(departed.status_code, 409, departed.text)
        self.assertEqual(
            departed.json()["detail"],
            f"{left} has left: a notice reaches only a flight yet to leave",
        )
        self.assertEqual(dropped.status_code, 409, dropped.text)
        self.assertIn("cancelled", dropped.json()["detail"])
        self.assertEqual(agent.status_code, 403, agent.text)
        self.assertEqual(offer.status_code, 400, offer.text)
        # The audit keeps only what the interface validated: no free text, no unread fields.
        audited = {
            row["request_id"]: row
            for row in (json.loads(call["request"]) for call in store.calls if call["request"])
            if row.get("request_id")
        }
        self.assertEqual(
            audited["7a1b2c3d4e5f-0006-pax-notice"],
            {
                "event_id": event_ids.event_id(TRACE),
                "request_id": "7a1b2c3d4e5f-0006-pax-notice",
                "flights": first,
                "kind": "new_times",
            },
        )
        self.assertNotIn("flights", audited["7a1b2c3d4e5f-0013-pax-notice"])
        self.assertNotIn("free text", json.dumps(audited))

    def test_a_storm_warns_passengers_then_tells_them_their_times_or_that_nothing_changes(
        self,
    ) -> None:
        # Storm held until confirmed (case summary and held storm spec §2): on approval Booking
        # warns the passengers of possible delays (no new times); once the storm is confirmed it
        # tells them their new times, or, stood down, that no change is expected. One notice of each
        # kind per flight and day, so a warned flight is still told its times.
        store = OperatingDayStore()
        noon = f"{store.day.isoformat()}T12:00:00"
        later = sorted(n for n, f in store.flights.items() if f["std_local"] > noon)
        flights, cancelled = later[:3], later[3]
        store.flights[cancelled]["status"] = "cancelled"
        keys = [f"7a1b2c3d4e5f-00{n}-pax-notice" for n in range(21, 27)]
        with booking(store) as client:
            warned = notice(client, {"flights": flights, "kind": "possible_delay"}, keys[0])
            told = notice(client, {"flights": flights, "kind": "new_times"}, keys[1])
            again = notice(client, {"flights": flights, "kind": "possible_delay"}, keys[2])
            calm = notice(client, {"flights": flights, "kind": "no_change"}, keys[3])
            dropped = notice(client, {"flights": [cancelled], "kind": "possible_delay"}, keys[4])
            unknown = notice(client, {"flights": flights, "kind": "rumour"}, keys[5])

        passengers = booked_on(store, set(flights))
        for answer, what in (
            (warned, "of possible delays"),
            (told, "their new times"),
            (calm, "that no change is expected"),
        ):
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(
                answer.json(),
                {
                    "status": "accepted",
                    "notified_flights": 3,
                    "already_notified": [],
                    "notified_passengers": passengers,
                    "message": f"Told {passengers:,} passengers on 3 flights {what}."
                    f" {TRANSPARENCY}",
                },
            )
        # A second warning of the same flights tells nobody twice.
        self.assertEqual(
            {key: again.json()[key] for key in again.json() if key != "message"},
            {
                "status": "accepted",
                "notified_flights": 0,
                "already_notified": flights,
                "notified_passengers": 0,
            },
        )
        self.assertEqual(
            (dropped.status_code, dropped.json()["detail"]),
            (409, f"{cancelled} is cancelled: a notice reaches only a flight that operates"),
        )
        self.assertEqual(unknown.status_code, 400, unknown.text)
        self.assertEqual(store.passenger_notices(store.day), dict.fromkeys(flights, keys[1]))
        self.assertEqual(
            store.passenger_notices(store.day, "possible_delay"), dict.fromkeys(flights, keys[0])
        )
        self.assertEqual(
            store.passenger_notices(store.day, "no_change"), dict.fromkeys(flights, keys[3])
        )
        audited = [json.loads(call["request"]) for call in store.calls if call["request"]]
        self.assertIn(
            {
                "event_id": event_ids.event_id(TRACE),
                "request_id": keys[0],
                "flights": flights,
                "kind": "possible_delay",
            },
            audited,
        )


if __name__ == "__main__":
    unittest.main()
