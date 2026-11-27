"""The case as the Case Assistant reads it (case chat spec §3): the report and ARROW's reading,
each plan by the number and name its card shows (with the levers a what-if can start from, its
playbook checklist and outcomes), the agents' findings and what ARROW carried out (for a storm,
each action's stage and what the held part waits for).

Counts, flights, airports, times and euros only: never the trace id, a booking reference or a
name. At most 16,000 characters of the text the Case Assistant is given (``digest_text``): the
cost workings go first, then flights beyond 8 a plan, then checklist actions a plan does not
need.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from ops_app.engine import reading_label
from ops_app.planner import case_ref
from ops_app.playbook import checklist
from ops_app.policy import originals_of, plain_levers, standby_crews

MAX_CHARACTERS = 16_000
# A storm's delay plan follows the reduced airport rate across many aircraft (recovery_view.js).
REDUCED_RATE = re.compile(r"^Accept the reduced (.+?) rate\b", re.IGNORECASE)
MAX_FLIGHTS = 8

logger = logging.getLogger("ops_app")


def visible_plans(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The plans OCC sees, in card order: as stored, less any recorded infeasible."""
    return [
        plan
        for plan in document.get("options") or []
        if isinstance(plan, Mapping) and plan.get("feasible") is not False
    ]


def plan_number(document: Mapping[str, Any], plan_id: Any) -> int | None:
    """The number of the plan's card (1-based), or None for a plan OCC does not see."""
    return next(
        (
            number
            for number, plan in enumerate(visible_plans(document), 1)
            if plan.get("plan_id") == plan_id
        ),
        None,
    )


def _time(value: Any, day: str) -> str | None:
    """Local HH:MM, naming the day when it is not the operating day (past midnight)."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    text = f"{moment:%H:%M}"
    try:
        days = (moment.date() - date.fromisoformat(day)).days
    except ValueError:
        return text
    if days == 1:
        return f"{text} next day"
    return text if days == 0 else f"{text} on {moment:%Y-%m-%d}"


def _assessment(plan: Mapping[str, Any], domain: str) -> Mapping[str, Any]:
    return next(
        (
            item
            for item in plan.get("domain_assessments") or []
            if isinstance(item, Mapping) and item.get("domain") == domain
        ),
        {},
    )


def _checks(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(check.get("key")): check
        for check in (plan.get("policy") or {}).get("checks") or []
        if isinstance(check, Mapping)
    }


def _crew(plan: Mapping[str, Any], day: str) -> str:
    check = _checks(plan).get("crew")
    if check:
        text = f"{check.get('input')} (limit {check.get('limit')}): {check.get('result')}"
        measure = check.get("measure")
        if isinstance(measure, Mapping):
            value, limit = measure.get("value"), measure.get("limit")
            if isinstance(value, int) and isinstance(limit, int) and value > limit:
                text += f", {value - limit} min over"
    else:
        status = _assessment(plan, "crew").get("constraint_status")
        text = f"crew check {status}" if status else "not assessed"
    for reserve in standby_crews(plan):
        # Crew members by count, never by name or id.
        kind = reserve.get("standby_type")
        crew = f"{kind} standby, " if kind in ("home", "airport") else ""
        times = [
            f"{words} {_time(reserve[key], day)}"
            for key, words in (("called_local", "call by"), ("report_local", "report"))
            if reserve.get(key) and _time(reserve[key], day)
        ]
        flights = ", ".join(map(str, reserve.get("flights_for_reserve") or [])) or "the flights"
        text += (
            f"; standby crew {reserve.get('reserve_option_id') or ''}"
            f" ({crew}{len(reserve.get('crew') or [])} crew):"
            f" {', '.join(times) or 'times not recorded'}, flies {flights},"
            f" duty {reserve.get('fdp_minutes')} of {reserve.get('max_fdp_minutes')} min"
        )
    return text


def _slots(plan: Mapping[str, Any]) -> str:
    check = _checks(plan).get("capacity")
    slots = _assessment(plan, "slots")
    if check:
        text = f"{check.get('input')}: {check.get('result')}"
    elif slots.get("capacity_fit") is True:
        text = "fits at LIS"
    elif slots.get("capacity_fit") is False:
        text = "does not fit at LIS"
    else:
        text = "not assessed"
    extra = slots.get("required_additional_delay_minutes")
    if isinstance(extra, int) and extra > 0:
        # Slots' answer is the total extra delay at which LIS fits (any ARROW added included).
        text += f"; with departures {extra} min later"
    return text


def _cost(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    cost = plan.get("cost")
    if not isinstance(cost, Mapping) or cost.get("total_eur") is None:
        return None
    return {
        "total": cost.get("total_eur"),
        "airline": cost.get("airline_eur"),
        "compensation": cost.get("compensation_eur"),
        "care": cost.get("care_eur"),
        "rebooking": cost.get("rebooking_eur"),
        # A storm's plan owes no compensation; its workings say what would have been owed.
        "workings": cost.get("workings"),
    }


def _playbook(
    plan: Mapping[str, Any], originals: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The plan's playbook checklist (stored, else read from what it does): each action's id,
    status and its facts or reason, for the Case Assistant to explain."""
    rows = plan.get("playbook") or checklist(plan, originals)
    return [
        {
            "id": row.get("id"),
            "status": row.get("status"),
            **{key: row[key] for key in ("facts", "reason") if row.get(key)},
        }
        for row in rows
        if isinstance(row, Mapping)
    ]


