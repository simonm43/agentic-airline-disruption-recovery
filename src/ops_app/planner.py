"""Deterministic recovery planner (spec §6): agents choose levers; code computes the plan.

Every number in a plan comes from the systems (flight times, assessments, rebooking and duty
plans, standby crews, airport rules, passenger counts) or from the published cost basis. A lever
the systems did not offer is rejected with its reason; the policy then blocks the plan.

The OCC playbook: each named action that applies to the case and is not read from what
the plan does is recorded in ``playbook_steps``: on a storm plan PAX-NOTICE tells the passengers
of the flights it retimes their new times (sent after the retime; no cost, no booking changes);
FUEL-FLAG, ALT-FLAG and MX-ETR are advice with their facts, never in cost or route.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, NamedTuple, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from airline_systems.facts import FIELDS as FACTS
from ops_app.actions import EOBT_MESSAGE_MINUTES
from ops_app.cost import NOT_OWED_REASON, estimate
from ops_app.playbook import (
    BY_ID,
    PLAYBOOK_VERSION,
    checklist,
    disruption_type,
    lever_problems,
    named,
    refusal,
)
from ops_app.policy import Limits, PolicyVerdict, classify, originals_of, standby_name
from ops_app.validator import validate

logger = logging.getLogger("ops_app")

DOMAINS = ("crew", "booking", "slots")
# Owner: a reported delay also weighs a swap and a cancellation, as a defect does; the
# orchestrator builds them in escalation order and stops once a plan runs automatically. A storm
# (Case 5) spans many aircraft: Schedule offers only its sequenced delay and a cancellation.
ALLOWED_AIRCRAFT_KINDS = {
    "aircraft_defect": {"delay", "swap", "cancel"},
    "weather": {"delay", "cancel"},
}
DEFAULT_AIRCRAFT_KINDS = frozenset({"delay", "swap", "cancel"})
MAX_ROUNDS = 3
MAX_ADJUSTMENT_MINUTES = 180
STANDBY_OPTION = r"^R[0-9]{1,2}$"


class CaseRef(NamedTuple):
    """A case as ARROW calls the airline systems about it (owner): every read names the
    disruption by its facts (``disruption``: its type and that type's own fields), never by an
    ARROW case or a record, as nothing is recorded in the systems before a plan runs. ARROW's
    trace travels as telemetry only (write bodies still carry it as the sample systems'
    correlation)."""

    trace: str
    disruption: Mapping[str, Any]


def disruption_facts(
    disruption: Mapping[str, Any], defect: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The facts the airline systems are asked about a recorded disruption: its type and that
    type's own fields (a defect's aircraft and time out of service come from its defect);
    ValueError when it is not recorded yet or a fact is missing."""
    kind = disruption.get("type")
    if kind not in FACTS:
        raise ValueError("The disruption is not recorded yet: the systems have nothing to read")
    values = dict(disruption)
    if kind == "aircraft_defect":
        defect = defect or {}
        values = {
            "aircraft_reg": (disruption.get("scope") or {}).get("aircraft_reg")
            or defect.get("aircraft_reg"),
            "unavailable_minutes": defect.get("unavailable_minutes"),
        }
    facts = {"type": kind, **{name: values.get(name) for name in FACTS[str(kind)]}}
    missing = [name for name, value in facts.items() if value in (None, "")]
    if missing:
        raise ValueError(f"The recorded disruption has no {', '.join(missing)}")
    return facts


def case_ref(document: Mapping[str, Any]) -> CaseRef:
    """The case's reference for system calls; ValueError before its disruption is recorded."""
    disruption = document.get("disruption")
    if not isinstance(disruption, Mapping):
        raise ValueError("The disruption is not recorded yet: the systems have nothing to read")
    defect = document.get("defect")
    return CaseRef(
        str(document["trace_id"]),
        disruption_facts(disruption, defect if isinstance(defect, Mapping) else None),
    )


class PlannerGateway(Protocol):
    def options(self, case: CaseRef, additional_delay_minutes: int) -> dict[str, Any]: ...
    def assessment(
        self, domain: str, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def reserve_options(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def rebooking(
        self, case: CaseRef, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]: ...
    def duty_plan(
        self,
        case: CaseRef,
        option_id: str,
        additional_delay_minutes: int,
        reserve_option_ids: Sequence[str] = (),
    ) -> dict[str, Any]: ...
    def airports(self, trace: str) -> dict[str, Any]: ...
    def network(self, trace: str) -> dict[str, Any]: ...
    def dispatch_advisories(self, case: CaseRef) -> dict[str, Any]: ...


class Levers(BaseModel):
    """The choices agents make; everything else is computed."""

    model_config = ConfigDict(extra="forbid")
    aircraft_option_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,40}$")
    reserve_option_ids: list[Annotated[str, Field(pattern=STANDBY_OPTION)]] = Field(
        default_factory=list,
        max_length=20,
        description="Crew's standby crew options for this plan: one per breached duty",
    )
    # Earlier agent versions name one standby crew; for one release it is read as a one-item list
    # and never written back.
    reserve_option_id: str | None = Field(
        default=None,
        pattern=STANDBY_OPTION,
        exclude=True,
        description="Deprecated: one standby crew option; use reserve_option_ids",
    )
    rebook: bool = True
    delay_messages: bool = True
    playbook_actions: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,20}$")]] = Field(
        default_factory=list,
        max_length=20,
        description="Ids from get_playbook of the actions this plan uses, each once",
    )

    @model_validator(mode="after")
    def _one_list(self) -> Self:
        if self.reserve_option_id is not None:
            if self.reserve_option_id not in self.reserve_option_ids:
                self.reserve_option_ids = [self.reserve_option_id, *self.reserve_option_ids]
            self.reserve_option_id = None
        return self


