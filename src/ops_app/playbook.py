"""The OCC playbook (OCC playbook spec §1): the standard actions an airline's operations control
takes, versioned in code like the Rules.

The orchestrator reads the actions that apply to its case (``get_playbook``) and names them in
``build_plan``; code, not the model, decides which apply and records each plan's checklist. Plans
that name none (and plans built by earlier versions) show the actions read from what the plan does.

The planner carries out each named action that is not read from what the plan does (playbook
Task 3, trimmed when the owner dropped Act now): a weather plan's passenger notice, and dispatch's
and maintenance's advice. Each is recorded as the plan's ``playbook_steps`` (status, facts, and
what the executor sends).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

PLAYBOOK_VERSION = "playbook-2026-11-23"
# The disruption types a case can have once recorded, as an action's reason names them.
DISRUPTIONS = {
    "flight_delay": "flight delay",
    "aircraft_defect": "aircraft defect",
    "weather": "weather forecast",
}
# A named action the planner has no step for (a plan built before playbook Task 3).
NOT_CARRIED_OUT = "not yet carried out by ARROW"
NOT_USED = "named, but this plan does not use it"
# ARROW plans weather at Lisbon only (its weather recording refuses any other airport).
STORM_AIRPORT = "LIS"

_ALL = frozenset(DISRUPTIONS)
_STORM = frozenset({"weather"})

Status = Literal["in_plan", "advice", "refused", "not_needed"]


@dataclass(frozen=True)
class Action:
    id: str
    name: str
    desk: str
    applies_to: frozenset[str]
    # Prepare: reversible, hours ahead; commit: irreversible.
    phase: Literal["prepare", "commit"]
    # Advice: the dispatcher, commander or maintenance decides; ARROW never carries it out.
    autonomy: Literal["auto", "occ", "advice"]
    checks: str
    recipient: str
    needs: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "desk": self.desk,
            "applies_to": sorted(self.applies_to),
            "phase": self.phase,
            "autonomy": self.autonomy,
            "checks": self.checks,
            "recipient": self.recipient,
            "needs": self.needs,
        }


CATALOGUE: tuple[Action, ...] = (
    Action(
        "RETIME",
        "Retime flights, protect connections",
        "Aircraft control",
        _ALL,
        "commit",
        # Within limits it runs automatically; a storm plan goes to OCC (the Weather plans rule).
        "auto",
        "Minimum turn, crew flight-time limits, Lisbon hourly capacity, night quotas and"
        " curfews, connections, EU261 care thresholds",
        "Operations control",
        "flights whose departures can move within crew and airport limits",
    ),
    Action(
        "GROUND-HOLD",
        "Hold departures at origin, not in the air",
        "Aircraft control",
        _STORM,
        "commit",
        "occ",
        "Lisbon arrival hour at the reduced rate, crew flight-time limits, connections; a delay"
        " message over 15 min",
        "Operations control",
        "flights into Lisbon in the weather window that can wait at their origin",
    ),
    Action(
        "THIN",
        "Cancel a round trip early, rebook",
        "Aircraft control",
        _ALL,
        "commit",
        "occ",
        "A whole round trip, free seats to rebook, EU261 notice, the aircraft's end-of-day"
        " position",
        "Operations control",
        "a round trip whose cancellation puts later flights back on time",
    ),
    Action(
        "SWAP-AC",
        "Swap aircraft",
        "Aircraft control",
        frozenset({"aircraft_defect", "flight_delay"}),
        "commit",
        "occ",
        "Same type and seats, a free ground window, a crew who can reach it",
        "Operations control",
        "a free aircraft of the same type in its ground window",
    ),
    Action(
        "STANDBY",
        "Call standby crews",
        "Crew control",
        _ALL,
        "commit",
        # Up to two crews automatically; more, or a base's last, is OCC's (the Standby crews rule).
        "auto",
        "Standby window, call-out notice, flight-time limits after standby, qualification; each"
        " crew once a plan",
        "Crew system",
        "a standby crew at the duty's base",
    ),
    Action(
        "REBOOK",
        "Rebook on own or other airlines",
        "Hub and passengers",
        _ALL,
        "commit",
        "auto",
        "Free seats, minimum connection times, EU261 re-routing; beyond the rebooking limit, OCC",
        "Passenger system",
        "free seats on later flights to the passengers' destinations",
    ),
    Action(
        "CAR",
        "Drive a short connection at Lisbon",
        "Hub and passengers",
        _ALL,
        "commit",
        "auto",
        "A connection the delay shortened that a car still makes: 15 min, 20 through passport"
        " control",
        "Ground handling",
        "a Lisbon connection a car can still make",
    ),
    Action(
        "PAX-NOTICE",
        "Tell passengers their new times",
        "Hub and passengers",
        _STORM,
        # A forecast gives hours of notice: passengers hear their new times well before the weather.
        "prepare",
        # Within the plan, after its retime; a storm plan goes to OCC (the Weather plans rule).
        "auto",
        "Flights the plan retimes that are yet to leave; one notice a flight a day; no booking"
        " changes",
        "Passenger system",
        "flights the plan retimes that are yet to leave",
    ),
    Action(
        "FUEL-FLAG",
        "Flag holding and extra-fuel risk for flights into the window",
        "Dispatch",
        _STORM,
        "prepare",
        "advice",
        "Flights arriving at Lisbon in the weather's exposure window; the dispatcher and"
        " commander decide the fuel",
        "Dispatch",
        "flights arriving at Lisbon around the weather window",
    ),
    Action(
        "ALT-FLAG",
        "Screen alternates (Porto, Faro) against the same weather",
        "Dispatch",
        _STORM,
        "prepare",
        "advice",
        "Porto and Faro against the same forecast window; the dispatcher and commander decide",
        "Dispatch",
        "flights arriving at Lisbon around the weather window",
    ),
    Action(
        "MX-ETR",
        "Return-to-service estimate and deferral check",
        "Maintenance control",
        frozenset({"aircraft_defect"}),
        "prepare",
        "advice",
        "The estimate against the next departures; a deferral's MEL interval; the commander"
        " accepts",
        "Maintenance control",
        "the defect's return-to-service estimate from Maintenance",
    ),
)
BY_ID = {action.id: action for action in CATALOGUE}
# The actions ARROW already carries out, read from what a plan does.
DERIVED = frozenset({"RETIME", "GROUND-HOLD", "THIN", "SWAP-AC", "STANDBY", "REBOOK", "CAR"})


def applicable(disruption_type: str) -> list[Action]:
    """The actions that apply to a disruption type, in catalogue order."""
    return [action for action in CATALOGUE if disruption_type in action.applies_to]


def disruption_type(kind: Any, cause: Any) -> str | None:
    """A case's disruption type: as recorded, else read from the cause Schedule gave a plan (a
    flight delay's cause is its reason, such as staff_shortage)."""
    if kind in DISRUPTIONS:
        return str(kind)
    if cause in DISRUPTIONS:
        return str(cause)
    return "flight_delay" if isinstance(cause, str) and cause else None


