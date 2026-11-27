"""The sample systems at each recipient's own interface (native dispatch spec §3).

Operations control takes a flight change as a resource update bound to the flight's version
(If-Match); the crew and passenger systems take changes bound to the plan digest ARROW validated;
external parties get their own messages: EUROCONTROL's Network Manager an ICAO DLA, CHG or CNL,
Lisbon A-CDM a target off-block time, the slot coordinator a request it answers later. Each route
only translates into the action service, which keeps the ledger (one action per Idempotency-Key,
else X-Request-Id), the conditional writes, the receipts and the coordinator's pending answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from airline_systems.actions import ActionRequest, ActionService, source_changed
from airline_systems.caller import trace_id
from airline_systems.defects import DefectInput, DefectService
from airline_systems.delays import DelayInput, DelayService
from airline_systems.operating_day import ICAO
from airline_systems.schedule import LISBON, ScheduleStore, lisbon_today
from airline_systems.versions import VERSIONED, flight_version

Reply = tuple[int, dict[str, Any]]

_TRACE = r"^[0-9a-f]{32}$"
_FLIGHT = r"^[A-Z0-9]{2,8}$"
_RESERVE = r"^R[0-9]{1,2}$"


class PassengerNotices(BaseModel):
    """Booking: tell the passengers of these flights of possible delays (a storm plan approved,
    no new times), their new times (sent after the retime, the default) or that no change is
    expected (the storm stood down)."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=_TRACE)
    flights: list[str] = Field(min_length=1, max_length=400)
    kind: Literal["possible_delay", "new_times", "no_change"] = "new_times"


class StandbyMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    crew_id: str = Field(pattern=r"^[A-Z0-9]{1,8}$")
    rank: Literal["CPT", "FO", "SC", "CC"]


class StandbyCrew(BaseModel):
    """One standby crew a storm plan calls: its option, its call-by time (Lisbon local time,
    report minus 60 minutes) and its members, as the reserve options name them."""

    model_config = ConfigDict(extra="forbid")
    reserve_option_id: str = Field(pattern=_RESERVE)
    call_by_local: datetime
    crew: list[StandbyMember] = Field(min_length=1, max_length=20)


class StandbyAlerts(BaseModel):
    """Crew: put a case's standby crews on alert (an alert is not a call)."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=_TRACE)
    crews: list[StandbyCrew] = Field(min_length=1, max_length=20)


class StandbyCalls(BaseModel):
    """Crew: call standby crews the case put on alert (crew only)."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=_TRACE)
    reserve_option_ids: list[Annotated[str, Field(pattern=_RESERVE)]] = Field(
        min_length=1, max_length=20
    )


class StandbyRelease(BaseModel):
    """Crew: release every standby crew the case has on alert or called (a stood-down storm)."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=_TRACE)


class BodyRefused(ValueError):
    """A playbook write's body its interface does not take (400); nothing of it is recorded."""


def _local(moment: datetime) -> str:
    """A Lisbon local time as the systems keep it (no offset, whole seconds)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(LISBON).replace(tzinfo=None)
    return moment.replace(microsecond=0).isoformat()


def playbook_body(path: str, body: Mapping[str, Any]) -> tuple[str, dict[str, Any], str] | None:
    """The OCC playbook write at ``path`` (the passenger notice) or a storm case's standby alert,
    call or release, as its interface validates it: the action kind, the payload (normalised,
    validated fields only, as the audit records it) and the case's trace; None for any other
    route. Raises BodyRefused for a body the interface does not take."""
    if path == "/api/passenger-notices":
        notices = _parsed(PassengerNotices, body)
        flights = list(dict.fromkeys(_flight_number(number) for number in notices.flights))
        return "notify_passengers", {"flights": flights, "kind": notices.kind}, notices.trace_id
    if path == "/api/standby-alerts":
        alerts = _parsed(StandbyAlerts, body)
        crews = [
            {
                "reserve_option_id": item.reserve_option_id,
                "call_by_local": _local(item.call_by_local),
                "crew": [member.model_dump() for member in item.crew],
            }
            for item in alerts.crews
        ]
        return "alert_standby", {"crews": crews}, alerts.trace_id
    if path == "/api/standby-alerts/call":
        calls = _parsed(StandbyCalls, body)
        return "call_standby", {"reserve_option_ids": calls.reserve_option_ids}, calls.trace_id
    if path == "/api/standby-alerts/release":
        return "release_standby", {}, _parsed(StandbyRelease, body).trace_id
    return None


NM_MESSAGE = re.compile(
    r"^\((DLA|CHG|CNL)-([A-Z0-9]+)-([A-Z]{4})(\d{4})-([A-Z]{4})-DOF/(\d{6})"
    r"(?:-18/REG/([A-Z0-9]+))?\)$"
)
_NM_KINDS = {
    "DLA": "network_manager.fpl_delay",
    "CHG": "network_manager.fpl_change",
    "CNL": "network_manager.fpl_cancel",
}
# The flight changes operations control takes, by the fields a PATCH sends.
_FLIGHT_CHANGES = {
    frozenset({"std_local", "sta_local"}): "retime_flights",
    frozenset({"status"}): "cancel_flights",
    frozenset({"aircraft_reg"}): "change_tail",
}
_SLOT_KINDS = {
    "change": "slot_coordinator.slot_change_request",
    "non_use": "slot_coordinator.slot_non_use",
}
_PLAYBOOK_STATUS = {
    "notify_passengers": "accepted",
    "alert_standby": "alerted",
    "call_standby": "called",
    "release_standby": "released",
}
_FLIGHT_NO = r"(?P<flight_no>[A-Za-z0-9]{2,8})"


class _Answer(Exception):
    """A reply decided before the action service is reached, or its refusal."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _named(body: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: body[key] for key in keys if body.get(key) is not None}


