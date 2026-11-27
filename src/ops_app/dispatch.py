"""Each planned action as the calls its recipients understand, each reply as a receipt and the
answers in words for the case (native dispatch spec §2–3).

The airline's own systems take resource updates bound to what ARROW validated (If-Match) that are
safe to send again (Idempotency-Key). External parties get their own messages, sent once. Every
call carries X-Request-Id, its call id, so the recipient's record and ARROW's call match. The OCC
playbook's passenger notices and a storm's standby alerts are keyed writes at Booking's and Crew's
own interfaces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

from airline_systems.operating_day import ICAO
from airline_systems.schedule import LISBON
from ops_app.actions import RECORD
from ops_app.policy import standby_ids, standby_name

RECIPIENTS = {
    "schedule": "Operations control",
    "maintenance": "Maintenance",
    "crew": "Crew system",
    "booking": "Passenger system",
    "ground_handling": "Ground handling",
    "network_manager": "EUROCONTROL Network Manager",
    "airport_cdm": "Lisbon airport (A-CDM)",
    "slot_coordinator": "Slot coordinator",
}
# The service that answers for each recipient: kept on each call for the audit, never shown to OCC.
PLAYED_BY = {
    "schedule": "Schedule",
    "maintenance": "Maintenance",
    "crew": "Crew",
    "booking": "Booking",
    "slots": "Slots",
}
# The reported disruption, recorded first by its owning system: a delay by operations control,
# a defect by Maintenance (a keyed write, safe to send again: the system keeps one record per
# case and answers a repeat as recorded).
_RECORDS = {"schedule": "/api/delays", "maintenance": "/api/defects"}

_NM_CODES = {
    "network_manager.fpl_delay": "DLA",
    "network_manager.fpl_change": "CHG",
    "network_manager.fpl_cancel": "CNL",
}
_NM_NAMES = {
    "DLA": "delay message",
    "CHG": "aircraft change message",
    "CNL": "cancellation message",
}
# Messages to parties outside the airline: they answer in their own codes (ACK, REJ, accepted), so
# the case tells who accepted or rejected what.
MESSAGES = frozenset(
    {
        *_NM_CODES,
        "airport_cdm.tobt_update",
        "slot_coordinator.slot_change_request",
        "slot_coordinator.slot_non_use",
    }
)
_FLIGHT_CHANGES = {"retime_flights", "cancel_flights", "change_tail"}
_NOT_DONE = {"refused": "refused", "failed": "failed", "unknown": "outcome unknown"}
# The crew and passenger systems' own writes, bound to the plan digest ARROW validated.
_OWN_WRITES = {"update_duties", "assign_reserve_crew", "rebook_passengers"}
# The OCC playbook's writes, each its recipient's own keyed write (Booking and Crew keep one record
# per Idempotency-Key, so each is safe to send again); none is bound to a digest. Booking's
# passenger notices (new times; a storm's warning of possible delays and its no-change notice)
# and Crew's standby alerts, calls and releases (storm held until confirmed).
_PLAYBOOK = {
    "notify_passengers": ("booking", "/api/passenger-notices"),
    "warn_passengers": ("booking", "/api/passenger-notices"),
    "notify_no_change": ("booking", "/api/passenger-notices"),
    "alert_standby": ("crew", "/api/standby-alerts"),
    "call_standby": ("crew", "/api/standby-alerts/call"),
    "release_standby": ("crew", "/api/standby-alerts/release"),
}
_CONFIRMED = {
    "ACK",
    "accepted",
    "acknowledged",
    "rebooked",
    "confirmed",
    "alerted",
    "called",
    "released",
    "recorded",
}
_REFUSED = {"REJ", "refused"}


@dataclass(frozen=True)
class Outbound:
    call_id: str
    system: str
    recipient: str
    played_by: str
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] | None
    says: str
    resend_safe: bool


def _hhmm(value: Any) -> str:
    return datetime.fromisoformat(str(value)).strftime("%H:%M")


def _icao(airport: Any) -> str:
    # ZZZZ is ICAO's "no location indicator"; the Network Manager rejects it by name.
    return ICAO.get(str(airport), "ZZZZ")


def _no_icao(payload: Mapping[str, Any]) -> str:
    """Which airport a ZZZZ in the message stands for, so OCC can see what was meant."""
    unknown = [str(payload.get(key)) for key in ("origin", "destination")]
    unknown = [code for code in unknown if code not in ICAO]
    return f" (no ICAO code for {_listed(unknown)}; sent as ZZZZ)" if unknown else ""


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def nm_message(kind: str, payload: Mapping[str, Any]) -> str:
    """The ICAO message EUROCONTROL reads: off-block time and date of flight in UTC."""
    code = _NM_CODES.get(kind, kind)
    off_block = datetime.fromisoformat(str(payload["eobt"])).replace(tzinfo=LISBON).astimezone(UTC)
    text = (
        f"({code}-{payload['flight_no']}-{_icao(payload['origin'])}{off_block:%H%M}"
        f"-{_icao(payload['destination'])}-DOF/{off_block:%y%m%d}"
    )
    if code == "CHG":
        text += f"-18/REG/{str(payload['aircraft_reg']).replace('-', '')}"
    return f"{text})"


def _own(call_id: str, match: Any) -> dict[str, str]:
    """An airline system's write: safe to repeat under its key, bound to what was validated."""
    return {"Idempotency-Key": call_id, "If-Match": f'"{match or ""}"', "X-Request-Id": call_id}