def named(plan: Mapping[str, Any]) -> list[str]:
    """The playbook action ids the plan's levers named, in order, each once."""
    levers = plan.get("levers")
    listed = levers.get("playbook_actions") if isinstance(levers, Mapping) else None
    return list(dict.fromkeys(str(item) for item in listed)) if isinstance(listed, list) else []


def refusal(action: Action, kind: str | None) -> str | None:
    """Why an action does not apply to a case, or None when it does."""
    if kind is not None and kind in action.applies_to:
        return None
    return f"does not apply to a {DISRUPTIONS[kind]}" if kind else "does not apply to this case"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _plural(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _hhmm(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "?"


def _late(flight: Mapping[str, Any], was: Any) -> int:
    try:
        span = datetime.fromisoformat(str(flight.get("std_local"))) - datetime.fromisoformat(
            str(was)
        )
    except ValueError:
        return 0
    return round(span.total_seconds() / 60)


def _standby_facts(crew: Mapping[str, Any]) -> str:
    """A standby crew's head start (OCC playbook spec §3): when crew control calls it, when it
    reports and the flights it takes, as Crew offered it. A home standby needs 60 min to answer
    and reports 60 min before an A320 departure, so the call comes hours ahead of a storm."""
    called = crew.get("called_local")
    call = f"called {_hhmm(called)}" if called else "call time not established"
    return (
        f"{crew.get('reserve_option_id')} {call} · reports {_hhmm(crew.get('report_local'))}"
        f" · {', '.join(map(str, crew.get('flights_for_reserve') or []))}"
    )


def _done(
    plan: Mapping[str, Any], kind: str | None, originals: Mapping[str, Mapping[str, Any]]
) -> dict[str, str | None]:
    """The playbook actions a plan already carries out, read from what it does, each with its
    facts in labels and numbers (None when the plan does not record them)."""
    flights = [f for f in plan.get("flights") or [] if isinstance(f, Mapping)]
    flying = [f for f in flights if f.get("status") != "cancelled"]

    def was(flight: Mapping[str, Any], key: str) -> Any:
        original = originals.get(str(flight.get("flight_no")))
        if isinstance(original, Mapping) and key in original:
            return original[key]
        return flight.get(f"published_{key}") if key == "std_local" else None

    moved = [f for f in flying if was(f, "std_local") not in (None, f.get("std_local"))]
    done: dict[str, str | None] = {}
    if moved:
        most = max(_late(f, was(f, "std_local")) for f in moved)
        done["RETIME"] = f"{_plural(len(moved), 'flight')}, up to +{most} min"
    held = [f for f in moved if f.get("destination") == STORM_AIRPORT]
    if kind == "weather" and held:
        done["GROUND-HOLD"] = f"{_plural(len(held), 'arrival')} into Lisbon wait at origin"
    cancelled = [str(f.get("flight_no")) for f in flights if f.get("status") == "cancelled"]
    if cancelled or (kind == "weather" and plan.get("aircraft_option_id") == "cancel"):
        done["THIN"] = f"{', '.join(cancelled)} cancelled" if cancelled else None
    tails = [f for f in flying if was(f, "aircraft_reg") not in (None, f.get("aircraft_reg"))]
    if plan.get("kind") == "swap" or tails:
        done["SWAP-AC"] = (
            "; ".join(f"{f.get('flight_no')} → {f.get('aircraft_reg')}" for f in tails) or None
        )
    reserves = plan.get("crew_reserves")
    crews = (
        [crew for crew in reserves if isinstance(crew, Mapping)]
        if isinstance(reserves, list)
        else [plan["crew_reserve"]]
        if isinstance(plan.get("crew_reserve"), Mapping)
        else []
    )
    if crews:
        done["STANDBY"] = "; ".join(_standby_facts(crew) for crew in crews)
    validation = plan.get("validation")
    rebooking = validation.get("rebooking") if isinstance(validation, Mapping) else None
    if isinstance(rebooking, Mapping):
        if rebooked := _count(rebooking.get("rebooked_passengers")):
            done["REBOOK"] = f"{_plural(rebooked, 'passenger')} rebooked"
        if driven := _count(rebooking.get("transferred_passengers")):
            done["CAR"] = f"{_plural(driven, 'passenger')} by car"
    return done


def checklist(
    plan: Mapping[str, Any], originals: Mapping[str, Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """A plan's playbook, in catalogue order: each action that applies to its case (in the plan,
    advice or not needed), and each named action that does not (refused, with the reason). Each
    row has its facts (labels and numbers) when the plan records them.

    An action the planner carried out (``playbook_steps``, plans since Task 3) shows its own
    status, facts and reason (a check that blocks the plan); the others are read from what the
    plan does. ``originals`` are the case's flights as scheduled; without them a flight's
    published time stands in, as for a plan read back on its own."""
    kind = disruption_type(plan.get("disruption_type"), plan.get("cause"))
    asked = named(plan)
    done = _done(plan, kind, originals or {})
    stored = plan.get("playbook_steps")
    steps: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    rows: list[dict[str, Any]] = []
    for action in CATALOGUE:
        status: Status
        reason: str | None = None
        facts: str | None = None
        step = steps.get(action.id)
        if (refused := refusal(action, kind)) is not None:
            if action.id not in asked:
                continue
            status, reason = "refused", refused
        elif isinstance(step, Mapping):
            status = step.get("status") or "not_needed"
            facts, reason = step.get("facts"), step.get("reason")
        elif action.id in done:
            status, facts = "in_plan", done[action.id]
        elif action.autonomy == "advice":
            status = "advice"
        else:
            status = "not_needed"
            if action.id in asked:
                reason = NOT_USED if action.id in DERIVED else NOT_CARRIED_OUT
        rows.append(
            {
                "id": action.id,
                "name": action.name,
                "desk": action.desk,
                "autonomy": action.autonomy,
                "status": status,
                "reason": reason,
                "facts": facts,
            }
        )
    return rows


def lever_problems(ids: Sequence[str]) -> list[str]:
    """Named playbook actions that are not levers, each refused once: ids outside the playbook,
    and ids named twice."""
    unique = list(dict.fromkeys(ids))
    problems = [
        f"playbook action {item} is not in the playbook" for item in unique if item not in BY_ID
    ]
    problems += [
        f"playbook action {item} named twice"
        for item in unique
        if item in BY_ID and ids.count(item) > 1
    ]
    return problems


def for_case(kind: Any) -> dict[str, Any]:
    """What get_playbook returns for a case of this disruption type."""
    return {
        "version": PLAYBOOK_VERSION,
        "disruption_type": kind,
        "actions": [
            {
                "id": action.id,
                "name": action.name,
                "desk": action.desk,
                "autonomy": action.autonomy,
                "phase": action.phase,
                "needs": action.needs,
            }
            for action in applicable(str(kind))
        ],
    }


def catalogue_view() -> dict[str, Any]:
    """The whole playbook, read only, for the Rules screen."""
    return {"version": PLAYBOOK_VERSION, "actions": [action.as_dict() for action in CATALOGUE]}