def _passengers(plan: Mapping[str, Any]) -> dict[str, Any]:
    rebooking = (plan.get("validation") or {}).get("rebooking") or {}
    cost = plan.get("cost")
    cost = cost if isinstance(cost, Mapping) else {}
    disrupted = rebooking.get("disrupted_passengers")
    if not isinstance(disrupted, int):
        disrupted = plan.get("misconnected_passengers")
    return {
        # Shown on the plan cards as "lose flight or connection".
        "disrupted": disrupted,
        "owed_compensation": cost.get("owed_compensation_passengers"),
        "meals": cost.get("meals_passengers"),
        "rebooked": rebooking.get("rebooked_passengers"),
        "passenger_services": rebooking.get("unplaced_passengers"),
        "by_car": rebooking.get("transferred_passengers"),
    }


def _flights(
    plan: Mapping[str, Any], originals: Mapping[str, Mapping[str, Any]], day: str
) -> list[dict[str, Any]]:
    flights = []
    for flight in plan.get("flights") or []:
        if not isinstance(flight, Mapping):
            continue
        cancelled = flight.get("status") == "cancelled"
        was = originals.get(str(flight.get("flight_no")), {}).get("std_local") or flight.get(
            "published_std_local"
        )
        flights.append(
            {
                "flight_no": flight.get("flight_no"),
                "from": flight.get("origin"),
                "to": flight.get("destination"),
                "departs": None if cancelled else _time(flight.get("std_local"), day),
                "departs_was": _time(was, day),
                "arrives": None if cancelled else _time(flight.get("sta_local"), day),
                "status": flight.get("status"),
            }
        )
    return flights