def _flight_updates(action: Mapping[str, Any]) -> list[Outbound]:
    kind, payload = action["kind"], action.get("payload") or {}
    expected = action.get("expected") or {}
    before = expected.get("flights") or {}
    versions = expected.get("versions") or {}
    changes: list[tuple[str, dict[str, Any], str]] = []
    if kind == "retime_flights":
        for flight in payload["flights"]:
            number, leaves = flight["flight_no"], _hhmm(flight["std_local"])
            was = (before.get(number) or {}).get("std_local")
            says = f"{number} departs {leaves}" + (f" (was {_hhmm(was)})" if was else "")
            body = {"std_local": flight["std_local"], "sta_local": flight["sta_local"]}
            changes.append((number, body, says))
    elif kind == "cancel_flights":
        changes = [(n, {"status": "cancelled"}, f"{n} cancelled") for n in payload["flight_nos"]]
    else:
        for flight in payload["flights"]:
            number, aircraft = flight["flight_no"], flight["aircraft_reg"]
            was = (before.get(number) or {}).get("aircraft_reg")
            says = f"{number} flies with {aircraft}" + (f" (was {was})" if was else "")
            changes.append((number, {"aircraft_reg": aircraft}, says))
    updates = []
    for number, body, says in changes:
        call_id = f"{action['action_id']}-{number.lower()}"
        updates.append(
            Outbound(
                call_id=call_id,
                system="schedule",
                recipient=RECIPIENTS["schedule"],
                played_by=PLAYED_BY["schedule"],
                method="PATCH",
                path=f"/api/flights/{quote(number)}",
                headers=_own(call_id, versions.get(number)),
                body=body,
                says=says,
                resend_safe=True,
            )
        )
    return updates


