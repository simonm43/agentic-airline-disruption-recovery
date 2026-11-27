"""Standby crews for duties a recovery option pushes past their FDP limit, and their alerts.

A duty breaches when its flight duty period under the option (report to the last arrival,
on-blocks, as EASA defines FDP and as Crew data records it) exceeds the duty's own
``max_fdp_minutes``. The duty's release (30 minutes after on-blocks) stays the duty record.

A standby crew takes the legs after the latest arrival at the duty's base that leaves the
original duty legal, or the whole duty from the base if its crew has not reported yet. EASA calls
a same-day call-out standby (ORO.FTL.225, CS FTL.1.225; reserve crew practice research §2 and §4):

- It is called no earlier than the operating clock or the start of its standby, and no later than
  its end. At home it answers in 60 minutes and then reports, so it may report up to an hour after
  its window (EASA FAQ: the response time continues the standby); at the airport it reports at
  the call, inside its window. The duty may end after the window.
- It reports 60 minutes (A320 family) or 75 minutes (A330) before the first departure it takes.
  A crew that cannot be there by then reports as soon as it can, and the planner moves the
  departure until the crew is ready (``earliest_departure_local``). Until it has, a duty plan
  calling that crew leaves the breach unresolved and Crew refuses to write it.
- Its limit is Table 2 at the report time and sector count, less the standby time before the
  report beyond 6 h (home) or 4 h (airport); airport standby plus FDP stays within 16 h. Home
  standby that starts between 23:00 and 07:00 does not count those hours before the call. The
  most restricted member sets the crew's limit.
- It is a complete crew of the duty's base and qualification and of one standby type, home
  preferred when both are ready in time; within a rank, the member whose standby ends first.
  Nobody is in two options; duties are served in duty order.

Not modelled (assumptions): splitting flight deck and cabin, positioning a crew from another
base, the collective agreement's split-duty guard, cumulative and rest limits, and the 8 h
threshold with in-flight rest or split duty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol, cast

from airline_systems.defects import recovery_options
from airline_systems.facts import inventory as inventory_of
from airline_systems.operating_day import max_daily_fdp_minutes
from airline_systems.schedule import DEMO_CLOCK, ScheduleStore, lisbon_today

RESERVE_RELEASE = timedelta(minutes=30)
STANDBY_TYPES = ("home", "airport")
CALL_OUT = {"home": timedelta(minutes=60), "airport": timedelta(0)}
# Standby time before the report beyond which the maximum FDP falls (CS FTL.1.225).
STANDBY_ALLOWANCE = {"home": timedelta(hours=6), "airport": timedelta(hours=4)}
AIRPORT_STANDBY_AND_FDP = timedelta(hours=16)
# Home standby starting in these hours does not count them before the call (CS FTL.1.225(b)).
NIGHT_START, NIGHT_END = time(23, 0), time(7, 0)
# Report to the first departure (TAP pilots' agreement, cl. 18: medium haul 60, long haul 75).
REPORT_BEFORE_DEPARTURE = {"A330": timedelta(minutes=75)}
REPORT_DEFAULT = timedelta(minutes=60)
RANKS = ("CPT", "FO", "SC", "CC")


def minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


def moved_duty(
    duty: dict[str, Any],
    proposed: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    before: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """The duty's flown legs (by departure), moved release and FDP; None if nothing is flown."""
    legs = [proposed.get(number) or current[number] for number in duty["flight_nos"]]
    flown = sorted(
        (leg for leg in legs if leg.get("status") != "cancelled"), key=lambda leg: leg["std_local"]
    )
    if not flown:
        return None
    last_now = max(datetime.fromisoformat(leg["sta_local"]) for leg in flown)
    last_before = max(
        datetime.fromisoformat(
            (before.get(leg["flight_no"]) or current[leg["flight_no"]])["sta_local"]
        )
        for leg in flown
    )
    release = datetime.fromisoformat(duty["release_local"]) + (last_now - last_before)
    report = datetime.fromisoformat(duty["report_local"])
    limit = duty.get("max_fdp_minutes")
    if not isinstance(limit, int):
        raise ValueError(f"Crew data has no FDP limit for duty {duty['duty_id']}")
    return {
        "legs": flown,
        "release": release,
        "fdp_minutes": minutes(last_now - report),
        "max_fdp_minutes": limit,
        "post_flight": datetime.fromisoformat(duty["release_local"]) - last_before,
    }