def plan_name(plan: Mapping[str, Any]) -> str:
    """A plan's name as its card shows it (recovery_view.js planName): "Delay the rotation",
    "Delay the rotation, 30 min more", "Cancel AR101 + AR102", "Swap the aircraft", and a storm's
    "Accept the reduced Lisbon rate"."""
    kind = plan.get("kind")
    if kind == "delay":
        reduced = REDUCED_RATE.match(str(plan.get("title") or ""))
        if reduced:
            return f"Accept the reduced {reduced[1]} rate"
        aircraft = {
            flight.get("aircraft_reg")
            for flight in plan.get("flights") or []
            if isinstance(flight, Mapping) and flight.get("aircraft_reg")
        }
        if len(aircraft) > 1:
            return str(plan.get("title") or "Delay the flights")
        extra = (plan.get("levers") or {}).get("extra_delay_minutes")
        more = isinstance(extra, int | float) and not isinstance(extra, bool) and extra > 0
        return f"Delay the rotation, {extra:g} min more" if more else "Delay the rotation"
    if kind == "cancel":
        cancelled = [
            str(flight.get("flight_no"))
            for flight in plan.get("flights") or []
            if isinstance(flight, Mapping)
            and str(flight.get("status") or "").strip().lower() in {"cancelled", "canceled"}
            and flight.get("flight_no")
        ]
        return f"Cancel {' + '.join(cancelled) or 'flights'}"
    if kind == "swap":
        return "Swap the aircraft"
    return str(plan.get("title") or "Plan")


def _plan(
    facts: Mapping[str, Any] | None,
    number: int,
    plan: Mapping[str, Any],
    originals: Mapping[str, Mapping[str, Any]],
    day: str,
) -> dict[str, Any]:
    policy = plan.get("policy") or {}
    asked = plan.get("what_if")
    return {
        "number": number,
        # Named as its card is, so an answer's "Plan 2, Delay the rotation" matches the card.
        "title": plan_name(plan),
        "description": plan.get("title"),
        "kind": plan.get("kind"),
        # What a what-if starts from: the plan's own levers, with no extra delay unless asked
        # (a single reserve_option_id, in plans built by earlier versions, as reserve_option_ids).
        "levers": {"extra_delay_minutes": 0, **plain_levers(plan.get("levers"))},
        # The request a domain agent takes for this plan, as the orchestrator sends it: the
        # option in words and the facts its systems are asked by, never the card number.
        "live_check": _live_check(facts, plan),
        "route": policy.get("route"),
        "reasons": [str(reason) for reason in policy.get("reasons") or []],
        "recommended": plan.get("recommended") is True,
        "reason": plan.get("recommendation_reason"),
        # Asked in the chat: the duty manager's question, when, and the Case Assistant's own
        # wording of it; not who (the chat keeps that).
        "what_if": {
            "question": asked.get("question"),
            "asked_at": asked.get("asked_at"),
            "assumption": asked.get("assumption"),
        }
        if isinstance(asked, Mapping)
        else None,
        "cost": _cost(plan),
        "passengers": _passengers(plan),
        # The figures OCC compares plans by: stranded overnight, losing a flight or connection,
        # crews over their limit.
        "outcomes": plan.get("outcomes"),
        "playbook": _playbook(plan, originals),
        "flights": _flights(plan, originals, day),
        "crew": _crew(plan, day),
        "slots": _slots(plan),
    }


def _live_check(facts: Mapping[str, Any] | None, plan: Mapping[str, Any]) -> str:
    """A domain agent's question about this plan's option: the option in words, then the
    disruption as the systems are asked about it and the option's references."""
    option = (plan.get("levers") or {}).get("aircraft_option_id") or plan.get("aircraft_option_id")
    extra = plan.get("adjustment_minutes") or 0
    asked = json.dumps(dict(facts), ensure_ascii=False) if facts else "not recorded"
    return (
        f"{plan.get('title') or plan_name(plan)}: disruption {asked}, option_id {option},"
        f" additional_delay_minutes {extra}"
    )


def _reading(disruption: Mapping[str, Any]) -> str | None:
    if not disruption or disruption.get("type") == "message":
        return None  # not read yet
    label = reading_label(dict(disruption))
    note = disruption.get("reading")
    return f"{label}: {note}" if note else label


def _without_workings(digest: dict[str, Any]) -> None:
    for plan in digest["plans"]:
        if plan["cost"] is not None:
            plan["cost"].pop("workings", None)


def _first_flights(digest: dict[str, Any]) -> None:
    for plan in digest["plans"]:
        if len(plan["flights"]) > MAX_FLIGHTS:
            plan["flights_not_listed"] = len(plan["flights"]) - MAX_FLIGHTS
            plan["flights"] = plan["flights"][:MAX_FLIGHTS]