def _message(action: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], str]:
    """Recipient, path, body and what it says, for an action sent as one call."""
    kind, payload = action["kind"], action.get("payload") or {}
    number = payload.get("flight_no")
    flights = ", ".join(str(f.get("flight_no")) for f in payload.get("flights") or [])
    if kind in ("update_duties", "assign_reserve_crew"):
        body = {key: payload[key] for key in ("flights", "originals")}
        says = f"Update crew duties for {flights}"
        if kind == "assign_reserve_crew":
            # Every standby crew in one write (an action planned by an earlier version names one).
            called = standby_ids(payload)
            body["reserve_option_ids"] = called
            says = f"Assign {standby_name(called)} to {flights}"
        return RECIPIENTS["crew"], "/api/duty-changes", body, says
    if kind == "rebook_passengers":
        keys = ("option_id", "additional_delay_minutes", "flights", "originals")
        body = {key: payload[key] for key in keys}
        return RECIPIENTS["booking"], "/api/rebookings", body, str(action["summary"])
    if kind == "ground_handling.direct_transfer":
        body = {"transfers": payload["transfers"]}
        path = "/ground-handling/transfer-orders"
        return RECIPIENTS["ground_handling"], path, body, str(action["summary"])
    if kind in _NM_CODES:
        says = {
            "DLA": f"Delay message {number}, off-block {_hhmm(payload.get('eobt'))}",
            "CHG": f"Change message {number}, aircraft {payload.get('aircraft_reg')}",
            "CNL": f"Cancel message {number}",
        }[_NM_CODES[kind]] + _no_icao(payload)
        recipient = RECIPIENTS["network_manager"]
        return recipient, "/nm/messages", {"text": nm_message(kind, payload)}, says
    airport = str(payload.get("airport"))
    if kind == "airport_cdm.tobt_update":
        place = "Lisbon" if airport == "LIS" else airport
        recipient = RECIPIENTS["airport_cdm"] if airport == "LIS" else f"{airport} airport (A-CDM)"
        body = {"airport": airport, "tobt": payload["tobt"]}
        path = f"/acdm/flights/{quote(str(number))}/tobt"
        return recipient, path, body, f"{number} ready {_hhmm(payload['tobt'])} at {place}"
    if kind == "slot_coordinator.slot_change_request":
        request = payload["request"]
        body = {"airport": airport, "flight_no": number, "type": "change", "request": request}
        says = f"Slot request: {request}"
    elif kind == "slot_coordinator.slot_non_use":
        body = {"airport": airport, "flight_no": number, "type": "non_use"}
        says = f"{number} will not use its slot at {airport}"
    else:
        raise ValueError(f"No recipient takes {kind}")
    return RECIPIENTS["slot_coordinator"], "/slot-coordinator/requests", body, says


def _keyed(call_id: str) -> dict[str, str]:
    """An OCC playbook write: safe to repeat under its key."""
    return {"Idempotency-Key": call_id, "X-Request-Id": call_id}


def _playbook_calls(action: Mapping[str, Any]) -> list[Outbound]:
    """An OCC playbook action as its recipient's own write."""
    recipient, path = _PLAYBOOK[str(action["kind"])]
    system, call_id = str(action["system"]), str(action["action_id"])
    return [
        Outbound(
            call_id=call_id,
            system=system,
            recipient=RECIPIENTS[recipient],
            played_by=PLAYED_BY[system],
            method="POST",
            path=path,
            headers=_keyed(call_id),
            body=dict(action.get("payload") or {}),
            says=str(action["summary"]),
            resend_safe=True,
        )
    ]


def _record_calls(action: Mapping[str, Any]) -> list[Outbound]:
    """The reported disruption as its owning system records it (RECORD, ops_app.actions)."""
    system, call_id = str(action["system"]), str(action["action_id"])
    return [
        Outbound(
            call_id=call_id,
            system=system,
            recipient=RECIPIENTS[system],
            played_by=PLAYED_BY[system],
            method="POST",
            path=_RECORDS[system],
            headers=_keyed(call_id),
            body=dict(action.get("payload") or {}),
            says=str(action["summary"]),
            resend_safe=True,
        )
    ]


def calls(action: Mapping[str, Any]) -> list[Outbound]:
    """The calls that carry out an action, in the order they are sent."""
    if action["kind"] == RECORD:
        return _record_calls(action)
    if action["kind"] in _FLIGHT_CHANGES:
        return _flight_updates(action)
    if action["kind"] in _PLAYBOOK:
        return _playbook_calls(action)
    call_id, system = str(action["action_id"]), str(action["system"])
    recipient, path, body, says = _message(action)
    own = action["kind"] in _OWN_WRITES
    digest = (action.get("expected") or {}).get("plan_digest")
    return [
        Outbound(
            call_id=call_id,
            system=system,
            recipient=recipient,
            played_by=PLAYED_BY[system],
            method="POST",
            path=path,
            headers=_own(call_id, digest) if own else {"X-Request-Id": call_id},
            body=body,
            says=says,
            resend_safe=own,
        )
    ]


def _flight_now(reply: Mapping[str, Any]) -> str:
    if reply.get("status") == "cancelled":
        return f"{reply['flight_no']} cancelled"
    return (
        f"{reply['flight_no']} departs {_hhmm(reply['std_local'])}, "
        f"arrives {_hhmm(reply['sta_local'])}, {reply['aircraft_reg']}"
    )


