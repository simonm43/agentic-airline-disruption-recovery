"""Carry out an authorised recovery plan, one action at a time (spec D4, D5, D13).

The agents decided; this executor only acts. Before the first write it rechecks what the plan
depends on, reading the systems by the disruption's facts. A delay or defect plan's first action
records the reported disruption in the system that owns it (owner: the airline's systems are
written only once a plan runs), and every other action waits for its receipt. Each action goes to
its recipients as the calls they understand (ops_app.dispatch);
each write is conditional where the system supports it and its reply is recorded. A refusal or
failure stops the actions that depend on it and escalates to OCC. Nothing is rolled back across
systems automatically. Re-running after a crash repeats no write: a confirmed call is not sent
again, an airline system's write is re-sent under the same Idempotency-Key, and a message to an
external party is never sent twice.

A weather plan runs in two stages (case summary and held storm spec §2). On approval only its
"now" actions are sent and the case is held. A weather update recorded on the case settles it:
confirmed inside the planned window at no lower rate, the sources are rechecked and the held
actions sent; confirmed worse, or a source changed, nothing more is sent and OCC decides; dropped,
the warned passengers are told no change is expected, the standby crews released and nothing held
is sent. While held, a standby crew whose call-by time comes on the systems' clock is called
(crew only).
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from opentelemetry.trace import SpanKind

from airline_systems import telemetry
from airline_systems.operating_day import CITY
from ops_app import dispatch
from ops_app.actions import (
    NOW,
    STAND_DOWN,
    WHEN_CONFIRMED,
    authorise_execution,
    call_action,
    disruption_record,
    plan_actions,
    plan_hash,
    stand_down_actions,
    standby_alerted,
    standby_called,
    system_option_id,
)
from ops_app.dispatch import Outbound
from ops_app.engine import STORM_OPEN, CloudDependencyError, Gateway
from ops_app.events import storm_update
from ops_app.planner import case_ref
from ops_app.policy import (
    STANDBY_CHANGED,
    classify,
    last_standby_crews,
    limits_of,
    originals_of,
    review_passengers,
    standby_crews,
    standby_ids,
    standby_name,
)
from ops_app.reports import HAZARD_WORDS, storm_period
from ops_app.store import CaseChange, CaseStore, ExecutionLeaseLost, Unchanged

logger = logging.getLogger(__name__)
__all__ = ["Executor", "authorise_execution"]


def _refusal(error: Exception) -> str:
    """A system's definitive refusal (4xx other than timeouts/throttling) is a change since
    approval; anything transient is re-raised so the supervisor retries."""
    if isinstance(error, CloudDependencyError):
        code = error.status_code
        if code is None or not 400 <= code < 500 or code in {408, 429}:
            raise error
        return error.detail or str(error)
    return str(error)


DONE = {"confirmed", "refused", "failed", "blocked", "unknown", "stood_down"}
STOPPED = {"refused", "failed", "blocked", "unknown"}
# Nothing more to do for the action: confirmed, or never to be sent (the storm stood down).
SETTLED = {"confirmed", "stood_down"}
# An execution that ended: a weather update that arrives then is only recorded.
ENDED = {"completed", "needs_attention", "stood_down", "failed"}
MAX_INTERRUPTED_RUNS = 5
LEVELS = {
    # The reported disruption it read, recorded by the executor as the plan's first action.
    "disruption-detection-agent": "L4",
    "aircraft-recovery-agent": "L2",
    "crew-recovery-agent": "L3",
    "passenger-reaccommodation-agent": "L3",
    "slot-negotiation-agent": "L3",
}


def _definitive(error: CloudDependencyError) -> bool:
    """A 4xx answer is the system's decision; anything else leaves the outcome unknown."""
    code = error.status_code
    return code is not None and 400 <= code < 500 and code not in {408, 429}


def _not_sent(error: CloudDependencyError) -> bool:
    """Nothing left ARROW (no token, or no connection made): the recipient cannot have it."""
    return error.status_code is None and not error.may_continue


_RECEIPT = ("call_id", "status", "message", "at", "request_id", "event_id")
# A refused condition (the flight or plan changed) or a reused key: the recipient's decision.
_REFUSALS = {409, 412}


def _receipt_of(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in _RECEIPT if key in record}


def _summary(action: dict[str, Any], receipt: dict[str, Any]) -> str:
    """What the recipients answered, in words (dispatch.answered); the receipt's message when no
    call was made."""
    return dispatch.answered(action) or str(receipt.get("message", ""))


def _moves_flights(option: dict[str, Any], originals: dict[str, Any]) -> bool:
    """True if the plan flies a flight at another time (a cancellation frees a slot)."""
    return any(
        flight.get("status") != "cancelled"
        and flight["flight_no"] in originals
        and any(
            flight.get(key) != originals[flight["flight_no"]].get(key)
            for key in ("std_local", "sta_local")
        )
        for flight in option.get("flights") or []
    )


def _capacity_change(slots: dict[str, Any]) -> str:
    conflicts = [c for c in slots.get("slot_conflicts") or [] if isinstance(c, dict)]
    if not conflicts:
        return "Lisbon capacity not confirmed by Slots"
    return "; ".join(
        f"{'Lisbon' if c.get('airport') == 'LIS' else c.get('airport')} capacity"
        f" {str(c.get('hour_local') or '')[11:16]}"
        f" ({c.get('movements')} movements, limit {c.get('hourly_capacity')})"
        for c in conflicts
    )


WEATHER = {"actor": "recovery-executor", "autonomy": "system"}


def _stage(action: Mapping[str, Any]) -> str:
    """An action's stage; one planned by an earlier version has none and runs now."""
    return str(action.get("stage") or NOW)


