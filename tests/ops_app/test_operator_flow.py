"""Operator flows at the API boundary, with scripted external cloud services."""

import base64
import copy
import json
import re
import threading
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pyodbc
from azure.identity import ManagedIdentityCredential
from fastapi.testclient import TestClient

from ops_app.app import create_app
from ops_app.engine import CloudDependencyError, CloudGateway, Operations
from ops_app.store import (
    RUNNABLE,
    AssessmentInactive,
    apply_decision,
    claim,
    decision_receipt,
    merge_execution,
    prepare_decision,
    prepare_review,
)

OPERATOR = "00000000-0000-4000-8000-000000000010"
EXPERT = "7a0d9c6e-1b2f-4c3d-8e9f-0a1b2c3d4e5f"
HOST = "11111111-2222-3333-4444-555555555555"


def headers(oid: str = OPERATOR) -> dict[str, str]:
    principal = base64.b64encode(
        json.dumps(
            {
                "claims": [{"typ": "oid", "val": oid}],
            }
        ).encode()
    ).decode()
    return {
        "X-MS-CLIENT-PRINCIPAL": principal,
        "X-ARROW-Request": "operator",
        "Origin": "https://arrow.test",
    }


def decision_option(
    option_id: str,
    *,
    requires_duty_manager: bool = True,
    crew_status: str = "pass",
    booking_status: str = "pass",
    slots_status: str = "pass",
) -> dict[str, Any]:
    delay = 240 if requires_duty_manager else 0
    return {
        "option_id": option_id,
        "kind": "delay",
        "cost": {"total_eur": 900},
        "requires_duty_manager": requires_duty_manager,
        "flights": [
            {
                "flight_no": "AR101",
                "origin": "LIS",
                "destination": "OPO",
                "std_local": f"2026-11-08T{10 + delay // 60:02d}:{30 + delay % 60:02d}:00",
                "sta_local": f"2026-11-08T{11 + delay // 60:02d}:{25 + delay % 60:02d}:00",
                "status": "scheduled",
                "distance_km": 274,
                "intra_eu": True,
            }
        ],
        "validation": {
            "rebooking": {
                "disrupted_passengers": 0,
                "rebooked_passengers": 0,
                "unplaced_passengers": 0,
            },
            "airport_requests": [],
            "airport_rules_unknown": [],
            "crew_plan": {
                "duties_changed": 0,
                "plan_digest": "c" * 64,
                "breaches_unresolved": 0,
            },
        },
        "domain_assessments": [
            {"domain": "crew", "constraint_status": crew_status},
            {"domain": "booking", "constraint_status": booking_status},
            {
                "domain": "slots",
                "constraint_status": slots_status,
                "capacity_fit": slots_status == "pass",
            },
        ],
    }


def seed_assessment(
    store: Any, options: list[dict[str, Any]], *, status: str = "assessment_ready"
) -> str:
    trace = f"{len(store.docs) + 1:032x}"
    store.create(
        {
            "trace_id": trace,
            "display_id": "DSR-TEST",
            "version": 7,
            "created_at": datetime.now(UTC).isoformat(),
            "operating_date": datetime.now(UTC).date().isoformat(),
            "status": status,
            "stage": "Assessment ready",
            "defect": {
                "aircraft_reg": "CS-TQA",
                "unavailable_minutes": 240,
                "description": "Hydraulic leak",
            },
            # Recorded on ARROW's case only: a plan that runs records it in Maintenance first.
            "disruption": {
                "type": "aircraft_defect",
                "cause": "aircraft_defect",
                "scope": {"flights": [], "aircraft_reg": "CS-TQA"},
            },
            "rotation": {
                "flights": [
                    {
                        "flight_no": "AR101",
                        "origin": "LIS",
                        "destination": "OPO",
                        "std_local": "2026-11-08T10:30:00",
                        "sta_local": "2026-11-08T11:25:00",
                        "status": "scheduled",
                        "aircraft_reg": "CS-TQA",
                    }
                ]
            },
            "options": copy.deepcopy(options),
            "events": [],
            "metrics": {},
        }
    )
    return trace


def decision_payload(
    option_id: str, decision: str, expected_version: int, idempotency_key: str
) -> dict[str, Any]:
    return {
        "option_id": option_id,
        "decision": decision,
        "expected_version": expected_version,
        "idempotency_key": idempotency_key,
    }


