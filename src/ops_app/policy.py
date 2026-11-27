"""Policy engine (spec §8): management by exception, deterministic and outside the LLM.

A plan within every domain's limits runs automatically and OCC is informed. Aircraft changes,
cancellations, storm plans, delays at the EU261 care thresholds, high cost, many rebookings,
airport requests, more than two standby crews and a base's last standby crew need OCC. A
flight-time breach the standby crews do not resolve (none called, or not enough) is blocked: the
case goes to OCC for manual intervention, never as a plan to approve.
Unknown is never clearance: a missing, unknown or contradictory domain check blocks the plan, and
a missing cost estimate, flight distance or count of standby crews left (business thresholds OCC
can judge) sends it to OCC, never to automatic execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from airline_systems.operating_day import CITY

MAX_PLANS = 4
CREW_LIMIT_REASON = "flight-time limit exceeded with no standby crew"
# Standby crews took some legs, yet a duty still breaks its limit (a storm that breaks more
# duties than there are standby crews).
CREW_AFTER_STANDBY_REASON = "flight-time limit still exceeded after the standby crew"
CREW_AFTER_STANDBY_CREWS_REASON = "flight-time limit still exceeded after the standby crews"
# Plans built by earlier versions word the same breaches with "reserve crew"; they are still read.
CREW_AFTER_RESERVE_REASON = "flight-time limit still exceeded after the reserve crew"
CREW_BREACH_REASONS = frozenset(
    {
        CREW_LIMIT_REASON,
        CREW_AFTER_STANDBY_REASON,
        CREW_AFTER_STANDBY_CREWS_REASON,
        "flight-time limit exceeded with no reserve crew",
        CREW_AFTER_RESERVE_REASON,
    }
)
CREW_CONNECTION_REASON = "crew cannot reach a flight of the new aircraft rotation in time"
# Final review: a plan a forecast drives across the network is a major decision under
# the scenario's autonomy levels, so every storm plan that is not blocked goes to OCC.
WEATHER_REASON = "weather: a forecast moves flights across the network"
# Standby crews (reserve crew practice research; fixed in code): an automatic plan
# calls at most two, and never a base's last complete standby crew of a qualification.
MAX_STANDBY_CREWS = 2
STANDBY_LEFT_UNKNOWN = "standby crews left today not established"
STANDBY_TIME_UNKNOWN = "standby crew report time not established"
# What the executor says when a crew taken since an automatic plan would leave a base none.
STANDBY_CHANGED = "standby crews changed since the plan: OCC decides"
QUALIFICATIONS = {"A320F": "A320", "A330": "A330"}
CAPACITY_FAILED = "LIS capacity check failed"
CAPACITY_UNFIT = "LIS capacity does not fit"
ALWAYS_OCC = "Always OCC"


@dataclass(frozen=True)
class Rule:
    """A rule as the operator sees it; ``words`` map an older plan's reasons to it."""

    key: str
    name: str
    source: str
    effect: Literal["OCC decides", "Blocks plan", "Passenger Services decides"]
    group: Literal["limit", "occ", "safety", "expert"]
    words: tuple[str, ...]

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "name": self.name,
            "source": self.source,
            "effect": self.effect,
            "group": self.group,
        }


