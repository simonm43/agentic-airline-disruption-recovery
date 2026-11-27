"""The decision trail (business UI and free-text spec §4, D95): one readable view of how ARROW
decided a case.

Built from what is recorded: the orchestrator's steps as Foundry returned them, every airline
system call logged for the case, each agent's prompt, the plans with their policy verdicts and
the execution receipts. Nothing here decides anything; it only describes what happened.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from airline_systems.audit import MCP_TOOLS, action_request, call_request
from airline_systems.audit import endpoint as endpoint_of
from airline_systems.facts import FIELDS as FACTS
from airline_systems.operating_day import CITY
from ops_app.dispatch import RECIPIENTS
from ops_app.engine import CASE_ASSISTANT_AGENT, ORCHESTRATOR_AGENT, reading_label
from ops_app.playbook import checklist
from ops_app.policy import (
    RULE_CATALOGUE,
    limits_of,
    originals_of,
    standby_crews,
    standby_times,
)
from ops_app.reports import storm_subject, storm_title


@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    foundry: str
    tool: str
    level: str
    autonomy: str
    role: str


AGENTS = (
    Agent(
        "detection",
        "Detection",
        "disruption-detection-agent",
        "a2a-detection",
        "L4",
        "Autonomous: reads and records the disruption, changes no flight",
        "Reads the report, records the disruption and predicts its knock-on",
    ),
    Agent(
        "aircraft",
        "Aircraft",
        "aircraft-recovery-agent",
        "a2a-aircraft",
        "L2",
        "Recommends only: an aircraft change needs a person",
        "Finds the aircraft options: delay, swap or cancel",
    ),
    Agent(
        "crew",
        "Crew",
        "crew-recovery-agent",
        "a2a-crew",
        "L3",
        "Acts within pre-approved rules",
        "Checks crew duty limits and finds standby crews",
    ),
    Agent(
        "passenger",
        "Passenger",
        "passenger-reaccommodation-agent",
        "a2a-passenger",
        "L3",
        "Acts within policy limits",
        "Counts passengers who lose their flight or connection, plans rebooking and reports "
        "the EU261 exposure",
    ),
    Agent(
        "slot",
        "Slot",
        "slot-negotiation-agent",
        "a2a-slots",
        "L3",
        "Acts within allocated slots",
        "Checks airport capacity and delay messages",
    ),
)
_BY_TOOL = {agent.tool: agent for agent in AGENTS}
# The Case Assistant (case chat spec §2) answers OCC's questions on a case. The orchestrator
# never asks it, so it is not among the trail's domain agents; its own identity still names its
# calls.
CASE_ASSISTANT = Agent(
    "assistant",
    "Case Assistant",
    CASE_ASSISTANT_AGENT,
    "",
    "L2",
    "Explains and proposes: OCC decides",
    "Answers questions on the case and asks ARROW for what-if plans",
)

SYSTEMS = {
    "schedule": "Operations control",
    "maintenance": "Maintenance",
    "crew": "Crew system",
    "booking": "Passenger system",
    "slots": "Airport capacity",
}
# One vocabulary (owner): a call that writes to a party outside the airline's own systems
# is named by its recipient, as What ARROW did names it (dispatch.RECIPIENTS).
_RECIPIENT_OF = {
    "send_nm_message": RECIPIENTS["network_manager"],
    "update_tobt": RECIPIENTS["airport_cdm"],
    "request_slot": RECIPIENTS["slot_coordinator"],
    "get_slot_request": RECIPIENTS["slot_coordinator"],
    "order_transfer": RECIPIENTS["ground_handling"],
}
# Operations that change an airline system; everything else only reads.
WRITES = frozenset(
    {
        "apply_action",
        "record_defect",
        "record_delay",
        "record_weather",
        "create_recovery_hold",
        "update_flight",
        "submit_duty_changes",
        "submit_rebooking",
        "order_transfer",
        "send_nm_message",
        "update_tobt",
        "request_slot",
        # The OCC playbook's passenger notice, and a storm's standby alerts, calls and releases.
        "send_passenger_notice",
        "alert_standby_crews",
        "call_standby_crews",
        "release_standby_crews",
    }
)
# ARROW's records of a reported disruption in the airline system that owns it. The executor
# makes them as the first action of a plan it carries out (joined to that action by its request
# id); an older run made them at intake, listed apart from its checks.
RECORDS = frozenset({"record_defect", "record_delay", "record_weather"})
# What each call was for, in plain words: the system's own resource it read or wrote (owner:
# "how should I explain that to a normal user?").
OPERATIONS = {
    "get_disruption": "Disruption",
    "get_aircraft_rotation": "Aircraft rotation",
    "get_recovery_options": "Recovery options",
    "get_defect": "Defect",
    "assess_crew": "Crew impact",
    "assess_booking": "Passenger impact",
    "assess_slots": "Capacity impact",
    "get_reserve_options": "Standby crews",
    "get_rebooking_plan": "Rebooking plan",
    "get_duty_plan": "Duty plan",
    "get_airport_rules": "Airport rules",
    "get_flight_summary": "Flight details",
    "get_network": "Today's network",
    "record_delay": "Flight delay",
    "record_defect": "Aircraft defect",
    "record_weather": "Weather forecast",
    "apply_action": "Approved action",
    "create_recovery_hold": "Seat hold",
    "update_flight": "Flight times",
    "submit_duty_changes": "Duty changes",
    "submit_rebooking": "Rebookings",
    "order_transfer": "Transfer order",
    "send_nm_message": "Delay message",
    "update_tobt": "Ready time",
    "request_slot": "Slot request",
    "get_slot_request": "Slot request status",
    "get_dispatch_advisories": "Fuel and alternates",
    "send_passenger_notice": "Passenger notice",
    "alert_standby_crews": "Standby alert",
    "call_standby_crews": "Standby call",
    "release_standby_crews": "Standby release",
}
# The ARROW tools a domain agent calls itself, logged on the case as agent_tool_call events
# (final review build brief B2, B3): the Detection agent records the disruption it read, the
# Passenger agent reads an option's EU261 exposure.
AGENT_TOOLS = {
    "record_aircraft_defect": "Aircraft defect",
    "record_flight_delay": "Flight delay",
    "record_weather_forecast": "Weather forecast",
    "get_compensation_exposure": "EU261 exposure",
}
# The record in the disruption's own system each of the Detection agent's tools stands for: the
# tool records the disruption on ARROW's case, and the executor records it there when a plan runs
# (an older run recorded it at once).
AGENT_RECORDS = {
    "record_aircraft_defect": "record_defect",
    "record_flight_delay": "record_delay",
    "record_weather_forecast": "record_weather",
}
CAUSES = {
    "border_control": "Border-control queue",
    "staff_shortage": "Staff shortage",
    "baggage": "Baggage",
    "late_inbound": "Late inbound aircraft",
    "aircraft_defect": "Aircraft defect",
    "weather": "Weather",
    "other": "Other",
}
DISRUPTION_KINDS = {
    "aircraft_defect": "Aircraft defect",
    "flight_delay": "Flight delay",
    "weather": "Weather forecast",
}
# Which agent's tools call which operation (the agents' allowed tools in Foundry).
_AGENT_OPERATIONS = {
    ("schedule", "get_disruption"): "detection",
    ("schedule", "get_aircraft_rotation"): "detection",
    ("maintenance", "get_defect"): "detection",
    ("schedule", "get_recovery_options"): "aircraft",
    ("crew", "assess_crew"): "crew",
    ("crew", "get_reserve_options"): "crew",
    ("booking", "assess_booking"): "passenger",
    ("booking", "get_rebooking_plan"): "passenger",
    ("slots", "assess_slots"): "slot",
}
TOOL_STEPS = {
    "record_disruption": "Read the message",
    "record_case": "Wrote the case summary",
    "get_playbook": "Read the playbook",
    "build_plan": "Built a plan",
    "submit_plan": "Recommended a plan",
}
RULES = RULE_CATALOGUE


def _text(value: Any) -> str:
    """Readable text from an argument or output that may be JSON, a list of parts or a dict."""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                return _text(json.loads(stripped)) or stripped
            except ValueError:
                return stripped
        return stripped
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, Mapping):
        for key in ("message", "text", "input", "query", "content", "parts", "output", "result"):
            if key in value and (found := _text(value[key])):
                return found
        return ""
    return str(value)


def _facts(output: Any) -> dict[str, Any] | None:
    """An agent's answer as an object when it is a JSON object; plain text or a list is None."""
    if isinstance(output, Mapping):
        return dict(output)
    if isinstance(output, list):  # content parts
        output = "".join(
            str(part.get("text") or "") for part in output if isinstance(part, Mapping)
        )
    if not isinstance(output, str):
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _args(item: Mapping[str, Any]) -> dict[str, Any]:
    raw = item.get("arguments")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _agent_of(item: Mapping[str, Any]) -> Agent | None:
    for key in ("name", "server_label", "agent_name"):
        name = str(item.get(key) or "")
        if name in _BY_TOOL:
            return _BY_TOOL[name]
        for agent in AGENTS:
            if name and (name == agent.foundry or agent.key in name):
                return agent
    return None