class Store:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self._subjects: dict[str, threading.RLock] = {}

    def subject_lock(self, key: str) -> threading.RLock:
        return self._subjects.setdefault(key, threading.RLock())

    def create(self, document: dict[str, Any]) -> None:
        self.docs[document["trace_id"]] = copy.deepcopy(document)

    def find_by_event(self, event_id: str) -> dict[str, Any] | None:
        return next(
            (copy.deepcopy(d) for d in self.docs.values() if d.get("event_id") == event_id), None
        )

    def case_traces(self, day: str, prefix: str) -> list[str]:
        return [
            t for t, d in self.docs.items() if t.startswith(prefix) and d["operating_date"] == day
        ]

    def event_traces(self, day: str, digits: str) -> list[str]:
        return [t for t, d in self.docs.items() if t[6:12] == digits and d["operating_date"] == day]

    def get(self, trace: str) -> dict[str, Any]:
        if trace not in self.docs:
            raise KeyError(trace)
        return copy.deepcopy(self.docs[trace])

    def list(self, day: str) -> list[dict[str, Any]]:
        return [self.get(t) for t, d in self.docs.items() if d.get("operating_date") == day]

    def mutate(
        self, trace: str, update: Any, *, statuses: frozenset[str] = frozenset({"assessing"})
    ) -> None:
        from ops_app.store import apply_change

        doc = self.docs[trace]
        if doc["status"] not in statuses:
            raise AssessmentInactive("This assessment is no longer active")
        change = update(copy.deepcopy(doc))
        apply_change(doc, copy.deepcopy(change), datetime.now(UTC))
        for event in change.events:
            doc["events"].append(
                {**copy.deepcopy(event), "sequence": len(doc["events"]) + 1, "trace_id": trace}
            )

    def update_chat(self, trace: str, update: Any) -> None:
        doc = self.docs[trace]
        doc["chat"] = copy.deepcopy(update(copy.deepcopy(doc.get("chat") or [])))

    def change(self, trace: str, values: dict[str, Any], event: dict[str, Any]) -> None:
        doc = self.docs[trace]
        if doc["status"] != "assessing":
            raise AssessmentInactive("This assessment is no longer active")
        doc.update(copy.deepcopy(values))
        doc["version"] += 1
        doc["events"].append(
            {
                **copy.deepcopy(event),
                "sequence": len(doc["events"]) + 1,
                "at": "2026-11-07T10:00:00Z",
                "trace_id": trace,
            }
        )

    def record_review(self, trace: str, kind: str, outcome: str, actor_oid: str) -> None:
        doc = self.docs[trace]
        execution, event = prepare_review(doc, kind, outcome, actor_oid)
        self.update_execution(trace, execution, event)

    def runnable_traces(self) -> "list[str]":
        return [
            trace
            for trace, doc in self.docs.items()
            if (doc.get("execution") or {}).get("status") in RUNNABLE
        ]

    def claim_execution(self, trace: str, owner: str) -> dict[str, Any] | None:
        claimed = claim(self.docs[trace].get("execution"), owner, datetime.now(UTC))
        if claimed is None:
            return None
        self.docs[trace]["execution"] = claimed
        return self.get(trace)

    def update_execution(
        self,
        trace: str,
        execution: dict[str, Any],
        event: dict[str, Any] | None,
        *,
        owner: str | None = None,
        release: bool = False,
        create: bool = False,
    ) -> None:
        doc = self.docs[trace]
        doc["execution"] = merge_execution(
            doc.get("execution"),
            copy.deepcopy(execution),
            owner=owner,
            release=release,
            create=create,
            now=datetime.now(UTC),
        )
        doc["version"] += 1
        if event is not None:
            doc["events"].append({**copy.deepcopy(event), "sequence": len(doc["events"]) + 1})

    def record_decision(
        self,
        trace: str,
        *,
        option_id: str,
        decision: str,
        expected_version: int,
        idempotency_key: str,
        actor_oid: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        doc = self.docs[trace]
        previous = next(
            (event for event in doc["events"] if event.get("idempotency_key") == idempotency_key),
            None,
        )
        event, is_new = prepare_decision(
            doc,
            option_id=option_id,
            decision=decision,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            actor_oid=actor_oid,
            previous_event=previous,
            comment=comment,
        )
        if is_new:
            apply_decision(doc, event)
            doc["events"].append(
                {
                    **event,
                    "sequence": len(doc["events"]) + 1,
                    "at": "2026-11-07T10:00:00Z",
                    "trace_id": trace,
                }
            )
        return decision_receipt(trace, event)


class Gateway:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.actions: list[tuple[str, str, str]] = []

    def prepare(self, document: dict[str, Any], progress: Any) -> dict[str, Any]:
        return {
            "defect": document["defect"],
            "rotation": {"flights": []},
            "candidates": {"affected_flight_ids": [], "options": []},
        }

    def flight_details(self, flight_no: str, trace: str) -> dict[str, Any]:
        raise AssertionError("This test gateway does not provide flight details")

    def airports(self, trace: str) -> dict[str, Any]:
        return {
            "airports": [
                {"iata": "LIS", "night_departure_from": "23:55", "night_departure_until": "05:50"},
                {"iata": "OPO"},
            ]
        }

    def rotation(self, trace: str, aircraft_reg: str) -> dict[str, Any]:
        return {
            "flights": [
                {"flight_no": "AR101", "std_local": "2026-11-08T10:30:00", "status": "scheduled"}
            ]
        }

    def send(
        self,
        system: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        trace: str,
    ) -> dict[str, Any]:
        self.actions.append((system, method, path))
        if path.startswith("/api/flights/"):
            flight = {
                "flight_no": path.rsplit("/", 1)[1],
                "std_local": "2026-11-08T10:30:00",
                "sta_local": "2026-11-08T11:25:00",
                "status": "scheduled",
                "aircraft_reg": "CS-TQA",
                **(body or {}),
            }
            return {**flight, "version": "0" * 16}
        if path == "/nm/messages":
            return {"status": "ACK"}
        return {"status": "accepted", "message": f"{method} {path} done"}

    def rebooking(
        self, trace: str, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return {
            "disrupted_passengers": 0,
            "rebooked_passengers": 0,
            "unplaced_passengers": 0,
            "plan_digest": "0" * 64,
        }

    def duty_plan(
        self,
        trace: str,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        return {
            "duties_changed": 0,
            "plan_digest": "d" * 64,
            "message": "Crew duties: none",
            "breaches_unresolved": 0,
        }

    def assessment(
        self, domain: str, trace: str, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        return {"domain": domain, "constraint_status": "pass", "capacity_fit": True}

    def orchestrate(
        self, trace: str, brief: dict[str, Any], *, timeout: float = 600
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("Foundry timed out")
        return {
            "outcome": "no_disruption",
            "summary": "No affected departures",
            "submitted_plan_id": None,
        }


class OperatorFlowTests(unittest.TestCase):
    def test_booking_gateway_retries_with_same_key_and_verifies_receipt(self) -> None:
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        receipt = {
            "action_id": "00000000-0000-0000-0000-000000000001",
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "flight_no": "AR111",
            "party_count": 1,
            "seat_count": 2,
            "created_at": "2026-11-07T10:00:00+00:00",
            "expires_at": "2026-11-07T10:15:00+00:00",
            "status": "held",
        }
        calls: list[tuple[str, str, str, dict[str, Any] | None]] = []

        def dependency(
            url: str, scope: str, trace: str, body: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            calls.append((url, scope, trace, body))
            if len(calls) == 1:
                raise CloudDependencyError("Booking response was lost")
            return receipt

        with patch.object(gateway, "request", side_effect=dependency):
            result = gateway.create_passenger_hold(
                "4bf92f3577b34da6a3ce929d0e0e4736",
                "cancel",
                "AR101",
                14,
                "hold-4bf92f3577b34da6a3ce929d0e0e4736-cancel-14",
            )

        self.assertEqual(result, {"status": "held", "receipt": receipt})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0], "https://booking/api/recovery/holds")
        self.assertEqual(calls[0][1], "systems/.default")
        self.assertEqual(
            calls[0][3],
            {
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "option_id": "cancel",
                "source_flight_no": "AR101",
                "assessment_version": 14,
                "idempotency_key": "hold-4bf92f3577b34da6a3ce929d0e0e4736-cancel-14",
            },
        )

    def test_booking_gateway_maps_conflict_to_a_blocked_action(self) -> None:
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        with patch.object(
            gateway,
            "request",
            side_effect=CloudDependencyError(
                "Booking returned HTTP 409", status_code=409, detail="No opt-in"
            ),
        ) as request:
            result = gateway.create_passenger_hold(
                "4bf92f3577b34da6a3ce929d0e0e4736",
                "cancel",
                "AR101",
                14,
                "hold-4bf92f3577b34da6a3ce929d0e0e4736-cancel-14",
            )

        self.assertEqual(result, {"status": "blocked", "reason": "No opt-in"})
        request.assert_called_once()

    def test_approval_carries_out_the_plan_and_rejection_changes_nothing(self) -> None:
        client, store = self.app()
        trace = seed_assessment(
            store, [decision_option("major-delay"), decision_option("major-cancel")]
        )
        with client:
            session = client.get("/api/session", headers=headers()).json()
            self.assertEqual(session["user"]["role"], "OCC Duty Manager")
            self.assertTrue(session["capabilities"]["approve"])

            rejected = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json={
                    **decision_payload("major-cancel", "reject", 7, "decision-reject-0001"),
                    "comment": "Cancelling strands too many passengers",
                },
            )
            self.assertNotIn("execution", store.get(trace))
            # A rejected plan records nothing: no airline system was called.
            self.assertEqual(self.gateway.actions, [])
            approved = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json={
                    **decision_payload("major-delay", "approve", 8, "decision-approve-0001"),
                    "comment": "  Crew control agrees with the delay  ",
                },
            )
            shown = client.get(f"/api/cases/{trace}", headers=headers()).json()

        self.assertEqual(shown["execution"]["status"], "completed")
        self.assertNotIn("rotation", shown)
        self.assertNotIn("airports", shown)
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(approved.status_code, 200, approved.text)
        state = store.get(trace)
        decisions = [
            event for event in state["events"] if event["event"] == "duty_manager_decision"
        ]
        self.assertEqual([event["decision"] for event in decisions], ["reject", "approve"])
        self.assertIn("rejected option major-cancel; nothing changes", decisions[0]["message"])
        self.assertIn(
            "approved option major-delay; ARROW is carrying it out", decisions[1]["message"]
        )
        # Owner: OCC can say why it decided; the comment stays on the audited decision.
        self.assertEqual(
            [event.get("comment") for event in decisions],
            ["Cancelling strands too many passengers", "Crew control agrees with the delay"],
        )
        execution = state["execution"]
        self.assertEqual(execution["option_id"], "major-delay")
        self.assertEqual(execution["approved_by"], OPERATOR)
        self.assertEqual(execution["status"], "completed", state["events"][-1])
        self.assertTrue(execution["actions"])
        # The approved plan records the defect in Maintenance first, then changes the flights.
        self.assertEqual(execution["actions"][0]["kind"], "record_disruption")
        self.assertEqual(self.gateway.actions[0], ("maintenance", "POST", "/api/defects"))
        self.assertEqual(execution["actions"][1]["kind"], "retime_flights")

    def test_a_case_shows_its_plans_against_its_own_original_times(self) -> None:
        # Review: the case gives the times ARROW read for it, the same ones its cost
        # workings price against, so a later case never shows an earlier case's executed delay
        # as its own; they stay the case's after its plan runs.
        client, store = self.app()
        trace = seed_assessment(store, [decision_option("major-delay")])
        with client:
            pending = client.get(f"/api/cases/{trace}", headers=headers()).json()
            client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("major-delay", "approve", 7, "decision-approve-0002"),
            )
            ran = client.get(f"/api/cases/{trace}", headers=headers()).json()

        self.assertEqual(ran["execution"]["status"], "completed")
        for shown in (pending, ran):
            self.assertEqual(list(shown["originals"]), ["AR101"])
            self.assertEqual(shown["originals"]["AR101"]["std_local"], "2026-11-08T10:30:00")
            self.assertEqual(shown["originals"]["AR101"]["sta_local"], "2026-11-08T11:25:00")
        self.assertEqual(pending["options"][0]["flights"][0]["std_local"], "2026-11-08T14:30:00")
        self.assertNotIn("rotation", ran)
        client, store = self.app()
        trace = seed_assessment(
            store, [decision_option("booking-hold", requires_duty_manager=False)]
        )
        with client:
            response = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("booking-hold", "approve", 7, "decision-l3-hold-0001"),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(store.get(trace)["version"], 7)
        self.assertEqual(store.get(trace)["events"], [])

    def test_stale_versions_and_unknown_candidates_do_not_record_decisions(self) -> None:
        client, store = self.app()
        trace = seed_assessment(store, [decision_option("major-delay")])
        with client:
            stale = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("major-delay", "approve", 6, "decision-stale-0001"),
            )
            unknown = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("unknown-option", "approve", 7, "decision-unknown-0001"),
            )

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(store.get(trace)["version"], 7)
        self.assertEqual(store.get(trace)["events"], [])

    def test_idempotent_decision_retries_return_one_audited_record(self) -> None:
        client, store = self.app()
        trace = seed_assessment(store, [decision_option("major-delay")])
        payload = {
            **decision_payload("major-delay", "approve", 7, "decision-idempotent-0001"),
            "comment": "First words",
        }
        with client:
            too_long = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json={
                    **decision_payload("major-delay", "approve", 7, "decision-too-long-0001"),
                    "comment": "x" * 301,
                },
            )
            first = client.post(f"/api/cases/{trace}/approval", headers=headers(), json=payload)
            retry = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json={**payload, "comment": "Other words on a resend"},
            )
            changed = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("major-delay", "reject", 7, "decision-idempotent-0001"),
            )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(first.json(), retry.json())
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(too_long.status_code, 422, too_long.text)
        self.assertEqual(first.json()["version"], 8)
        decisions = [
            event
            for event in store.get(trace)["events"]
            if event["event"] == "duty_manager_decision"
        ]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["comment"], "First words")

    def test_terminal_or_uncleared_domain_assessments_block_decisions(self) -> None:
        client, store = self.app()
        cases = [
            seed_assessment(store, [decision_option("major-failed")], status="failed"),
            seed_assessment(store, [decision_option("major-unknown", crew_status="unknown")]),
            seed_assessment(store, [decision_option("major-blocked", slots_status="fail")]),
        ]
        with client:
            responses = [
                client.post(
                    f"/api/cases/{trace}/approval",
                    headers=headers(),
                    json=decision_payload(option_id, "approve", 7, f"decision-blocked-{index:04d}"),
                )
                for index, (trace, option_id) in enumerate(
                    zip(cases, ("major-failed", "major-unknown", "major-blocked"))
                )
            ]

        self.assertTrue(all(response.status_code == 409 for response in responses))
        for trace in cases:
            state = store.get(trace)
            self.assertEqual(state["version"], 7)
            self.assertEqual(state["events"], [])

    def test_passengers_outside_policy_go_to_review_instead_of_blocking(self) -> None:
        client, store = self.app()
        stranded = decision_option("major-stranded", booking_status="fail")
        stranded["validation"]["rebooking"]["unplaced_passengers"] = 2
        stranded["validation"]["rebooking"]["disrupted_passengers"] = 2
        trace = seed_assessment(store, [stranded])
        with client:
            approved = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("major-stranded", "approve", 7, "decision-rebook-0002"),
            )

        self.assertEqual(approved.status_code, 200, approved.text)

    def test_passenger_services_expert_closes_the_review_and_cannot_approve(self) -> None:
        store = Store()
        client = TestClient(
            create_app(
                store,
                Operations(store, Gateway()),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
                expert_oids=frozenset({EXPERT}),
            )
        )
        trace = seed_assessment(store, [decision_option("major-delay")])
        store.docs[trace]["execution"] = {
            "status": "completed",
            "actions": [],
            "reviews": [
                {"kind": "passenger_services", "passengers": 4, "status": "awaiting_expert"}
            ],
        }
        path = f"/api/cases/{trace}/reviews/passenger_services"
        with client:
            role = client.get("/api/session", headers=headers(EXPERT)).json()
            by_operator = client.post(path, headers=headers(), json={"outcome": "handled"})
            approve = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(EXPERT),
                json=decision_payload("major-delay", "approve", 7, "decision-expert-0001"),
            )
            recorded = client.post(path, headers=headers(EXPERT), json={"outcome": "handled"})
            again = client.post(path, headers=headers(EXPERT), json={"outcome": "handled"})
            opened = client.post(
                "/api/cases",
                headers=headers(EXPERT),
                json={"aircraft_reg": "CS-TQA", "unavailable_minutes": 60, "description": "Leak"},
            )
            rejected_after_execution = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload("major-delay", "reject", 7, "decision-late-0001"),
            )

        self.assertEqual(opened.status_code, 403, opened.text)
        self.assertEqual(rejected_after_execution.status_code, 409, rejected_after_execution.text)
        self.assertFalse(
            any(event.get("decision") == "reject" for event in store.get(trace)["events"])
        )
        self.assertEqual(role["user"]["role"], "Passenger Services")
        self.assertEqual(role["capabilities"], {"approve": False, "review": True})
        self.assertEqual(by_operator.status_code, 403)
        self.assertEqual(approve.status_code, 403)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        self.assertEqual(again.status_code, 409)
        review = store.get(trace)["execution"]["reviews"][0]
        self.assertEqual((review["status"], review["decided_by"]), ("handled", EXPERT))

    def test_non_duty_manager_and_client_role_claim_cannot_record_a_decision(self) -> None:
        client, store = self.app()
        trace = seed_assessment(store, [decision_option("major-delay")])
        with client:
            session = client.get("/api/session", headers=headers(HOST))
            denied = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(HOST),
                json=decision_payload("major-delay", "approve", 7, "decision-host-0001"),
            )
            forged = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json={
                    **decision_payload("major-delay", "approve", 7, "decision-forged-0001"),
                    "role": "OCC Duty Manager",
                },
            )

        self.assertEqual(session.status_code, 403)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(forged.status_code, 422)
        self.assertEqual(store.get(trace)["events"], [])

    def test_flight_details_combine_schedule_aircraft_and_booking_aggregates(self) -> None:
        store = Store()
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        network = {
            "flights": [
                {
                    "flight_no": "AR101",
                    "origin": "LIS",
                    "destination": "OPO",
                    "std_local": "10:00",
                    "sta_local": "11:00",
                    "aircraft_reg": "CS-TQA",
                    "status": "scheduled",
                }
            ],
            "aircraft": [
                {
                    "registration": "CS-TQA",
                    "fleet_type": "A320",
                    "seats": 180,
                    "status": "serviceable",
                }
            ],
        }
        booking = {
            "flight_no": "AR101",
            "status": "available",
            "passenger_count": 126,
            "seat_capacity": 180,
            "held_seat_count": 2,
            "available_seats": 52,
            "connecting_passenger_count": 18,
            "connections": [{"flight_no": "AR131", "passenger_count": 18}],
        }
        calls: list[tuple[str, str, str]] = []

        def dependency(url: str, scope: str, trace: str, body: Any = None) -> dict[str, Any]:
            self.assertEqual(scope, "systems/.default")
            self.assertRegex(trace, r"^[a-f0-9]{32}$")
            self.assertIsNone(body)
            calls.append((url, scope, trace))
            if url == "https://schedule/api/network":
                return network
            if url == "https://booking/api/flights/AR101/summary":
                return booking
            self.fail(f"Unexpected dependency: {url}")

        client = TestClient(
            create_app(
                store,
                Operations(store, gateway),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
            )
        )
        with client, patch.object(gateway, "request", side_effect=dependency):
            response = client.get("/api/flights/AR101", headers=headers())

        self.assertEqual(response.status_code, 200, response.text)
        details = response.json()
        self.assertEqual(details["flight"]["destination"], "OPO")
        self.assertEqual(details["aircraft"]["fleet_type"], "A320")
        self.assertEqual(details["booking"], booking)
        self.assertEqual(details["booking"]["held_seat_count"], 2)
        self.assertEqual(details["booking"]["available_seats"], 52)
        self.assertEqual(
            [call[0] for call in calls],
            [
                "https://schedule/api/network",
                "https://booking/api/flights/AR101/summary",
            ],
        )
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertNotIn("locator", response.text.lower())
        self.assertNotIn("pnr", response.text.lower())

    def test_flight_details_return_unavailable_booking_data_and_reject_unknown_flight(self) -> None:
        store = Store()
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        network = {
            "flights": [{"flight_no": "AR101", "aircraft_reg": "CS-TQA"}],
            "aircraft": [],
        }

        def dependency(url: str, scope: str, trace: str, body: Any = None) -> dict[str, Any]:
            if url == "https://schedule/api/network":
                return network
            if url == "https://booking/api/flights/AR101/summary":
                return {
                    "flight_no": "AR101",
                    "status": "unavailable",
                    "reason": "Booking inventory is not recorded for this flight.",
                    "passenger_count": None,
                    "seat_capacity": None,
                    "held_seat_count": None,
                    "available_seats": None,
                    "connecting_passenger_count": None,
                    "connections": None,
                }
            self.fail(f"Unexpected dependency: {url}")

        client = TestClient(
            create_app(
                store,
                Operations(store, gateway),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
            )
        )
        with client, patch.object(gateway, "request", side_effect=dependency):
            unavailable = client.get("/api/flights/AR101", headers=headers())
            unknown = client.get("/api/flights/AR999", headers=headers())

        self.assertEqual(unavailable.status_code, 200, unavailable.text)
        self.assertEqual(unavailable.json()["booking"]["status"], "unavailable")
        self.assertIsNone(unavailable.json()["booking"]["passenger_count"])
        self.assertIsNone(unavailable.json()["aircraft"])
        self.assertEqual(unknown.status_code, 404)

    def test_flight_details_reject_connection_totals_above_passenger_count(self) -> None:
        store = Store()
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        network = {
            "flights": [{"flight_no": "AR101", "aircraft_reg": "CS-TQA"}],
            "aircraft": [],
        }
        booking = {
            "flight_no": "AR101",
            "status": "available",
            "passenger_count": 4,
            "seat_capacity": 180,
            "held_seat_count": 0,
            "available_seats": 176,
            "connecting_passenger_count": 5,
            "connections": [{"flight_no": "AR131", "passenger_count": 5}],
        }

        def dependency(url: str, scope: str, trace: str, body: Any = None) -> dict[str, Any]:
            return network if url.endswith("/api/network") else booking

        client = TestClient(
            create_app(
                store,
                Operations(store, gateway),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
            )
        )
        with client, patch.object(gateway, "request", side_effect=dependency):
            response = client.get("/api/flights/AR101", headers=headers())

        self.assertEqual(response.status_code, 503)

    def test_flight_details_refuse_callers_before_cloud_requests(self) -> None:
        store = Store()
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        client = TestClient(
            create_app(
                store,
                Operations(store, gateway),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
            )
        )
        with client, patch.object(gateway, "request") as request:
            self.assertEqual(client.get("/api/flights/AR101").status_code, 401)
            self.assertEqual(
                client.get("/api/flights/AR101", headers=headers(HOST)).status_code, 403
            )
            request.assert_not_called()

    def test_network_is_operator_only_and_no_map_token_is_issued(self) -> None:
        client, store = self.app()
        with client:
            self.assertEqual(client.get("/api/network").status_code, 401)
            self.assertEqual(client.get("/api/network", headers=headers(HOST)).status_code, 403)
            # D103: the map draws a bundled outline; Azure Maps is retired, so no token exists.
            self.assertEqual(client.get("/api/maps/token", headers=headers()).status_code, 404)

        credential = ManagedIdentityCredential(client_id=OPERATOR)
        gateway = CloudGateway(
            credential,
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        app = create_app(
            store,
            Operations(store, gateway),
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
        )
        network = {"flights": [{"flight_no": "AR101", "aircraft_reg": "CS-TQA"}]}

        def dependency(url: str, scope: str, trace: str, body: Any = None) -> dict[str, Any]:
            self.assertEqual(url, "https://schedule/api/network")
            self.assertEqual(scope, "systems/.default")
            self.assertRegex(trace, r"^[a-f0-9]{32}$")
            self.assertIsNone(body)
            return network

        with TestClient(app) as client, patch.object(gateway, "request", side_effect=dependency):
            result = client.get("/api/network", headers=headers())
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["flights"][0]["flight_no"], "AR101")
            self.assertEqual(store.docs, {})

    def test_operator_reads_progress_while_each_cloud_dependency_is_pending(self) -> None:
        store = Store()
        gateway = CloudGateway(
            ManagedIdentityCredential(client_id=OPERATOR),
            "systems",
            "https://schedule",
            "https://maintenance",
            "https://foundry",
            booking_url="https://booking",
        )
        client = TestClient(
            create_app(
                store,
                Operations(store, gateway),
                frozenset({OPERATOR}),
                frozenset({HOST}),
                "https://arrow.test",
            )
        )
        stages = []

        def dependency(
            url: str, scope: str, trace: str, body: Any = None, **_: Any
        ) -> dict[str, Any]:
            # Each dependency is still pending when this separate browser read occurs.
            response = client.get(f"/api/cases/{trace}", headers=headers())
            self.assertEqual(response.status_code, 200)
            state = response.json()
            stages.append(state["stage"])
            # Owner: nothing is recorded in an airline system while ARROW assesses a case.
            if not url.startswith("https://foundry/"):
                self.assertIsNone(body, url)
            if url.endswith("/rotation"):
                return {"flights": [], "date": state["operating_date"]}
            if url.startswith("https://schedule/api/recovery-options?"):
                # Operations control is asked by the defect's facts.
                self.assertIn("type=aircraft_defect&aircraft_reg=CS-TQA", url)
                return {"affected_flight_ids": [], "options": []}
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "outcome": "no_disruption",
                                        "summary": "No affected departures",
                                        "submitted_plan_id": None,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }

        with client, patch.object(gateway, "request", side_effect=dependency):
            opened = client.post(
                "/api/cases",
                headers=headers(),
                json={
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
            )
            self.assertEqual(opened.status_code, 202)
            self.assertEqual(len(stages), 3)
            for stage, dependency_name in zip(
                stages,
                (
                    "operations control system",
                    "operations control system",
                    "orchestrator",
                ),
                strict=True,
            ):
                self.assertIn(dependency_name, stage)
            state = client.get(f"/api/cases/{opened.json()['trace_id']}", headers=headers()).json()
            self.assertEqual(state["status"], "no_disruption")
            self.assertGreater(state["metrics"]["total_elapsed_seconds"], 0)

    def test_restart_expiry_is_audited_and_late_host_callback_is_refused(self) -> None:
        client, store = self.app()
        operation = Operations(store, Gateway())
        opened = operation.open(
            {
                "aircraft_reg": "CS-TQA",
                "description": "Fuel-pump defect",
                "unavailable_minutes": 240,
            },
            OPERATOR,
        )
        trace = opened["trace_id"]
        # A previous process persisted this operation, then exited before running its task.
        store.docs[trace]["_deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with client:
            listed = client.get("/api/cases", headers=headers()).json()["cases"][0]
            self.assertEqual(listed["status"], "failed")
            state = client.get(f"/api/cases/{trace}", headers=headers()).json()
            self.assertEqual(state["events"][-1]["event"], "assessment_interrupted")
            self.assertIn("interrupted", state["error"])
            h = {**headers(HOST), "X-ARROW-Run": store.get(trace)["_run_token"]}
            self.assertEqual(client.get(f"/internal/cases/{trace}", headers=h).status_code, 409)
            operation.run(trace)
            self.assertEqual(store.get(trace)["status"], "failed")
            self.assertEqual(
                len(client.get(f"/api/cases/{trace}/events", headers=headers()).json()["events"]), 2
            )

    def test_reconnected_operator_keeps_interrupted_case_when_host_finishes_late(self) -> None:
        for late_failure in (False, True):
            with self.subTest(late_failure=late_failure):
                store = Store()
                gateway = Gateway()
                operation = Operations(store, gateway)
                admitted = TestClient(
                    create_app(
                        store,
                        operation,
                        frozenset({OPERATOR}),
                        frozenset({HOST}),
                        "https://arrow.test",
                    )
                )
                reconnected = TestClient(
                    create_app(
                        store,
                        Operations(store, Gateway()),
                        frozenset({OPERATOR}),
                        frozenset({HOST}),
                        "https://arrow.test",
                    )
                )
                terminal: list[dict[str, Any]] = []
                callback_statuses: list[int] = []
                host_outcomes: list[str] = []

                def delayed_host(
                    trace: str, brief: dict[str, Any], *, timeout: float = 600
                ) -> dict[str, Any]:
                    run_token = store.docs[trace]["_run_token"]
                    pending = reconnected.get(f"/api/cases/{trace}", headers=headers()).json()
                    self.assertEqual(pending["status"], "assessing")
                    self.assertEqual(
                        [event["event"] for event in pending["events"]],
                        ["defect_submitted", "disruption_recorded", "assessment_progress"],
                    )
                    store.docs[trace]["_deadline_at"] = (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat()
                    failed = reconnected.get(f"/api/cases/{trace}", headers=headers()).json()
                    self.assertEqual(failed["status"], "failed")
                    self.assertEqual(failed["options"], [])
                    self.assertEqual(failed["events"][-1]["event"], "assessment_interrupted")
                    terminal.append(failed)
                    callback = reconnected.post(
                        f"/internal/cases/{trace}/events",
                        headers={**headers(HOST), "X-ARROW-Run": run_token},
                        json={
                            "actor": "disruption-detection-agent",
                            "autonomy": "L4",
                            "event": "agent_started",
                            "message": "Late specialist callback",
                        },
                    )
                    callback_statuses.append(callback.status_code)
                    if late_failure:
                        host_outcomes.append("timeout")
                        raise TimeoutError("Late Foundry timeout")
                    host_outcomes.append("completed")
                    return {
                        "outcome": "submitted",
                        "summary": "Late assessment ready",
                        "submitted_plan_id": "late-delay",
                    }

                with admitted, reconnected, patch.object(gateway, "orchestrate", delayed_host):
                    opened = admitted.post(
                        "/api/cases",
                        headers=headers(),
                        json={
                            "aircraft_reg": "CS-TQA",
                            "description": "Fuel-pump defect",
                            "unavailable_minutes": 240,
                        },
                    )
                    self.assertEqual(opened.status_code, 202)
                    trace = opened.json()["trace_id"]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(callback_statuses, [409])
                    self.assertEqual(host_outcomes, ["timeout" if late_failure else "completed"])
                    after = reconnected.get(f"/api/cases/{trace}", headers=headers()).json()
                    self.assertEqual(after, terminal[0])
                    self.assertEqual(
                        reconnected.get(f"/api/cases/{trace}/events", headers=headers()).json(),
                        {"events": terminal[0]["events"]},
                    )
                    refused = reconnected.post(
                        f"/api/cases/{trace}/approval",
                        headers=headers(),
                        json=decision_payload(
                            "late-delay", "approve", after["version"], "late-approval-0001"
                        ),
                    )
                    self.assertEqual(refused.status_code, 409)
                    self.assertEqual(
                        reconnected.get(f"/api/cases/{trace}", headers=headers()).json(), after
                    )

    def test_sql_wake_failure_returns_retryable_status_without_creating_a_case(self) -> None:
        client, store = self.app()
        client = TestClient(client.app, raise_server_exceptions=False)
        with (
            client,
            patch.object(
                store,
                "create",
                side_effect=pyodbc.OperationalError("42000", "Database unavailable (40613)"),
            ),
        ):
            result = client.post(
                "/api/cases",
                headers=headers(),
                json={
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
            )
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.headers["Retry-After"], "10")
            self.assertIn("waking", result.json()["detail"])
            self.assertEqual(store.docs, {})

    def test_cold_store_reads_recover_the_existing_case_without_changing_history(self) -> None:
        client, store = self.app()
        client = TestClient(client.app, raise_server_exceptions=False)
        trace = seed_assessment(store, [decision_option("delay")])
        store.record_decision(
            trace,
            option_id="delay",
            decision="reject",
            expected_version=7,
            idempotency_key="cold-read-history-0001",
            actor_oid=OPERATOR,
        )
        before = copy.deepcopy(store.docs)
        self.assertEqual(len(before[trace]["events"]), 1)
        with client:
            for operation, path in (("list", "/api/cases"), ("get", f"/api/cases/{trace}")):
                with self.subTest(operation=operation):
                    with patch.object(
                        store,
                        operation,
                        side_effect=pyodbc.OperationalError(
                            "42000", "Database unavailable (40613)"
                        ),
                    ):
                        unavailable = client.get(path, headers=headers())
                    self.assertEqual(unavailable.status_code, 503)
                    self.assertEqual(unavailable.headers["Retry-After"], "10")
                    self.assertIn("waking", unavailable.json()["detail"])
                    self.assertEqual(store.docs, before)
                    recovered = client.get(path, headers=headers())
                    self.assertEqual(recovered.status_code, 200, recovered.text)
                    if operation == "list":
                        self.assertEqual(
                            [case["trace_id"] for case in recovered.json()["cases"]], [trace]
                        )
                    else:
                        self.assertEqual(recovered.json()["trace_id"], trace)
                        self.assertEqual(recovered.json()["version"], before[trace]["version"])
                        self.assertEqual(recovered.json()["events"], before[trace]["events"])
                    self.assertEqual(store.docs, before)

    def app(self, fail: bool = False) -> tuple[TestClient, Store]:
        store = Store()
        self.gateway = Gateway(fail)
        app = create_app(
            store,
            Operations(store, self.gateway),
            frozenset({OPERATOR}),
            frozenset({HOST}),
            "https://arrow.test",
        )
        return TestClient(app), store

    def test_operator_shell_serves_every_asset_it_references(self) -> None:
        client, _ = self.app()
        with client:
            shell = client.get("/")
            self.assertEqual(shell.status_code, 200)
            self.assertEqual(client.get("/recovery_view.js").status_code, 200)
            referenced = re.findall(r'(?:src|href)="([a-z_]+\.(?:js|css))"', shell.text)
            self.assertIn("recovery_view.js", referenced)
            # After a release the browser must check every file again: a guessed cache lifetime
            # kept a stale map_draw.js beside a new map_view.js ("markerBoxes is not a
            # function").
            self.assertEqual(shell.headers.get("cache-control"), "no-cache")
            for asset in referenced:
                served = client.get(f"/{asset}")
                self.assertEqual(served.status_code, 200, asset)
                self.assertTrue(served.content.strip(), asset)
                self.assertEqual(served.headers.get("cache-control"), "no-cache", asset)
            self.assertEqual(client.get("/missing.js").status_code, 404)

    def test_test_cases_stay_out_of_the_operator_case_list(self) -> None:
        # Owner: "why do I want to see test cases in the UI??!" Evaluation cases are
        # labelled QA-; they stay readable as evidence but never appear in the case list.
        client, store = self.app()
        with client:
            report = {"aircraft_reg": "CS-TQA", "description": "Fuel-pump defect"}
            traces = [
                client.post(
                    "/api/cases", headers=headers(), json={**report, "unavailable_minutes": n}
                ).json()["trace_id"]
                for n in (240, 120)
            ]
            store.docs[traces[1]]["display_id"] = "QA-ORCH-CASE1-ABC123"
            listed = client.get("/api/cases", headers=headers()).json()["cases"]
            self.assertEqual([case["trace_id"] for case in listed], [traces[0]])
            evidence = client.get(f"/api/cases/{traces[1]}", headers=headers())
            self.assertEqual(evidence.status_code, 200)

    def test_signed_in_operator_gets_durable_audit_and_no_execution_capability(self) -> None:
        client, store = self.app()
        with client:
            session = client.get("/api/session", headers=headers()).json()
            self.assertEqual(session["user"]["role"], "OCC Duty Manager")
            self.assertTrue(session["capabilities"]["approve"])
            opened = client.post(
                "/api/cases",
                headers=headers(),
                json={
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
            )
            self.assertEqual(opened.status_code, 202, opened.text)
            trace = opened.json()["trace_id"]
            state = client.get(f"/api/cases/{trace}", headers=headers()).json()
            self.assertEqual(state["status"], "no_disruption")
            self.assertNotIn("_run_token", state)
            self.assertFalse(session["capabilities"].get("execute", False))
            self.assertEqual(state["events"][0]["actor"], OPERATOR)
            self.assertEqual(state["events"][-1]["event"], "assessment_completed")
            self.assertEqual(len(client.get("/api/cases", headers=headers()).json()["cases"]), 1)
            denied = client.post(
                f"/api/cases/{trace}/approval",
                headers=headers(),
                json=decision_payload(
                    "delay", "approve", state["version"], "decision-no-disruption-0001"
                ),
            )
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(store.get(trace)["status"], "no_disruption")
            self.assertEqual(store.get(trace)["events"], state["events"])

    def test_unauthorised_or_cross_site_requests_do_not_create_cases(self) -> None:
        client, store = self.app()
        with client:
            for h in ({}, headers(HOST), {**headers(), "Origin": "https://evil.test"}):
                result = client.post(
                    "/api/cases",
                    headers=h,
                    json={
                        "aircraft_reg": "CS-TQA",
                        "description": "Fuel-pump defect",
                        "unavailable_minutes": 240,
                    },
                )
                self.assertIn(result.status_code, (401, 403))
            self.assertEqual(store.docs, {})

    def test_agent_failure_is_persisted_not_a_success_shaped_plan(self) -> None:
        client, _ = self.app(fail=True)
        with client:
            opened = client.post(
                "/api/cases",
                headers=headers(),
                json={
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
            )
            state = client.get(f"/api/cases/{opened.json()['trace_id']}", headers=headers()).json()
            self.assertEqual(state["status"], "failed")
            self.assertIn("Foundry timed out", state["error"])
            self.assertEqual(state["options"], [])

    def test_internal_evidence_requires_host_identity_and_run_capability(self) -> None:
        client, store = self.app()
        with client:
            active = Operations(store, Gateway()).open(
                {
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
                OPERATOR,
            )["trace_id"]
            callback = f"/internal/cases/{active}/events"
            admitted = {**headers(HOST), "X-ARROW-Run": store.get(active)["_run_token"]}
            specialists = (
                ("disruption-detection-agent", "L4"),
                ("aircraft-recovery-agent", "L2"),
                ("crew-recovery-agent", "L3"),
                ("passenger-reaccommodation-agent", "L3"),
                ("slot-negotiation-agent", "L3"),
            )
            for actor, level in specialists:
                for event in ("agent_started", "agent_completed"):
                    response = client.post(
                        callback,
                        headers=admitted,
                        json={
                            "actor": actor,
                            "autonomy": level,
                            "event": event,
                            "message": f"{actor}: {event}",
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.text)
            recorded = client.get(f"/api/cases/{active}/events", headers=headers()).json()["events"]
            self.assertEqual(
                [(event["actor"], event["autonomy"]) for event in recorded[1:]],
                [specialist for specialist in specialists for _ in range(2)],
            )
            evidence = {
                "input": {"option_id": "delay", "additional_delay_minutes": 15},
                "tools": [
                    {
                        "system": "booking",
                        "operation": "assess_passenger_recovery",
                        "status": "completed",
                    }
                ],
                "output": {
                    "constraint_status": "unknown",
                    "reaccommodation_status": "not_assessed",
                    "approval_ready": False,
                    "total_eur": None,
                    "limitations": ["Rebooking has not been assessed or executed."],
                },
            }
            completed = {
                "actor": "passenger-reaccommodation-agent",
                "autonomy": "L3",
                "event": "agent_completed",
                "message": "Read-only passenger impact checked; no rebooking executed.",
                "response_id": "resp_booking_evidence",
                "model": "gpt-4.1-mini",
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "output_tokens": 20,
                "evidence": evidence,
            }
            response = client.post(callback, headers=admitted, json=completed)
            self.assertEqual(response.status_code, 200, response.text)
            recorded = client.get(f"/api/cases/{active}/events", headers=headers()).json()["events"]
            self.assertEqual(recorded[-1]["evidence"], evidence)
            self.assertEqual(recorded[-1]["model"], "gpt-4.1-mini")
            self.assertEqual(recorded[-1]["cached_input_tokens"], 10)
            for h in (headers(), {**headers(HOST), "X-ARROW-Run": "wrong"}):
                self.assertEqual(client.post(callback, headers=h, json=completed).status_code, 403)
            private_context = {
                **evidence,
                "input": {**evidence["input"], "_run_token": "must-not-be-recorded"},
            }
            refused = client.post(
                callback, headers=admitted, json={**completed, "evidence": private_context}
            )
            self.assertEqual(refused.status_code, 422)
            self.assertNotIn(
                "must-not-be-recorded",
                client.get(f"/api/cases/{active}/events", headers=headers()).text,
            )
            for actor, level in (("invented-agent", "L3"), ("crew-recovery-agent", "human")):
                refused = client.post(
                    callback,
                    headers=admitted,
                    json={
                        "actor": actor,
                        "autonomy": level,
                        "event": "agent_started",
                        "message": "Invalid callback",
                    },
                )
                self.assertEqual(refused.status_code, 422)
            result = client.post(
                "/api/cases",
                headers=headers(),
                json={
                    "aircraft_reg": "CS-TQA",
                    "description": "Fuel-pump defect",
                    "unavailable_minutes": 240,
                },
            )
            trace = result.json()["trace_id"]
            for h in (headers(), {**headers(HOST), "X-ARROW-Run": "wrong"}):
                self.assertEqual(client.get(f"/internal/cases/{trace}", headers=h).status_code, 403)
            # A completed run cannot be reopened by a late callback, even with its old token.
            h = {**headers(HOST), "X-ARROW-Run": store.get(trace)["_run_token"]}
            self.assertEqual(client.get(f"/internal/cases/{trace}", headers=h).status_code, 409)
