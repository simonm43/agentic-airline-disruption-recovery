"""Turn a validated recovery option into the ordered updates and messages ARROW sends.

Follows the airline-ops knowledge base coordination matrix (§6.3): schedule first, then flight
plan, airport readiness and slot coordination, then crew and passengers. ARROW never sends REA.
A storm plan's passenger notice (the OCC playbook's PAX-NOTICE) follows the retime and its
messages and comes before the crew and passenger writes, which do not wait for it.

A weather plan runs in two stages (case summary and held storm spec §2): "now", the reversible part
carried out when OCC approves (passengers warned of possible delays, standby crews on alert), and
"when confirmed", everything else, held until the storm is confirmed. A stand-down adds the
"stand_down" actions that undo the warning and the alerts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from airline_systems.versions import flight_version
from ops_app.policy import playbook_steps, standby_crews, standby_ids, standby_name

LABELS = {
    "schedule": "Ops control",
    "maintenance": "Maintenance",
    "network_manager": "Network manager",
    "airport_cdm": "Airport CDM",
    "slot_coordinator": "Slot coordinator",
    "crew": "Crew",
    "booking": "Passengers",
    "ground_handling": "Ground handling",
}
AGENTS = {
    "schedule": "aircraft-recovery-agent",
    "slots": "slot-negotiation-agent",
    "crew": "crew-recovery-agent",
    "booking": "passenger-reaccommodation-agent",
}
# The reported disruption, as the Detection agent read it, is recorded by the executor as the
# first action of the plan (owner).
RECORD = "record_disruption"
RECORDED_BY = "disruption-detection-agent"
CAUSE_WORDS = {
    "border_control": "border-control queue",
    "staff_shortage": "staff shortage",
    "baggage": "baggage",
    "late_inbound": "late inbound aircraft",
    "other": "other cause",
}
EOBT_MESSAGE_MINUTES = 15
NOW, WHEN_CONFIRMED, STAND_DOWN = "now", "when_confirmed", "stand_down"
# A standby crew is called by its report time less 60 minutes (home standby answers in 60).
CALL_BEFORE_REPORT = timedelta(minutes=60)


@dataclass(frozen=True)
class PlannedAction:
    action_id: str
    system: str
    kind: str
    proposed_by: str
    label: str
    summary: str
    depends_on: tuple[str, ...] = ()
    expected: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    # "now", "when_confirmed" (a weather plan's held part) or "stand_down"; an action planned
    # by an earlier version has none and runs now.
    stage: str = NOW

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "depends_on": list(self.depends_on), "status": "not_started"}


def plan_hash(option: Mapping[str, Any], case_version: int) -> str:
    bound = {
        key: option.get(key)
        for key in ("option_id", "kind", "flights", "domain_assessments", "validation")
    }
    # The rule version and limits the plan was built under; a plan without them (built before
    # rule versions) hashes as it always did.
    policy = option.get("policy")
    if isinstance(policy, Mapping):
        rules = {key: policy[key] for key in ("version", "limits") if key in policy}
        if rules:
            bound["policy"] = rules
    # The playbook actions the executor sends (Task 3); a plan without them hashes as before.
    if option.get("playbook_steps"):
        bound["playbook_steps"] = option["playbook_steps"]
    canonical = json.dumps({**bound, "version": case_version}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def system_option_id(option: Mapping[str, Any]) -> str:
    """The Schedule option a plan is built on; the systems know options, not ARROW plans."""
    return str(option.get("aircraft_option_id") or option["option_id"])


def _minutes(later: str, earlier: str) -> int:
    delta = datetime.fromisoformat(later) - datetime.fromisoformat(earlier)
    return round(delta.total_seconds() / 60)


def _route(flight: Mapping[str, Any], original: Mapping[str, Any]) -> dict[str, Any]:
    """Where and on which day a flight operates, as its messages to external parties name it."""
    return {
        key: flight.get(key) or original.get(key)
        for key in ("origin", "destination", "flight_date")
    }


def _asked(request: str) -> tuple[str, str]:
    """The airport and flight a validator request names ("FAO night quota: AR131 in-block ...")."""
    airport, _, rest = request.partition(" ")
    return airport, rest.partition(": ")[2].partition(" ")[0]


def _span(minutes: int) -> str:
    """A duration in words: "4 h", "1 h 30 min", "45 min"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def disruption_record(trace: str, document: Mapping[str, Any]) -> dict[str, Any] | None:
    """The reported disruption as ARROW's executor records it in the system that owns it, as
    the first action of a plan it carries out (owner: the airline's systems are written only
    then): a delay in operations control, with its cause; a defect in Maintenance, which marks
    the aircraft out of service as reported. The system issues its event id then. None for a
    weather forecast (the met service owns it: no airline system records it) and for a
    disruption already recorded (an event that carried its source system's event id, or a
    case recorded before this change)."""
    disruption = document.get("disruption")
    if not isinstance(disruption, Mapping) or disruption.get("event_id"):
        return None
    kind = disruption.get("type")
    description = str(disruption.get("description") or disruption.get("message") or "")[:1000]
    if kind == "flight_delay":
        cause = str(disruption.get("cause"))
        return {
            "system": "schedule",
            "summary": f"Record the {disruption.get('flight_no')} delay:"
            f" {disruption.get('delay_minutes')} min, {CAUSE_WORDS.get(cause, cause)}",
            "payload": {
                "trace_id": trace,
                "flight_no": disruption.get("flight_no"),
                "delay_minutes": disruption.get("delay_minutes"),
                "cause": cause,
                "description": description,
            },
        }
    if kind != "aircraft_defect":
        return None
    defect = document.get("defect")
    defect = defect if isinstance(defect, Mapping) else {}
    registration = (disruption.get("scope") or {}).get("aircraft_reg") or defect.get("aircraft_reg")
    minutes = defect.get("unavailable_minutes")
    out = f": out of service {_span(minutes)}" if isinstance(minutes, int) else ""
    return {
        "system": "maintenance",
        "summary": f"Record the {registration} defect{out}",
        "payload": {
            "trace_id": trace,
            "aircraft_reg": registration,
            "unavailable_minutes": minutes,
            "description": str(defect.get("description") or description)[:1000],
        },
    }