def receipt(call: Outbound, reply: Mapping[str, Any]) -> dict[str, Any]:
    """The recipient's answer as ARROW records it."""
    answer = str(reply.get("status") or "")
    if "flight_no" in reply and "version" in reply:
        status, message = "confirmed", _flight_now(reply)
    elif answer in _CONFIRMED:
        status, message = "confirmed", str(reply.get("message") or answer)
    elif answer in _REFUSED:
        status, message = "refused", str(reply.get("reason") or reply.get("message") or answer)
    elif answer == "pending":
        status, message = "pending", str(reply.get("message") or f"Awaiting {call.recipient}")
    else:
        status, message = "unknown", f"Unrecognised answer from {call.recipient}"
    result = {
        "call_id": call.call_id,
        "status": status,
        "message": message,
        "at": datetime.now(UTC).isoformat(),
    }
    if reply.get("request_id"):
        result["request_id"] = str(reply["request_id"])
    if reply.get("event_id"):
        # The event id the recipient issued as it recorded the reported disruption.
        result["event_id"] = str(reply["event_id"])
    return result


def _told(action: Mapping[str, Any]) -> tuple[str, str] | None:
    """The outside party a message went to and what it was, in words."""
    kind, payload = action["kind"], action.get("payload") or {}
    number, airport = payload.get("flight_no"), str(payload.get("airport"))
    if kind in _NM_CODES:
        name = _NM_NAMES[_NM_CODES[kind]]
        return "EUROCONTROL", f"the {name} for {number}{_no_icao(payload)}"
    if kind == "airport_cdm.tobt_update":
        place = "Lisbon" if airport == "LIS" else airport
        tobt = _hhmm(payload.get("tobt"))
        return f"{place} airport", f"the target off-block time {tobt} for {number}"
    if kind == "slot_coordinator.slot_change_request":
        return "The slot coordinator", f"the slot request for {number} at {airport}"
    if kind == "slot_coordinator.slot_non_use":
        return "The slot coordinator", f"the release of {number}'s slot at {airport}"
    return None


def _answer(action: Mapping[str, Any], call: Mapping[str, Any]) -> str:
    """One call's answer: an outside party's code as who accepted or rejected what; any other
    reply as ARROW recorded it."""
    status, message = call.get("status"), str(call.get("message") or "")
    told = _told(action)
    if told is None:
        return message
    party, what = told
    if status == "confirmed":
        return f"{party} accepted {what}"
    if status == "refused":
        return f"{party} rejected {what}" + ("" if message in _REFUSED else f": {message}")
    if status == "pending":
        return f"{party} is considering {what}"
    return f"{what[0].upper()}{what[1:]}: {message}"


def _flight_of(call: Mapping[str, Any]) -> str:
    return unquote(str(call.get("path") or "").rsplit("/", 1)[-1])


def _changed(kind: str, call: Mapping[str, Any]) -> str:
    """A flight operations control confirmed, in words."""
    if kind == "change_tail":
        return f"{_flight_of(call)} moved to {(call.get('sent') or {}).get('aircraft_reg')}"
    return f"{_flight_of(call)} {'cancelled' if kind == 'cancel_flights' else 'moved'}"


def answered(action: Mapping[str, Any]) -> str:
    """What the recipients answered, in words, from the calls recorded on the action: every
    reply once all are confirmed, else the answer that stopped or holds it. A flight change
    stopped part-way also says which flights were changed and which were not sent."""
    made = [call for call in action.get("calls") or [] if isinstance(call, Mapping)]
    stopped = next((call for call in made if call.get("status") != "confirmed"), None)
    if stopped is None:
        return "; ".join(text for call in made if (text := _answer(action, call)))
    kind = action["kind"]
    if kind not in _FLIGHT_CHANGES:
        return _answer(action, stopped)
    done = [_changed(kind, call) for call in made if call.get("status") == "confirmed"]
    status, message = str(stopped.get("status")), str(stopped.get("message") or "")
    held = f"{_flight_of(stopped)} {_NOT_DONE.get(status, status)}"
    sent = {_flight_of(call) for call in made}
    unsent = [_flight_of({"path": call.path}) for call in calls(action)]
    unsent = [flight for flight in unsent if flight not in sent]
    return "; ".join(
        [
            *done,
            f"{held}: {message}" if message else held,
            *([f"{_listed(unsent)} not sent"] if unsent else []),
        ]
    )
