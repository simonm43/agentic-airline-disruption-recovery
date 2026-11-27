"""Flight delays reported by OCC (staff shortage, baggage, late inbound, border-control queues) as
one delay option."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from airline_systems import event_ids, facts
from airline_systems.defects import (
    _candidate,
    _names,
    _span,
    check_adjustment,
    find_swap,
    propagate,
    recovery_options,
    shift,
    turnaround,
)
from airline_systems.facts import DelayCause
from airline_systems.knock_on import knock_on
from airline_systems.schedule import ScheduleStore, lisbon_today

__all__ = ["DelayCause", "DelayInput", "DelayService", "described", "disruption_with_knock_on"]

_WEATHER = ("airport", "window_start_local", "window_end_local", "hazard", "hourly_rate")


def described(disruption: Mapping[str, Any]) -> dict[str, Any]:
    """The disruption in the fields a read answers with: its cause and what it names (a flight
    and its delay, an aircraft and its time out of service, or a forecast's airport, window,
    hazard and reduced hourly rate), and the operating clock it is read at."""
    kind = disruption.get("type")
    found: dict[str, Any] = {
        "type": kind,
        "cause": "aircraft_defect"
        if kind == "aircraft_defect"
        else "weather"
        if kind == "weather"
        else disruption.get("cause"),
        # One case for the whole storm: it names no aircraft and no flight.
        "aircraft_reg": disruption.get("aircraft_reg") if kind != "weather" else None,
        "flight_no": disruption.get("flight_no") if kind == "flight_delay" else None,
        "delay_minutes": disruption.get("delay_minutes") if kind == "flight_delay" else None,
        "unavailable_minutes": disruption.get("unavailable_minutes")
        if kind == "aircraft_defect"
        else None,
        "reported_at": disruption.get("reported_at"),
    }
    if kind == "weather":
        found.update({key: disruption.get(key) for key in _WEATHER})
    return found


def disruption_with_knock_on(store: ScheduleStore, disruption: Mapping[str, Any]) -> dict[str, Any]:
    """The disruption as operations control sees it and what it does if nothing changes, from
    Schedule's own options (GET /api/disruption and the agents' get_disruption)."""
    inventory = facts.inventory(store, disruption)
    options = recovery_options(store, disruption, inventory=inventory)
    return {**described(disruption), "knock_on": knock_on(options, inventory["flights"])}


class DelayInput(BaseModel):
    """A delay ARROW's executor records in operations control, once a plan for it runs."""

    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    flight_no: str = Field(pattern=r"^AR[0-9]{1,4}$")
    delay_minutes: int = Field(strict=True, ge=5, le=180, multiple_of=5)
    cause: DelayCause
    description: str = Field(min_length=3, max_length=1000)
    user_oid: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class DelayService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def record(self, report: DelayInput) -> dict[str, Any]:
        """Record the delay ARROW's executor reports, checked against today's operation. The
        same record sent again (a retry after a lost reply) is answered as recorded whatever
        has changed in the operation since; a different one under the same trace is refused."""
        existing = self.store.get_delay(report.trace_id)
        if existing is not None:
            facts.same_record(
                existing, report, ("flight_no", "delay_minutes", "cause", "description"), "delay"
            )
            return {**existing, "event_id": event_ids.event_id(report.trace_id)}
        found = facts.reported_delay(
            self.store, report.flight_no, report.delay_minutes, report.cause
        )
        recorded = self.store.record_delay(
            {
                **report.model_dump(),
                "aircraft_reg": found["aircraft_reg"],
                "operating_date": found["operating_date"],
                "reported_at": found["reported_at"],
            }
        )
        # Operations control's own reference for the delay, issued as it records it.
        return {**recorded, "event_id": event_ids.event_id(report.trace_id)}

    def options(
        self,
        delay: Mapping[str, Any],
        additional_delay_minutes: int = 0,
        *,
        inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        check_adjustment(additional_delay_minutes)
        inventory = (
            inventory if inventory is not None else self.store.recovery_inventory(lisbon_today())
        )
        flights = {f["flight_no"]: f for f in inventory["flights"]}
        reported = flights[delay["flight_no"]]
        fleet = {a["registration"]: a for a in inventory["aircraft"]}
        aircraft = fleet[reported["aircraft_reg"]]
        turn = turnaround(aircraft["fleet_type"])
        future = sorted(
            (
                f
                for f in inventory["flights"]
                if f["aircraft_reg"] == reported["aircraft_reg"]
                and f["std_local"] >= reported["std_local"]
            ),
            key=lambda f: f["std_local"],
        )
        ready = datetime.fromisoformat(reported["std_local"]) + timedelta(
            minutes=delay["delay_minutes"]
        )
        carried = propagate(future, ready, turn, aircraft["seats"])
        delayed = shift(carried, additional_delay_minutes, turn, aircraft["seats"])
        changed = [
            moved["flight_no"]
            for moved, original in zip(delayed, future, strict=True)
            if moved["std_local"] != original["std_local"]
        ]
        title = (
            f"Fly {reported['flight_no']} {delay['delay_minutes']} min late and carry the delay "
            "through the aircraft's rotation"
        )
        options = [
            _candidate("delay", title, delayed, additional_delay_minutes=additional_delay_minutes)
        ]
        checked: list[dict[str, Any]] = []
        # Owner: the Aircraft agent also weighs a swap and a cancellation. Both take out
        # one round trip so the rest of the rotation is back on time; a swap keeps it flying.
        # A swap never takes the reported flight: its crew may come in on the late aircraft.
        trip = _recovering_trip(future, carried, ready, turn, aircraft["seats"], 1)
        if trip is None:
            checked.append(
                {
                    "kind": "swap",
                    "available": False,
                    "why": "No round trip of this aircraft could be handed to another aircraft"
                    " to bring its later flights back on time",
                }
            )
        else:
            start, end, rest = trip
            legs = future[start : end + 1]
            donor, why = find_swap(inventory, fleet, reported["aircraft_reg"], legs, turn)
            checked.append({"kind": "swap", "available": donor is not None, "why": why})
            if donor is not None:
                options.append(
                    _candidate(
                        "swap",
                        f"Use {donor} for {_span(legs)} during its scheduled ground window",
                        [
                            *carried[:start],
                            *({**f, "aircraft_reg": donor} for f in legs),
                            *rest,
                        ],
                    )
                )
        trip = _recovering_trip(future, carried, ready, turn, aircraft["seats"], 0)
        if trip is None:
            checked.append(
                {
                    "kind": "cancel",
                    "available": False,
                    "why": "No round trip whose cancellation brings later flights back on time",
                }
            )
        else:
            start, end, rest = trip
            legs = future[start : end + 1]
            on_time = [
                f["flight_no"]
                for f, original in zip(rest, future[end + 1 :], strict=True)
                if f["std_local"] == original["std_local"]
            ]
            options.append(
                _candidate(
                    "cancel",
                    f"Cancel {_span(legs)} and put the rest of the rotation back on time",
                    [*carried[:start], *({**f, "status": "cancelled"} for f in legs), *rest],
                )
            )
            checked.append(
                {
                    "kind": "cancel",
                    "available": True,
                    "why": f"Cancelling {_span(legs)} puts {_names(on_time)} back on time",
                }
            )
        # Owner: "or recommend to cancel last flight due to crew". When the delay carries
        # to the end of the day, cancelling the aircraft's last round trip ends its day (and its
        # crew's duty) earlier, while the earlier flights fly late. Offered beside the cancel
        # above when it is another round trip, so OCC can compare whom each disrupts.
        last = _last_round_trip(future)
        if (
            last is not None
            and last[0] >= 1
            and future[last[0]]["std_local"] != carried[last[0]]["std_local"]
            and (trip is None or last[0] > trip[1])
        ):
            start, end = last
            legs = future[start : end + 1]
            options.append(
                _candidate(
                    "cancel",
                    f"Fly the earlier flights late and cancel {_span(legs)}, the aircraft's last"
                    " round trip",
                    [*carried[:start], *({**f, "status": "cancelled"} for f in legs)],
                    option_id="cancel-last",
                )
            )
            checked.append(
                {
                    "kind": "cancel",
                    "available": True,
                    "why": f"Cancelling {_span(legs)} ends the aircraft's day, and its crew's,"
                    f" after {future[start - 1]['flight_no']}",
                }
            )
        return {
            "cause": delay["cause"],
            "affected_flight_ids": changed,
            "aircraft": {
                "registration": reported["aircraft_reg"],
                "fleet_type": aircraft["fleet_type"],
                "seats": aircraft["seats"],
                "available_from": ready.isoformat(),
            },
            "options": options,
            "checked": checked,
        }


def _last_round_trip(future: list[dict[str, Any]]) -> tuple[int, int] | None:
    """The aircraft's last round trip today: the shortest run of legs at the end of the rotation
    that returns to where it started, so the aircraft ends the day where it would have."""
    if len(future) < 2:
        return None
    end = len(future) - 1
    for start in range(end - 1, -1, -1):
        if future[start]["origin"] == future[end]["destination"]:
            return start, end
    return None


def _recovering_trip(
    future: list[dict[str, Any]],
    carried: list[dict[str, Any]],
    ready: datetime,
    turn: timedelta,
    seats: int,
    first: int,
) -> tuple[int, int, list[dict[str, Any]]] | None:
    """The earliest round trip (from index ``first``) whose removal brings at least one later,
    delayed flight back to its scheduled time: its first and last index and the flights after
    it, as the aircraft then flies them."""
    for start in range(first, len(future)):
        end = next(
            (
                index
                for index in range(start + 1, len(future))
                if future[index]["destination"] == future[start]["origin"]
            ),
            None,
        )
        if end is None:
            continue
        available = (
            ready if start == 0 else datetime.fromisoformat(carried[start - 1]["sta_local"]) + turn
        )
        rest = propagate(future[end + 1 :], available, turn, seats)
        if any(
            after["std_local"] == original["std_local"] != late["std_local"]
            for after, original, late in zip(
                rest, future[end + 1 :], carried[end + 1 :], strict=True
            )
        ):
            return start, end, rest
    return None