def _parsed[Model: BaseModel](model: type[Model], body: Mapping[str, Any]) -> Model:
    """The body as its interface defines it, or BodyRefused naming the first field at fault."""
    try:
        return model.model_validate(dict(body))
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "body"
        raise BodyRefused(f"{where}: {first['msg']}") from error


def _flight_number(value: str) -> str:
    """A flight number as the systems keep it, or BodyRefused."""
    code = value.strip().upper()
    if not re.fullmatch(_FLIGHT, code):
        raise BodyRefused(f"Not a flight: {value[:16]!r}")
    return code


def _rejected(reason: str) -> Reply:
    return 200, {"status": "REJ", "reason": reason}


def _recorded_body[Model: BaseModel](model: type[Model], body: Mapping[str, Any]) -> Model:
    """A record's body as its interface defines it; 400 naming the first field at fault."""
    try:
        return _parsed(model, body)
    except BodyRefused as error:
        raise _Answer(400, {"detail": str(error)}) from error


class NativeRoutes:
    def __init__(self, service: ActionService, store: ScheduleStore) -> None:
        self.service = service
        self.store = store
        self._routes: list[tuple[str, str, re.Pattern[str], Callable[..., Reply]]] = [
            ("schedule", "PATCH", re.compile(rf"/api/flights/{_FLIGHT_NO}"), self._flight_change),
            ("crew", "POST", re.compile(r"/api/duty-changes"), self._duty_changes),
            ("schedule", "POST", re.compile(r"/api/delays"), self._record_delay),
            ("maintenance", "POST", re.compile(r"/api/defects"), self._record_defect),
            ("booking", "POST", re.compile(r"/api/rebookings"), self._rebooking),
            ("booking", "POST", re.compile(r"/api/passenger-notices"), self._playbook_write),
            ("crew", "POST", re.compile(r"/api/standby-alerts"), self._playbook_write),
            ("crew", "POST", re.compile(r"/api/standby-alerts/call"), self._playbook_write),
            ("crew", "POST", re.compile(r"/api/standby-alerts/release"), self._playbook_write),
            ("booking", "POST", re.compile(r"/ground-handling/transfer-orders"), self._transfers),
            ("slots", "POST", re.compile(r"/nm/messages"), self._nm_message),
            ("slots", "POST", re.compile(rf"/acdm/flights/{_FLIGHT_NO}/tobt"), self._tobt),
            ("slots", "POST", re.compile(r"/slot-coordinator/requests"), self._slot_request),
            (
                "slots",
                "GET",
                re.compile(r"/slot-coordinator/requests/(?P<request_id>[a-z0-9-]{8,80})"),
                self._slot_answer,
            ),
        ]

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Reply:
        lowered = {key.lower(): value for key, value in headers.items()}
        for system, verb, pattern, route in self._routes:
            found = pattern.fullmatch(path)
            if found and verb == method.upper() and system == self.service.system:
                arguments = {"path": path} if route == self._playbook_write else {}
                try:
                    return route(lowered, dict(body or {}), **found.groupdict(), **arguments)
                except _Answer as answer:
                    return answer.status, answer.body
        return 404, {"detail": "Not found"}

    # Operations control (Schedule)
    def _flight_change(
        self, headers: Mapping[str, str], body: dict[str, Any], flight_no: str
    ) -> Reply:
        number = flight_no.upper()
        kind = _FLIGHT_CHANGES.get(frozenset(body))
        if kind is None or (kind == "cancel_flights" and body["status"] != "cancelled"):
            raise _Answer(
                400,
                {
                    "detail": "A flight change sends std_local and sta_local, "
                    "status cancelled, or aircraft_reg"
                },
            )
        current = self._flight(number)
        if current is None:
            raise _Answer(404, {"detail": f"Flight {number} not found"})
        prior = self.service.actions.find_action(self._ledger_id(headers))
        if prior is not None:
            # A retry of an applied change is answered from the ledger, whatever the version now.
            expected: dict[str, Any] = prior["request"].get("expected") or {}
        else:
            if headers.get("if-match", "").strip().strip('"') != flight_version(current):
                raise _Answer(412, {"detail": source_changed("flight", {"flight_no": number})})
            # The conditional write still refuses a flight changed after this check.
            expected = {"flights": {number: {key: current[key] for key in VERSIONED}}}
        payload = (
            {"flight_nos": [number]}
            if kind == "cancel_flights"
            else {"flights": [{"flight_no": number, **body}]}
        )
        self._confirmed(self._apply(headers, kind, payload, expected))
        flight = self._flight(number) or current
        return 200, {
            "flight_no": number,
            **{key: flight[key] for key in VERSIONED},
            "version": flight_version(flight),
        }

    # Crew system (Crew)
    def _duty_changes(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        # Standby crews: reserve_option_ids, every one the plan calls; a single
        # reserve_option_id is still taken (ARROW before several standby crews).
        standby = body.get("reserve_option_ids") or body.get("reserve_option_id")
        kind = "assign_reserve_crew" if standby else "update_duties"
        payload = _named(body, "flights", "originals", "reserve_option_ids", "reserve_option_id")
        receipt = self._confirmed(self._apply(headers, kind, payload, self._digest(headers)))
        return 200, {"status": "accepted", "message": receipt["message"]}

    # The reported disruption, recorded by ARROW's executor as the first action of a plan it
    # carries out: operations control records a delay, Maintenance a defect (and marks the
    # aircraft out of service until its estimate), each issuing its event id now. The same
    # record sent again is answered as recorded; a different one under the case is refused.
    def _record_delay(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        report = _recorded_body(DelayInput, body)
        try:
            recorded = DelayService(self.store).record(report)
        except ValueError as error:
            raise _Answer(409, {"detail": str(error)}) from error
        event = str(recorded["event_id"])
        cause = str(recorded["cause"]).replace("_", " ")
        return 201, {
            "status": "recorded",
            "event_id": event,
            **_named(recorded, "flight_no", "delay_minutes", "cause", "aircraft_reg"),
            "message": f"Delay recorded as event {event}: {recorded['flight_no']}"
            f" {recorded['delay_minutes']} min late, {cause}",
        }

    def _record_defect(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        report = _recorded_body(DefectInput, body)
        try:
            recorded = DefectService(self.store).record(report)
        except ValueError as error:
            raise _Answer(409, {"detail": str(error)}) from error
        event = str(recorded["event_id"])
        until = datetime.fromisoformat(str(recorded["unavailable_until"]))
        return 201, {
            "status": "recorded",
            "event_id": event,
            **_named(recorded, "aircraft_reg", "unavailable_minutes", "unavailable_until"),
            "message": f"Defect recorded as event {event}: {recorded['aircraft_reg']} out of"
            f" service until {until:%H:%M}",
        }

    # Passenger system and ground handling (Booking)
    def _rebooking(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        payload = _named(body, "option_id", "additional_delay_minutes", "flights", "originals")
        receipt = self._confirmed(
            self._apply(headers, "rebook_passengers", payload, self._digest(headers))
        )
        return 200, {"status": "rebooked", "message": receipt["message"]}

    def _transfers(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        kind = "ground_handling.direct_transfer"
        receipt = self._apply(headers, kind, _named(body, "transfers"))
        return 200, {"status": "acknowledged", "message": receipt["message"]}

    # The OCC playbook's passenger notice (OCC playbook plan) and a storm case's standby alerts
    # (case summary and held storm plan): each answers its status with its facts, or a plain 409
    # refusal; a repeat under the same Idempotency-Key gets the first answer.
    def _playbook_write(self, headers: Mapping[str, str], body: dict[str, Any], path: str) -> Reply:
        try:
            found = playbook_body(path, body)
        except BodyRefused as error:
            raise _Answer(400, {"detail": str(error)}) from error
        if found is None:
            raise _Answer(404, {"detail": "Not found"})
        kind, payload, trace = found
        receipt = self._confirmed(self._apply(headers, kind, payload, trace=trace))
        return 200, {
            "status": _PLAYBOOK_STATUS[kind],
            **(receipt.get("answer") or {}),
            "message": receipt["message"],
        }

    # EUROCONTROL Network Manager, Lisbon A-CDM and the slot coordinator (Slots)
    def _nm_message(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        self._ledger_id(headers)
        text = str(body.get("text") or "")
        found = NM_MESSAGE.fullmatch(text)
        if found is None:
            return _rejected("Unreadable message")
        kind, flight_no, adep, eobt, ades, dof, reg = found.groups()
        try:
            day = datetime.strptime(dof, "%y%m%d").date()
            off_block = datetime.combine(day, datetime.strptime(eobt, "%H%M").time())
        except ValueError:
            return _rejected("Unreadable message")
        # EUROCONTROL works in UTC; the airline's day runs on Lisbon time.
        off_block = off_block.replace(tzinfo=UTC).astimezone(LISBON).replace(tzinfo=None)
        day = off_block.date()
        for aerodrome in (adep, ades):
            if aerodrome not in ICAO.values():
                return _rejected(f"Unknown aerodrome {aerodrome}")
        # The flight of that day, or of the day before: a delay can carry it past midnight.
        flight = next(
            (
                candidate
                for on in (day, day - timedelta(days=1))
                if (candidate := self._flight(flight_no, on)) is not None
                and (ICAO.get(candidate["origin"]), ICAO.get(candidate["destination"]))
                == (adep, ades)
            ),
            None,
        )
        if flight is None:
            return _rejected(f"Unknown flight {flight_no} {adep}-{ades} DOF/{dof}")
        if kind == "CHG" and reg is None:
            return _rejected("CHG without a change")
        payload: dict[str, Any] = {"flight_no": flight_no, "text": text}
        if kind == "DLA":
            payload["eobt"] = off_block.isoformat()
        if kind == "CHG":
            payload["aircraft_reg"] = reg
        try:
            receipt = self._apply(headers, _NM_KINDS[kind], payload)
        except _Answer as answer:
            return _rejected(str(answer.body.get("detail")))
        if receipt["status"] != "confirmed":
            return _rejected(str(receipt["message"]))
        return 200, {"status": "ACK"}

    def _tobt(self, headers: Mapping[str, str], body: dict[str, Any], flight_no: str) -> Reply:
        payload = {"flight_no": flight_no.upper(), **_named(body, "airport", "tobt")}
        self._apply(headers, "airport_cdm.tobt_update", payload)
        return 200, {"status": "accepted"}

    def _slot_request(self, headers: Mapping[str, str], body: dict[str, Any]) -> Reply:
        kind = _SLOT_KINDS.get(str(body.get("type")))
        if kind is None:
            raise _Answer(400, {"detail": "A slot request's type is change or non_use"})
        receipt = self._apply(headers, kind, _named(body, "airport", "flight_no", "request"))
        status = str(receipt["status"])
        return 202 if status == "pending" else 200, {
            "request_id": receipt["action_id"],
            "status": status,
        }

    def _slot_answer(
        self, headers: Mapping[str, str], body: dict[str, Any], request_id: str
    ) -> Reply:
        receipt = self.service.status(request_id)
        if receipt is None or not str(receipt["kind"]).startswith("slot_coordinator."):
            raise _Answer(404, {"detail": f"No slot request {request_id}"})
        return 200, {
            "request_id": request_id,
            "status": receipt["status"],
            "message": receipt["message"],
        }

    # Translation into the action service
    @staticmethod
    def _ledger_id(headers: Mapping[str, str]) -> str:
        key = headers.get("idempotency-key") or headers.get("x-request-id")
        if not key:
            raise _Answer(400, {"detail": "Idempotency-Key or X-Request-Id is required"})
        return key

    @staticmethod
    def _digest(headers: Mapping[str, str]) -> dict[str, Any]:
        digest = headers.get("if-match", "").strip().strip('"')
        return {"plan_digest": digest} if digest else {}

    def _apply(
        self,
        headers: Mapping[str, str],
        kind: str,
        payload: dict[str, Any],
        expected: dict[str, Any] | None = None,
        *,
        trace: str | None = None,
    ) -> dict[str, Any]:
        try:
            request = ActionRequest.model_validate(
                {
                    "action_id": self._ledger_id(headers),
                    "kind": kind,
                    "trace_id": trace or trace_id(headers) or "0" * 32,
                    "expected": expected or {},
                    "payload": payload,
                }
            )
        except ValidationError as error:
            detail = "Idempotency-Key or X-Request-Id: 8-80 lowercase letters, digits or hyphens"
            raise _Answer(400, {"detail": detail}) from error
        try:
            receipt, _ = self.service.apply(request)
        except ValueError as error:
            raise _Answer(409, {"detail": str(error)}) from error
        return receipt

    @staticmethod
    def _confirmed(receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt["status"] == "refused":
            raise _Answer(412, {"detail": receipt["message"]})
        return receipt

    def _flight(self, flight_no: str, day: date | None = None) -> dict[str, Any] | None:
        on = day or lisbon_today()
        flights = self.store.recovery_inventory(on)["flights"]
        return next(
            (
                flight
                for flight in flights
                if flight["flight_no"] == flight_no and flight.get("flight_date") == on.isoformat()
            ),
            None,
        )