def _steps(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The orchestrator's run in order, in words: what it read, asked, heard and did."""
    steps: list[dict[str, Any]] = []
    asked: dict[str, dict[str, Any]] = {}
    for item in items:
        kind = str(item.get("type") or "")
        ok = item.get("status") in (None, "completed") and not item.get("error")
        if kind == "mcp_call":
            # Foundry returns a tool's refusal as ordinary output (AR444 run).
            ok = ok and not _text(item.get("output")).startswith("Error executing tool")
            name = str(item.get("name") or "")
            args = _args(item)
            detail = {
                "record_disruption": args.get("reading"),
                "record_case": args.get("summary"),
                "build_plan": (args.get("levers") or {}).get("aircraft_option_id")
                if isinstance(args.get("levers"), Mapping)
                else None,
                "submit_plan": args.get("reason"),
            }.get(name)
            steps.append(
                {
                    "kind": "tool",
                    "tool": name,
                    "title": TOOL_STEPS.get(name, name.replace("_", " ").capitalize()),
                    "detail": _text(detail) if detail else "",
                    "result": _text(item.get("error")) or _text(item.get("output")),
                    "ok": ok,
                    "raw": {"arguments": args, "output": item.get("output")},
                }
            )
        elif kind.endswith("_call_output") and "a2a" in kind:
            agent = _agent_of(item)
            # The answer names its question by call_id; parallel answers may return in any order.
            call_id = str(item.get("call_id") or "") or None
            ask = (asked.get(call_id) if call_id else None) or next(
                (
                    s
                    for s in reversed(steps)
                    if s["kind"] == "ask" and agent and s.get("agent") == agent.key
                ),
                None,
            )
            answer = _text(item.get("output")) or _text(item.get("error"))
            steps.append(
                {
                    "kind": "answer",
                    "agent": agent.key if agent else None,
                    "title": f"{agent.name if agent else 'An agent'} answered",
                    "detail": answer,
                    "facts": _facts(item.get("output")),
                    "call_id": call_id,
                    "ok": ok,
                    "raw": {"output": item.get("output")},
                }
            )
            if ask is not None:
                ask["answer"] = answer
        elif "a2a" in kind and kind.endswith("_call"):
            agent = _agent_of(item)
            question = _text(item.get("arguments")) or _text(item.get("input"))
            # Live Foundry answers quote the question's call_id; older records quoted its id.
            keys = [str(item[key]) for key in ("call_id", "id") if item.get(key)]
            step = {
                "kind": "ask",
                "agent": agent.key if agent else None,
                "title": f"Asked {agent.name if agent else 'an agent'}",
                "detail": question,
                "call_id": keys[0] if keys else None,
                "ok": ok,
                "raw": {"arguments": item.get("arguments")},
            }
            steps.append(step)
            for key in keys:
                asked[key] = step
        elif kind == "message":
            steps.append(
                {
                    "kind": "final",
                    "title": "Final answer",
                    "detail": _text(item.get("content")),
                    "ok": True,
                    "raw": {},
                }
            )
    return steps


# The steps of any agent run ARROW keeps (the orchestrator's, the Case Assistant's).
run_steps = _steps


def _event_steps(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Without Foundry's record (running or failed): the orchestrator's steps ARROW logged."""
    titles = {
        "disruption_read": "Read the message",
        "case_recorded": "Wrote the case summary",
        "plan_built": "Built a plan",
        "plan_refused": "A plan was blocked",
        "plan_submitted": "Recommended a plan",
        "message_unreadable": "Could not read the message",
    }
    return [
        {
            "kind": "tool",
            "title": titles[e["event"]],
            "detail": str(e.get("message") or ""),
            "ok": e["event"] not in {"plan_refused", "message_unreadable"},
            "at": e.get("at"),
            "raw": {},
        }
        for e in document.get("events") or []
        if e.get("actor") == "recovery-orchestrator" and e.get("event") in titles
    ]


def _named(value: Any, trace: str, display_id: str) -> Any:
    """A recorded value with the case's trace id replaced by the display id OCC knows."""
    if isinstance(value, Mapping):
        return {str(key): _named(item, trace, display_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_named(item, trace, display_id) for item in value]
    if trace and value == trace:
        return display_id
    return value


def _recorded(text: Any, trace: str, display_id: str) -> Any:
    """A stored request or answer (JSON text); None where nothing was recorded (older rows)."""
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return _named(value, trace, display_id)


def _caller(
    call: Mapping[str, Any], arrow: frozenset[str], identities: Mapping[str, Agent]
) -> dict[str, Any]:
    """Who made a call, from the identity the system validated; never guessed. ``arrow`` holds
    every ARROW app's identity: the operator app's, the tools app's and the workers'."""
    oid = str(call.get("caller_oid") or "").lower()
    if oid and oid in arrow:
        return {"kind": "arrow", "name": "ARROW", "id": oid}
    if oid in identities:
        return {"kind": "agent", "name": identities[oid].name, "id": oid}
    return {
        "kind": "unknown",
        "name": str(call.get("caller_name") or "Unknown caller"),
        "id": oid or None,
    }


@dataclass(frozen=True)
class _Case:
    """What a call's words draw on besides the call itself: the case's recorded disruption, its
    defect and the airport rules ARROW kept on it."""

    disruption: Mapping[str, Any]
    defect: Mapping[str, Any]
    airports: Mapping[str, Any]


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _span(minutes: Any) -> str:
    """A duration in words: "4 h", "4 h 15 min", "45 min"; "" when unknown."""
    value = _count(minutes)
    if value is None or value < 0:
        return ""
    hours, rest = divmod(value, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _later(minutes: Any) -> str:
    """Minutes late in words: "+4 h", "+20 min"; "" for none."""
    value = _count(minutes)
    return f"+{_span(value)}" if value else ""


def _hhmm(value: Any) -> str:
    found = re.search(r"(?:T|^)(\d{2}):(\d{2})", str(value or ""))
    return f"{found[1]}:{found[2]}" if found else ""


def _many(value: Any, one: str, many: str = "") -> str:
    """ "1 flight", "1,234 flights"; "" when not a count."""
    count = _count(value)
    if count is None:
        return ""
    return f"{count:,} {one if count == 1 else many or one + 's'}"


def _euros(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return ""
    return f"€{value:,.0f}"


def _joined(*parts: str, between: str = " · ") -> str:
    return between.join(part for part in parts if part)


def _listed(values: Any) -> tuple[list[str], int]:
    """A recorded list as the audit shortens it ("+3 more" last): the items and the full count."""
    items = [str(item) for item in values] if isinstance(values, list) else []
    more = [item for item in items if re.fullmatch(r"\+\d+ more", item)]
    shown = [item for item in items if item not in more]
    return shown, len(shown) + sum(int(item[1:].split()[0]) for item in more)


def _cause(value: Any) -> str:
    cause = str(value or "")
    return CAUSES.get(cause, cause.replace("_", " ").capitalize())


def _period(values: Mapping[str, Any]) -> str:
    """A weather window in Lisbon time: "16:00–18:00"; "" when not recorded."""
    start = values.get("window_start") or _hhmm(values.get("window_start_local"))
    end = values.get("window_end") or _hhmm(values.get("window_end_local"))
    return f"{start}–{end}" if start and end else ""


def _crosswind(values: Mapping[str, Any]) -> str:
    """ "gusts 33 kt across runway 02/20" when the forecast records its crosswind: "gusts" only
    when its wind gusts (code computes it from the gust then, else from the mean wind)."""
    knots, runway = values.get("crosswind_kt"), values.get("runway")
    if isinstance(knots, bool) or not isinstance(knots, int | float):
        return ""
    gusting = re.search(r"G\d{2,3}KT$", str(values.get("wind") or "")) is not None
    return f"{'gusts ' if gusting else ''}{knots:g} kt" + (
        f" across runway {runway}" if runway else ""
    )


def _rate(values: Mapping[str, Any]) -> str:
    """ "24 movements an hour (normally 40)" when the forecast records the reduced rate."""
    rate, normal = _count(values.get("hourly_rate")), _count(values.get("normal_rate"))
    if rate is None:
        return ""
    return f"{rate} movements an hour" + (f" (normally {normal})" if normal is not None else "")


def _defect_words(values: Mapping[str, Any]) -> str:
    """ "CS-TQA out of service 4 h"."""
    registration = values.get("aircraft_reg")
    if not registration:
        return ""
    return f"{registration} out of service {_span(values.get('unavailable_minutes'))}".strip()


def _delay_words(values: Mapping[str, Any]) -> str:
    """ "AR259 +20 min · Border-control queue"."""
    flight = values.get("flight_no")
    if not flight:
        return ""
    return _joined(
        f"{flight} {_later(values.get('delay_minutes'))}".strip(), _cause(values.get("cause"))
    )


def _weather_words(values: Mapping[str, Any]) -> str:
    """ "Strong crosswind at Lisbon · 16:00–18:00 · 24 movements an hour (normally 40)"."""
    if not values.get("hazard"):
        return ""
    return _joined(storm_subject(values), _period(values), _crosswind(values), _rate(values))


def _disruption_words(values: Mapping[str, Any]) -> str:
    """A recorded disruption in words, by its own fields."""
    cause = values.get("cause") or values.get("type")
    if cause == "weather" or values.get("hazard"):
        return _weather_words(values)
    if cause == "aircraft_defect" or values.get("unavailable_minutes"):
        return _defect_words(values)
    return _delay_words(values)


def _knock_on_words(values: Mapping[str, Any], *, unchanged: bool = False) -> str:
    """ "4 flights affected, up to +4 h", with " if nothing changes" for the prediction itself."""
    affected = _count(values.get("flights_affected"))
    if affected is None:
        return ""
    if affected:
        late = _later(values.get("max_minutes_late"))
        words = f"{_many(affected, 'flight')} affected" + (f", up to {late}" if late else "")
    else:
        words = "No other flight affected"
    return words + (" if nothing changes" if unchanged else "")


def _option_words(option: Any) -> str:
    """An aircraft option id in words: "delay", "cancel", "swap to CS-TQB"."""
    text = str(option or "")
    registration = re.search(r"CS-?[A-Z]{3}", text, re.IGNORECASE)
    if text.lower().startswith("swap") and registration:
        return f"swap to {registration[0].upper()}"
    return text.replace("_", " ")


def _airport_words(rules: Mapping[str, Any]) -> str:
    """An airport's rules as ARROW kept them: "Lisbon coordinated, A-CDM, night period
    23:55–06:00"."""
    iata = str(rules.get("iata") or "")
    level = {3: "coordinated", 2: "schedules facilitated"}.get(
        _count(rules.get("coordination_level")) or 0, ""
    )
    start = rules.get("night_departure_from") or rules.get("night_arrival_from")
    ends = [
        str(rules[key])
        for key in ("night_departure_until", "night_arrival_until")
        if rules.get(key)
    ]
    return _joined(
        _joined(CITY.get(iata, iata), level, between=" "),
        "A-CDM" if rules.get("a_cdm") else "",
        f"night period {start}–{max(ends)}" if start and ends else "",
        between=", ",
    )


# What a weather case would owe were it not an extraordinary circumstance, as ARROW may name it.
_WOULD_OWE = ("would_be_compensation_eur", "compensation_if_owed_eur", "would_be_owed_eur")


def _exposure_words(values: Mapping[str, Any]) -> str:
    """The EU261 exposure ARROW computed for an option: "Delay +15 min · 331 owed compensation ·
    €85,050 · care €12,026 · rebooking €6,698"; weather owes none, and says what it would be."""
    option = _option_words(values.get("option_id"))
    head = _joined(
        option[:1].upper() + option[1:], _later(values.get("additional_delay_minutes")), between=" "
    )
    care, rebooking = _euros(values.get("care_eur")), _euros(values.get("rebooking_eur"))
    costs = [f"care {care}" if care else "", f"rebooking {rebooking}" if rebooking else ""]
    if values.get("extraordinary_circumstances") is True:
        would = next((_euros(values.get(key)) for key in _WOULD_OWE if _euros(values.get(key))), "")
        reason = values.get("reason") or "extraordinary circumstances"
        return _joined(
            head, f"no compensation owed: {reason}", f"would be {would}" if would else "", *costs
        )
    owed = _count(values.get("passengers_owed_compensation"))
    return _joined(
        head,
        f"{owed:,} owed compensation" if owed is not None else "",
        _euros(values.get("compensation_eur")),
        *costs,
    )


def _said(operation: str, request: Mapping[str, Any], response: Any, case: _Case) -> str:
    """A call's result in words, as a person would say it (owner: "reading airport rules,
    returns 70, whats that?"); "" when nothing was recorded."""
    if isinstance(response, str):
        return response
    r = _mapping(response)
    if not r:
        return ""
    if isinstance(r.get("error"), str):
        return str(r["error"])
    event = f"event {r['event_id']}" if r.get("event_id") else ""
    if operation == "get_disruption":
        return _joined(_disruption_words(r), _knock_on_words(r))
    if operation in ("get_defect", "record_defect"):
        until = _hhmm(r.get("unavailable_until"))
        return _joined(_defect_words(r) + (f", until {until}" if until else ""), event)
    if operation == "record_delay":
        return _joined(_delay_words(r), event)
    if operation == "record_weather":
        return _joined(_weather_words(r), event)
    if operation == "get_aircraft_rotation":
        flights, count = _listed(r.get("flights"))
        listed = ", ".join(flights) + (", …" if count > len(flights) else "")
        return _joined(
            str(r.get("aircraft_reg") or ""), f"{_many(count, 'flight')}: {listed}" if count else ""
        )
    if operation == "get_recovery_options":
        options, count = _listed(r.get("options"))
        affected, late = _listed(r.get("affected_flights"))
        where = f"{', '.join(affected)} affected" if late <= 4 else f"{late} flights affected"
        return _joined(
            f"{count}: {', '.join(_option_words(option) for option in options)}"
            if count
            else "No option",
            where if late else "",
        )
    if operation == "assess_crew":
        breaches = _count(r.get("breaches"))
        over = (
            "Within duty limits"
            if breaches == 0
            else _many(breaches, "duty over its limit", "duties over their limits")
        )
        return _joined(over, _many(r.get("duties"), "duty", "duties"))
    if operation == "assess_booking":
        # In the plan cards' words (owner review): a cancelled flight's passengers lose it,
        # they do not miss a connection.
        lose = _count(r.get("misconnected_passengers"))
        on = _many(r.get("affected_passengers"), "passenger")
        return _joined(
            f"{on} on the flights" if on else "",
            f"{lose:,} lose flight or connection" if lose is not None else "",
        )
    if operation == "assess_slots":
        if r.get("capacity_fit") is True:
            return "Fits airport capacity"
        extra = _later(r.get("required_additional_delay_minutes"))
        return (
            _joined("Over capacity", f"{extra} to fit" if extra else "")
            if r.get("capacity_fit") is False
            else ""
        )
    if operation == "get_reserve_options":
        crews, count = _listed(r.get("options"))
        if count:
            return f"{_many(count, 'standby crew')}: {', '.join(crews)}"
        return "No standby crew needed" if r.get("breaches") == 0 else "No standby crew available"
    if operation == "get_rebooking_plan":
        lose, rebooked = _count(r.get("disrupted_passengers")), _count(r.get("rebooked_passengers"))
        unplaced, kept = (
            _count(r.get("unplaced_passengers")),
            _count(r.get("transferred_passengers")),
        )
        return _joined(
            f"{lose:,} lose flight or connection" if lose is not None else "",
            f"{rebooked:,} rebooked" if rebooked is not None else "",
            f"{unplaced:,} to Passenger Services" if unplaced else "",
            f"{kept:,} kept by car" if kept else "",
        )
    if operation == "get_duty_plan":
        left = _count(r.get("breaches_unresolved"))
        return _joined(
            _many(r.get("duties_changed"), "duty changed", "duties changed"),
            "none over its limit"
            if left == 0
            else f"{left} still over"
            if left is not None
            else "",
        )
    if operation == "get_airport_rules":
        hub = _mapping(case.airports.get("LIS"))
        return _joined(_many(r.get("airports"), "airport"), _airport_words(hub) if hub else "")
    if operation == "get_network":
        return _joined(
            _many(r.get("flights"), "flight"),
            _many(r.get("aircraft"), "aircraft", "aircraft"),
            _many(r.get("airports"), "airport"),
        )
    if operation == "get_flight_summary":
        if r.get("found") is False:
            return "Not in today's schedule"
        booked, seats = _count(r.get("passenger_count")), _count(r.get("seat_capacity"))
        connecting = _count(r.get("connecting_passenger_count"))
        return _joined(
            str(r.get("flight_no") or ""),
            f"{booked} of {seats} seats booked" if booked is not None and seats is not None else "",
            f"{connecting} connecting" if connecting is not None else "",
        )
    if operation == "get_dispatch_advisories":
        alternates, _ = _listed(r.get("alternates"))
        return _joined(
            _many(r.get("flights"), "flight"),
            f"alternates {', '.join(alternates)}" if alternates else "",
        )
    if operation == "update_flight":
        flight, departs = str(r.get("flight_no") or ""), _hhmm(r.get("std_local"))
        if r.get("status") == "cancelled":
            return f"{flight} cancelled".strip()
        return f"{flight} departs {departs}".strip() if departs else flight
    if operation == "create_recovery_hold":
        held = _many(r.get("seat_count"), "seat")
        return f"{held} held on {r.get('flight_no')}" if held else ""
    message = r.get("message") or r.get("reason")
    return str(message) if message else str(r.get("status") or "").replace("_", " ").capitalize()


_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# The reads that named a disruption by its operations event id until the systems were asked by
# its facts.
_FACTS_READS = frozenset(
    {
        "get_disruption",
        "get_recovery_options",
        "assess_crew",
        "assess_booking",
        "assess_slots",
        "get_rebooking_plan",
        "get_reserve_options",
        "get_duty_plan",
        "get_dispatch_advisories",
    }
)
# A read's values the system takes as query parameters when its route does not name them: the
# disruption's facts (its type and that type's own fields), then the option's.
_QUERY = (
    "type",
    *dict.fromkeys(name for names in FACTS.values() for name in names),
    "option_id",
    "additional_delay_minutes",
)


def _api(route: str, operation: str, request: Mapping[str, Any]) -> str | None:
    """ARROW's own call to a system's route with its values filled in: "GET /api/recovery-options?
    type=flight_delay&flight_no=AR259&delay_minutes=20&cause=border_control". Never a host
    (owner: "dev hardcoded in the url??"). An agent's tool call has none: it called the tool."""
    if not re.match(r"(GET|POST|PUT|PATCH|DELETE) /", route):
        return None
    used = set(_PLACEHOLDER.findall(route))
    if any(request.get(name) in (None, "") for name in used):
        return None  # a route without its values says nothing a person can use
    if operation in _FACTS_READS and "event_id" in request:
        return None  # an older read by event id ran a route the systems no longer have
    filled = _PLACEHOLDER.sub(lambda found: str(request[found[1]]), route)
    if not route.startswith("GET "):
        return filled
    query = [
        f"{key}={request[key]}"
        for key in _QUERY
        if key not in used and request.get(key) not in (None, "", 0)
    ]
    return f"{filled}?{'&'.join(query)}" if query else filled


def _call_view(
    call: Mapping[str, Any],
    options: Mapping[str, str],
    *,
    caller: dict[str, Any],
    by_agent: bool,
    trace: str,
    display_id: str,
    case: _Case,
) -> dict[str, Any]:
    detail = ""
    if call.get("detail"):
        try:
            values = json.loads(str(call["detail"]))
        except ValueError:
            values = {}
        option = str(values.get("option_id") or "")
        if option:
            detail = f"Option: {options.get(option, option)}"
            if values.get("added_minutes"):
                detail += f" · +{values['added_minutes']} min"
    operation = str(call.get("operation") or "")
    request = _recorded(call.get("request"), trace, display_id)
    response = _recorded(call.get("response"), trace, display_id)
    if operation == "record_weather" and isinstance(request, dict):
        detail = storm_title(request)  # the window and hazard Schedule recorded
    endpoint = str(call.get("endpoint") or endpoint_of(operation, mcp=by_agent))
    mcp = endpoint.startswith("MCP ")
    system = str(call.get("system") or "")
    name = _RECIPIENT_OF.get(operation) or SYSTEMS.get(system, system.capitalize())
    sent = request if isinstance(request, dict) else {}
    airport = sent.get("airport")
    if operation == "update_tobt" and airport and airport != "LIS":
        name = f"{airport} airport (A-CDM)"
    return {
        "at": call.get("at"),
        "system": name,
        "operation": operation,
        # Owner: the system's name, what the call was for and its result, in words.
        "label": OPERATIONS.get(operation, operation.replace("_", " ").capitalize()),
        "summary": _said(operation, sent, response, case),
        "detail": detail,
        "ok": call.get("outcome") == "ok",
        "outcome": call.get("outcome"),
        # Rows from earlier versions did not record the endpoint: the agents reach the systems only
        # through MCP and ARROW only over REST, so where the call is listed names it.
        "endpoint": endpoint,
        "access": "write" if operation in WRITES else "read",
        "transport": "mcp" if mcp else "rest",
        # An agent's call is a tool call (MCP) to the system, shown as the tool and what it sent;
        # a REST route it never called read as a second call (owner, 25 Nov).
        "tool": MCP_TOOLS.get(operation, operation) if mcp else None,
        "api": None if mcp else _api(endpoint, operation, sent),
        "caller": caller,
        "request": request if isinstance(request, dict) else None,
        "response": response,
    }


def _tool_view(
    event: Mapping[str, Any],
    agent: Agent,
    oid: str | None,
    *,
    trace: str,
    display_id: str,
    case: _Case,
) -> dict[str, Any]:
    """A domain agent's call to one of ARROW's tools, as ARROW logged it on the case."""
    tool = str(event.get("tool") or "")
    request = _named(event.get("request"), trace, display_id)
    response = _named(event.get("response"), trace, display_id)
    ok = event.get("ok") is not False
    # Another open case already holds the disruption: nothing was recorded again.
    repeat = _repeat(event)
    if not ok:
        refused = _mapping(response).get("error") or _text(response)
        summary = str(event.get("message") or refused or "Refused")
    elif repeat:
        summary = str(event.get("message") or "Already recorded for another case")
    elif tool in AGENT_RECORDS:
        recorded = _disruption_view(case.disruption, case.defect)
        event_id = _mapping(response).get("event_id") or case.disruption.get("event_id")
        summary = _joined(
            recorded["title"] if recorded else "", f"event {event_id}" if event_id else ""
        )
    elif tool == "get_compensation_exposure":
        summary = _exposure_words(_mapping(response)) or _text(response)
    else:
        summary = _text(response)
    return {
        "at": event.get("at"),
        "system": "ARROW",
        "operation": tool,
        "label": AGENT_TOOLS.get(tool, tool.replace("_", " ").capitalize()),
        "summary": summary,
        "detail": "",
        "ok": ok,
        "outcome": "ok" if ok else "refused",
        "endpoint": f"MCP {tool}",
        "access": None if repeat else "write" if tool in AGENT_RECORDS else "read",
        "transport": "mcp",
        "tool": tool,
        "api": None,
        "caller": {"kind": "agent", "name": agent.name, "id": oid},
        "request": dict(request) if isinstance(request, Mapping) else None,
        "response": response,
    }


def _repeat(event: Mapping[str, Any]) -> bool:
    """A Detection agent's record call that recorded nothing: the disruption was already held."""
    response = _mapping(event.get("response"))
    return str(event.get("tool")) in AGENT_RECORDS and (
        bool(response.get("repeat")) or response.get("recorded") is False
    )


def _disruption_view(
    disruption: Mapping[str, Any], defect: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The disruption as the Detection agent recorded it: its type, only that type's own fields,
    an event id when an airline system issued one (its source, or ARROW's record in an older
    run) and what happens if nothing changes."""
    kind = str(disruption.get("type") or "")
    if kind not in DISRUPTION_KINDS:
        return None
    fields: list[tuple[str, Any]]
    if kind == "aircraft_defect":
        registration = (
            _mapping(disruption.get("scope")).get("aircraft_reg")
            or defect.get("aircraft_reg")
            or disruption.get("aircraft_reg")
        )
        minutes = defect.get("unavailable_minutes") or disruption.get("unavailable_minutes")
        title = _defect_words({"aircraft_reg": registration, "unavailable_minutes": minutes})
        fields = [("Aircraft", registration), ("Out of service", _span(minutes))]
    elif kind == "flight_delay":
        title = f"{disruption.get('flight_no')} {_later(disruption.get('delay_minutes'))}".strip()
        fields = [
            ("Flight", disruption.get("flight_no")),
            ("Delay", _later(disruption.get("delay_minutes"))),
            ("Cause", _cause(disruption.get("cause"))),
        ]
    else:
        title = _joined(storm_subject(disruption), _period(disruption))
        fields = [
            ("Forecast", storm_subject(disruption)),
            ("Window", _period(disruption)),
            ("Crosswind", _crosswind(disruption)),
            ("Rate", _rate(disruption)),
        ]
    knock_on = _mapping(disruption.get("knock_on"))
    return {
        "type": kind,
        "kind": DISRUPTION_KINDS[kind],
        "title": title,
        "fields": [[name, str(value)] for name, value in fields if value not in (None, "")],
        "event_id": disruption.get("event_id"),
        "knock_on": dict(knock_on) if knock_on else None,
        "knock_on_words": _knock_on_words(knock_on, unchanged=True) if knock_on else "",
    }


def _rule_of(reason: str) -> str:
    """The rule an older plan's reason belongs to: the most specific matching word wins, so
    "rebooking declined" is not read as the rebooking limit."""
    lower = reason.lower()
    matches = [(len(word), rule.key) for rule in RULES for word in rule.words if word in lower]
    return max(matches, key=lambda match: match[0])[1] if matches else "answered"


def _standby_detail(plan: Mapping[str, Any]) -> str:
    """A plan's crew rule in words: "Standby crew R1 covers the duty: call by 15:54, report
    16:54", or "Within limits" when it calls none."""
    crews = standby_crews(plan)
    if not crews:
        return "Within limits"
    ids = [str(crew.get("reserve_option_id") or "") for crew in crews]
    if len(crews) == 1:
        return f"Standby crew {ids[0]} covers the duty: {standby_times(crews[0])}"
    times = "; ".join(f"{id_} {standby_times(crew)}" for id_, crew in zip(ids, crews, strict=True))
    return f"Standby crews {', '.join(ids)} cover the duties: {times}"


def _rules(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Today's reason view of a plan's rules, for plans recorded before checks existed."""
    policy = plan.get("policy") or {}
    route = policy.get("route")
    reasons = [str(r) for r in policy.get("reasons") or []]
    found: dict[str, list[tuple[str, str]]] = {}
    for reason in reasons:
        if reason.lower().startswith(("occ decides", "within every domain")):
            continue
        key = _rule_of(reason)
        result = (
            "expert" if key == "passenger_services" else "blocked" if route == "blocked" else "occ"
        )
        found.setdefault(key, []).append((result, reason))
    rebooking = (plan.get("validation") or {}).get("rebooking") or {}
    passed = {
        "crew": _standby_detail(plan),
        "rebooking": f"{rebooking.get('rebooked_passengers', 0)} rebooked within policy"
        if isinstance(rebooking.get("rebooked_passengers"), int)
        else "Within policy",
        "aircraft": "Same aircraft",
        "cancellation": "No cancellation",
        "cost": "Within the limit",
        "passenger_services": "None",
    }
    rules = []
    for rule in RULES:
        hits = found.get(rule.key, [])
        # Rules a plan shows only when they apply (no older plan called several standby crews).
        if rule.key in ("declined_rebooking", "standby") and not hits:
            continue
        order = ("blocked", "occ", "expert")
        result = next((r for r in order if any(h[0] == r for h in hits)), "pass")
        rules.append(
            {
                "key": rule.key,
                "rule": rule.name,
                "source": rule.source,
                "result": result,
                "detail": "; ".join(h[1] for h in hits) or passed.get(rule.key, "Met"),
            }
        )
    return rules


COST_PARTS = ("airline", "compensation", "care", "rebooking")


def _cost_parts(cost: Any) -> dict[str, Any] | None:
    parts = {part: _mapping(cost).get(f"{part}_eur") for part in COST_PARTS}
    return parts if all(isinstance(value, int | float) for value in parts.values()) else None


def _plan_view(
    plan: Mapping[str, Any], originals: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    rebooking = (plan.get("validation") or {}).get("rebooking") or {}
    flights = [f for f in plan.get("flights") or [] if isinstance(f, Mapping)]
    policy = plan.get("policy") or {}
    asked = _mapping(plan.get("what_if"))
    return {
        "plan_id": plan.get("plan_id"),
        "title": plan.get("title") or str(plan.get("kind") or "plan").capitalize(),
        "kind": plan.get("kind"),
        "route": policy.get("route"),
        "recommended": bool(plan.get("recommended")),
        "reason": plan.get("recommendation_reason"),
        # A what-if asked in the case chat: not part of how ARROW decided (case chat spec §5).
        "what_if": {
            "question": asked.get("question"),
            "asked_by": _mapping(asked.get("asked_by")).get("name"),
            "asked_at": asked.get("asked_at"),
            "entry_id": asked.get("entry_id"),
        }
        if asked
        else None,
        "cost_eur": (plan.get("cost") or {}).get("total_eur"),
        # The four parts and the counts the plan cards show (cancellation and rebooking cost
        # research §5); None for a plan priced before a part was recorded.
        "cost_parts": _cost_parts(plan.get("cost")),
        "passengers_disrupted": rebooking.get("disrupted_passengers"),
        "passengers_owed_compensation": _mapping(plan.get("cost")).get(
            "owed_compensation_passengers"
        ),
        "rebooked": rebooking.get("rebooked_passengers"),
        "to_expert": rebooking.get("unplaced_passengers"),
        "flights": len(flights),
        "cancelled": sum(1 for f in flights if f.get("status") == "cancelled"),
        # Minutes ARROW added after the agents answered (capacity or standby crew timing).
        "adjustment_minutes": plan.get("adjustment_minutes") or 0,
        # The levers as ARROW stored them, delay_messages from the plan's own flights: the
        # orchestrator's panel shows the recommended plan's (review). None before.
        "levers": dict(_mapping(plan.get("levers"))) or None,
        "rules": _rules(plan),
        # None for plans recorded before checks: the screen shows the reason view instead.
        "checks": list(policy.get("checks") or []) or None,
        "rules_version": limits_of(plan).version,
        # The plan's playbook; a plan built before it is read from what it does.
        "playbook": list(plan.get("playbook") or []) or checklist(plan, originals),
        # The figures OCC compares plans by (Task 3); None for plans built before.
        "outcomes": plan.get("outcomes"),
    }


_STOPPED = {"refused", "failed", "unknown"}


def _receipt(action: Mapping[str, Any]) -> dict[str, Any] | None:
    """The system's answer to this action: its own receipt (same action id), else None (never
    sent, or a receipt that names another action)."""
    receipt = action.get("receipt")
    if not isinstance(receipt, Mapping) or receipt.get("action_id") not in (
        None,
        action.get("action_id"),
    ):
        return None
    status = receipt.get("status")
    return {
        "status": status,
        "at": receipt.get("at"),
        "reason": (str(receipt.get("message") or "") or None) if status in _STOPPED else None,
    }


def _call_rows(
    action: Mapping[str, Any], recorded: Mapping[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Each call that carried out the action, named by its recipient (native dispatch spec §4),
    with the recipient's own record of it (audit rows with the call's X-Request-Id)."""
    rows = []
    for call in _items(action.get("calls")):
        call_id = str(call.get("call_id") or "")
        request_id = str(call.get("request_id") or "")
        records = [*recorded.get(call_id, [])]
        if request_id and request_id != call_id:
            records += recorded.get(request_id, [])
        sent = call.get("sent")
        rows.append(
            {
                **{
                    key: call.get(key)
                    for key in (
                        "recipient",
                        "played_by",
                        "says",
                        "method",
                        "path",
                        "status",
                        "message",
                        "at",
                    )
                },
                # In named fields, as the recipients audit them: never the original rows.
                "sent": call_request(sent) if isinstance(sent, Mapping) else None,
                "reply": records[-1]["response"] if records else None,
                "call_id": call_id or None,
                "request_id": request_id or None,
                "recorded": records,
            }
        )
    return rows


def _action_view(
    action: Mapping[str, Any], recorded: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    kind = action.get("kind")
    return {
        **{key: action.get(key) for key in ("label", "system", "status", "message")},
        "action_id": action.get("action_id"),
        # A weather plan's "now", "when_confirmed" or "stand_down"; every other plan's are now.
        "stage": action.get("stage") or "now",
        "summary": action.get("summary"),
        # What ARROW sent, in named fields (no original rows, no booking).
        "request": action_request(str(kind), _mapping(action.get("payload"))) if kind else None,
        "receipt": _receipt(action),
        # Empty for actions carried out before each recipient had its own interface.
        "calls": _call_rows(action, recorded),
    }


def _call_ids(document: Mapping[str, Any]) -> set[str]:
    """The ids the recipients record ARROW's calls by: each call's id and their request id."""
    actions = _items((document.get("execution") or {}).get("actions"))
    return {
        str(call[key])
        for action in actions
        for call in _items(action.get("calls"))
        for key in ("call_id", "request_id")
        if call.get(key)
    }


def _outcome(
    document: Mapping[str, Any], recorded: Mapping[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    execution = document.get("execution") or {}
    status = document.get("status")
    if document.get("manual_intervention"):
        state = "manual"
    elif execution.get("status") == "completed":
        state = "executed"
    elif execution.get("status") in ("held", "stood_down"):
        state = str(execution["status"])
    elif execution:
        state = "executing" if execution.get("status") != "failed" else "failed"
    elif status == "assessment_ready":
        state = "occ"
    elif status == "assessing":
        state = "assessing"
    else:
        state = str(status)
    return {
        "state": state,
        "stage": document.get("stage"),
        "approved_by": execution.get("approved_by"),
        "execution_status": execution.get("status"),
        # A storm's two stages: what the held part waits for and the weather updates so far
        # (recorded on the case before OCC approves a plan).
        "storm": storm_of(document),
        "actions": [_action_view(action, recorded) for action in _items(execution.get("actions"))],
    }


def storm_of(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """A storm case's weather: the approved plan's ``execution.storm``, else the updates
    recorded before approval (``storm``: state and updates); None for any other case."""
    found = _mapping(document.get("execution")).get("storm") or document.get("storm")
    return dict(found) if isinstance(found, Mapping) else None


def _seconds(document: Mapping[str, Any]) -> float | None:
    """Time from the report to the orchestrator's last step (stored only when it settles)."""
    stored = (document.get("metrics") or {}).get("total_elapsed_seconds")
    if isinstance(stored, int | float):
        return float(stored)
    try:
        created = datetime.fromisoformat(str(document.get("created_at")))
        last = max(
            datetime.fromisoformat(str(e["at"]))
            for e in document.get("events") or []
            if e.get("actor") == "recovery-orchestrator" and e.get("at")
        )
    except ValueError:
        return None
    elapsed = (last - created).total_seconds()
    return round(elapsed, 1) if elapsed >= 0 else None


def _duration(minutes: Any) -> str:
    if not isinstance(minutes, int):
        return "unknown time"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def case_state(document: Mapping[str, Any]) -> tuple[str, str]:
    """Where a case stands for OCC, in words: needs, progress, handled, stopped or closed."""
    status = document.get("status")
    execution = document.get("execution") or {}
    if status == "closed":
        return "closed", "Closed"
    if document.get("manual_intervention"):
        return "needs", "Needs manual action"
    if execution:
        done = execution.get("status")
        if done == "completed":
            if any(r.get("status") == "awaiting_expert" for r in execution.get("reviews") or []):
                return "needs", "Passenger Services review"
            if execution.get("approved_by") == "policy":
                return "handled", "Handled automatically"
            return "handled", "Approved and carried out"
        if done == "needs_attention":
            return "needs", "Needs attention"
        if done == "failed":
            return "stopped", "Stopped"
        # A storm plan: its "now" part done, the rest held until the storm is confirmed.
        if done == "held":
            decide_by = _mapping(execution.get("storm")).get("decide_by")
            return "progress", f"Held · decide by {decide_by}" if decide_by else "Held"
        if done == "stood_down":
            if _mapping(execution.get("storm")).get("confirmed_after_stand_down"):
                # A weather case reads as a forecast, never a storm (owner).
                return "needs", "Forecast confirmed after stand-down"
            return "handled", "Stood down"
        return "progress", "Carrying out the plan"
    if status == "assessing":
        return "progress", "ARROW is assessing"
    if status == "assessment_ready":
        return "needs", "Needs your decision"
    if status == "no_disruption":
        return "handled", "No disruption"
    if status == "failed":
        return "stopped", "Stopped · nothing changed"
    return "progress", str(status or "Unknown")


_URGENCY = {"needs": 1, "stopped": 2, "progress": 4, "handled": 5, "closed": 5}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _case_plan(
    document: Mapping[str, Any], plans: list[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The plan the case stands on: the one carried out, else the recommended, else the first."""
    executed = _mapping(document.get("execution")).get("option_id")
    return (
        next((p for p in plans if executed and p.get("option_id") == executed), None)
        or next((p for p in plans if p.get("recommended")), None)
        or (plans[0] if plans else None)
    )


def _case_flights(
    document: Mapping[str, Any], plan: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """The plan's flights (else the scope's), by originally scheduled departure."""
    originals = _originals(document)
    if plan is not None:
        flights = [
            {
                "flight_no": flight.get("flight_no"),
                "std_local": _mapping(originals.get(str(flight.get("flight_no")))).get("std_local")
                or flight.get("std_local"),
                "planned_local": None
                if flight.get("status") == "cancelled"
                else flight.get("std_local"),
                "status": flight.get("status"),
            }
            for flight in _items(plan.get("flights"))
        ]
    else:
        scope = _mapping(_mapping(document.get("disruption")).get("scope")).get("flights")
        flights = [
            {
                "flight_no": number,
                "std_local": originals[number].get("std_local"),
                "planned_local": None,
                "status": originals[number].get("status"),
            }
            for number in (scope if isinstance(scope, list) else [])
            if isinstance(number, str) and number in originals
        ]
    timed = [flight for flight in flights if isinstance(flight["std_local"], str)]
    return sorted(timed, key=lambda flight: str(flight["std_local"]))


def _impact(
    document: Mapping[str, Any],
    plans: list[Mapping[str, Any]],
    plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    chosen = plan or {}
    execution = _mapping(document.get("execution"))
    actions = _items(execution.get("actions"))
    waiting = [r for r in _items(execution.get("reviews")) if r.get("status") == "awaiting_expert"]
    rebooking = _mapping(_mapping(chosen.get("validation")).get("rebooking"))
    approver = execution.get("approved_by")
    return {
        "passengers": rebooking.get("disrupted_passengers"),
        # The rebooking limit checks rebooked passengers, not disrupted ones.
        "rebooked": rebooking.get("rebooked_passengers"),
        "review_passengers": sum(
            r["passengers"] for r in waiting if isinstance(r.get("passengers"), int)
        ),
        "cost_eur": _mapping(chosen.get("cost")).get("total_eur"),
        "plans": len(plans),
        "plan_title": (chosen.get("title") or str(chosen.get("kind") or "plan").capitalize())
        if plan
        else None,
        # For the one plan name every screen shows (recovery_view.js planName).
        "plan_kind": chosen.get("kind") if plan else None,
        "plan_cancelled": [
            f.get("flight_no")
            for f in _items(chosen.get("flights"))
            if f.get("status") == "cancelled"
        ],
        "route": _mapping(chosen.get("policy")).get("route"),
        # Who authorised execution: policy ran it, or OCC approved it (even an automatic plan).
        "authorised_by": None if not approver else "policy" if approver == "policy" else "occ",
        # Carried out only when "completed": a held or stood-down storm is not (final review).
        "execution_status": execution.get("status"),
        "actions_done": sum(1 for a in actions if a.get("status") == "confirmed"),
        "actions_total": len(actions),
        # A stood-down storm's held actions, never to be sent.
        "actions_stood_down": sum(1 for a in actions if a.get("status") == "stood_down"),
    }


def case_card(document: Mapping[str, Any]) -> dict[str, Any]:
    """The case list row: what happened, where it stands, its first flight and its impact."""
    disruption = document.get("disruption") or {}
    kind = disruption.get("type")
    if kind == "flight_delay":
        headline = f"{disruption.get('flight_no')} · {disruption.get('delay_minutes')} min late"
    elif kind == "weather":
        headline = storm_title(disruption)
    elif kind == "aircraft_defect":
        defect = document.get("defect") or {}
        registration = (disruption.get("scope") or {}).get("aircraft_reg") or defect.get(
            "aircraft_reg"
        )
        headline = f"{registration} · out of service {_duration(defect.get('unavailable_minutes'))}"
    else:
        text = " ".join(str(disruption.get("text") or "").split())
        headline = text if len(text) <= 70 else text[:69] + "…"
    state, label = case_state(document)
    source = document.get("source") or {}
    plans = _items(document.get("options"))
    plan = _case_plan(document, plans)
    flights = _case_flights(document, plan)
    defect = _mapping(document.get("defect"))
    storm = storm_of(document)
    return {
        "headline": headline,
        "state": state,
        "label": label,
        "source": source.get("system")
        or ("System event" if source.get("kind") in {"event", "fabric"} else "OCC"),
        "urgency": 3 if label == "Passenger Services review" else _URGENCY.get(state, 4),
        "station": _station(document, flights),
        "first_departure": {
            key: flights[0][key] for key in ("flight_no", "std_local", "planned_local")
        }
        if flights
        else None,
        "impact": _impact(document, plans, plan),
        "lane": {
            "flights": flights[:6],
            "unavailable": {"from": defect["reported_at"], "until": defect["unavailable_until"]}
            if defect.get("reported_at") and defect.get("unavailable_until")
            else None,
        },
        # A storm case only: awaiting, confirmed or dropped, and when OCC must decide by.
        **(
            {"storm": {"state": storm.get("state"), "decide_by": storm.get("decide_by")}}
            if storm
            else {}
        ),
    }


def _originals(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        return originals_of(document)
    except (AttributeError, TypeError):
        return {}


def _station(document: Mapping[str, Any], flights: list[dict[str, Any]]) -> str | None:
    """Where the disruption is: a storm's airport, else the departure airport of its first
    affected flight."""
    disruption = _mapping(document.get("disruption"))
    if disruption.get("type") == "weather" and isinstance(disruption.get("airport"), str):
        return str(disruption["airport"])
    if not flights:
        return None
    origin = _originals(document).get(str(flights[0]["flight_no"]), {}).get("origin")
    return origin if isinstance(origin, str) and origin else None


def _executed(document: Mapping[str, Any], option_id: Any) -> bool:
    """Carried out: this plan was executed and every one of its actions is confirmed."""
    execution = _mapping(document.get("execution"))
    actions = _items(execution.get("actions"))
    return bool(
        option_id
        and execution.get("option_id") == option_id
        and actions
        and all(action.get("status") == "confirmed" for action in actions)
    )


_LIMIT_NAMES = {"cost": "Cost", "rebooking": "Rebooked"}


def _map_reason(check: Mapping[str, Any]) -> str:
    """One rule as a map chip: a limit rule shows the plan's value against the limit, never the
    value under the limit's name (a Dev run: "Cost limit for automatic plans: €118,305")."""
    value = str(check.get("input"))
    if not _mapping(check.get("measure")):
        return f"{check.get('rule')}: {value}"
    limit = re.sub(r"\s*\(rules v\d+\)$", "", str(check.get("limit") or ""))
    if check.get("key") == "rebooking":
        limit = re.sub(r" passengers?$", "", limit)
    name = _LIMIT_NAMES.get(str(check.get("key")))
    return (
        f"{name} {value} · limit {limit}"
        if name
        else f"{check.get('rule')}: {value} · limit {limit}"
    )


def _map_reasons(plan: Mapping[str, Any]) -> list[str]:
    checks = _items(_mapping(plan.get("policy")).get("checks"))
    if checks:
        return [_map_reason(c) for c in checks if c.get("result") != "pass"]
    return [str(rule["rule"]) for rule in _rules(plan) if rule["result"] != "pass"]


def map_view(document: Mapping[str, Any]) -> dict[str, Any]:
    """The operations map's view of a case (operations map spec §4.2): where it is, the
    aircraft, and what each plan would change, with the control tower's own figures."""
    card = case_card(document)
    disruption = _mapping(document.get("disruption"))
    defect = _mapping(document.get("defect"))
    originals = _originals(document)
    scope = _mapping(disruption.get("scope"))
    scoped = [f for f in scope.get("flights") or [] if isinstance(f, str)]
    registration = (
        scope.get("aircraft_reg")
        or defect.get("aircraft_reg")
        or (originals.get(scoped[0], {}).get("aircraft_reg") if scoped else None)
    )
    execution = _mapping(document.get("execution"))
    actions = _items(execution.get("actions"))
    plans = []
    for plan in _items(document.get("options")):
        view = _plan_view(plan)
        executed = _executed(document, plan.get("option_id"))
        ran = bool(plan.get("option_id")) and execution.get("option_id") == plan.get("option_id")
        plans.append(
            {
                "option_id": plan.get("option_id"),
                "kind": plan.get("kind"),
                "title": view["title"],
                "recommended": view["recommended"],
                "executed": executed,
                # The plan the case carried out, finished or not; the others were not chosen.
                "execution": {
                    "status": execution.get("status"),
                    "actions_done": sum(1 for a in actions if a.get("status") == "confirmed"),
                    "actions_total": len(actions),
                }
                if ran
                else None,
                "authorised_by": None
                if not ran
                else "policy"
                if execution.get("approved_by") == "policy"
                else "occ",
                "route": view["route"],
                "reason": view["reason"],
                "passengers": view["passengers_disrupted"],
                "rebooked": view["rebooked"],
                "review": view["to_expert"],
                "cost_eur": view["cost_eur"],
                "reasons": _map_reasons(plan),
                "flights": [
                    {
                        "flight_no": flight.get("flight_no"),
                        "std_local": originals.get(str(flight.get("flight_no")), {}).get(
                            "std_local"
                        )
                        or flight.get("std_local"),
                        "planned_local": None
                        if flight.get("status") == "cancelled"
                        else flight.get("std_local"),
                        "status": flight.get("status"),
                        "aircraft_reg": flight.get("aircraft_reg"),
                    }
                    for flight in _items(plan.get("flights"))
                ],
            }
        )
    return {
        "trace_id": document.get("trace_id"),
        "display_id": document.get("display_id"),
        **{key: card[key] for key in ("headline", "state", "label", "urgency", "source")},
        "disruption_type": disruption.get("type"),
        "report": disruption.get("message")
        or disruption.get("text")
        or disruption.get("description"),
        "reported_at": disruption.get("reported_at"),
        # When ARROW received the report, as the case page and list show it.
        "created_at": document.get("created_at"),
        "aircraft_reg": registration,
        "station": card["station"],
        "unavailable": card["lane"]["unavailable"],
        "plans": plans,
    }


def unreadable_card(document: Mapping[str, Any]) -> dict[str, Any]:
    """The case list row of a document that cannot be read: listed for OCC, never hidden."""
    display_id = str(document.get("display_id") or "")
    created = document.get("created_at")
    return {
        "trace_id": str(document.get("trace_id") or ""),
        "display_id": display_id,
        "status": str(document.get("status") or ""),
        "created_at": created if isinstance(created, str) else None,
        "headline": display_id or "Unknown case",
        "state": "needs",
        "label": "Case could not be read",
        "source": "Source unknown",
        "urgency": 1,
        "station": None,
        "first_departure": None,
        "impact": None,
        "lane": {"flights": [], "unavailable": None},
    }


# A chat turn without an outcome yet is over after 5 minutes (chat.STALE).
CHAT_TURN = timedelta(minutes=5)


def _moment(value: Any) -> datetime | None:
    """An aware time from a stored ISO value (audit rows end in Z; naive ones are UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Run:
    """A chat turn's run, or a what-if's build: when, and the domain agents the turn asked."""

    entry_id: str
    start: datetime
    end: datetime
    agents: frozenset[str] = frozenset()

    def holds(self, at: datetime) -> bool:
        return self.start <= at <= self.end


def _chat_runs(document: Mapping[str, Any]) -> list[_Run]:
    """Each chat turn's run, from the question to its outcome (CHAT_TURN while it has none)."""
    keys = {agent.foundry: agent.key for agent in AGENTS}
    runs = []
    for turn in _items(document.get("chat")):
        start = _moment(turn.get("asked_at"))
        if start is None:
            continue
        asked = frozenset(
            keys[str(call.get("name"))]
            for call in _items(turn.get("calls"))
            if call.get("kind") == "agent" and str(call.get("name")) in keys
        )
        end = _moment(turn.get("ended_at")) or start + CHAT_TURN
        runs.append(_Run(str(turn.get("id") or ""), start, end, asked))
    return runs


def _what_if_builds(document: Mapping[str, Any]) -> list[_Run]:
    """Each what-if's build: ARROW's planning reads from its start until it was stored."""
    builds = []
    for plan in _items(document.get("options")):
        asked = _mapping(plan.get("what_if"))
        start, end = _moment(asked.get("started_at")), _moment(asked.get("asked_at"))
        if start is not None and end is not None:
            builds.append(_Run(str(asked.get("entry_id") or ""), start, end))
    return builds


def _assessed_until(document: Mapping[str, Any], saved: Mapping[str, Any]) -> datetime | None:
    """When the orchestrator's run ended, as its record says; else its last logged step."""
    ended = _moment(saved.get("ended_at"))
    if ended is not None:
        return ended
    logged = [
        moment
        for event in _items(document.get("events"))
        if event.get("actor") == "recovery-orchestrator"
        and (moment := _moment(event.get("at"))) is not None
    ]
    return max(logged, default=None)


def _chat_turn_of(
    document: Mapping[str, Any],
    saved: Mapping[str, Any],
    steps: list[dict[str, Any]],
) -> Callable[[Mapping[str, Any], str | None, bool], str | None]:
    """Which chat turn caused a system call, if any (case chat spec: How ARROW decided holds
    the assessment only). The Case Assistant runs in the case's trace, so the systems audit its
    work under the case: a domain agent it asked, or ARROW building a what-if for it."""
    turns, builds = _chat_runs(document), _what_if_builds(document)
    running = document.get("status") == "assessing"
    ended = _assessed_until(document, saved)
    orchestrator_asked = {step.get("agent") for step in steps if step["kind"] == "ask"}

    def turn_of(call: Mapping[str, Any], agent: str | None, arrow: bool) -> str | None:
        at = _moment(call.get("at"))
        if at is None:
            return None
        if agent is None:
            # ARROW's own read while it built a what-if (its sent actions are listed already).
            return next((b.entry_id for b in builds if arrow and b.holds(at)), None)
        for turn in turns:
            if not turn.holds(at):
                continue
            # Once the orchestrator's run is over only the chat asks the agents. While it runs it
            # may ask any agent, so a call then is the assessment's, unless the run's record shows
            # the orchestrator never asked that agent and the turn did. The systems' audit cannot
            # tell two runs apart: a turn asked during the assessment that asks the same agent
            # leaves those calls with the assessment.
            assessing = running or (ended is not None and at <= ended)
            if assessing and (running or agent in orchestrator_asked or agent not in turn.agents):
                continue
            return turn.entry_id
        return None

    return turn_of


_MODEL = re.compile(r"gpt-(\d+(?:\.\d+)?o?)(?:-(mini|nano)\b)?", re.IGNORECASE)


def model_name(deployment: Any) -> str | None:
    """A model deployment by the model's own name (owner: "make this professional"):
    "gpt-4.1-eval" is GPT-4.1, "gpt-4.1-mini" GPT-4.1 mini; other names as they are."""
    if not deployment:
        return None
    found = _MODEL.match(str(deployment))
    if found is None:
        return str(deployment)
    return f"GPT-{found[1]}" + (f" {found[2].lower()}" if found[2] else "")


def _authorised_at(document: Mapping[str, Any]) -> datetime | None:
    """When the plan was first authorised to run: by ARROW's rules, or OCC's approval. From then
    on ARROW's reads are the executor's re-checks before it writes. A time before the case was
    opened is no boundary (a clock that cannot be compared), so then nothing is split off."""
    opened = _moment(document.get("created_at"))
    found = min(
        (
            moment
            for event in _items(document.get("events"))
            if (
                event.get("event") == "auto_execution_authorised"
                or (
                    event.get("event") == "duty_manager_decision"
                    and event.get("decision") == "approve"
                )
            )
            and (moment := _moment(event.get("at"))) is not None
        ),
        default=None,
    )
    return found if found is not None and (opened is None or found >= opened) else None


def _recorded_via(
    document: Mapping[str, Any], steps: list[dict[str, Any]], reported: list[dict[str, Any]]
) -> str | None:
    """Who had the disruption recorded: the Detection agent through ARROW's tools (newer runs),
    the orchestrator (older runs), or ARROW itself when a typed event arrived."""
    events = _items(document.get("events"))
    if any(
        e.get("event") == "agent_tool_call"
        and e.get("tool") in AGENT_RECORDS
        and e.get("ok") is not False
        and not _repeat(e)
        for e in events
    ):
        return "detection"
    if any(step.get("tool") == "record_disruption" for step in steps) or any(
        e.get("event") == "disruption_read" and e.get("actor") == "recovery-orchestrator"
        for e in events
    ):
        return "orchestrator"
    intake = any(
        e.get("event") == "disruption_recorded" and e.get("actor") == "arrow-intake" for e in events
    )
    return "intake" if reported or intake else None


def _recorded_in(disruption: Mapping[str, Any]) -> str:
    """Where the reported disruption was recorded: on ARROW's case ("arrow"; owner: no airline
    system changes until a plan for it is carried out), or, in an older run, at once in the
    airline system that owns it ("system", which issued the event id the case keeps). An event
    its source had recorded already keeps that source's id and is recorded on the case alone."""
    if disruption.get("event_id") and not disruption.get("recorded_at_source"):
        return "system"
    return "arrow"


def _in_time(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    earliest = datetime.min.replace(tzinfo=UTC)
    return sorted(calls, key=lambda call: _moment(call.get("at")) or earliest)


def build_trail(
    document: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]] | None,
    prompt: Callable[[str], dict[str, Any] | None],
    arrow_oids: Collection[str] | None,
    hosts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """``arrow_oids``: the identities of ARROW's apps (operator, tools, workers), whose calls show
    as ARROW's own. ``hosts`` is accepted for the caller and never shown: a call names the system
    and its own route, never the backend that serves it (owner)."""
    saved = document.get("_trail") or {}
    items = [i for i in saved.get("items") or [] if isinstance(i, Mapping)]
    steps = _steps(items) if items else _event_steps(document)
    disruption = document.get("disruption") or {}
    case = _Case(
        disruption=_mapping(disruption),
        defect=_mapping(document.get("defect")),
        airports=_mapping(document.get("airports")),
    )
    options = {
        str(p.get("aircraft_option_id")): str(p.get("kind") or p.get("aircraft_option_id"))
        for p in document.get("options") or []
        if isinstance(p, Mapping)
    }
    arrow = frozenset(oid.lower() for oid in arrow_oids or () if oid)

    def read(name: str) -> Mapping[str, Any]:
        try:
            found = prompt(name)
        except Exception:
            found = None
        return found if isinstance(found, Mapping) else {}

    named = (*AGENTS, CASE_ASSISTANT)
    definitions = {name: read(name) for name in (*(a.foundry for a in named), ORCHESTRATOR_AGENT)}
    # Each agent's own Entra identity, as Foundry reports it: the object id the systems audit.
    identities: dict[str, Agent] = {}
    for agent in named:
        identity = _mapping(definitions[agent.foundry].get("identity"))
        for key in ("principal_id", "client_id"):
            if isinstance(identity.get(key), str) and identity[key]:
                identities[identity[key].lower()] = agent
    trace = str(document.get("trace_id") or "")
    display_id = str(document.get("display_id") or trace)
    agent_calls: dict[str, list[dict[str, Any]]] = {agent.key: [] for agent in AGENTS}
    arrow_checks: list[dict[str, Any]] = []
    # The executor's reads once the plan was authorised: it checks its sources again before
    # it writes (owner: "I again see repetitive calls"), listed apart.
    rechecks: list[dict[str, Any]] = []
    authorised = _authorised_at(document)
    # ARROW's records of the reported disruption: writes of a fact, never among its checks.
    reported_calls: list[dict[str, Any]] = []
    # The system calls a chat turn caused, each with its turn: never part of the assessment.
    chat_calls: list[dict[str, Any]] = []
    turn_of = _chat_turn_of(document, saved, steps)
    # Each recipient's record of a call ARROW sent joins that call's row, never listed twice.
    sent = _call_ids(document)
    received: dict[str, list[dict[str, Any]]] = {}
    for call in calls or []:
        oid = str(call.get("caller_oid") or "").lower()
        recorded = str(call.get("endpoint") or "")
        if oid in identities:
            # The Case Assistant holds no system role: a call by it shows by its name with
            # ARROW's checks, never credited to the domain agent whose tool it is.
            owner: str | None = identities[oid].key if identities[oid] in AGENTS else None
        elif oid in arrow or (recorded and not recorded.startswith("MCP ")):
            owner = None  # ARROW's own call; the agents reach the systems only through MCP
        else:
            owner = _AGENT_OPERATIONS.get((str(call.get("system")), str(call.get("operation"))))
        view = _call_view(
            call,
            options,
            caller=_caller(call, arrow, identities),
            by_agent=owner is not None,
            trace=trace,
            display_id=display_id,
            case=case,
        )
        request_id = str((view["request"] or {}).get("request_id") or "")
        at = _moment(view["at"])
        if request_id in sent:
            received.setdefault(request_id, []).append(view)
        elif entry_id := turn_of(call, owner, view["caller"]["kind"] != "agent"):
            chat_calls.append({**view, "entry_id": entry_id})
        elif owner:
            agent_calls[owner].append(view)
        elif view["operation"] in RECORDS:
            reported_calls.append(view)
        elif (
            authorised
            and at is not None
            and at >= authorised
            and view["access"] == "read"
            and view["caller"]["kind"] != "agent"
        ):
            rechecks.append(view)
        else:
            arrow_checks.append(view)
    # The ARROW tools a domain agent called itself, as ARROW logged them on the case.
    oids = {agent.key: oid for oid, agent in identities.items() if agent in AGENTS}
    by_key = {agent.key: agent for agent in AGENTS}
    for event in _items(document.get("events")):
        tool_agent = by_key.get(str(event.get("actor") or ""))
        if event.get("event") != "agent_tool_call" or tool_agent is None:
            continue
        view = _tool_view(
            event,
            tool_agent,
            oids.get(tool_agent.key),
            trace=trace,
            display_id=display_id,
            case=case,
        )
        if entry_id := turn_of(view, tool_agent.key, False):
            chat_calls.append({**view, "entry_id": entry_id})
        else:
            agent_calls[tool_agent.key].append(view)

    def prompt_view(name: str) -> dict[str, Any]:
        found = definitions[name]
        if not found.get("instructions"):
            return {"available": False}
        return {
            "available": True,
            "version": str(found.get("version") or ""),
            "instructions": str(found["instructions"]),
        }

    def identity_view(name: str) -> dict[str, Any]:
        identity = _mapping(definitions[name].get("identity"))
        return {"client_id": identity.get("client_id"), "name": identity.get("name") or name}

    def foundry_view(name: str) -> dict[str, Any]:
        """The agent's name and version in Foundry, for its link in the portal."""
        version = definitions[name].get("version")
        return {"name": identity_view(name)["name"], "version": str(version) if version else None}

    def model_of(name: str) -> str | None:
        return model_name(definitions[name].get("model"))

    assessing = document.get("status") == "assessing"
    agents: list[dict[str, Any]] = []
    for agent in AGENTS:
        asks = [s for s in steps if s["kind"] == "ask" and s.get("agent") == agent.key]
        answers = [s for s in steps if s["kind"] == "answer" and s.get("agent") == agent.key]
        looked = _in_time(agent_calls[agent.key])
        status = (
            "done"
            if answers
            else "working"
            if (asks or looked) and assessing
            else "asked"
            if asks or looked
            else "not asked"
        )
        agents.append(
            {
                "key": agent.key,
                "name": agent.name,
                "role": agent.role,
                "autonomy": {"level": agent.level, "label": agent.autonomy},
                "asked": "\n\n".join(s["detail"] for s in asks if s["detail"]),
                "answered": "\n\n".join(s["detail"] for s in answers if s["detail"]),
                "calls": looked,
                "status": status,
                "prompt": prompt_view(agent.foundry),
                "identity": identity_view(agent.foundry),
                "model": model_of(agent.foundry),
                "foundry": foundry_view(agent.foundry),
            }
        )
    originals = _originals(document)
    plans = [
        _plan_view(p, originals) for p in document.get("options") or [] if isinstance(p, Mapping)
    ]
    outcome = _outcome(document, received)
    usage = saved.get("usage") or {}
    seconds = _seconds(document)
    message_text = str(
        disruption.get("message") or disruption.get("text") or disruption.get("description") or ""
    )
    source = document.get("source") or {}
    return {
        "trace_id": document.get("trace_id"),
        "display_id": document.get("display_id"),
        "status": document.get("status"),
        "message": {
            "text": message_text,
            "source": source.get("system")
            or ("System event" if source.get("kind") in {"event", "fabric"} else "OCC"),
            "reported_at": disruption.get("reported_at"),
            "event_id": source.get("event_id"),
            "reading": "" if disruption.get("type") == "message" else reading_label(disruption),
            "reading_note": disruption.get("reading"),
        },
        # The disruption as recorded (its type's own fields, an event id only when an airline
        # system issued one, the knock-on), shown under the Detection agent that read the report.
        "disruption": _disruption_view(case.disruption, case.defect),
        "recorded_via": _recorded_via(document, steps, reported_calls),
        "recorded_in": _recorded_in(case.disruption),
        "orchestrator": {
            "name": "Recovery orchestrator",
            "brief": saved.get("brief") or {},
            "steps": steps,
            "steps_source": "foundry" if items else "arrow",
            "summary": document.get("summary"),
            # The model the recorded run used, else the one its definition names now.
            "model": model_name(saved.get("model")) or model_of(ORCHESTRATOR_AGENT),
            "identity": identity_view(ORCHESTRATOR_AGENT),
            "foundry": foundry_view(ORCHESTRATOR_AGENT),
            "tokens": usage.get("total_tokens"),
            "seconds": seconds,
            "prompt": prompt_view(ORCHESTRATOR_AGENT),
        },
        "agents": agents,
        "arrow_checks": arrow_checks,
        "rechecks": rechecks,
        "reported_calls": reported_calls,
        "chat_calls": _in_time(chat_calls),
        "calls_available": calls is not None,
        "plans": plans,
        "outcome": outcome,
        "stats": {
            "agents_asked": sum(1 for a in agents if a["status"] != "not asked"),
            "steps": len(steps),
            "lookups": sum(len(a["calls"]) for a in agents),
            "arrow_checks": len(arrow_checks),
            "rechecks": len(rechecks),
            "reported_calls": len(reported_calls),
            "calls": sum(len(action["calls"]) for action in outcome["actions"]),
            "plans": len(plans),
            "rule_checks": sum(len(p["rules"]) for p in plans),
            "actions": len(outcome["actions"]),
            "seconds": seconds,
        },
    }