class WhatIfLevers(Levers):
    """A what-if asked in chat may also hold a delay option back (case chat spec §3): the
    planner's search for the delay every domain agrees on starts at ``extra_delay_minutes``."""

    extra_delay_minutes: int = Field(default=0, ge=0, le=180, multiple_of=5)


def _minutes(later: Any, earlier: Any) -> int:
    span = datetime.fromisoformat(str(later)) - datetime.fromisoformat(str(earlier))
    return round(span.total_seconds() / 60)


def _reserve_gap(reserve: Mapping[str, Any]) -> int | None:
    """Minutes the standby crew's first departure must move: Crew's earliest departure for it
    (its report plus 60 min on an A320, 75 on an A330) after the departure as planned; None if
    Crew did not say (the policy then blocks the plan).

    Crew reports that departure itself: a duty may continue on another aircraft, so the leg
    need not be one of this option's flights.
    """
    try:
        return _minutes(reserve["earliest_departure_local"], reserve["departure_local"])
    except (KeyError, TypeError, ValueError):
        return None


def _twice(values: Sequence[Any]) -> Any:
    """The first value that appears twice, else None."""
    return next((value for value in values if values.count(value) > 1), None)


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _without_timestamps(v) for k, v in value.items() if k != "checked_at"}
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def airport_rules(gateway: PlannerGateway, trace: str) -> dict[str, Any]:
    """Slots' airport rules by IATA code, read now."""
    return {
        rule["iata"]: rule
        for rule in gateway.airports(trace).get("airports") or []
        if isinstance(rule, Mapping) and "iata" in rule
    }