def _playbook_in_use(digest: dict[str, Any]) -> None:
    """Each plan's checklist without the actions it does not need and says nothing about."""
    for plan in digest["plans"]:
        plan["playbook"] = [
            row for row in plan["playbook"] if row.get("status") != "not_needed" or "reason" in row
        ]


# What goes, in order, while the digest is longer than the Case Assistant reads.
SHRINK: tuple[Callable[[dict[str, Any]], None], ...] = (
    _without_workings,
    _first_flights,
    _playbook_in_use,
)


def digest_text(digest: Mapping[str, Any]) -> str:
    """The digest as the Case Assistant is given it: compact JSON, the text the limit is on."""
    return json.dumps(digest, ensure_ascii=False, separators=(",", ":"))


def _size(digest: Mapping[str, Any]) -> int:
    return len(digest_text(digest))


def case_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """What the Case Assistant reads about one case."""
    disruption = document.get("disruption") or {}
    day = str(document.get("operating_date") or "")
    originals = originals_of(document)
    execution = document.get("execution") or {}
    found = execution.get("storm") or document.get("storm")
    storm: Mapping[str, Any] | None = found if isinstance(found, Mapping) else None
    case_id = str(document.get("display_id") or "")
    try:
        facts: dict[str, Any] | None = dict(case_ref(document).disruption)
    except ValueError:
        facts = None  # not read yet
    event_id = disruption.get("event_id")
    digest: dict[str, Any] = {
        "case_id": case_id,
        # The disruption as the domain agents ask their systems about it: its type and own
        # fields. An event id only when its source system had recorded it already.
        "disruption": facts,
        **({"event_id": event_id} if event_id else {}),
        "report": disruption.get("message")
        or disruption.get("text")
        or disruption.get("description"),
        "reading": _reading(disruption),
        "status": document.get("status"),
        "stage": document.get("stage"),
        "plans": [
            _plan(facts, number, plan, originals, day)
            for number, plan in enumerate(visible_plans(document), 1)
        ],
        "findings": [
            {"agent": finding.get("agent"), "text": finding.get("text")}
            for finding in document.get("findings") or []
            if isinstance(finding, Mapping)
        ],
        "executed": [
            {
                "label": action.get("label"),
                "status": action.get("status"),
                "message": action.get("message") or action.get("summary"),
                # A storm plan's stage: now, when_confirmed (held) or stand_down.
                **({"stage": action.get("stage") or "now"} if storm else {}),
            }
            for action in execution.get("actions") or []
            if isinstance(action, Mapping)
        ],
    }
    if storm:
        # Held until confirmed, confirmed or stood down: what confirms or stands it down, the
        # time OCC decides by, and each weather update with when it came.
        digest["storm"] = {
            "execution_status": execution.get("status"),
            **{
                key: storm.get(key)
                for key in (
                    "state",
                    "decide_by",
                    "confirm_by",
                    "stand_down_by",
                    "planned",
                    "returned",
                    "worse",
                    "confirmed_after_stand_down",
                )
                if key in storm
            },
            "updates": [
                {
                    key: update.get(key)
                    for key in ("at", "status", "window", "rate_per_hour", "description")
                }
                for update in storm.get("updates") or []
                if isinstance(update, Mapping)
            ],
        }
    trace = str(document.get("trace_id") or "")
    text = json.dumps(digest)
    if trace and trace in text:
        # Free text written by an agent or a system never carries the internal trace id out.
        digest = json.loads(text.replace(trace, case_id))
    for shrink in SHRINK:
        if _size(digest) <= MAX_CHARACTERS:
            break
        shrink(digest)
    if _size(digest) > MAX_CHARACTERS:
        # Nothing more is cut silently: the facts stay whole and the length is reported.
        logger.warning("Case digest of %s is %d characters", case_id, _size(digest))
    return digest