def reserve_options(
    crew: dict[str, Any],
    proposed: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    before: dict[str, dict[str, Any]],
    held: Iterable[str] = (),
) -> dict[str, Any]:
    """Standby crews for the duties ``proposed`` breaches; nobody in ``held`` (called by
    another case) is offered."""
    now = datetime.combine(lisbon_today(), DEMO_CLOCK)
    breaches: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    used: set[str] = set(held)
    for duty in sorted(crew["duties"], key=lambda item: item["duty_id"]):
        if not proposed.keys() & set(duty["flight_nos"]):
            continue
        moved = moved_duty(duty, proposed, current, before)
        if moved is None or moved["fdp_minutes"] <= moved["max_fdp_minutes"]:
            continue
        breaches.append(
            {
                "duty_id": duty["duty_id"],
                "fdp_minutes": moved["fdp_minutes"],
                "max_fdp_minutes": moved["max_fdp_minutes"],
            }
        )
        option = _reserve_for(duty, moved, crew["reserves"], used, now)
        if option is not None:
            used.update(member["crew_id"] for member in option["crew"])
            options.append({"reserve_option_id": f"R{len(options) + 1}", **option})
    return {"breaches": breaches, "options": options}


def _reserve_for(
    duty: dict[str, Any],
    moved: dict[str, Any],
    reserves: list[dict[str, Any]],
    used: set[str],
    now: datetime,
) -> dict[str, Any] | None:
    legs = moved["legs"]
    report = datetime.fromisoformat(duty["report_local"])
    splits = [
        index
        for index, leg in enumerate(legs[:-1])
        if leg["destination"] == duty["base"]
        and minutes(datetime.fromisoformat(leg["sta_local"]) - report) <= moved["max_fdp_minutes"]
    ]
    if report > now:
        splits.insert(0, -1)  # The original crew has not reported: a standby crew may fly it all.
    lead = REPORT_BEFORE_DEPARTURE.get(duty["qualification"], REPORT_DEFAULT)
    for index in reversed(splits):
        taken = legs[index + 1 :]
        if taken[0]["origin"] != duty["base"]:
            continue  # A standby crew waits at its own base.
        departure = datetime.fromisoformat(taken[0]["std_local"])
        on_blocks = datetime.fromisoformat(taken[-1]["sta_local"])
        crew = _crew_for(
            duty,
            reserves,
            used,
            now,
            report=departure - lead,
            flown=on_blocks - departure + lead,
            sectors=len(taken),
        )
        if crew is None:
            continue
        start: datetime = crew["report"]
        # A crew that reports late moves the departure, and the legs after it, by as much.
        late = start - (departure - lead)
        return {
            "duty_id": duty["duty_id"],
            # Where the crew stands by: the Standby crews rule counts what each base keeps.
            "base": duty["base"],
            "qualification": duty["qualification"],
            "split_after_flight": legs[index]["flight_no"] if index >= 0 else None,
            "flights_for_reserve": [leg["flight_no"] for leg in taken],
            "crew": [{"crew_id": m["crew_id"], "rank": m["rank"]} for m in crew["members"]],
            "standby_type": crew["standby_type"],
            "departure_local": departure.isoformat(),
            "called_local": (start - CALL_OUT[crew["standby_type"]]).isoformat(),
            "report_local": start.isoformat(),
            "earliest_departure_local": (start + lead).isoformat(),
            "release_local": (on_blocks + late + RESERVE_RELEASE).isoformat(),
            "fdp_minutes": minutes(on_blocks + late - start),
            "max_fdp_minutes": crew["max_fdp_minutes"],
        }
    return None


def _crew_for(
    duty: dict[str, Any],
    reserves: list[dict[str, Any]],
    used: set[str],
    now: datetime,
    *,
    report: datetime,
    flown: timedelta,
    sectors: int,
) -> dict[str, Any] | None:
    """The complete crew of one standby type that can report soonest from ``report`` on, each
    member inside its window and within its own limit for the FDP ``flown`` from report."""
    needed = _needed(duty["pilots_required"], duty["cabin_required"])
    best: dict[str, Any] | None = None
    for standby_type in STANDBY_TYPES:
        members = [
            _member(row, now)
            for row in reserves
            if row["crew_id"] not in used
            and row["base"] == duty["base"]
            and row["qualification"] == duty["qualification"]
            and row["rank"] in needed
            and row["standby_type"] == standby_type
        ]
        for at in sorted({report, *(m["earliest"] for m in members if m["earliest"] > report)}):
            limits = {
                m["crew_id"]: _limit(m, at, sectors, duty["pilots_required"])
                for m in members
                if m["earliest"] <= at <= m["latest"]
            }
            ready = [m for m in members if limits.get(m["crew_id"], -1) >= minutes(flown)]
            picked = _pick(ready, needed)
            if picked is None:
                continue
            if best is None or at < best["report"]:  # home first: the airport crew stays ready
                best = {
                    "standby_type": standby_type,
                    "report": at,
                    "members": picked,
                    "max_fdp_minutes": min(limits[m["crew_id"]] for m in picked),
                }
            break
    return best


