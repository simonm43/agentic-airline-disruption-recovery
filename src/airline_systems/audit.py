"""What an airline system records about each call for the decision trail (owner).

Every audited call keeps the endpoint it came through, its inputs and a short answer. The answer
is built per operation from named fields (counts, statuses, option ids), never by copying a
body, so no booking locator, passenger name or journey can reach the audit. An operation without
a summary here records no answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from airline_systems.caller import CallerRefused

REQUEST_LIMIT = 2000
RESPONSE_LIMIT = 1000

# MCP tools whose name differs from the audited operation.
MCP_TOOLS = {
    "assess_crew": "assess_crew_recovery",
    "assess_booking": "assess_passenger_recovery",
    "assess_slots": "assess_slot_recovery",
}
REST_ENDPOINTS = {
    "get_network": "GET /api/network",
    "get_flight_summary": "GET /api/flights/{flight_no}/summary",
    "create_recovery_hold": "POST /api/recovery/holds",
    # ARROW's executor records the reported disruption, once a plan for it runs.
    "record_defect": "POST /api/defects",
    "record_delay": "POST /api/delays",
    # Reads about one disruption by its facts (owner: the query parameters name it, never
    # ARROW's case).
    "get_disruption": "GET /api/disruption",
    "get_recovery_options": "GET /api/recovery-options",
    "assess_crew": "GET /api/recovery-options/{option_id}/impact",
    "assess_booking": "GET /api/recovery-options/{option_id}/impact",
    "assess_slots": "GET /api/recovery-options/{option_id}/impact",
    "get_rebooking_plan": "GET /api/recovery-options/{option_id}/rebooking-plan",
    "get_reserve_options": "GET /api/recovery-options/{option_id}/standby-crews",
    "get_duty_plan": "GET /api/recovery-options/{option_id}/duty-plan",
    "get_airport_rules": "GET /api/airports",
    "get_aircraft_rotation": "GET /api/aircraft/{aircraft_reg}/rotation",
    # Each recipient's own interface (native dispatch spec §3).
    "update_flight": "PATCH /api/flights/{flight_no}",
    "submit_duty_changes": "POST /api/duty-changes",
    "submit_rebooking": "POST /api/rebookings",
    "order_transfer": "POST /ground-handling/transfer-orders",
    "send_nm_message": "POST /nm/messages",
    "update_tobt": "POST /acdm/flights/{flight_no}/tobt",
    "request_slot": "POST /slot-coordinator/requests",
    "get_slot_request": "GET /slot-coordinator/requests/{request_id}",
    # The OCC playbook's sample interfaces (OCC playbook plan).
    "send_passenger_notice": "POST /api/passenger-notices",
    "get_dispatch_advisories": "GET /api/dispatch-advisories",
    # A weather case's standby crews (case summary and held storm plan).
    "alert_standby_crews": "POST /api/standby-alerts",
    "call_standby_crews": "POST /api/standby-alerts/call",
    "release_standby_crews": "POST /api/standby-alerts/release",
}


def endpoint(operation: str, *, mcp: bool) -> str:
    """The endpoint an operation is reached through: an MCP tool or a REST route template."""
    if mcp:
        return f"MCP {MCP_TOOLS.get(operation, operation)}"
    return REST_ENDPOINTS.get(operation, "")


def as_json(value: Any, limit: int) -> str | None:
    """Compact JSON within ``limit`` characters: long lists keep their first items and say how
    many more there were, so a stored value always parses."""
    if value is None:
        return None
    for keep in (None, 8, 3, 1, 0):
        text = json.dumps(_shortened(value, keep), separators=(",", ":"), default=str)
        if len(text) <= limit:
            return text
    return json.dumps({"truncated": True})


def _shortened(value: Any, keep: int | None) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _shortened(item, keep) for key, item in value.items()}
    if isinstance(value, list | tuple):
        items = [_shortened(item, keep) for item in value]
        if keep is None or len(items) <= keep:
            return items
        return [*items[:keep], f"+{len(items) - keep} more"]
    if isinstance(value, str) and keep is not None and len(value) > 160:
        return value[:159] + "…"
    return value


def _pick(result: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: result[key] for key in keys if key in result}


def _count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _ids(items: Any, key: str) -> list[Any]:
    return [item.get(key) for item in items or [] if isinstance(item, Mapping)]


def _reply(*keys: str) -> Callable[[Any], dict[str, Any]]:
    """A native route's answer (status code, reply) in named fields."""
    return lambda result: _pick(result[1], *keys)