RULE_CATALOGUE = (
    Rule(
        "cost",
        "Cost limit for automatic plans",
        "ARROW rules, editable with four-eyes approval",
        "OCC decides",
        "limit",
        ("cost",),
    ),
    Rule(
        "rebooking",
        "Rebooking limit",
        "ARROW rules, editable with four-eyes approval; Passenger agent L3",
        "OCC decides",
        "limit",
        ("passenger", "rebook"),
    ),
    Rule(
        "aircraft",
        "Aircraft change or standby",
        "Aircraft agent L2 recommends; OCC decides",
        "OCC decides",
        "occ",
        ("aircraft change",),
    ),
    Rule(
        "cancellation",
        "Cancellation",
        "OCC keeps authority over major decisions",
        "OCC decides",
        "occ",
        ("cancellation",),
    ),
    Rule(
        "airport",
        "Airport and slot requests",
        "Declared airport rules",
        "OCC decides",
        "occ",
        ("airport", "quota", "curfew", "slot request"),
    ),
    Rule(
        "declined_rebooking",
        "Declining to rebook",
        "A passenger decision for OCC",
        "OCC decides",
        "occ",
        ("rebooking declined",),
    ),
    Rule(
        "weather",
        "Weather plans",
        "A forecast moves flights across the network: a major decision for OCC",
        "OCC decides",
        "occ",
        ("weather",),
    ),
    Rule(
        "standby",
        "Standby crews",
        "Standby cover for the rest of the day: more than two crews, or a base's last, is OCC's",
        "OCC decides",
        "occ",
        ("standby crews called", "last standby crew", "standby crews left"),
    ),
    Rule(
        "crew",
        "Crew flight-time limits",
        "EASA flight-time limits; Crew agent L3",
        "Blocks plan",
        "safety",
        ("crew", "flight-time"),
    ),
    Rule(
        "eu261",
        "Long delays",
        "EU261 Art. 6 care thresholds",
        "OCC decides",
        "safety",
        ("eu261",),
    ),
    Rule(
        "capacity",
        "Lisbon capacity",
        "Hourly capacity; Slot agent L3",
        "Blocks plan",
        "safety",
        ("lis capacity",),
    ),
    Rule(
        "answered",
        "Every check answered",
        "A missing or unknown answer is never a pass",
        "Blocks plan",
        "safety",
        (),
    ),
    Rule(
        "passenger_services",
        "Passengers without rebooking",
        "Rebooking policy; Passenger Services expert",
        "Passenger Services decides",
        "expert",
        ("passenger services",),
    ),
)
_RULE_NAMES = {rule.key: rule.name for rule in RULE_CATALOGUE}


@dataclass(frozen=True)
class Limits:
    """The airline's editable autonomy limits in one rule version (control tower and rules
    spec §3)."""

    version: int
    cost_cap_eur: int
    rebook_limit: int

    def as_dict(self) -> dict[str, int]:
        return {
            "version": self.version,
            "cost_cap_eur": self.cost_cap_eur,
            "rebook_limit": self.rebook_limit,
        }


RELEASE_LIMITS = Limits(1, 20_000, 10)
COST_CAP_EUR = RELEASE_LIMITS.cost_cap_eur
REBOOK_LIMIT = RELEASE_LIMITS.rebook_limit


def limits_of(option: Mapping[str, Any]) -> Limits:
    """The limits a plan was built under; a plan without valid recorded limits uses version 1."""
    policy = option.get("policy")
    recorded = policy.get("limits") if isinstance(policy, Mapping) else None
    if not isinstance(recorded, Mapping):
        return RELEASE_LIMITS
    keys = ("version", "cost_cap_eur", "rebook_limit")
    version, cost_cap, rebook = (_count(recorded.get(key)) for key in keys)
    if version is None or cost_cap is None or rebook is None or version < 1:
        return RELEASE_LIMITS
    return Limits(version, cost_cap, rebook)


@dataclass(frozen=True)
class PolicyVerdict:
    route: Literal["auto", "occ", "blocked"]
    reasons: tuple[str, ...]
    limits: Limits
    checks: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reasons": list(self.reasons),
            "version": self.limits.version,
            "limits": self.limits.as_dict(),
            "checks": [dict(check) for check in self.checks],
        }