def _needed(pilots: int, cabin: int) -> dict[str, int]:
    return {"CPT": 1, "FO": pilots - 1, "SC": 1, "CC": cabin - 1}


def _member(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A standby crew member with the earliest and latest times it can report: called no earlier
    than the clock or the start of its standby and no later than its end, then its call-out."""
    standby_type = row["standby_type"]
    if standby_type not in CALL_OUT:
        raise ValueError(f"Unknown standby type {standby_type!r} for {row['crew_id']}")
    start = datetime.fromisoformat(row["start_local"])
    end = datetime.fromisoformat(row["end_local"])
    return {
        "crew_id": row["crew_id"],
        "rank": row["rank"],
        "standby_type": standby_type,
        "start": start,
        "end": end,
        "earliest": max(now, start) + CALL_OUT[standby_type],
        "latest": end + CALL_OUT[standby_type],
    }


def _limit(member: dict[str, Any], report: datetime, sectors: int, pilots: int) -> int:
    """Table 2 at the report, less the standby time before it beyond the allowance (CS
    FTL.1.225); airport standby plus FDP at most 16 h."""
    waited = report - member["start"]
    if member["standby_type"] == "home":
        waited -= _night_before_call(member["start"], report - CALL_OUT["home"])
    allowance = STANDBY_ALLOWANCE[member["standby_type"]]
    limit = max_daily_fdp_minutes(report.time(), sectors, pilots) - minutes(
        max(timedelta(0), waited - allowance)
    )
    if member["standby_type"] == "airport":
        limit = min(limit, minutes(AIRPORT_STANDBY_AND_FDP - waited))
    return limit


def _night_before_call(start: datetime, call: datetime) -> timedelta:
    """The hours of a home standby starting between 23:00 and 07:00 that pass before the call."""
    clock = start.time()
    if NIGHT_END <= clock < NIGHT_START:
        return timedelta(0)
    morning = start.date() + timedelta(days=1 if clock >= NIGHT_START else 0)
    return max(timedelta(0), min(call, datetime.combine(morning, NIGHT_END)) - start)


def too_late(option: dict[str, Any]) -> str | None:
    """Why a standby crew option cannot take its departure yet, or None if it can."""
    departure = datetime.fromisoformat(option["departure_local"])
    if datetime.fromisoformat(option["earliest_departure_local"]) <= departure:
        return None
    return (
        f"{option['reserve_option_id']} reports {option['report_local'][11:16]}, too late for "
        f"{option['flights_for_reserve'][0]} at {departure:%H:%M}"
    )


def _pick(ready: list[dict[str, Any]], needed: dict[str, int]) -> list[dict[str, Any]] | None:
    """Each rank's members whose standby ends first, or None if a rank is short."""
    picked: list[dict[str, Any]] = []
    for rank in RANKS:
        of_rank = sorted(
            (m for m in ready if m["rank"] == rank),
            key=lambda m: (m["end"], m["start"], m["crew_id"]),
        )
        if len(of_rank) < needed[rank]:
            return None
        picked += of_rank[: needed[rank]]
    return picked


def standby_left(crew: dict[str, Any], used: Iterable[str], now: datetime) -> list[dict[str, Any]]:
    """Complete standby crews still callable today, per base and qualification, nobody in
    ``used``. A complete crew is the largest that base's duties of that qualification need, all
    of one standby type and able to report together. A crew a case has called counts as used
    (``called_crew``); one only on alert does not."""
    largest: dict[tuple[str, str], tuple[int, int]] = {}
    for duty in crew["duties"]:
        key = (duty["base"], duty["qualification"])
        pilots, cabin = largest.get(key, (0, 0))
        largest[key] = (max(pilots, duty["pilots_required"]), max(cabin, duty["cabin_required"]))
    taken = set(used)
    left: list[dict[str, Any]] = []
    for (base, qualification), size in sorted(largest.items()):
        members = [
            _member(row, now)
            for row in crew["reserves"]
            if (row["base"], row["qualification"]) == (base, qualification)
            and row["crew_id"] not in taken
        ]
        left.append(
            {"base": base, "qualification": qualification, "crews": _crews(members, _needed(*size))}
        )
    return left


def _crews(members: list[dict[str, Any]], needed: dict[str, int]) -> int:
    """How many complete crews the members make, formed in the order they can report."""
    count = 0
    for standby_type in STANDBY_TYPES:
        free = [m for m in members if m["standby_type"] == standby_type]
        for at in sorted({m["earliest"] for m in free}):
            while (
                picked := _pick([m for m in free if m["earliest"] <= at <= m["latest"]], needed)
            ) is not None:
                count += 1
                ids = {m["crew_id"] for m in picked}
                free = [m for m in free if m["crew_id"] not in ids]
    return count


def chosen(reserve_option_ids: Sequence[str] | None, reserve_option_id: str | None) -> list[str]:
    """The standby crew options a duty plan or crew write calls, each once. A single
    ``reserve_option_id`` (ARROW before several standby crews) counts as a one-item list."""
    if isinstance(reserve_option_ids, str) or not all(
        isinstance(item, str) for item in reserve_option_ids or []
    ):
        raise TypeError("reserve_option_ids is a list of standby crew option ids")
    ids = list(reserve_option_ids or [])
    if reserve_option_id and reserve_option_id not in ids:
        ids.insert(0, reserve_option_id)
    if len(set(ids)) != len(ids):
        raise ValueError("A standby crew option may be called only once")
    return ids


# Standby alerts (sql/019, storm held until confirmed): a case puts its standby crews on alert
# when OCC approves a storm plan, calls one at its call-by time if the storm is still unconfirmed,
# and releases them when the storm is stood down. Records only: no duty or standby row changes.
ALERTED, CALLED, RELEASED = "alerted", "called", "released"


class StandbyAlertStore(Protocol):
    def standby_alerts(self, day: date) -> list[dict[str, Any]]:
        """Today's alerts, each with ``alert_id``, ``trace_id``, ``reserve_option_id``,
        ``crew_ids`` (a list), ``call_by_local`` and ``status``."""
        ...


def called_crew(alerts: Iterable[dict[str, Any]]) -> list[str]:
    """The members of every standby crew a case has called today."""
    return [member for alert in alerts if alert["status"] == CALLED for member in alert["crew_ids"]]


def _crews_named(ids: Sequence[str]) -> str:
    return f"crew{'s' if len(ids) > 1 else ''} {', '.join(ids)}"


def _by_option(option: str) -> int:
    return int(option[1:])


def standby_alert(
    crew: dict[str, Any],
    alerts: list[dict[str, Any]],
    action_id: str,
    trace_id: str,
    crews: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Put a case's standby crews on alert, each with its call-by time and members: the rows,
    the answer and the message. Refused (ValueError) for a member not on standby today in that
    rank, a call-by time not today, a crew already on alert for this case, or a member on alert
    or called for another case."""
    day = lisbon_today()
    on_standby = {row["crew_id"]: row["rank"] for row in crew["reserves"]}
    active = [alert for alert in alerts if alert["status"] != RELEASED]
    ids = [item["reserve_option_id"] for item in crews]
    members = [member["crew_id"] for item in crews for member in item["crew"]]
    if len(set(ids)) != len(ids) or len(set(members)) != len(members):
        raise ValueError("A standby crew, and each of its members, is put on alert once")
    changes: list[dict[str, Any]] = []
    for item in crews:
        option = item["reserve_option_id"]
        if any(a["trace_id"] == trace_id and a["reserve_option_id"] == option for a in active):
            raise ValueError(f"{option} is already on alert for this case")
        for member in item["crew"]:
            crew_id = member["crew_id"]
            if on_standby.get(crew_id) != member["rank"]:
                raise ValueError(f"{option}: {crew_id} is not on standby today")
            if any(crew_id in a["crew_ids"] for a in active if a["trace_id"] != trace_id):
                raise ValueError(f"{option}: {crew_id} is already on alert for another case")
        call_by = datetime.fromisoformat(item["call_by_local"])
        if call_by.date() != day:
            raise ValueError(f"{option}: call by {item['call_by_local']} is not today")
        changes.append(
            {
                "op": "insert",
                "table": "standby_alert",
                "key": {"alert_id": f"{action_id}-{option}"},
                "expected": {},
                "values": {
                    "trace_id": trace_id,
                    "reserve_option_id": option,
                    "crew_ids": ",".join(member["crew_id"] for member in item["crew"]),
                    "call_by_local": call_by.isoformat(),
                    "status": ALERTED,
                },
            }
        )
    times = ", ".join(f"{datetime.fromisoformat(item['call_by_local']):%H:%M}" for item in crews)
    message = f"Standby {_crews_named(ids)} on alert: call by {times}"
    return changes, {"alerted": ids}, message


def standby_call(
    crew: dict[str, Any], alerts: list[dict[str, Any]], trace_id: str, ids: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Call standby crews this case put on alert (crew only: no duty, flight or passenger
    change). Refused (ValueError) for a crew not on alert for this case, already called or
    released, or with a member no longer on standby today (another case's duty change took
    them while they were only on alert)."""
    if len(set(ids)) != len(ids):
        raise ValueError("A standby crew is called once")
    on_standby = {row["crew_id"] for row in crew["reserves"]}
    changes: list[dict[str, Any]] = []
    for option in ids:
        mine = [a for a in alerts if (a["trace_id"], a["reserve_option_id"]) == (trace_id, option)]
        current = next((a for a in mine if a["status"] != RELEASED), None)
        if current is None:
            raise ValueError(
                f"{option} was released" if mine else f"{option} is not on alert for this case"
            )
        if current["status"] == CALLED:
            raise ValueError(f"{option} was already called")
        gone = next((member for member in current["crew_ids"] if member not in on_standby), None)
        if gone is not None:
            raise ValueError(f"{option}: {gone} is no longer on standby today")
        changes.append(
            {
                "op": "update",
                "table": "standby_alert",
                "key": {"alert_id": current["alert_id"]},
                "expected": {"status": ALERTED},
                "values": {"status": CALLED},
            }
        )
    return changes, {"called": ids}, f"Called standby {_crews_named(ids)}"


def standby_release(
    alerts: list[dict[str, Any]], trace_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Release every standby crew this case has on alert or called (a stood-down storm)."""
    held = sorted(
        (a for a in alerts if a["trace_id"] == trace_id and a["status"] != RELEASED),
        key=lambda alert: _by_option(alert["reserve_option_id"]),
    )
    changes = [
        {
            "op": "update",
            "table": "standby_alert",
            "key": {"alert_id": alert["alert_id"]},
            "expected": {"status": alert["status"]},
            "values": {"status": RELEASED},
        }
        for alert in held
    ]
    ids = [alert["reserve_option_id"] for alert in held]
    message = (
        f"Released standby {_crews_named(ids)}"
        if ids
        else "No standby crew on alert for this case: nothing to release"
    )
    return changes, {"released": ids}, message


class ReserveService:
    def __init__(self, store: ScheduleStore) -> None:
        self.store = store

    def options(
        self,
        disruption: Mapping[str, Any],
        option_id: str,
        additional_delay_minutes: int = 0,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """The standby crews for one option of ``disruption``, offered to ``caller``: the case
        asking (its trace, from the call's traceparent)."""
        schedule = inventory_of(self.store, disruption)
        candidates = recovery_options(
            self.store, disruption, additional_delay_minutes, inventory=schedule
        )
        option = next(
            (item for item in candidates["options"] if item["option_id"] == option_id), None
        )
        if option is None:
            raise ValueError(f"Recovery option {option_id!r} is not available")
        return {
            **self.options_for(option["flights"], schedule=schedule, trace_id=caller),
            "option_id": option_id,
            "additional_delay_minutes": additional_delay_minutes,
        }

    def options_for(
        self,
        flights: list[dict[str, Any]],
        *,
        originals: list[dict[str, Any]] | None = None,
        schedule: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """The standby crews offered to case ``trace_id``: a crew another case has called stays
        with that case (without a case, no called crew is offered)."""
        day = lisbon_today()
        schedule = schedule or self.store.recovery_inventory(day)
        alerts = cast(StandbyAlertStore, self.store).standby_alerts(day)
        return reserve_options(
            self.store.domain_inventory(day, "crew"),
            {flight["flight_no"]: flight for flight in flights},
            {flight["flight_no"]: flight for flight in schedule["flights"]},
            {flight["flight_no"]: flight for flight in originals or []},
            called_crew(alert for alert in alerts if alert["trace_id"] != trace_id),
        )