def _crew(result: Mapping[str, Any]) -> dict[str, Any]:
    duties = [duty for duty in result.get("duties") or [] if isinstance(duty, Mapping)]
    return {
        **_pick(result, "constraint_status"),
        "duties": len(duties),
        "breaches": sum(1 for duty in duties if duty.get("status") == "fail"),
        "changed_flights": _count(result.get("changed_flight_ids")),
    }


def _booking(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_pick(result, "constraint_status", "affected_passengers", "misconnected_passengers"),
        "disrupted_bookings": result.get("disrupted_booking_count"),
    }


def _slots(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_pick(
            result,
            "constraint_status",
            "capacity_fit",
            "required_additional_delay_minutes",
            "delay_messages_required",
            "departures_moved_over_15_min",
        ),
        "slot_conflicts": _count(result.get("slot_conflicts")),
    }


def _rebooking(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_pick(
            result,
            "disrupted_passengers",
            "rebooked_passengers",
            "unplaced_passengers",
            "transferred_passengers",
        ),
        "transfers": _count(result.get("transfers")),
    }


def _knock_on(result: Mapping[str, Any]) -> dict[str, Any]:
    """A disruption's knock-on in counts: how many flights it makes late, by up to how much."""
    found = result.get("knock_on")
    if not isinstance(found, Mapping):
        return {}
    return {
        "flights_affected": found.get("flights_affected"),
        "max_minutes_late": found.get("max_minutes_late"),
    }


_DEFECT = ("aircraft_reg", "unavailable_minutes", "reported_at", "unavailable_until")
_STORM = ("airport", "window_start_local", "window_end_local", "hazard", "hourly_rate")
_SUMMARIES: dict[str, Callable[[Any], dict[str, Any]]] = {
    "get_disruption": lambda r: {
        **_pick(
            r, "cause", "aircraft_reg", "flight_no", "delay_minutes", "unavailable_minutes", *_STORM
        ),
        **_knock_on(r),
    },
    "get_defect": lambda r: _pick(r, *_DEFECT),
    # Each record answers with the operations event id the system issued for it.
    "record_defect": _reply("status", "event_id", *_DEFECT),
    "record_delay": _reply(
        "status", "event_id", "flight_no", "delay_minutes", "cause", "aircraft_reg"
    ),
    "get_aircraft_rotation": lambda r: {
        **_pick(r, "aircraft_reg"),
        "flights": _ids(r.get("flights"), "flight_no"),
    },
    "get_recovery_options": lambda r: {
        "affected_flights": r.get("affected_flight_ids"),
        "options": _ids(r.get("options"), "option_id"),
    },
    "assess_crew": _crew,
    "assess_booking": _booking,
    "assess_slots": _slots,
    "get_reserve_options": lambda r: {
        "breaches": _count(r.get("breaches")),
        "options": _ids(r.get("options"), "reserve_option_id"),
    },
    "get_rebooking_plan": _rebooking,
    "get_duty_plan": lambda r: _pick(r, "duties_changed", "breaches_unresolved", "message"),
    "get_airport_rules": lambda r: {"airports": _count(r)},
    "get_network": lambda r: {
        "flights": _count(r.get("flights")),
        "aircraft": _count(r.get("aircraft")),
        "airports": _count(r.get("airports")),
    },
    "get_flight_summary": lambda r: (
        {"found": False}
        if r is None
        else _pick(
            r,
            "flight_no",
            "status",
            "passenger_count",
            "seat_capacity",
            "available_seats",
            "connecting_passenger_count",
        )
    ),
    "update_flight": _reply("flight_no", "std_local", "sta_local", "status", "aircraft_reg"),
    "submit_duty_changes": _reply("status", "message"),
    "submit_rebooking": _reply("status", "message"),
    "order_transfer": _reply("status", "message"),
    "send_nm_message": _reply("status", "reason"),
    "update_tobt": _reply("status"),
    "request_slot": _reply("request_id", "status"),
    "get_slot_request": _reply("request_id", "status", "message"),
    "create_recovery_hold": lambda r: _pick(r[0], "status", "flight_no", "seat_count"),
    "send_passenger_notice": _reply("status", "notified_flights", "notified_passengers"),
    "alert_standby_crews": _reply("status", "alerted"),
    "call_standby_crews": _reply("status", "called"),
    "release_standby_crews": _reply("status", "released"),
    "get_dispatch_advisories": lambda r: {
        "flights": _count(r.get("flights")),
        "alternates": _ids(r.get("alternates"), "iata"),
    },
}