def _hazard(record: Mapping[str, Any]) -> str:
    hazard = str(record.get("hazard"))
    return HAZARD_WORDS.get(hazard, hazard).lower()


def _flights(count: int) -> str:
    return f"{count} flight{'' if count == 1 else 's'}"


def storm_plan(
    document: Mapping[str, Any], option: Mapping[str, Any], actions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """What a weather plan's held part waits for (``execution.storm``); None for a plan whose
    actions all run now. Decide by the earliest standby crew's call-by time, else the first
    departure the plan changes."""
    if not any(_stage(action) == WHEN_CONFIRMED for action in actions):
        return None
    disruption = document.get("disruption") or {}
    call_by = sorted(
        str(crew["call_by_local"])
        for action in actions
        if action["kind"] == "alert_standby"
        for crew in action["payload"]["crews"]
    )
    originals = originals_of(document)
    departures = sorted(
        str(originals[flight["flight_no"]]["std_local"])
        for flight in option.get("flights") or []
        if flight.get("flight_no") in originals
        and (
            flight.get("status") == "cancelled"
            or flight.get("std_local") != originals[flight["flight_no"]].get("std_local")
        )
    )
    first = (call_by or departures or [""])[0]
    period, hazard = storm_period(disruption), _hazard(disruption)
    airport = str(disruption.get("airport"))
    planned = _planned(disruption)
    rate = planned["rate_per_hour"]
    regulation = f"EUROCONTROL regulation: {CITY.get(airport, airport)} arrivals" + (
        f" at {rate} an hour or more" if rate else ""
    )
    return {
        "state": "awaiting",
        "decide_by": first[11:16] or None,
        "planned": planned,
        "confirm_by": [f"METAR or amended TAF: {hazard} {period}", regulation],
        "stand_down_by": [f"Amended TAF: no {hazard} {period}"],
        "updates": [],
    }


def _planned(disruption: Mapping[str, Any]) -> dict[str, Any]:
    """The weather a plan was built for: its window and Schedule's reduced rate."""
    rate = disruption.get("hourly_rate")
    return {
        "window": storm_period(disruption),
        "window_start": disruption.get("window_start"),
        "window_end": disruption.get("window_end"),
        "rate_per_hour": rate if isinstance(rate, int) and not isinstance(rate, bool) else None,
        "hazard": disruption.get("hazard"),
    }


def _said(update: Mapping[str, Any]) -> str:
    """A weather update in words: "Strong crosswind confirmed: 16:00–18:00, rate 24 an hour"."""
    if update.get("status") == "confirmed":
        rate = update.get("rate_per_hour")
        hazard = _hazard(update)
        return f"{hazard[:1].upper()}{hazard[1:]} confirmed: {update.get('window')}" + (
            f", rate {rate} an hour" if rate is not None else ""
        )
    return f"Forecast dropped: no {_hazard(update)} {update.get('window')}"


def _worse(storm: Mapping[str, Any], update: Mapping[str, Any]) -> str | None:
    """Why the confirmed weather is worse than the plan was built for: a lower rate, or a window
    reaching outside the planned one; None if it is not."""
    planned = storm.get("planned") or {}
    found: list[str] = []
    rate, wanted = update.get("rate_per_hour"), planned.get("rate_per_hour")
    if isinstance(rate, int) and isinstance(wanted, int) and rate < wanted:
        found.append(f"rate {rate} an hour, planned {wanted}")
    start, end = planned.get("window_start"), planned.get("window_end")
    if (
        start
        and end
        and not (
            str(start) <= str(update.get("window_start"))
            and str(update.get("window_end")) <= str(end)
        )
    ):
        found.append(f"{update.get('window')}, planned {planned.get('window')}")
    return f"the confirmed weather is worse than planned ({'; '.join(found)})" if found else None


def _sent_now(execution: Mapping[str, Any], kind: str) -> bool:
    return any(
        action["kind"] == kind and action["status"] == "confirmed"
        for action in execution["actions"]
    )


def _returned(execution: Mapping[str, Any], reason: str) -> str:
    """A weather plan back with OCC: why, and what stays in place."""
    kept = [
        words
        for kind, words in (
            ("warn_passengers", "the passengers stay warned"),
            ("alert_standby", "the standby crews on alert"),
        )
        if _sent_now(execution, kind)
    ]
    if not any(action["status"] == "confirmed" for action in execution["actions"]):
        return f"OCC decides: {reason}. Nothing was sent."
    return f"OCC decides: {reason}. Nothing more was sent" + (
        f"; {' and '.join(kept)}." if kept else "."
    )


def _recorded(execution: Mapping[str, Any], state: str, update: Mapping[str, Any]) -> str:
    """A weather update that arrives once the held part was released, the plan stood down or the
    case is with OCC: it is recorded and nothing is reversed or sent."""
    released = bool((execution.get("storm") or {}).get("rechecked"))
    if update.get("status") == "dropped" and released:
        return "Forecast dropped after the held actions were sent: recorded; nothing is reversed."
    if update.get("status") == "confirmed" and state == "dropped":
        return (
            f"{_said(update)} after the stand-down: recorded; nothing is sent. OCC decides"
            " whether to plan again."
        )
    if execution.get("status") == "needs_attention":
        return f"{_said(update)}: recorded; the case is with OCC and nothing more is sent."
    return f"{_said(update)}: recorded; nothing changes."


def settle(trace: str, execution: dict[str, Any], update: dict[str, Any]) -> tuple[str, str]:
    """Apply one weather update to an approved weather plan's execution: the event to log and
    its message. Code, never the model, decides (case summary and held storm spec §2). Until the
    held part is released the latest update decides: a later drop stands the plan down, a later
    confirmation is checked against the plan again. Once released, stood down or with OCC, an
    update is only recorded."""
    storm = execution["storm"]
    state = str(storm.get("state"))
    storm["updates"] = [*(storm.get("updates") or []), update]
    if execution.get("status") in ENDED or storm.get("rechecked") or state == "dropped":
        if update.get("status") == "confirmed" and state == "dropped":
            # The storm came after all: OCC decides whether to plan again (case_state "needs").
            storm["confirmed_after_stand_down"] = True
        return "storm_update_recorded", _recorded(execution, state, update)
    if update.get("status") == "confirmed":
        storm["state"] = "confirmed"
        worse = _worse(storm, update)
        if worse is not None:
            storm["returned"] = worse
            execution["status"] = "needs_attention"
            return "storm_returned_to_occ", _returned(execution, worse)
        return (
            "storm_confirmed",
            f"{_said(update)}. ARROW re-checks the plan, then sends the held actions.",
        )
    storm["state"] = "dropped"
    for action in execution["actions"]:
        # A "now" write with a call recorded may have reached its recipient (a run interrupted
        # mid-call): it is sent again under its key to learn its outcome, and the stand-down is
        # built once it is known (_stand_down_due). Anything never sent is not sent now.
        if action["status"] == "not_started" and not (
            _stage(action) == NOW and action.get("calls")
        ):
            action["status"] = "stood_down"
            action["message"] = "Not sent: the forecast was dropped and the plan stood down"
    return (
        "storm_dropped",
        f"{_said(update)}. ARROW releases the standby crews and tells the warned passengers no"
        " change is expected; nothing held is sent.",
    )


def _stand_down_due(execution: Mapping[str, Any]) -> bool:
    """A dropped storm whose "now" writes all have their outcome: its stand-down can be built
    from the ones confirmed."""
    storm = execution.get("storm")
    return (
        isinstance(storm, dict)
        and storm.get("state") == "dropped"
        and not storm.get("standing_down")
        and execution.get("status") == "executing"
        and all(
            action["status"] in DONE for action in execution["actions"] if _stage(action) == NOW
        )
    )


def uncalled(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The standby crews on alert for a storm case that ARROW has not called yet."""
    called = standby_called(actions)
    return [
        crew for crew in standby_alerted(actions) if str(crew["reserve_option_id"]) not in called
    ]


def _recorded_first(execution: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    """A storm's own write (its warning, alerts, calls and stand-down): recorded before it is
    sent, so a stand-down after an interruption knows it may have reached its recipient."""
    return isinstance(execution.get("storm"), dict) and _stage(action) != WHEN_CONFIRMED


def _stood_down(execution: Mapping[str, Any]) -> str:
    done = {
        action["kind"]: action
        for action in execution["actions"]
        if _stage(action) == STAND_DOWN and action["status"] == "confirmed"
    }
    parts: list[str] = []
    if "release_standby" in done:
        ids = [str(crew["reserve_option_id"]) for crew in standby_alerted(execution["actions"])]
        parts.append(f"{standby_name(ids)} released")
    if "notify_no_change" in done:
        told = len(done["notify_no_change"]["payload"]["flights"])
        parts.append(f"passengers on {_flights(told)} told no change is expected")
    if not parts:
        return "Stood down: the forecast was dropped before anything was sent; nothing changes."
    return f"Stood down: {'; '.join(parts)}; nothing held was sent."


class Executor:
    def __init__(
        self,
        store: CaseStore,
        gateway: Gateway,
        *,
        poll_seconds: float = 2.0,
        pending_timeout_seconds: float = 60.0,
        pending_limit_seconds: float = 1800.0,
        unknown_retries: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.poll_seconds = poll_seconds
        self.pending_timeout_seconds = pending_timeout_seconds
        self.pending_limit_seconds = pending_limit_seconds
        self.unknown_retries = unknown_retries
        self.monotonic = monotonic
        self.sleep = sleep
        self.now = now
        self.owner = ""

    def run(self, trace: str) -> None:
        """Run the case if no other executor holds it; one executor per case (lease)."""
        owner = uuid.uuid4().hex
        document = self.store.claim_execution(trace, owner)
        if document is None:
            return
        self.owner = owner
        execution = document["execution"]
        with telemetry.case_span(
            "Execute plan",
            trace,
            root_kind=SpanKind.CONSUMER,
            attributes={"arrow.actions": len(execution.get("actions") or [])},
        ):
            try:
                self._run(trace, document, execution)
            except ExecutionLeaseLost:
                logger.info("Execution of %s continues in another executor", trace)
            except Exception:
                execution["interrupted_runs"] = execution.get("interrupted_runs", 0) + 1
                try:
                    self.store.update_execution(trace, execution, None, owner=owner, release=True)
                except Exception:
                    logger.exception("Lease of %s not released; it expires by itself", trace)
                raise

    def confirm(self, trace: str, update: Mapping[str, Any]) -> None:
        """The storm is confirmed (``update``: a weather update's data). Recorded on the case;
        a held plan is rechecked and, if nothing got worse, its held actions sent."""
        self._settle_now(trace, {**update, "status": "confirmed"})

    def stand_down(self, trace: str, update: Mapping[str, Any]) -> None:
        """The forecast is dropped. Recorded on the case; a held plan is stood down."""
        self._settle_now(trace, {**update, "status": "dropped"})

    def _settle_now(self, trace: str, data: dict[str, Any]) -> None:
        day = date.fromisoformat(str(self.store.get(trace)["operating_date"]))
        if self.record(trace, storm_update(data, day=day, at=self.now())) == "runnable":
            self.run(trace)

    def record(self, trace: str, update: dict[str, Any]) -> str:
        """Record a weather update (``events.storm_update``) on its storm case once, under the
        case's lock: "duplicate" if the case has it already; "runnable" if it settles a plan
        held until confirmed (run the case next); else "recorded". Before OCC approves a plan
        it is only recorded and shown; once a plan has ended, recorded and nothing reversed."""
        outcome = "recorded"

        def decide(case: dict[str, Any]) -> CaseChange:
            nonlocal outcome
            inbox = dict(case.get("storm") or {})
            updates = list(inbox.get("updates") or [])
            if any(item.get("event_id") == update["event_id"] for item in updates):
                raise Unchanged(None)
            inbox["updates"] = [*updates, dict(update)]
            values: dict[str, Any] = {"storm": inbox}
            execution = case.get("execution")
            events: tuple[dict[str, Any], ...] = ()
            if not isinstance(execution, dict):
                # Before approval the latest update decides what approval will do: confirmed
                # (back to OCC if worse than the plan was built for) or dropped (stood down).
                inbox["state"] = "confirmed" if update["status"] == "confirmed" else "dropped"
                worse = (
                    _worse({"planned": _planned(case.get("disruption") or {})}, update)
                    if update["status"] == "confirmed"
                    else None
                )
                if worse is None:
                    inbox.pop("worse", None)
                else:
                    inbox["worse"] = worse
                event = "storm_confirmed" if update["status"] == "confirmed" else "storm_dropped"
                message = f"{_said(update)} · recorded; nothing is sent until OCC approves a plan."
                events = ({**WEATHER, "event": event, "message": message},)
            elif execution.get("status") == "held":
                values["execution"] = {**execution, "status": "executing"}
                outcome = "runnable"
            elif execution.get("status") in ENDED and isinstance(execution.get("storm"), dict):
                ended = copy.deepcopy(execution)
                event, message = settle(trace, ended, dict(update))
                values["execution"] = ended
                events = ({**WEATHER, "event": event, "message": message},)
            # Otherwise the plan is being carried out: its executor applies the update next.
            return CaseChange(values, events)

        try:
            self.store.mutate(trace, decide, statuses=STORM_OPEN)
        except Unchanged:
            return "duplicate"
        return outcome

    def tick(self, trace: str, clock: datetime | None = None) -> None:
        """A storm held until confirmed: each standby crew on alert whose call-by time has come
        on Schedule's clock (``as_of_local``, read outside any case when not given) is called
        now (crew only: no flight, passenger or EUROCONTROL change); a stand-down releases it."""
        execution = self.store.get(trace).get("execution")
        if not isinstance(execution, dict) or execution.get("status") != "held":
            return
        if (execution.get("storm") or {}).get("state") != "awaiting":
            return
        waiting = uncalled(execution["actions"])
        if not waiting:
            return
        if clock is None:
            clock = datetime.fromisoformat(self.gateway.clock())
        due = [crew for crew in waiting if datetime.fromisoformat(crew["call_by_local"]) <= clock]
        if not due:
            return
        ids = {str(crew["reserve_option_id"]) for crew in due}

        def decide(case: dict[str, Any]) -> CaseChange:
            current = case.get("execution")
            if (
                not isinstance(current, dict)
                or current.get("status") != "held"
                or (current.get("storm") or {}).get("state") != "awaiting"
                or standby_called(current["actions"]) & ids
            ):
                raise Unchanged(None)
            calling = copy.deepcopy(current)
            calling["actions"].append(
                call_action(trace, str(calling["plan_hash"]), calling["actions"], due)
            )
            calling["status"] = "executing"
            return CaseChange({"execution": calling}, ())

        try:
            self.store.mutate(trace, decide, statuses=STORM_OPEN)
        except Unchanged:
            return
        self.run(trace)

    def _run(self, trace: str, document: dict[str, Any], execution: dict[str, Any]) -> None:
        if execution.get("interrupted_runs", 0) >= MAX_INTERRUPTED_RUNS:
            self._stop(trace, execution, f"Execution was interrupted {MAX_INTERRUPTED_RUNS} times")
            return
        option = next(
            (
                o
                for o in document.get("options", [])
                if o.get("option_id") == execution["option_id"]
            ),
            None,
        )
        if option is None or plan_hash(option, execution["case_version"]) != execution["plan_hash"]:
            self._stop(trace, execution, "The approved plan no longer matches the case")
            return
        if execution["status"] == "approved":
            # The plan hash checked above binds the plan's recorded rule version and limits, so
            # they cannot change after approval. The route is rechecked under those limits and
            # must match the approved route; a rule change since approval does not affect it.
            verdict = classify(
                option,
                originals=originals_of(document),
                validation=option.get("validation"),
                limits=limits_of(option),
            )
            if verdict.route != execution["route"]:
                self._stop(trace, execution, "The plan no longer passes the authority policy")
                return
            changes = self._recheck(trace, document, option, execution["route"])
            if changes:
                self._stop(trace, execution, f"Changed since approval: {'; '.join(changes)}")
                return
            execution["actions"] = [
                action.as_dict()
                for action in plan_actions(
                    trace,
                    option,
                    originals=originals_of(document),
                    validation=option.get("validation") or {},
                    airports=document.get("airports") or {},
                    plan_hash=execution["plan_hash"],
                    record=disruption_record(trace, document),
                )
            ]
            execution["status"] = "executing"
            storm = storm_plan(document, option, execution["actions"])
            if storm is not None:
                execution["storm"] = storm
                now = sum(1 for action in execution["actions"] if _stage(action) == NOW)
                held = len(execution["actions"]) - now
                message = (
                    f"Executing {now} actions now; {held} held until the forecast is confirmed;"
                    " sources rechecked."
                )
            else:
                message = f"Executing {len(execution['actions'])} actions; sources rechecked."
            # A held plan's passengers outside policy go to Passenger Services once it goes
            # ahead (_weather).
            review = review_passengers(option.get("validation") or {}) if storm is None else 0
            if review:
                execution["reviews"] = [
                    {
                        "kind": "passenger_services",
                        "passengers": review,
                        "status": "awaiting_expert",
                    }
                ]
                message += (
                    f" {review} passengers outside rebooking policy await Passenger Services."
                )
            if execution["route"] == "auto" and execution.get("approved_by") == "policy":
                message += " Within every domain's limits; OCC informed."
            self._save(trace, execution, "recovery-executor", "execution_started", message)

        while True:
            if isinstance(execution.get("storm"), dict) and not self._weather(
                trace, document, option, execution
            ):
                return
            self._send_ready(trace, execution)
            if self._release_due(execution) or _stand_down_due(execution):
                continue  # recheck then send the held part, or build the stand-down, then send
            fresh = self._finish(trace, execution)
            if fresh is None:
                return
            document = fresh  # a weather update arrived as the plan was about to be held

    def _send_ready(self, trace: str, execution: dict[str, Any]) -> None:
        """Send every action whose dependencies are confirmed; a weather plan's held part only
        once the storm is confirmed and the sources rechecked."""
        storm = execution.get("storm")
        released = not isinstance(storm, dict) or (
            storm.get("state") == "confirmed" and bool(storm.get("rechecked"))
        )
        for action in execution["actions"]:
            if action["status"] in DONE:
                continue
            if _stage(action) == WHEN_CONFIRMED and not released:
                continue
            depends = [a for a in execution["actions"] if a["action_id"] in action["depends_on"]]
            stopped = next((d for d in depends if d["status"] in STOPPED), None)
            if stopped is not None:
                action["status"] = "blocked"
                action["message"] = f"Not sent: {stopped['label']} did not complete"
                continue
            if any(d["status"] != "confirmed" for d in depends):
                continue
            was_pending = action["status"] == "pending"
            receipt = self._send(trace, execution, action)
            status = str(receipt.get("status", "failed"))
            if status == "pending":
                since = action.setdefault("pending_since", self.now().isoformat())
                waited = (self.now() - datetime.fromisoformat(since)).total_seconds()
                if waited < self.pending_limit_seconds:
                    if was_pending:
                        continue  # still waiting; nothing new to record
                else:
                    status = "unknown"
                    receipt = {
                        **receipt,
                        "status": status,
                        "message": f"No answer from {action['label']} after "
                        f"{round(self.pending_limit_seconds / 60)} min",
                    }
                    for call in action["calls"]:
                        if call["call_id"] == receipt.get("call_id"):
                            call.update(status=status, message=receipt["message"])
            action["status"] = status
            action["message"] = _summary(action, receipt)
            action["receipt"] = receipt
            said = action["message"] or action["summary"]
            event = "action_recorded"
            if action["kind"] == "call_standby" and status == "confirmed" and _stage(action) == NOW:
                # Called at its call-by time while the forecast was still unconfirmed.
                event = "standby_called"
                times = str(action["summary"]).partition(" · call-by ")[2]
                said += f" · call-by {times} reached before the forecast was confirmed"
            self._save(
                trace,
                execution,
                action["proposed_by"],
                event,
                # An outside party's answer already names it (EUROCONTROL accepted ...).
                said if action["kind"] in dispatch.MESSAGES else f"{action['label']}: {said}",
                action=action,
            )

    def _release_due(self, execution: dict[str, Any]) -> bool:
        """The storm is confirmed and the "now" part done, but the held part not released yet."""
        storm = execution.get("storm")
        return (
            isinstance(storm, dict)
            and storm.get("state") == "confirmed"
            and not storm.get("rechecked")
            and execution.get("status") == "executing"
            and all(
                action["status"] == "confirmed"
                for action in execution["actions"]
                if _stage(action) == NOW
            )
        )

    def _weather(
        self,
        trace: str,
        document: dict[str, Any],
        option: dict[str, Any],
        execution: dict[str, Any],
    ) -> bool:
        """Apply the weather updates the case recorded since this plan last read them, in order
        (until the held part is released the latest decides); build a dropped storm's stand-down
        once its "now" writes have their outcome; once the storm is confirmed and the "now" part
        done, recheck the sources and release the held part. False when the plan went back to
        OCC (nothing more is sent)."""
        storm = execution["storm"]
        while True:
            seen = {item.get("event_id") for item in storm.get("updates") or []}
            arrived = [
                dict(update)
                for update in (document.get("storm") or {}).get("updates") or []
                if update.get("event_id") not in seen
            ]
            if (
                len(arrived) > 1
                and not storm.get("rechecked")
                and storm.get("state") != "dropped"
                and execution.get("status") not in ENDED
            ):
                # Until the held part is released the latest update decides (those before
                # approval included); the earlier ones are kept, marked as superseded.
                superseded = [{**update, "superseded": True} for update in arrived[:-1]]
                storm["updates"] = [*(storm.get("updates") or []), *superseded]
                arrived = arrived[-1:]
            for update in arrived:
                event, message = settle(trace, execution, update)
                back = execution["status"] == "needs_attention"
                self._save(trace, execution, "recovery-executor", event, message, release=back)
                if back:
                    self._catch_up(trace, execution)
                    return False
            if _stand_down_due(execution):
                execution["actions"] += stand_down_actions(
                    trace, execution["plan_hash"], execution["actions"]
                )
                storm["standing_down"] = True
                self.store.update_execution(trace, execution, None, owner=self.owner)
                return True
            if not self._release_due(execution):
                return True
            changes = self._recheck(trace, document, option, execution["route"])
            if changes:
                reason = f"changed since approval: {'; '.join(changes)}"
                storm["returned"] = reason
                execution["status"] = "needs_attention"
                message = _returned(execution, reason)
                self._save(
                    trace,
                    execution,
                    "recovery-executor",
                    "storm_returned_to_occ",
                    message,
                    release=True,
                )
                self._catch_up(trace, execution)
                return False
            fresh = self._release(trace, execution, option)
            if fresh is None:
                return True
            document = fresh  # an update arrived during the recheck: the latest decides

    def _release(
        self, trace: str, execution: dict[str, Any], option: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Release the held part, under the case's lock, unless a weather update arrived since
        they were applied: then the case as it is now. Standby crews still only on alert are
        called first: the storm is confirmed, and the duty change then assigns them (after it
        they are no longer on standby, so Crew would refuse the call)."""
        storm = execution["storm"]
        released = copy.deepcopy({key: value for key, value in execution.items() if key != "lease"})
        released["storm"]["rechecked"] = True
        waiting = uncalled(released["actions"])
        if waiting:
            call = call_action(
                trace, str(released["plan_hash"]), released["actions"], waiting, WHEN_CONFIRMED
            )
            first = next(
                (
                    index
                    for index, action in enumerate(released["actions"])
                    if _stage(action) == WHEN_CONFIRMED and action["status"] == "not_started"
                ),
                len(released["actions"]),
            )
            released["actions"].insert(first, call)
        held = [
            action
            for action in released["actions"]
            if _stage(action) == WHEN_CONFIRMED and action["status"] == "not_started"
        ]
        message = f"Sources rechecked: sending the {len(held)} held actions."
        if review := review_passengers(option.get("validation") or {}):
            released["reviews"] = [
                {"kind": "passenger_services", "passengers": review, "status": "awaiting_expert"}
            ]
            message += f" {review} passengers outside rebooking policy await Passenger Services."
        owner = self.owner

        def decide(case: dict[str, Any]) -> CaseChange:
            stored = case.get("execution")
            lease = stored.get("lease") if isinstance(stored, dict) else None
            if not isinstance(lease, dict) or lease.get("owner") != owner:
                raise ExecutionLeaseLost("Another executor holds this case")
            seen = {item.get("event_id") for item in storm.get("updates") or []}
            arrived = (case.get("storm") or {}).get("updates") or []
            if any(item.get("event_id") not in seen for item in arrived):
                raise Unchanged(case)
            saved = {**released, "lease": lease}
            if isinstance(stored, dict) and stored.get("reviews"):
                saved["reviews"] = stored["reviews"]
            event = {**WEATHER, "event": "storm_released", "message": message}
            return CaseChange({"execution": saved}, (event,))

        try:
            self.store.mutate(trace, decide, statuses=STORM_OPEN)
        except Unchanged as fresh:
            return dict(fresh.result)
        lease = execution.get("lease")
        execution.clear()
        execution.update(released)
        if lease is not None:
            execution["lease"] = lease
        return None

    def _send(
        self, trace: str, execution: dict[str, Any], action: dict[str, Any]
    ) -> dict[str, Any]:
        """Send the action's calls in order; a confirmed call is never sent again. Returns the
        receipt that decides the action: the first call not confirmed, else the last."""
        records: list[dict[str, Any]] = action.setdefault("calls", [])
        known = {record["call_id"]: record for record in records}
        answer: dict[str, Any] = {"status": "failed", "message": "Nothing to send"}
        for call in dispatch.calls(action):
            record = known.get(call.call_id)
            if record is not None and record["status"] == "confirmed":
                answer = _receipt_of(record)
                continue
            if record is None:
                record = {
                    "call_id": call.call_id,
                    "recipient": call.recipient,
                    "played_by": call.played_by,
                    "method": call.method,
                    "path": call.path,
                    "says": call.says,
                    "sent": call.body,
                    "status": "sending",
                    "message": "",
                }
                records.append(record)
                if not call.resend_safe or _recorded_first(execution, action):
                    # Recorded before it goes, so a restart never sends a message twice, and a
                    # storm's own write that may have reached its recipient is known as such.
                    self.store.update_execution(trace, execution, None, owner=self.owner)
                answer = self._post(trace, call)
            elif record["status"] == "pending":
                answer = self._status(trace, call, record)
            elif call.resend_safe:
                answer = self._post(trace, call)
            else:
                answer = self._answer(call, "unknown", "Outcome not confirmed: ARROW restarted")
            deadline = self.monotonic() + self.pending_timeout_seconds
            while answer["status"] == "pending" and self.monotonic() < deadline:
                self.sleep(self.poll_seconds)
                answer = self._status(trace, call, {**record, **answer})
            record.update(answer)
            if answer["status"] != "confirmed":
                break
        return answer

    def _post(self, trace: str, call: Outbound) -> dict[str, Any]:
        """Send one call. A write the recipient de-duplicates is re-sent under the same key while
        its outcome is unclear; a message to an external party is sent again only while nothing
        has left ARROW, and never once it may have reached the recipient."""
        last: CloudDependencyError | None = None
        for attempt in range(self.unknown_retries + 1):
            if attempt:
                self.sleep(self.poll_seconds * attempt)
            try:
                reply = self.gateway.send(
                    call.system, call.method, call.path, call.headers, call.body, trace
                )
            except CloudDependencyError as error:
                if not _definitive(error):
                    logger.warning("Call %s outcome unclear: %s", call.call_id, error)
                    last = error
                    if call.resend_safe or _not_sent(error):
                        continue
                    break
                if error.status_code in _REFUSALS:
                    return self._answer(call, "refused", error.detail or str(error))
                detail = f": {error.detail}" if error.detail else ""
                return self._answer(
                    call,
                    "failed",
                    f"{call.recipient} refused the request (HTTP {error.status_code}){detail}",
                )
            return dispatch.receipt(call, reply)
        return self._answer(call, "unknown", f"Outcome not confirmed: {last}")

    def _status(self, trace: str, call: Outbound, record: dict[str, Any]) -> dict[str, Any]:
        """The slot coordinator's answer to a request it is still considering."""
        request_id = str(record.get("request_id") or "")
        try:
            reply = self.gateway.send(
                call.system,
                "GET",
                f"/slot-coordinator/requests/{quote(request_id)}",
                {"X-Request-Id": call.call_id},
                None,
                trace,
            )
        except CloudDependencyError as error:
            return {
                **_receipt_of(record),
                "status": "pending",
                "message": f"Answer not read: {error}",
            }
        return {"request_id": request_id, **dispatch.receipt(call, reply)}

    def _answer(self, call: Outbound, status: str, message: str) -> dict[str, Any]:
        return {
            "call_id": call.call_id,
            "status": status,
            "message": message,
            "at": self.now().isoformat(),
        }

    def _recheck(
        self, trace: str, document: dict[str, Any], option: dict[str, Any], route: str
    ) -> list[str]:
        originals = originals_of(document)
        changes: list[str] = []
        registration = str(
            ((document.get("disruption") or {}).get("scope") or {}).get("aircraft_reg")
            or (document.get("defect") or {}).get("aircraft_reg")
            or ""
        )
        # A storm's plan spans many aircraft and the case names none: read every flight's
        # current version from the network instead of one aircraft's rotation.
        read = (
            self.gateway.rotation(trace, registration)
            if registration
            else self.gateway.network(trace)
        )
        current = {flight["flight_no"]: flight for flight in read.get("flights", [])}
        for flight in option.get("flights") or []:
            before, now = originals.get(flight["flight_no"]), current.get(flight["flight_no"])
            if before is None or now is None:
                changes.append(f"{flight['flight_no']} no longer in the rotation")
                continue
            for key, label in (("std_local", "departure"), ("status", "status")):
                if before.get(key) != now.get(key):
                    changes.append(
                        f"{flight['flight_no']} {label} {before.get(key)} → {now.get(key)}"
                    )
        rebooking = (option.get("validation") or {}).get("rebooking") or {}
        try:
            case = case_ref(document)
        except ValueError:
            # A disruption the case never recorded cannot be read again, so nothing runs.
            return [
                *changes,
                "the case holds no recorded disruption, so its sources cannot be re-checked",
            ]
        if not changes and rebooking.get("rebooked_passengers"):
            try:
                fresh = self.gateway.rebooking(
                    case, system_option_id(option), option.get("additional_delay_minutes", 0)
                )
            except (CloudDependencyError, ValueError) as error:
                changes.append(f"booking: {_refusal(error)}")
            else:
                if fresh.get("plan_digest") != rebooking.get("plan_digest"):
                    changes.append("seats or bookings changed for the rebooking plan")
        crew = (option.get("validation") or {}).get("crew_plan") or {}
        # Every standby crew the plan calls, rechecked in one duty plan (one digest).
        called = standby_ids(option.get("levers"))
        if not changes and (crew.get("duties_changed") or called):
            try:
                duties = self.gateway.duty_plan(
                    case,
                    system_option_id(option),
                    option.get("additional_delay_minutes", 0),
                    called,
                )
            except (CloudDependencyError, ValueError) as error:
                changes.append(f"crew: {_refusal(error)}")
            else:
                if duties.get("plan_digest") != crew.get("plan_digest"):
                    changes.append("crew duties changed since validation")
                elif route == "auto" and called and "reserve_option_ids" in crew:
                    # Cases are assessed two at a time (D110): another may have called a standby
                    # crew of a base this plan calls one at. Left with none now, OCC decides.
                    last = last_standby_crews(standby_crews(option), duties.get("standby_left"))
                    if last != []:
                        changes.append(STANDBY_CHANGED)
        # Another case may have filled an airport hour since approval (review): Slots
        # counts the hours this plan moves flights into, as they are now.
        if not changes and _moves_flights(option, originals):
            try:
                slots = self.gateway.assessment(
                    "slots",
                    case,
                    system_option_id(option),
                    option.get("additional_delay_minutes", 0),
                )
            except (CloudDependencyError, ValueError) as error:
                changes.append(f"Lisbon capacity: {_refusal(error)}")
            else:
                if slots.get("capacity_fit") is not True:
                    changes.append(_capacity_change(slots))
        return changes

    def _finish(self, trace: str, execution: dict[str, Any]) -> dict[str, Any] | None:
        """Record where the plan stands once nothing more can be sent now. Returns the case as
        it is now when a weather update arrived just as the plan was to be held, else None."""
        actions = execution["actions"]
        failed = [a for a in actions if a["status"] in {"refused", "failed", "unknown"}]
        # An unknown outcome first, then any action other than the passenger notice (final
        # review): OCC must check those before acting.
        stopped = next(
            (a for a in failed if a["status"] == "unknown"),
            next((a for a in failed if a["kind"] != "notify_passengers"), None)
            or (failed[0] if failed else None),
        )
        if stopped is not None:
            execution["status"] = "needs_attention"
            partly = any(call.get("status") == "confirmed" for call in stopped.get("calls") or [])
            if stopped["status"] == "unknown":
                message = (
                    f"OCC attention: {stopped['label']} outcome unknown ({stopped['message']}). "
                    f"Dependent actions were not sent; check {stopped['label']} before acting."
                )
            elif partly:
                message = (
                    f"OCC attention: {stopped['label']} partly carried out "
                    f"({stopped['message']}). "
                    "Dependent actions were not sent; what was done stays in place."
                )
            elif stopped["kind"] == "notify_passengers" and not any(
                stopped["action_id"] in a["depends_on"] for a in actions
            ):
                what = stopped.get("summary") or stopped["label"]
                message = (
                    f"OCC attention: {what}: {stopped['status']} "
                    f"({stopped['message']}). "
                    "Nothing depended on it; the other actions were sent."
                )
            else:
                message = (
                    f"OCC attention: {stopped['label']} {stopped['status']} "
                    f"({stopped['message']}). "
                    "Dependent actions were not sent; completed actions stay in place."
                )
            also = [a for a in failed if a is not stopped]
            if also:
                message += (
                    " Also not done: "
                    + "; ".join(f"{a.get('summary') or a['label']} ({a['status']})" for a in also)
                    + "."
                )
            if any(_stage(a) == WHEN_CONFIRMED and a["status"] == "not_started" for a in actions):
                message += " The held actions were not sent."
            self._save(
                trace,
                execution,
                "recovery-executor",
                "execution_needs_attention",
                message,
                release=True,
            )
            self._catch_up(trace, execution)
            return None
        pending = [a for a in actions if a["status"] not in SETTLED]
        storm = execution.get("storm") if isinstance(execution.get("storm"), dict) else None
        if (
            storm is not None
            and pending
            and storm.get("state") == "awaiting"
            and all(_stage(a) == WHEN_CONFIRMED and a["status"] == "not_started" for a in pending)
        ):
            return self._hold(trace, execution)
        if pending:
            waiting = pending[0]
            first_wait = execution.get("status") != "pending_external"
            execution["status"] = "pending_external"
            self._save(
                trace,
                execution,
                "recovery-executor",
                "execution_waiting",
                f"Waiting for {waiting['label']} to confirm.",
                release=True,
                record_event=first_wait,
            )
        elif storm is not None and storm.get("state") == "dropped":
            execution["status"] = "stood_down"
            self._save(
                trace,
                execution,
                "recovery-executor",
                "storm_stood_down",
                _stood_down(execution),
                release=True,
            )
        else:
            execution["status"] = "completed"
            self._save(
                trace,
                execution,
                "recovery-executor",
                "execution_completed",
                f"All {len(actions)} actions confirmed by the airline systems.",
                release=True,
            )
        self._catch_up(trace, execution)
        return None

    def _catch_up(self, trace: str, execution: dict[str, Any]) -> None:
        """A storm plan that has just ended: weather updates the case recorded while it was being
        carried out are applied now (recorded, nothing reversed), never left unshown."""
        if execution.get("status") not in ENDED or not isinstance(execution.get("storm"), dict):
            return

        def decide(case: dict[str, Any]) -> CaseChange:
            current = case.get("execution")
            if not isinstance(current, dict) or current.get("status") not in ENDED:
                raise Unchanged(None)
            seen = {
                item.get("event_id") for item in (current.get("storm") or {}).get("updates") or []
            }
            late = [
                item
                for item in (case.get("storm") or {}).get("updates") or []
                if item.get("event_id") not in seen
            ]
            if not late or not isinstance(current.get("storm"), dict):
                raise Unchanged(None)
            ended = copy.deepcopy(current)
            events = []
            for update in late:
                event, message = settle(trace, ended, dict(update))
                events.append({**WEATHER, "event": event, "message": message})
            return CaseChange({"execution": ended}, tuple(events))

        try:
            self.store.mutate(trace, decide, statuses=STORM_OPEN)
        except Unchanged:
            pass

    def _hold(self, trace: str, execution: dict[str, Any]) -> dict[str, Any] | None:
        """Hold the plan until the storm is confirmed or dropped, releasing the case, unless a
        weather update arrived meanwhile: then the case as it is now, to apply it. Decided under
        the case's lock, so an update is never left unapplied on a held plan."""
        storm = execution["storm"]
        first = not storm.get("held_at")
        held = {key: value for key, value in execution.items() if key != "lease"}
        held["status"] = "held"
        held["storm"] = {**storm, "held_at": storm.get("held_at") or self.now().isoformat()}
        decide_by = storm.get("decide_by")
        message = "Now: done · when confirmed: held" + (
            f" · decide by {decide_by}" if decide_by else ""
        )
        owner = self.owner

        def decide(case: dict[str, Any]) -> CaseChange:
            stored = case.get("execution")
            lease = stored.get("lease") if isinstance(stored, dict) else None
            if not isinstance(lease, dict) or lease.get("owner") != owner:
                raise ExecutionLeaseLost("Another executor holds this case")
            seen = {item.get("event_id") for item in storm.get("updates") or []}
            arrived = (case.get("storm") or {}).get("updates") or []
            if any(item.get("event_id") not in seen for item in arrived):
                raise Unchanged(case)
            saved = dict(held)
            if isinstance(stored, dict) and stored.get("reviews"):
                saved["reviews"] = stored["reviews"]
            event = {**WEATHER, "event": "storm_held", "message": message}
            return CaseChange({"execution": saved}, (event,) if first else ())

        try:
            self.store.mutate(trace, decide, statuses=STORM_OPEN)
        except Unchanged as fresh:
            return dict(fresh.result)
        execution.clear()
        execution.update(held)
        return None

    def _stop(self, trace: str, execution: dict[str, Any], reason: str) -> None:
        execution["status"] = "needs_attention"
        self._save(
            trace,
            execution,
            "recovery-executor",
            "execution_stopped",
            f"{reason}. Nothing further was sent; Reset the day to assess it again.",
            release=True,
        )

    def _save(
        self,
        trace: str,
        execution: dict[str, Any],
        actor: str,
        event: str,
        message: str,
        *,
        action: dict[str, Any] | None = None,
        release: bool = False,
        record_event: bool = True,
    ) -> None:
        record: dict[str, Any] = {
            "actor": actor,
            "autonomy": LEVELS.get(actor, "system"),
            "event": event,
            "message": message,
        }
        if action is not None:
            record.update(action_id=action["action_id"], status=action["status"])
        self.store.update_execution(
            trace,
            execution,
            record if record_event else None,
            owner=self.owner,
            release=release,
        )