def plan_id(plan: Mapping[str, Any], case_version: Any) -> str:
    """Stable ID of a plan's content and the case version it was computed for."""
    content = {k: v for k, v in plan.items() if k != "plan_id"}
    canonical = json.dumps(
        {"plan": _without_timestamps(content), "version": case_version},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _record_playbook(
    plan: dict[str, Any],
    document: Mapping[str, Any],
    cause: Any,
    originals: Mapping[str, Mapping[str, Any]],
) -> None:
    """The plan's playbook (OCC playbook spec §2): the named actions that apply to the case, and
    its checklist; a named action that does not apply is left out and shown refused."""
    disruption = document.get("disruption")
    kind = disruption_type(
        disruption.get("type") if isinstance(disruption, Mapping) else None, cause
    )
    plan["disruption_type"] = kind
    plan["playbook_actions"] = [
        item for item in named(plan) if item in BY_ID and refusal(BY_ID[item], kind) is None
    ]
    plan["playbook_version"] = PLAYBOOK_VERSION
    plan["playbook"] = checklist(plan, originals)


def _rejected(
    document: Mapping[str, Any],
    levers: Levers,
    rejected: list[str],
    limits: Limits,
    cause: Any = None,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "trace_id": document["trace_id"],
        "aircraft_option_id": levers.aircraft_option_id,
        "kind": None,
        "flights": [],
        "levers": levers.model_dump(),
        "adjustment_minutes": 0,
        "domain_assessments": [],
        "crew_reserves": [],
        "crew_reserve": None,
        "cost": None,
        "conflict": False,
        "rejected_levers": rejected,
        "unknowns": [],
        "validation": None,
    }
    _record_playbook(plan, document, cause, {})
    plan["policy"] = PolicyVerdict(
        "blocked", (f"lever not offered: {'; '.join(rejected)}",), limits
    ).as_dict()
    plan["requires_duty_manager"] = False
    plan["plan_id"] = plan["option_id"] = plan_id(plan, document.get("version"))
    return plan


def _uses(action_id: str, asked: Sequence[str], kind: str | None) -> bool:
    """Whether the plan named an action of the playbook that applies to the case."""
    return action_id in asked and action_id in BY_ID and refusal(BY_ID[action_id], kind) is None


def _hhmm(value: Any) -> str:
    return str(value)[11:16]


def _passenger_notice(
    gateway: PlannerGateway,
    case: CaseRef,
    option: Mapping[str, Any],
    originals: Mapping[str, Mapping[str, Any]],
    journeys: Any,
) -> dict[str, Any]:
    """PAX-NOTICE on a weather plan: the flights it retimes that are yet to leave at Schedule's
    clock, whose passengers Booking tells their new times after the retime. The passengers are
    counted from the plan's bookings (Booking's journeys: each booking a moved flight touches)
    that keep every flight; a booking with a cancelled flight is rebooked, not told a time. No
    cost line, and no booking changes."""
    clock = datetime.fromisoformat(str(gateway.network(case.trace)["as_of_local"]))
    flights = sorted(
        str(flight["flight_no"])
        for flight in option.get("flights") or []
        if flight.get("status") != "cancelled"
        and str(flight.get("flight_no")) in originals
        and flight.get("std_local") != originals[str(flight["flight_no"])].get("std_local")
        and datetime.fromisoformat(str(flight["std_local"])) > clock
    )
    if not flights:
        return {"status": "not_needed", "reason": "this plan retimes no flight yet to leave"}
    parties = (
        [j.get("party_size") for j in journeys if isinstance(j, Mapping) and not j.get("cancelled")]
        if isinstance(journeys, list)
        else [None]
    )
    told = (
        f"{sum(party for party in parties if isinstance(party, int)):,} passengers told their new"
        " times"
        if all(isinstance(party, int) for party in parties)
        else "passengers told not established"
    )
    on = f"{len(flights)} flight{'' if len(flights) == 1 else 's'}"
    return {"status": "in_plan", "facts": f"{on} · {told}", "flights": flights}


def _dispatch_advice(gateway: PlannerGateway, case: CaseRef) -> dict[str, dict[str, Any]]:
    """FUEL-FLAG and ALT-FLAG: dispatch's advice, with its facts; never a change. Advice never
    stops a plan: an unread advisory is shown as not established."""
    try:
        advice = gateway.dispatch_advisories(case)
    # A system that cannot answer raises CloudDependencyError, a RuntimeError (ops_app.engine).
    except (RuntimeError, ValueError) as error:
        logger.warning("Dispatch advisories unavailable for %s: %s", case.trace, error)
        unread = {"status": "advice", "facts": "dispatch advice not established"}
        return {"FUEL-FLAG": dict(unread), "ALT-FLAG": dict(unread)}
    window = advice.get("exposure_window") or {}
    arrivals = [f for f in advice.get("flights") or [] if isinstance(f, Mapping)]
    words = str(arrivals[0].get("advice")) if arrivals else "no arrival to advise"
    alternates = [a for a in advice.get("alternates") or [] if isinstance(a, Mapping)]
    hazards = sorted({str(a.get("hazard")) for a in alternates})
    clear = all(hazard.startswith("no forecast hazard") for hazard in hazards)
    places = ", ".join(f"{a.get('city')} {a.get('distance_km')} km" for a in alternates)
    return {
        "FUEL-FLAG": {
            "status": "advice",
            "facts": f"{len(arrivals)} arrivals into Lisbon"
            f" {_hhmm(window.get('start_local'))}–{_hhmm(window.get('end_local'))}: {words}",
        },
        "ALT-FLAG": {
            "status": "advice",
            "facts": f"{places}: {'no forecast hazard' if clear else '; '.join(hazards)}",
        },
    }


def _maintenance_estimate(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """MX-ETR: maintenance's return-to-service estimate, as advice (the commander accepts any
    deferral)."""
    defect = document.get("defect")
    if not isinstance(defect, Mapping) or not defect.get("unavailable_until"):
        return None
    return {
        "status": "advice",
        "facts": f"{defect.get('aircraft_reg')} back in service"
        f" {_hhmm(defect['unavailable_until'])} ({defect.get('unavailable_minutes')} min"
        " estimate)",
    }


def _priced(
    option: Mapping[str, Any],
    originals: Mapping[str, Mapping[str, Any]],
    journeys: Any,
    rebooking: Mapping[str, Any],
    cause: Any,
) -> dict[str, Any]:
    """An option priced as every plan is: its flights against the case's originals, with
    Booking's journeys and seats; weather owes no compensation (extraordinary circumstances)."""
    return estimate(
        option["flights"], originals, journeys, rebooking.get("seats"), weather=cause == "weather"
    )


EXPOSURE_BASIS = (
    "Computed by ARROW from Booking's journeys for this option with the rules every plan is"
    " priced by: EU261 compensation per booking by distance band (Arts 5 and 7), care (Art 9)"
    " and rebooking; indicative"
)
OWED_REASON = "No extraordinary circumstance is assumed: compensation is owed where EU261 says so."


class Settled(NamedTuple):
    """Where the domains' agreement left one aircraft option: the option as the operations
    control system offers it at the agreed delay (None when it is not offered for the cause, or
    cannot take the extra delay asked), the cause, that delay, the standby crews it calls and
    why it cannot settle (``conflict``)."""

    option: dict[str, Any] | None
    cause: Any
    adjustment: int
    reserves: list[dict[str, Any]]
    conflict: bool | str


def settle(
    gateway: PlannerGateway,
    case: CaseRef,
    option_id: str,
    chosen: Sequence[str],
    rejected: list[str],
    extra_delay_minutes: int = 0,
) -> Settled:
    """Cross-domain agreement on one option's delay, as every plan and the EU261 exposure read
    it: each domain says how much later this aircraft's departures must move (Crew: when each
    chosen standby crew can take its first departure; Slots: LIS hourly capacity), and the
    option is read again at that delay, at most MAX_ROUNDS times. Only a delay option can move,
    and only flights of this aircraft. Lever problems are added to ``rejected``."""
    adjustment = extra_delay_minutes
    conflict: bool | str = False
    reserves: list[dict[str, Any]] = []
    late: list[str] = []
    option: dict[str, Any] | None = None
    cause: Any = None
    for round_number in range(1, MAX_ROUNDS + 1):
        candidates = gateway.options(case, adjustment)
        cause = candidates.get("cause")
        allowed = ALLOWED_AIRCRAFT_KINDS.get(str(cause), DEFAULT_AIRCRAFT_KINDS)
        option = next(
            (
                item
                for item in candidates.get("options") or []
                if item.get("option_id") == option_id
            ),
            None,
        )
        if option is None or option.get("kind") not in allowed:
            rejected.append(f"aircraft option {option_id} not offered for {cause}")
            return Settled(None, cause, adjustment, [], False)
        if extra_delay_minutes and option["kind"] != "delay":
            rejected.append("extra delay applies only to a delay option")
            return Settled(None, cause, adjustment, [], False)
        needed = adjustment
        reserves, late = [], []
        if chosen:
            offered = {
                item.get("reserve_option_id"): item
                for item in gateway.reserve_options(case, option["option_id"], adjustment).get(
                    "options"
                )
                or []
                if isinstance(item, Mapping)
            }
            missing = next((item for item in chosen if item not in offered), None)
            if missing is not None:
                rejected.append(f"standby crew {missing} not offered")
                break
            reserves = [dict(offered[item]) for item in chosen]
            if duty := _twice([reserve.get("duty_id") for reserve in reserves]):
                rejected.append(f"two standby crews for duty {duty}")
                reserves = []
                break
            flown = {flight["flight_no"] for flight in option["flights"]}
            for reserve in reserves:
                gap = _reserve_gap(reserve)
                if gap is None or gap <= 0:
                    continue
                first = reserve["flights_for_reserve"][0]
                if first not in flown:
                    # Its departure is not this plan's to move: the crew cannot take it.
                    conflict = (
                        f"{standby_name([str(reserve['reserve_option_id'])])} cannot take"
                        f" {first} in time, and this plan does not move it"
                    )
                    break
                needed = max(needed, adjustment + 5 * math.ceil(gap / 5))
                late.append(str(reserve["reserve_option_id"]))
            if conflict:
                break
        if option["kind"] == "delay":
            slots = gateway.assessment("slots", case, option["option_id"], adjustment)
            required = slots.get("required_additional_delay_minutes")
            if slots.get("capacity_fit") is False and isinstance(required, int):
                needed = max(needed, required)
        if needed == adjustment:
            break
        if cause == "weather":
            # Schedule sequences the weather at the reduced rate and refuses added delay: blocked,
            # naming what asked for the extra minutes.
            conflict = (
                f"{standby_name(late)} cannot report in time, and a weather plan takes no added"
                " delay"
                if late
                else "a weather plan follows the reduced hourly rate; it takes no added delay"
            )
            break
        if (
            option["kind"] != "delay"
            or needed > MAX_ADJUSTMENT_MINUTES
            or round_number == MAX_ROUNDS
        ):
            conflict = True
            break
        adjustment = needed
    assert option is not None  # every round either returns, breaks with an option, or loops
    return Settled(option, cause, adjustment, reserves, conflict)


def compensation_exposure(
    gateway: PlannerGateway,
    document: Mapping[str, Any],
    option_id: str,
    additional_delay_minutes: int,
) -> dict[str, Any]:
    """The EU261 exposure of one option of the case (scenario: the Passenger Re-accommodation
    Agent computes it): the passengers owed compensation by band and the euros, care and
    rebooking. Read at the delay the domains agree the option needs (``settle``, as for a plan,
    without standby crews, which the Passenger agent does not choose) and priced by the same
    code, so it equals the plan built from that option. ValueError when the option is not
    offered or cannot take the delay asked."""
    case = case_ref(document)
    refused: list[str] = []
    settled = settle(gateway, case, option_id, (), refused, additional_delay_minutes)
    option, cause, priced = settled.option, settled.cause, settled.adjustment
    if option is None:
        raise ValueError("; ".join(refused))
    rebooking = gateway.rebooking(case, option_id, priced)
    cost = _priced(option, originals_of(document), rebooking.get("journeys"), rebooking, cause)
    workings = cost["workings"]
    not_owed = workings.get("compensation_not_owed")
    weather = cause == "weather"
    return {
        "option_id": option_id,
        "additional_delay_minutes": additional_delay_minutes,
        # The delay it is priced at: more than asked when Slots needs it (Lisbon's capacity).
        "priced_additional_delay_minutes": priced,
        "extraordinary_circumstances": weather,
        "reason": NOT_OWED_REASON if weather else OWED_REASON,
        "compensation_eur": cost["compensation_eur"],
        "passengers_owed_compensation": cost["owed_compensation_passengers"],
        "compensation_by_band": workings["compensation"],
        # Weather: what would otherwise have been owed, never counted.
        **({"compensation_not_owed": not_owed} if not_owed else {}),
        "care_eur": cost["care_eur"],
        "care": cost["care"],
        "meals_passengers": cost["meals_passengers"],
        "overnight_passengers": cost["overnight_passengers"],
        "hotel_rooms": (workings.get("overnight") or {}).get("rooms", 0),
        "rebooking_eur": cost["rebooking_eur"],
        "basis": EXPOSURE_BASIS,
    }


def build_plan(
    gateway: PlannerGateway,
    document: Mapping[str, Any],
    levers: Levers,
    limits: Limits,
    *,
    extra_delay_minutes: int = 0,
) -> dict[str, Any]:
    """One plan, routed and recorded under ``limits``: the rule version in force now.

    ``extra_delay_minutes`` (a what-if) moves a delay option at least that much later; the
    domains may then need more."""
    case = case_ref(document)
    trace = case.trace
    originals = originals_of(document)
    rejected: list[str] = []
    # One standby crew per breached duty, each called once.
    chosen = list(levers.reserve_option_ids)
    if twice := _twice(chosen):
        rejected.append(f"standby crew {twice} chosen twice")
        chosen = []
    # Playbook actions are ids from the catalogue, each named once.
    rejected.extend(lever_problems(levers.playbook_actions))
    asked = list(dict.fromkeys(levers.playbook_actions))
    disruption = document.get("disruption")
    recorded = disruption.get("type") if isinstance(disruption, Mapping) else None
    steps: dict[str, dict[str, Any]] = {}
    settled = settle(
        gateway, case, levers.aircraft_option_id, chosen, rejected, extra_delay_minutes
    )
    option, cause, adjustment = settled.option, settled.cause, settled.adjustment
    if option is None:
        return _rejected(document, levers, rejected, limits, cause)
    reserves, conflict = settled.reserves, settled.conflict
    option_id = option["option_id"]
    assessments = [gateway.assessment(domain, case, option_id, adjustment) for domain in DOMAINS]
    rebooking = gateway.rebooking(case, option_id, adjustment)
    journeys = rebooking.get("journeys")
    # The plan's bookings as Booking read them, whatever rebook says (PAX-NOTICE counts them).
    bookings = journeys
    if not levers.rebook:
        rebooking = {
            **rebooking,
            "rebooked_passengers": 0,
            "unplaced_passengers": rebooking.get("disrupted_passengers"),
            "plan_digest": None,
        }
        # Passengers Booking would have rebooked are stuck where their journey breaks instead
        # (Booking reports that as ``not_rebooked``); without it the cost is unknown.
        if isinstance(journeys, list):
            declined: list[Any] = []
            for journey in journeys:
                if not journey.get("rebooked"):
                    declined.append(journey)
                elif isinstance(journey.get("not_rebooked"), Mapping):
                    declined.append(
                        {
                            **journey,
                            **journey["not_rebooked"],
                            "rebooked": False,
                            "departs_new": None,
                            "arrives_new": None,
                        }
                    )
            complete = len(declined) == len(journeys)
            journeys = declined if complete else None
    called = [str(reserve["reserve_option_id"]) for reserve in reserves]
    duties = gateway.duty_plan(case, option_id, adjustment, called)
    stored = document.get("airports")
    airports = (
        dict(stored) if isinstance(stored, Mapping) and stored else airport_rules(gateway, trace)
    )
    validation = validate(
        option, originals=originals, rebooking=rebooking, airports=airports, duty_plan=duties
    )
    # Delay messages follow from the plan's own flights, as the executor sends them: a departure
    # leaving more than 15 min late needs one (an ICAO DLA to EUROCONTROL's Network Manager).
    # The lever records that fact whatever was asked, so the same option asked with it on or off
    # is one plan, never refused (live run 4: a cancellation that also flew flights late).
    moved = [
        flight["flight_no"]
        for flight in option["flights"]
        if flight.get("status") != "cancelled"
        and flight["flight_no"] in originals
        and _minutes(flight["std_local"], originals[flight["flight_no"]]["std_local"])
        > EOBT_MESSAGE_MINUTES
    ]
    levers = levers.model_copy(update={"delay_messages": bool(moved)})
    kind = disruption_type(recorded, cause)
    unknowns: list[str] = []
    try:
        cost: dict[str, Any] | None = _priced(option, originals, journeys, rebooking, cause)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        logger.warning("Cost estimate unavailable for %s: %s", trace, error)
        cost = None
        unknowns.append(f"estimated cost: {error}")
    if _uses("PAX-NOTICE", asked, kind):
        steps["PAX-NOTICE"] = _passenger_notice(gateway, case, option, originals, bookings)
    if _uses("FUEL-FLAG", asked, kind) or _uses("ALT-FLAG", asked, kind):
        advice = _dispatch_advice(gateway, case)
        steps.update({key: advice[key] for key in advice if key in asked})
    if kind == "aircraft_defect" and (estimate_back := _maintenance_estimate(document)):
        steps["MX-ETR"] = estimate_back
    crew_plan = validation.get("crew_plan") or {}
    disrupted = (validation.get("rebooking") or {}).get("disrupted_passengers")
    over = crew_plan.get("breaches_unresolved")

    plan: dict[str, Any] = {
        **{
            key: value
            for key, value in option.items()
            if key not in {"feasible", "approval_ready", "reason", "option_id"}
        },
        "aircraft_option_id": option_id,
        "trace_id": trace,
        "cause": cause,
        "levers": levers.model_dump(),
        "adjustment_minutes": adjustment,
        # ARROW's check of the plan's flights behind its delay_messages lever.
        "delay_messages_required": bool(moved),
        "departures_moved_over_15_min": len(moved),
        "domain_assessments": assessments,
        # Every standby crew the plan calls, as Crew offered it; crew_reserve, the first, for
        # readers of plans built before several standby crews.
        "crew_reserves": reserves,
        "crew_reserve": reserves[0] if reserves else None,
        "cost": cost,
        "conflict": conflict,
        "rejected_levers": rejected,
        "unknowns": unknowns,
        "validation": validation,
        # The playbook actions carried out, with their facts and what the executor sends; the
        # checklist reads them.
        "playbook_steps": steps,
        # The figures OCC compares plans by (OCC playbook spec §3).
        "outcomes": {
            "stranded_overnight": cost.get("overnight_passengers") if cost else None,
            "lose_flight_or_connection": disrupted if isinstance(disrupted, int) else None,
            "crews_over_limit": over if isinstance(over, int) else None,
        },
    }
    _record_playbook(plan, document, cause, originals)
    verdict = classify(plan, originals=originals, validation=validation, limits=limits)
    plan["policy"] = verdict.as_dict()
    plan["requires_duty_manager"] = verdict.route == "occ"
    # A plan is identified by its content; its aircraft option stays in aircraft_option_id.
    plan["plan_id"] = plan["option_id"] = plan_id(plan, document.get("version"))
    return plan