def plan_actions(
    trace: str,
    option: Mapping[str, Any],
    *,
    originals: Mapping[str, Mapping[str, Any]],
    validation: Mapping[str, Any],
    airports: Mapping[str, Mapping[str, Any]],
    plan_hash: str,
    record: Mapping[str, Any] | None = None,
) -> list[PlannedAction]:
    """The plan's actions in the order they are sent. ``record`` (disruption_record): the
    reported disruption, recorded first; every other action waits for its receipt, so a
    system that refuses the record stops the plan for OCC before anything changes."""
    flights = [flight for flight in option.get("flights") or [] if flight["flight_no"] in originals]
    changed = [
        flight
        for flight in flights
        if any(
            flight.get(key) != originals[flight["flight_no"]].get(key)
            for key in ("status", "std_local", "sta_local", "aircraft_reg")
            if key in flight
        )
    ]
    cancelled = [flight for flight in changed if flight.get("status") == "cancelled"]
    retimed = [
        flight
        for flight in changed
        if flight.get("status") != "cancelled"
        and flight["std_local"] != originals[flight["flight_no"]]["std_local"]
    ]
    tails = [
        flight
        for flight in changed
        if flight.get("status") != "cancelled"
        and flight.get("aircraft_reg")
        and flight["aircraft_reg"] != originals[flight["flight_no"]].get("aircraft_reg")
    ]
    actions: list[PlannedAction] = []
    # Owner: a storm may not hit as forecast, so a weather plan carries out only its
    # reversible part when OCC approves; everything else waits for the storm to be confirmed.
    weather = option.get("cause") == "weather"
    later = WHEN_CONFIRMED if weather else NOW
    # Owner: the reported disruption is recorded in its system first, as the plan is carried
    # out; every other action waits for that system's receipt.
    root: tuple[str, ...] = ()
    if record is not None and not weather:
        system = str(record["system"])
        root = (f"{trace[:12]}-{plan_hash[:8]}-01-{RECORD.replace('_', '-')}",)
        actions.append(
            PlannedAction(
                action_id=root[0],
                system=system,
                kind=RECORD,
                proposed_by=RECORDED_BY,
                label=LABELS[system],
                summary=str(record["summary"]),
                payload=dict(record["payload"]),
                stage=NOW,
            )
        )

    def add(
        system: str,
        kind: str,
        summary: str,
        *,
        depends: tuple[str, ...] = (),
        expected: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> str:
        slug = kind.replace(".", "-").replace("_", "-")
        action_id = f"{trace[:12]}-{plan_hash[:8]}-{len(actions) + 1:02d}-{slug}"
        capability = kind.split(".")[0] if "." in kind else system
        actions.append(
            PlannedAction(
                action_id=action_id,
                system=system,
                kind=kind,
                proposed_by=AGENTS[system],
                label=LABELS[capability],
                summary=summary,
                depends_on=depends or root,
                expected=expected or {},
                payload=payload or {},
                stage=stage or later,
            )
        )
        return action_id

    called = standby_ids(option.get("levers"))
    if weather:
        # Now: the passengers of every flight the plan would retime or cancel are warned of
        # possible delays (no new times), and the standby crews it calls are put on alert, each
        # to be called by its report time less 60 minutes.
        notice = playbook_steps(option).get("PAX-NOTICE") or {}
        told = (
            [str(number) for number in notice.get("flights") or []]
            if notice.get("status") == "in_plan"
            else [str(flight["flight_no"]) for flight in retimed]
        )
        warned = sorted({*told, *(str(flight["flight_no"]) for flight in cancelled)})
        if warned:
            add(
                "booking",
                "warn_passengers",
                f"Warn passengers on {_flights(len(warned))} of possible delays",
                payload={"trace_id": trace, "flights": warned, "kind": "possible_delay"},
                stage=NOW,
            )
        crews: list[dict[str, Any]] = [
            {
                "reserve_option_id": str(crew["reserve_option_id"]),
                "call_by_local": (
                    datetime.fromisoformat(str(crew["report_local"])) - CALL_BEFORE_REPORT
                ).isoformat(),
                "crew": [
                    {"crew_id": member["crew_id"], "rank": member["rank"]}
                    for member in crew.get("crew") or []
                ],
            }
            for crew in standby_crews(option)
            if changed
            and crew.get("reserve_option_id") in called
            and crew.get("report_local")
            and crew.get("crew")
        ]
        if crews:
            ids = [str(crew["reserve_option_id"]) for crew in crews]
            add(
                "crew",
                "alert_standby",
                f"Put {standby_name(ids)} on alert",
                payload={"trace_id": trace, "crews": crews},
                stage=NOW,
            )

    # Owner: a night-quota, curfew or slot change is asked for first; the schedule changes
    # only once the airport confirms, so a refusal leaves the flight as it was for OCC.
    by_number = {flight["flight_no"]: flight for flight in flights}

    def asked(request: str) -> dict[str, Any]:
        airport, flight_no = _asked(request)
        route = _route(by_number.get(flight_no, {}), originals.get(flight_no, {}))
        return {"request": request, "airport": airport, "flight_no": flight_no, **route}

    airport_requests = tuple(
        add(
            "slots",
            "slot_coordinator.slot_change_request",
            request,
            payload=asked(request),
        )
        for request in validation.get("airport_requests") or []
    )

    def versions(moved: list[dict[str, Any]]) -> dict[str, str]:
        """The version of each flight as ARROW validated it; operations control compares it."""
        return {f["flight_no"]: flight_version(originals[f["flight_no"]]) for f in moved}

    schedule: list[str] = []
    if retimed:
        schedule.append(
            add(
                "schedule",
                "retime_flights",
                f"Retime {', '.join(f['flight_no'] for f in retimed)}",
                depends=airport_requests,
                expected={
                    "flights": {
                        f["flight_no"]: {
                            "std_local": originals[f["flight_no"]]["std_local"],
                            "status": "scheduled",
                        }
                        for f in retimed
                    },
                    "versions": versions(retimed),
                },
                payload={
                    "flights": [
                        {key: f[key] for key in ("flight_no", "std_local", "sta_local")}
                        for f in retimed
                    ]
                },
            )
        )
    if cancelled:
        schedule.append(
            add(
                "schedule",
                "cancel_flights",
                f"Cancel {', '.join(f['flight_no'] for f in cancelled)}",
                depends=airport_requests,
                expected={
                    "flights": {
                        f["flight_no"]: {
                            "std_local": originals[f["flight_no"]]["std_local"],
                            "status": "scheduled",
                        }
                        for f in cancelled
                    },
                    "versions": versions(cancelled),
                },
                payload={"flight_nos": [f["flight_no"] for f in cancelled]},
            )
        )
    if tails:
        schedule.append(
            add(
                "schedule",
                "change_tail",
                f"Tail change {', '.join(f['flight_no'] for f in tails)}",
                depends=airport_requests,
                expected={
                    "flights": {
                        f["flight_no"]: {"aircraft_reg": originals[f["flight_no"]]["aircraft_reg"]}
                        for f in tails
                    },
                    "versions": versions(tails),
                },
                payload={
                    "flights": [
                        {"flight_no": f["flight_no"], "aircraft_reg": f["aircraft_reg"]}
                        for f in tails
                    ]
                },
            )
        )
    after = tuple(schedule)

    for flight in retimed:
        original = originals[flight["flight_no"]]
        route = _route(flight, original)
        if _minutes(flight["std_local"], original["std_local"]) > EOBT_MESSAGE_MINUTES:
            add(
                "slots",
                "network_manager.fpl_delay",
                f"DLA {flight['flight_no']}",
                depends=after,
                payload={"flight_no": flight["flight_no"], "eobt": flight["std_local"], **route},
            )
        origin = str(flight.get("origin") or original.get("origin") or "")
        if airports.get(origin, {}).get("a_cdm"):
            add(
                "slots",
                "airport_cdm.tobt_update",
                f"TOBT {flight['flight_no']} {origin}",
                depends=after,
                payload={
                    "flight_no": flight["flight_no"],
                    "tobt": flight["std_local"],
                    "airport": origin,
                    **route,
                },
            )
    for flight in cancelled:
        original = originals[flight["flight_no"]]
        route = _route(flight, original)
        add(
            "slots",
            "network_manager.fpl_cancel",
            f"CNL {flight['flight_no']}",
            depends=after,
            payload={"flight_no": flight["flight_no"], "eobt": original["std_local"], **route},
        )
        for airport in (original.get("origin"), original.get("destination")):
            if airports.get(airport or "", {}).get("coordination_level") is not None:
                add(
                    "slots",
                    "slot_coordinator.slot_non_use",
                    f"Slot non-use {flight['flight_no']} {airport}",
                    depends=after,
                    payload={"flight_no": flight["flight_no"], **route, "airport": airport},
                )
    for flight in tails:
        add(
            "slots",
            "network_manager.fpl_change",
            f"CHG {flight['flight_no']}",
            depends=after,
            payload={
                "flight_no": flight["flight_no"],
                "aircraft_reg": flight["aircraft_reg"],
                "eobt": flight["std_local"],
                **_route(flight, originals[flight["flight_no"]]),
            },
        )

    moved = {
        "flights": [dict(flight) for flight in changed],
        "originals": [dict(originals[flight["flight_no"]]) for flight in changed],
    }
    # A storm plan's passenger notice (PAX-NOTICE): once the retime and its messages are sent,
    # Booking tells the passengers of its retimed flights yet to leave their new times. The crew
    # and passenger writes come after it but do not wait for it: a refused notice never holds
    # back the duty change that keeps crews legal.
    notice = playbook_steps(option).get("PAX-NOTICE") or {}
    if notice.get("status") == "in_plan" and notice.get("flights"):
        told = [str(number) for number in notice["flights"]]
        add(
            "booking",
            "notify_passengers",
            f"Tell passengers on {len(told)} flight{'' if len(told) == 1 else 's'} their new times",
            depends=after,
            payload={"trace_id": trace, "flights": told},
        )
    crew_digest = {"plan_digest": (validation.get("crew_plan") or {}).get("plan_digest")}
    # One crew write calls every standby crew the plan names, bound to one digest.
    if changed and called:
        add(
            "crew",
            "assign_reserve_crew",
            f"Assign {standby_name(called)}",
            depends=after,
            expected=crew_digest,
            payload={**moved, "reserve_option_ids": called},
        )
    elif changed:
        add(
            "crew",
            "update_duties",
            "Update crew duties",
            depends=after,
            expected=crew_digest,
            payload=moved,
        )
    rebooking = validation.get("rebooking") or {}
    if rebooking.get("rebooked_passengers"):
        add(
            "booking",
            "rebook_passengers",
            f"Rebook {rebooking['rebooked_passengers']} passengers",
            depends=after,
            expected={"plan_digest": rebooking["plan_digest"]},
            payload={
                "option_id": system_option_id(option),
                "additional_delay_minutes": option.get("additional_delay_minutes", 0),
                **moved,
            },
        )
    # Owner (D108): a car takes passengers whose shortened connection it can still make
    # to their next aircraft; the ground handler always does it, at no cost shown.
    if rebooking.get("transferred_passengers"):
        add(
            "booking",
            "ground_handling.direct_transfer",
            f"Direct transfers for {rebooking['transferred_passengers']} passengers",
            depends=after,
            payload={"transfers": [dict(item) for item in rebooking.get("transfers") or []]},
        )
    # Owner: the airline's reservation system tells passengers of a schedule change or a
    # rebooking downstream. ARROW asks it only for a storm plan's PAX-NOTICE, above; Booking
    # sends the notices.
    return actions


def _flights(count: int) -> str:
    return f"{count} flight{'' if count == 1 else 's'}"


def _added(
    trace: str, plan_hash: str, index: int, system: str, kind: str, summary: str, **values: Any
) -> dict[str, Any]:
    """An action ARROW adds to a running storm execution, numbered after the planned ones."""
    slug = kind.replace("_", "-")
    return PlannedAction(
        action_id=f"{trace[:12]}-{plan_hash[:8]}-{index:02d}-{slug}",
        system=system,
        kind=kind,
        proposed_by=AGENTS[system],
        label=LABELS[system],
        summary=summary,
        **values,
    ).as_dict()


def standby_alerted(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The standby crews a storm case has on alert (the confirmed alert's crews), with their
    call-by times."""
    return [
        crew
        for action in actions
        if action.get("kind") == "alert_standby" and action.get("status") == "confirmed"
        for crew in (action.get("payload") or {}).get("crews") or []
    ]


def standby_called(actions: list[dict[str, Any]]) -> set[str]:
    """The standby crews ARROW has called (or is calling) at their call-by time."""
    return {
        str(crew)
        for action in actions
        if action.get("kind") == "call_standby"
        for crew in (action.get("payload") or {}).get("reserve_option_ids") or []
    }


def call_action(
    trace: str,
    plan_hash: str,
    actions: list[dict[str, Any]],
    crews: list[dict[str, Any]],
    stage: str = NOW,
) -> dict[str, Any]:
    """Call standby crews: at their call-by time while the forecast is unconfirmed ("now", crew
    only), or with the held part once it is confirmed and the duty change assigns them."""
    ids = [str(crew["reserve_option_id"]) for crew in crews]
    if stage == NOW:
        times = ", ".join(str(crew["call_by_local"])[11:16] for crew in crews)
        summary = f"Call {standby_name(ids)} · call-by {times}"
    else:
        summary = f"Call {standby_name(ids)} · forecast confirmed"
    return _added(
        trace,
        plan_hash,
        len(actions) + 1,
        "crew",
        "call_standby",
        summary,
        payload={"trace_id": trace, "reserve_option_ids": ids},
        stage=stage,
    )


def stand_down_actions(
    trace: str, plan_hash: str, actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What a stood-down storm sends: the passengers it warned are told no change is expected,
    and every standby crew it put on alert (or called) is released."""
    added: list[dict[str, Any]] = []
    warned = [
        str(number)
        for action in actions
        if action.get("kind") == "warn_passengers" and action.get("status") == "confirmed"
        for number in (action.get("payload") or {}).get("flights") or []
    ]
    if warned:
        added.append(
            _added(
                trace,
                plan_hash,
                len(actions) + 1,
                "booking",
                "notify_no_change",
                f"Tell passengers on {_flights(len(warned))} no change is expected",
                payload={"trace_id": trace, "flights": warned, "kind": "no_change"},
                stage=STAND_DOWN,
            )
        )
    alerted = [str(crew["reserve_option_id"]) for crew in standby_alerted(actions)]
    if alerted:
        added.append(
            _added(
                trace,
                plan_hash,
                len(actions) + len(added) + 1,
                "crew",
                "release_standby",
                f"Release {standby_name(alerted)}",
                payload={"trace_id": trace},
                stage=STAND_DOWN,
            )
        )
    return added


def authorise_execution(
    document: Mapping[str, Any], option_id: str, *, route: str, approved_by: str
) -> dict[str, Any]:
    """Bind an execution to the option and case version that was reviewed or auto-cleared."""
    option = next(o for o in document["options"] if o.get("option_id") == option_id)
    version = int(document["version"])
    return {
        "route": route,
        "status": "approved",
        "approved_by": approved_by,
        "option_id": option_id,
        "case_version": version,
        "plan_hash": plan_hash(option, version),
        "actions": [],
    }