def summary(operation: str, result: Any) -> str | None:
    """The short answer recorded for an operation's result (JSON), or None without a summary."""
    found = _SUMMARIES.get(operation)
    if found is None:
        return None
    return as_json(found(result), RESPONSE_LIMIT)


def failure(error: Exception | str) -> str:
    """The recorded answer of a refused or failed call: the systems' own refusal text (written
    by code, never containing booking data), else only the kind of error."""
    if isinstance(error, str | ValueError | CallerRefused):
        text = str(error)
    else:
        text = type(error).__name__
    return json.dumps({"error": text[:300]})


_FLIGHT_FIELDS = ("flight_no", "std_local", "sta_local", "status", "aircraft_reg")
_TRANSFER_FIELDS = ("from_flight_no", "to_flight_no", "passengers", "minutes", "passport_control")
_ACTION_FIELDS = (
    "flight_no",
    "flight_nos",
    "option_id",
    "additional_delay_minutes",
    "reserve_option_id",
    "reserve_option_ids",
    "aircraft_reg",
    # The reported disruption a plan records first (never its description).
    "delay_minutes",
    "cause",
    "unavailable_minutes",
    "eobt",
    "tobt",
    "airport",
    "request",
)
_CALL_FIELDS = (
    *_FLIGHT_FIELDS,
    # A reported disruption as ARROW's executor records it (never its description).
    "delay_minutes",
    "cause",
    "unavailable_minutes",
    "option_id",
    "additional_delay_minutes",
    "reserve_option_id",
    "reserve_option_ids",
    "airport",
    "tobt",
    "type",
    "request",
    "text",
    "kind",
)


def action_request(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """An action as sent, in named fields: the flights with their new times, the counterparty's
    inputs and passenger counts; never the original rows or any booking."""
    return {"kind": kind, **_sent(payload, _ACTION_FIELDS)}


def call_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """A call to a recipient's own interface as sent, in named fields: a flight's new times, a
    message's text, the counterparty's inputs; never the original rows or any booking."""
    return _sent(body, _CALL_FIELDS)


def _sent(body: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    request = _pick(body, *fields)
    if isinstance(body.get("passengers"), int):
        request["passengers"] = body["passengers"]
    if isinstance(body.get("flights"), list):
        # Flights as sent: changed rows' named fields, or a notice's flight numbers.
        request["flights"] = [
            flight if isinstance(flight, str) else _pick(flight, *_FLIGHT_FIELDS)
            for flight in body["flights"]
            if isinstance(flight, Mapping | str)
        ]
    if isinstance(body.get("transfers"), list):
        request["transfers"] = [
            _pick(item, *_TRANSFER_FIELDS)
            for item in body["transfers"]
            if isinstance(item, Mapping)
        ]
    if isinstance(body.get("crews"), list):
        # Standby crews put on alert: each option and its call-by time, not who is in it.
        request["crews"] = [
            _pick(item, "reserve_option_id", "call_by_local")
            for item in body["crews"]
            if isinstance(item, Mapping)
        ]
    return request