def originals_of(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Scheduled flights ARROW read for this case: the rotation, then the affected flights."""
    rotation = document.get("rotation")
    flights = rotation.get("flights", []) if isinstance(rotation, Mapping) else []
    known = [*flights, *(document.get("affected_flights") or [])]
    return {
        flight["flight_no"]: dict(flight)
        for flight in known
        if isinstance(flight, Mapping) and isinstance(flight.get("flight_no"), str)
    }


def standby_ids(record: Mapping[str, Any] | None) -> list[str]:
    """The standby crew options levers, a crew plan or an action payload name, in order: its
    ``reserve_option_ids``, with a single ``reserve_option_id`` (written by earlier versions)
    first."""
    record = record if isinstance(record, Mapping) else {}
    listed = record.get("reserve_option_ids")
    ids = [str(item) for item in listed if item] if isinstance(listed, list) else []
    single = record.get("reserve_option_id")
    if isinstance(single, str) and single and single not in ids:
        ids.insert(0, single)
    return ids


def standby_crews(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The standby crew options a plan calls: ``crew_reserves``, or one ``crew_reserve`` (plans
    built by earlier versions)."""
    listed = plan.get("crew_reserves")
    if isinstance(listed, list):
        return [item for item in listed if isinstance(item, Mapping)]
    single = plan.get("crew_reserve")
    return [single] if isinstance(single, Mapping) else []


def plain_levers(levers: Mapping[str, Any] | None) -> dict[str, Any]:
    """A plan's levers as plans are built now: a single ``reserve_option_id`` (from earlier
    versions) is read as ``reserve_option_ids``; a plan built before the playbook named no
    playbook actions."""
    found = dict(levers) if isinstance(levers, Mapping) else {}
    ids = standby_ids(found)
    found.pop("reserve_option_id", None)
    listed = found.get("playbook_actions")
    return {
        **found,
        "reserve_option_ids": ids,
        "playbook_actions": list(listed) if isinstance(listed, list) else [],
    }


def lever_key(levers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Levers as two plans compare them: the playbook actions in any order (Task 1 review), and
    without delay_messages, which ARROW sets from the plan's own flights, so the same option
    asked with delay messages on or off is one plan."""
    plain = plain_levers(levers)
    plain.pop("delay_messages", None)
    return {**plain, "playbook_actions": sorted(map(str, plain["playbook_actions"]))}


def playbook_steps(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The playbook actions the planner carried out for a plan (Task 3), by id."""
    steps = plan.get("playbook_steps")
    if not isinstance(steps, Mapping):
        return {}
    return {str(key): step for key, step in steps.items() if isinstance(step, Mapping)}


def standby_name(ids: list[str]) -> str:
    """Standby crews by option id: "standby crew R1", "standby crews R1, R2"."""
    return f"standby crew{'' if len(ids) == 1 else 's'} {', '.join(ids)}"


def standby_times(crew: Mapping[str, Any]) -> str:
    """When crew control calls a standby crew and when it reports: "call by 15:54, report
    16:54" (plans built by earlier versions recorded only the report)."""
    times = [
        f"{words} {str(crew[key])[11:16]}"
        for key, words in (("called_local", "call by"), ("report_local", "report"))
        if isinstance(crew.get(key), str) and len(str(crew[key])) >= 16
    ]
    return ", ".join(times) or "times not recorded"


def added_delay_minutes(
    option: Mapping[str, Any], originals: Mapping[str, Mapping[str, Any]]
) -> int | None:
    largest = 0
    for flight in option.get("flights") or []:
        if not isinstance(flight, Mapping) or flight.get("status") == "cancelled":
            continue
        original = originals.get(str(flight.get("flight_no")))
        try:
            shift = datetime.fromisoformat(str(flight["std_local"])) - datetime.fromisoformat(
                str(original["std_local"])  # type: ignore[index]
            )
        except (KeyError, TypeError, ValueError):
            return None
        largest = max(largest, round(shift.total_seconds() / 60))
    return largest


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def care_threshold_minutes(flight: Mapping[str, Any]) -> int | None:
    """EU261 Art. 6 care threshold for a flight's departure delay; None if distance is unknown."""
    distance = flight.get("distance_km")
    if not isinstance(distance, int | float) or isinstance(distance, bool):
        return None
    if distance <= 1500:
        return 120
    if distance <= 3500 or flight.get("intra_eu") is True:
        return 180
    return 240


def _delays(
    flights: list[Mapping[str, Any]], originals: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, int, int | None, Any]]:
    """Each flown, delayed flight: its number, added minutes, EU261 care threshold and distance."""
    delayed = []
    for flight in flights:
        if flight.get("status") == "cancelled":
            continue
        original = originals.get(str(flight.get("flight_no")), {})
        try:
            shift = datetime.fromisoformat(str(flight["std_local"])) - datetime.fromisoformat(
                str(original["std_local"])
            )
        except (KeyError, TypeError, ValueError):
            continue  # added_delay_minutes already blocks unknown originals
        delay = round(shift.total_seconds() / 60)
        if delay > 0:
            delayed.append(
                (
                    str(flight.get("flight_no")),
                    delay,
                    care_threshold_minutes(flight),
                    flight.get("distance_km"),
                )
            )
    return delayed


def _delay_reasons(delays: list[tuple[str, int, int | None, Any]]) -> list[str]:
    """OCC reasons from each flown flight's departure delay against the EU261 care thresholds."""
    occ = []
    for flight_no, delay, threshold, _ in delays:
        if threshold is None:
            occ.append(f"{flight_no} distance unknown: EU261 care threshold unknown")
        elif delay >= threshold:
            occ.append(
                f"{flight_no} departs {delay} min late: EU261 care threshold ({threshold // 60} h)"
            )
    return occ


def _assessments(option: Mapping[str, Any]) -> dict[Any, Mapping[str, Any]]:
    return {
        item.get("domain"): item
        for item in option.get("domain_assessments") or []
        if isinstance(item, Mapping)
    }


def _check_reasons(option: Mapping[str, Any], validation: Mapping[str, Any]) -> list[str]:
    checks = _assessments(option)
    reasons = []
    for domain, label in (("crew", "crew"), ("slots", "LIS capacity"), ("booking", "passenger")):
        status = checks.get(domain, {}).get("constraint_status")
        if status is None:
            reasons.append(f"{label} check missing")
        elif status == "unknown":
            reasons.append(f"{label} check unknown")
        elif status == "fail" and domain == "slots":
            reasons.append(CAPACITY_FAILED)
    if checks.get("slots", {}).get("capacity_fit") is False:
        reasons.append(CAPACITY_UNFIT)
    rebooking = validation.get("rebooking") or {}
    disrupted, rebooked, unplaced = (
        _count(rebooking.get(key))
        for key in ("disrupted_passengers", "rebooked_passengers", "unplaced_passengers")
    )
    if disrupted is None or rebooked is None or unplaced is None:
        reasons.append("passenger rebooking not established")
    else:
        card = option.get("misconnected_passengers")
        if rebooked + unplaced != disrupted or (isinstance(card, int) and card != disrupted):
            reasons.append("passenger counts disagree")
    unknown = validation.get("airport_rules_unknown")
    if not isinstance(unknown, list):
        reasons.append("airport rules not established")
    elif unknown:
        reasons.append(f"airport rules unknown: {', '.join(map(str, unknown))}")
    crew_plan = validation.get("crew_plan") or {}
    digest = crew_plan.get("plan_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or _count(crew_plan.get("breaches_unresolved")) is None
    ):
        reasons.append("crew duty plan not established")
    # Each standby crew's first departure and the earliest it can take it, as Crew said (plans
    # built by current versions): without them no gap was planned.
    listed = option.get("crew_reserves")
    if isinstance(listed, list) and not all(
        isinstance(crew, Mapping)
        and all(_moment(crew.get(key)) for key in ("departure_local", "earliest_departure_local"))
        for crew in listed
    ):
        reasons.append(STANDBY_TIME_UNKNOWN)
    if conflict := option.get("conflict"):
        detail = f": {conflict}" if isinstance(conflict, str) else " after 3 agreement rounds"
        reasons.append(f"domains disagree{detail}")
    if rejected := [str(lever) for lever in option.get("rejected_levers") or []]:
        reasons.append(f"lever not offered: {', '.join(rejected)}")
    return list(dict.fromkeys(reasons))


def review_passengers(validation: Mapping[str, Any]) -> int:
    """Passengers without a rebooking within policy; a Passenger Services expert decides (D12)."""
    unplaced = (validation.get("rebooking") or {}).get("unplaced_passengers")
    return unplaced if isinstance(unplaced, int) and not isinstance(unplaced, bool) else 0


def _check(
    key: str, value: str, limit: str, result: str, measure: tuple[Any, int] | None = None
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "key": key,
        "rule": _RULE_NAMES[key],
        "input": value,
        "limit": limit,
        "result": result,
    }
    if measure is not None:
        check["measure"] = {"value": measure[0], "limit": measure[1]}
    return check


def _passengers(count: Any) -> str:
    return f"{count} passenger{'' if count == 1 else 's'}"


def _crew_check(
    crew: Mapping[str, Any],
    *,
    breach: bool,
    failed: bool,
    unconnected: bool = False,
    called: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The modelled duty closest to its limit; a breach no standby crew covers blocks the plan."""
    duties = [
        duty
        for duty in crew.get("duties") or []
        if isinstance(duty, Mapping)
        and _count(duty.get("modeled_fdp_minutes")) is not None
        and _count(duty.get("modeled_fdp_limit_minutes"))
    ]
    duty = max(
        duties,
        key=lambda d: d["modeled_fdp_minutes"] / d["modeled_fdp_limit_minutes"],
        default=None,
    )
    limit = f"{duty['modeled_fdp_limit_minutes']} min" if duty else "modelled limit"
    if unconnected:
        where = next(
            (
                str(d["connection"])
                for d in crew.get("duties") or []
                if isinstance(d, Mapping) and d.get("connection")
            ),
            "",
        )
        return _check("crew", f"cannot connect: {where}", "reach each leg in time", "blocked")
    if not breach and crew.get("constraint_status") in (None, "unknown"):
        return _check("crew", "not established", limit, "blocked")
    if not breach and failed:
        who = standby_name(list(called)) if called else "a standby crew"
        covers = "cover the breaches" if len(called) > 1 else "covers the breach"
        return _check("crew", f"{who} {covers}", limit, "pass")
    result = "blocked" if breach else "pass"
    if duty is None:
        if not breach:
            return _check("crew", "no duty modelled", limit, result)
        after = f"after the {standby_name(list(called))}" if called else "no standby crew"
        return _check("crew", f"breach · {after}", limit, result)
    minutes, most = duty["modeled_fdp_minutes"], duty["modeled_fdp_limit_minutes"]
    name = duty.get("duty_id") or "duty"
    return _check("crew", f"{name}: {minutes} min", limit, result, (minutes, most))


def _standby_reasons(
    plan: Mapping[str, Any], crew_plan: Mapping[str, Any], called: list[str]
) -> list[str]:
    """Why a plan's standby crews are OCC's decision: more than two, or a base and
    qualification it calls a crew at left with no complete standby crew. A plan built by an
    earlier version called one crew and counted none left."""
    if not called:
        return []
    reasons = []
    if len(called) > MAX_STANDBY_CREWS:
        reasons.append(f"{len(called)} standby crews called (automatic limit {MAX_STANDBY_CREWS})")
    if "reserve_option_ids" not in crew_plan:
        return reasons
    last = last_standby_crews(standby_crews(plan), crew_plan.get("standby_left"))
    return [*reasons, *(last if last is not None else [STANDBY_LEFT_UNKNOWN])]


def last_standby_crews(crews: list[Mapping[str, Any]], left: Any) -> list[str] | None:
    """Each base and qualification the standby ``crews`` stand by at that ``left`` (Crew's
    ``standby_left``) gives no complete crew: "uses the last standby crew at Lisbon A320". None
    when a crew's base or a count is not established."""
    touched: dict[tuple[str, str], None] = {}
    for crew in crews:
        base, qualification = crew.get("base"), crew.get("qualification")
        if not (isinstance(base, str) and base and isinstance(qualification, str)):
            return None
        touched[(base, qualification)] = None
    if not touched or not isinstance(left, list):
        return None
    counts = {
        (str(entry.get("base")), str(entry.get("qualification"))): _count(entry.get("crews"))
        for entry in left
        if isinstance(entry, Mapping)
    }
    reasons = []
    for base, qualification in touched:
        crews_left = counts.get((base, qualification))
        if crews_left is None:
            return None
        if crews_left == 0:
            reasons.append(
                f"uses the last standby crew at {CITY.get(base, base)}"
                f" {QUALIFICATIONS.get(qualification, qualification)}"
            )
    return reasons


def _standby_check(
    plan: Mapping[str, Any], called: list[str], reasons: list[str]
) -> dict[str, Any]:
    crews = {str(crew.get("reserve_option_id")): crew for crew in standby_crews(plan)}
    called_as = " · ".join(
        f"{item} {standby_times(crews[item])}" if item in crews else item for item in called
    )
    return _check(
        "standby",
        called_as,
        f"{MAX_STANDBY_CREWS} a plan · keep a complete crew at each base",
        "occ" if reasons else "pass",
        (len(called), MAX_STANDBY_CREWS),
    )


def _capacity_check(slots: Mapping[str, Any]) -> dict[str, Any]:
    status = slots.get("constraint_status")
    if status == "fail" or slots.get("capacity_fit") is False:
        return _check("capacity", "does not fit at LIS", "Hourly allocation", "blocked")
    if status in (None, "unknown"):
        return _check("capacity", "not established", "Hourly allocation", "blocked")
    return _check("capacity", "fits at LIS", "Hourly allocation", "pass")


def _eu261_check(delays: list[tuple[str, int, int | None, Any]]) -> dict[str, Any]:
    if not delays:
        return _check("eu261", "none", "120 / 180 / 240 min by distance", "pass")
    late = " · ".join(f"{flight_no} +{delay} min" for flight_no, delay, _, _ in delays)
    thresholds = " · ".join(
        "distance unknown" if threshold is None else f"{threshold} min ({distance:,.0f} km)"
        for _, _, threshold, distance in delays
    )
    return _check("eu261", late, thresholds, "occ" if _delay_reasons(delays) else "pass")


def classify(
    option: Mapping[str, Any],
    *,
    originals: Mapping[str, Mapping[str, Any]],
    validation: Mapping[str, Any] | None,
    limits: Limits,
) -> PolicyVerdict:
    """The plan's route under ``limits``: the version in force when a plan is built, and the
    plan's own recorded limits (``limits_of``) when OCC decides and before execution.

    ``checks`` show each rule's input, limit and result, built from the same values as the
    route and its reasons, so they always agree."""
    checked: Mapping[str, Any] = validation if isinstance(validation, Mapping) else {}
    flights = [f for f in option.get("flights") or [] if isinstance(f, Mapping)]
    assessments = _assessments(option)
    # Owner: without legal crew the aircraft cannot fly the rotation. That is never a plan
    # to approve; the case goes to OCC for manual intervention instead.
    crew_plan = checked.get("crew_plan") or {}
    crew_failed = any(
        isinstance(item, Mapping)
        and item.get("domain") == "crew"
        and item.get("constraint_status") == "fail"
        for item in option.get("domain_assessments") or []
    )
    # A crew that cannot reach a reassigned flight is not a flight-time breach: no standby crew
    # covers it and it is no case for manual intervention; the plan is blocked.
    unconnected = any(
        isinstance(duty, Mapping) and duty.get("connection")
        for duty in assessments.get("crew", {}).get("duties") or []
    )
    # The standby crews Crew's duty plan called (one per breached duty).
    called = standby_ids(crew_plan)
    crew_breach = bool(crew_plan.get("breaches_unresolved")) or (
        crew_failed and not unconnected and not called
    )
    if isinstance(validation, Mapping):
        blocked = _check_reasons(option, validation)
        if added_delay_minutes(option, originals) is None:
            blocked.append("added delay unknown")
        if crew_breach:
            # The reason says what was tried: no standby crew, or crews that were not enough.
            blocked.append(
                CREW_LIMIT_REASON
                if not called
                else CREW_AFTER_STANDBY_REASON
                if len(called) == 1
                else CREW_AFTER_STANDBY_CREWS_REASON
            )
        if unconnected:
            blocked.append(CREW_CONNECTION_REASON)
    else:
        blocked = ["validation missing"]
    blocked = list(dict.fromkeys(blocked))

    new_aircraft = sorted(
        {
            str(flight["aircraft_reg"])
            for flight in flights
            if "aircraft_reg" in flight
            and flight["aircraft_reg"]
            != originals.get(str(flight.get("flight_no")), {}).get(
                "aircraft_reg", flight["aircraft_reg"]
            )
        }
    )
    aircraft_change = option.get("kind") in {"swap", "standby"} or bool(new_aircraft)
    cancelled = [str(f.get("flight_no")) for f in flights if f.get("status") == "cancelled"]
    delays = _delays(flights, originals)
    cost: Any = (option.get("cost") or {}).get("total_eur")
    cost_known = isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0
    over_cost = cost_known and cost > limits.cost_cap_eur
    rebooking = checked.get("rebooking") or {}
    rebooked = _count(rebooking.get("rebooked_passengers"))
    over_rebooking = rebooked is not None and rebooked > limits.rebook_limit
    # Declining to rebook is a passenger decision for OCC, not a way around the rebooking limit.
    disrupted = rebooking.get("disrupted_passengers") or 0
    declined = (option.get("levers") or {}).get("rebook") is False and bool(disrupted)
    requests = [str(request) for request in checked.get("airport_requests") or []]
    # D23: passengers outside rebooking policy go to the Passenger Services expert; they do not
    # by themselves send an otherwise automatic plan to OCC.
    review = review_passengers(checked)
    weather = option.get("cause") == "weather"

    occ: list[str] = []
    if aircraft_change:
        occ.append("aircraft change")
    if cancelled:
        occ.append("cancellation")
    if weather:
        occ.append(WEATHER_REASON)
    standby = _standby_reasons(option, crew_plan, called)
    occ.extend(standby)
    occ.extend(_delay_reasons(delays))
    if not cost_known:
        occ.append("estimated cost not established")
    elif over_cost:
        occ.append(f"estimated cost above €{limits.cost_cap_eur:,} (€{cost:,.0f})")
    if over_rebooking:
        occ.append(f"more than {limits.rebook_limit} passengers rebooked ({rebooked})")
    if declined:
        occ.append(
            f"rebooking declined for {disrupted} passengers who lose their flight or connection"
        )
    occ.extend(requests)
    notes = [f"{review} passengers need Passenger Services review"] if review else []

    version = f"(rules v{limits.version})"
    unanswered = [
        reason
        for reason in blocked
        if reason not in CREW_BREACH_REASONS and reason not in (CAPACITY_FAILED, CAPACITY_UNFIT)
    ]
    kind_change = {"standby": "standby aircraft", "swap": "aircraft swap"}
    checks = (
        _check(
            "cost",
            f"€{cost:,.0f}" if cost_known else "not established",
            f"€{limits.cost_cap_eur:,} {version}",
            "occ" if over_cost or not cost_known else "pass",
            (cost, limits.cost_cap_eur) if cost_known else None,
        ),
        _check(
            "rebooking",
            "not established" if rebooked is None else _passengers(rebooked),
            f"{_passengers(limits.rebook_limit)} {version}",
            "blocked" if rebooked is None else "occ" if over_rebooking else "pass",
            None if rebooked is None else (rebooked, limits.rebook_limit),
        ),
        _check(
            "aircraft",
            f"→ {' · '.join(new_aircraft)}"
            if new_aircraft
            else kind_change.get(str(option.get("kind")), "aircraft change")
            if aircraft_change
            else "same aircraft",
            ALWAYS_OCC,
            "occ" if aircraft_change else "pass",
        ),
        _check(
            "cancellation",
            " · ".join(cancelled) or "none",
            ALWAYS_OCC,
            "occ" if cancelled else "pass",
        ),
        _check(
            "airport", " · ".join(requests) or "none", ALWAYS_OCC, "occ" if requests else "pass"
        ),
        *(
            [_check("declined_rebooking", _passengers(disrupted), ALWAYS_OCC, "occ")]
            if declined
            else []
        ),
        *([_check("weather", "weather forecast", ALWAYS_OCC, "occ")] if weather else []),
        *([_standby_check(option, called, standby)] if called else []),
        _crew_check(
            assessments.get("crew", {}),
            breach=crew_breach,
            failed=crew_failed,
            unconnected=unconnected,
            called=tuple(called),
        ),
        _eu261_check(delays),
        _capacity_check(assessments.get("slots", {})),
        _check(
            "answered",
            " · ".join(unanswered) or "Crew · Passenger · Slot",
            "Unknown blocks",
            "blocked" if unanswered else "pass",
        ),
        _check(
            "passenger_services",
            _passengers(review) if review else "none",
            "Passenger Services decides",
            "expert" if review else "pass",
        ),
    )
    if blocked:
        return PolicyVerdict("blocked", tuple(blocked), limits, checks)
    if occ:
        return PolicyVerdict("occ", ("OCC decides", *occ, *notes), limits, checks)
    return PolicyVerdict(
        "auto", ("within every domain's limits; OCC is informed", *notes), limits, checks
    )
