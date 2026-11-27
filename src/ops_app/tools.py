"""The ARROW tools the orchestrator agent calls (spec D18): record the case, build plans, submit;
and the what-if plans the Case Assistant asks for (case chat spec §3).

Code, not the agent, computes each plan and decides its route. A plan within every domain's
limits is carried out at once, and once one is built the case takes no other plan; anything else
waits for OCC, and a blocked plan changes nothing.
A what-if plan never runs by itself. Results carry counts and reasons, never booking locators.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ops_app.actions import authorise_execution, system_option_id
from ops_app.engine import CloudDependencyError, typed_disruption
from ops_app.planner import (
    ALLOWED_AIRCRAFT_KINDS,
    DEFAULT_AIRCRAFT_KINDS,
    MAX_ADJUSTMENT_MINUTES,
    Levers,
    PlannerGateway,
    WhatIfLevers,
    airport_rules,
    build_plan,
    case_ref,
    compensation_exposure,
    disruption_facts,
)
from ops_app.playbook import DISRUPTIONS, for_case
from ops_app.policy import MAX_PLANS, Limits, lever_key
from ops_app.rules import RuleStore
from ops_app.store import AssessmentInactive, CaseChange, CaseStore, Unchanged

logger = logging.getLogger("ops_app")
ACTOR = {"actor": "recovery-orchestrator", "autonomy": "orchestrator"}
ASSISTANT = {"actor": "case-assistant", "autonomy": "L2"}
# Case chat spec §3: what-if plans the Case Assistant asks for, apart from the orchestrator's.
MAX_WHAT_IFS = 3
WHAT_IF_REASON = "asked in chat: OCC decides"
WAITING_ONLY = "Only a case waiting for OCC takes what-if plans"
AUTOMATIC_BUILT = "An automatic plan within every limit is already built; no other plan is needed"


class ToolRefused(ValueError):
    """The request cannot be carried out; the message tells the agent why."""


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    text: str = Field(min_length=1, max_length=300)

    @field_validator("agent", mode="before")
    @classmethod
    def _agent_key(cls, value: Any) -> Any:
        # The orchestrator may write an agent's name ("Detection"); keep its key ("detection").
        return "-".join(value.strip().lower().split()) if isinstance(value, str) else value


def _rank_key(plan: Mapping[str, Any]) -> tuple[bool, float, int, int]:
    cost = (plan.get("cost") or {}).get("total_eur")
    rebooking = (plan.get("validation") or {}).get("rebooking") or {}
    disrupted = rebooking.get("disrupted_passengers")
    return (
        (plan.get("policy") or {}).get("route") == "blocked",
        float(cost) if isinstance(cost, int | float) else math.inf,
        sum(1 for f in plan.get("flights") or [] if f.get("status") == "cancelled"),
        disrupted if isinstance(disrupted, int) else 0,
    )


def _summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What the orchestrator learns about a built plan: its route and reasons, and the figures
    it recommends by (passengers disrupted before cost), with who is owed compensation
    beside them, not the workings."""
    policy = plan.get("policy") or {}
    cost = plan.get("cost") or {}
    rebooking = (plan.get("validation") or {}).get("rebooking") or {}
    flights = [f for f in plan.get("flights") or [] if isinstance(f, Mapping)]
    return {
        "plan_id": plan["plan_id"],
        "route": policy.get("route"),
        "reasons": list(policy.get("reasons") or []),
        "passengers_disrupted": _disrupted(plan),
        "passengers_owed_compensation": cost.get("owed_compensation_passengers"),
        "passengers_to_passenger_services": rebooking.get("unplaced_passengers"),
        "flights_cancelled": [
            f.get("flight_no") for f in flights if f.get("status") == "cancelled"
        ],
        "cost": {
            key: cost.get(key)
            for key in (
                "total_eur",
                "airline_eur",
                "compensation_eur",
                "care_eur",
                "rebooking_eur",
            )
            if key in cost
        }
        or None,
        "rejected_levers": list(plan.get("rejected_levers") or []),
        # Delay messages as ARROW set them from the plan's flights, whatever was asked.
        "delay_messages_required": plan.get("delay_messages_required"),
        "departures_moved_over_15_min": plan.get("departures_moved_over_15_min"),
        # Named playbook actions that do not apply: left out of the plan, with why.
        "refused_actions": [
            f"{row.get('id')}: {row.get('reason')}"
            for row in plan.get("playbook") or []
            if isinstance(row, Mapping) and row.get("status") == "refused"
        ],
        # What the plan's playbook actions change for passengers and crews (Task 3).
        "outcomes": plan.get("outcomes"),
        "conflict": plan.get("conflict"),
    }


def _what_if_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What the Case Assistant learns about a plan, and whether it is a what-if."""
    return {**_summary(plan), "what_if": bool(plan.get("what_if"))}


def _not_waiting(case: Mapping[str, Any]) -> str | None:
    """Why a case takes no what-if plan now; None while it waits for OCC (case chat spec §3),
    including when it is flagged for manual intervention."""
    status = case.get("status")
    if status == "closed":
        reason = "it is closed"
    elif isinstance(case.get("execution"), Mapping):
        reason = "a plan is already being or has been carried out"
    elif status == "assessing":
        reason = "ARROW is still assessing it"
    elif status != "assessment_ready":
        reason = f"it is {status}"
    else:
        return None
    return f"{WAITING_ONLY}: {reason}"


class ArrowTools:
    def __init__(
        self,
        store: CaseStore,
        gateway: PlannerGateway,
        *,
        execute: Callable[[str], None],
        rules: RuleStore,
        record: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.execute = execute
        self.rules = rules
        self.record = record

    def record_disruption(self, trace_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Check and record the Detection agent's reading of a report (business UI and free-text
        spec §3.2): the typed tools record_aircraft_defect, record_flight_delay and
        record_weather_forecast each pass their own fields here."""
        if self.record is None:
            raise ToolRefused("Reading reports is not available here")
        try:
            return self.record(trace_id, values)
        except KeyError as error:
            raise ToolRefused(f"Case {trace_id} does not exist") from error
        except AssessmentInactive as error:
            raise ToolRefused("This case is no longer being assessed") from error
        except (TypeError, ValueError) as error:
            raise ToolRefused(f"Not recorded: {error}") from error

    def _assessing(self, trace: str) -> dict[str, Any]:
        try:
            document = self.store.get(trace)
        except KeyError as error:
            raise ToolRefused(f"Case {trace} does not exist") from error
        if document.get("status") != "assessing":
            raise ToolRefused("This case is no longer being assessed; no further plans")
        return document

    def _mutate(self, trace: str, update: Callable[[dict[str, Any]], CaseChange]) -> None:
        try:
            self.store.mutate(trace, update)
        except KeyError as error:
            raise ToolRefused(f"Case {trace} does not exist") from error
        except AssessmentInactive as error:
            raise ToolRefused("This case is no longer being assessed; no further plans") from error

    def _planning_inputs(
        self, trace: str, document: dict[str, Any]
    ) -> tuple[Limits, dict[str, Any]]:
        """The rule version in force, and the case with the airport rules to plan under."""
        # A plan is built only under the rule version in force, never under assumed limits.
        try:
            limits = self.rules.in_force()
        except Exception as error:
            logger.exception("Rules unavailable; no plan built for %s", trace)
            raise ToolRefused("Rules unavailable; no plan was built") from error
        if not document.get("airports"):
            # Slots could not be read when the disruption was recorded: read the airport rules
            # now and keep them, so the plan is executed under the rules it was checked against.
            document = {**document, "airports": airport_rules(self.gateway, trace)}
        return limits, document

    def _refused_option(self, trace: str, option_id: str) -> None:
        """Keep an option whose plan code refused: it counts as assessed (_unassessed)."""

        def update(case: dict[str, Any]) -> CaseChange:
            refused = list(case.get("options_refused") or [])
            if option_id in refused:
                raise Unchanged(None)
            return CaseChange({"options_refused": [*refused, option_id]}, ())

        try:
            self._mutate(trace, update)
        except (ToolRefused, Unchanged):
            pass

    def _offered_now(self, trace: str) -> list[Mapping[str, Any]] | None:
        """The options the operations control system offers the case now, for the plans OCC
        compares (_unassessed); None when a plan runs automatically, the case holds as many plans
        as it takes, or the read fails: then no plan is sent back for options."""
        try:
            document = self.store.get(trace)
        except KeyError:
            return None
        built = [plan for plan in document.get("options") or [] if not plan.get("what_if")]
        if len(built) >= MAX_PLANS or any(
            (plan.get("policy") or {}).get("route") == "auto" for plan in built
        ):
            return None
        try:
            read = self.gateway.options(case_ref(document), 0)
        except (CloudDependencyError, ValueError) as error:
            logger.warning(
                "Options for %s not read; no plan is sent back for them: %s", trace, error
            )
            return None
        return [option for option in read.get("options") or [] if isinstance(option, Mapping)]

    def record_case(
        self, trace_id: str, summary: str, findings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not 1 <= len(summary) <= 600:
            raise ToolRefused("The summary must be 1-600 characters")
        try:
            checked = [Finding(**finding).model_dump() for finding in findings[:10]]
        except (TypeError, ValidationError) as error:
            raise ToolRefused(f"Invalid findings: {error}") from error
        self._mutate(
            trace_id,
            lambda _: CaseChange(
                {"summary": summary, "findings": checked},
                ({**ACTOR, "event": "case_recorded", "message": summary},),
            ),
        )
        return {"recorded": True}

    def compensation_exposure(
        self, trace_id: str, option_id: str, additional_delay_minutes: int
    ) -> dict[str, Any]:
        """An option's EU261 exposure for the Passenger agent, read only: computed by the code
        that prices plans, so it equals the compensation of the plan built from it."""
        try:
            document = self.store.get(trace_id)
        except KeyError as error:
            raise ToolRefused(f"Case {trace_id} does not exist") from error
        if (document.get("disruption") or {}).get("type") not in DISRUPTIONS:
            raise ToolRefused("The disruption is not recorded yet; it has no options to price")
        # Checked here as for a what-if, so the agent reads the same refusal in Dev, where the
        # systems would answer a malformed delay with a validation error.
        if (
            isinstance(additional_delay_minutes, bool)
            or not isinstance(additional_delay_minutes, int)
            or not 0 <= additional_delay_minutes <= MAX_ADJUSTMENT_MINUTES
            or additional_delay_minutes % 5
        ):
            raise ToolRefused(
                f"No exposure: additional_delay_minutes must be 0-{MAX_ADJUSTMENT_MINUTES}"
                " in steps of 5"
            )
        try:
            return {
                "case_id": document.get("display_id"),
                **compensation_exposure(
                    self.gateway, document, option_id, additional_delay_minutes
                ),
            }
        except CloudDependencyError as error:
            # A system refusing the option or its delay (HTTP 409, e.g. weather takes no added
            # delay): the agent reads why.
            if error.status_code != 409 or not error.detail:
                raise
            raise ToolRefused(f"No exposure: {error.detail}") from error
        except (KeyError, TypeError, ValueError) as error:
            raise ToolRefused(f"No exposure: {error}") from error

    def get_playbook(self, trace_id: str) -> dict[str, Any]:
        """The recorded disruption and the playbook actions that apply to it (OCC playbook
        spec §2), read only: the orchestrator reads it right after the Detection agent recorded the
        disruption, so it never asks for the disruption again."""
        try:
            document = self.store.get(trace_id)
        except KeyError as error:
            raise ToolRefused(f"Case {trace_id} does not exist") from error
        disruption = document.get("disruption") or {}
        kind = disruption.get("type")
        if kind not in DISRUPTIONS:
            raise ToolRefused(
                "The disruption is not recorded yet; the playbook depends on its type"
            )
        knock_on = disruption.get("knock_on") or {}
        cause = str(disruption.get("cause"))
        event_id = disruption.get("event_id")
        defect = document.get("defect")
        try:
            # What every airline system is asked by: never the case, its trace or a record.
            facts = disruption_facts(disruption, defect if isinstance(defect, Mapping) else None)
        except ValueError as error:
            raise ToolRefused(str(error)) from error
        return {
            "disruption": {
                **typed_disruption(document),
                # Only an event its source system had recorded names that system's event id;
                # ARROW records nothing in the systems before a plan is carried out.
                **({"event_id": event_id} if event_id else {}),
                "scope": disruption.get("scope"),
                "knock_on": {
                    "flights_affected": knock_on.get("flights_affected"),
                    "max_minutes_late": knock_on.get("max_minutes_late"),
                    "aircraft_affected": len(knock_on.get("aircraft") or []),
                },
            },
            "facts": facts,
            "allowed_aircraft_kinds": sorted(
                ALLOWED_AIRCRAFT_KINDS.get(cause, DEFAULT_AIRCRAFT_KINDS)
            ),
            **for_case(kind),
        }

    def build_plan(self, trace_id: str, levers: dict[str, Any]) -> dict[str, Any]:
        document = self._assessing(trace_id)
        try:
            chosen = Levers(**levers)
        except (TypeError, ValidationError) as error:
            raise ToolRefused(f"Invalid levers: {error}") from error

        def same_levers(plans: list[dict[str, Any]]) -> dict[str, Any] | None:
            asked = lever_key(chosen.model_dump())
            return next((p for p in plans if lever_key(p.get("levers")) == asked), None)

        def refuse_if_full(plans: list[dict[str, Any]]) -> None:
            if len(plans) >= MAX_PLANS:
                raise ToolRefused(f"A case has at most {MAX_PLANS} plans; submit one of them")
            # Owner: once a plan runs within every limit, more plans are "useless
            # analysis"; code builds no other.
            if any((p.get("policy") or {}).get("route") == "auto" for p in plans):
                raise ToolRefused(AUTOMATIC_BUILT)

        plans = list(document.get("options") or [])
        if existing := same_levers(plans):
            return _summary(existing)
        refuse_if_full(plans)
        limits, document = self._planning_inputs(trace_id, document)
        # Planning calls the systems, so it runs before the case is locked; the write below
        # re-reads the plans so concurrent builds are all kept and the limit still holds.
        plan = build_plan(self.gateway, document, chosen, limits)
        # Owner ("why do I see the same plan twice?"): a plan from levers the systems did
        # not offer is no plan; the orchestrator reads why and builds without them.
        if plan["rejected_levers"]:
            self._refused_option(trace_id, chosen.aircraft_option_id)
            raise ToolRefused(f"Not built: {'; '.join(plan['rejected_levers'])}")
        summary = _summary(plan)

        def update(case: dict[str, Any]) -> CaseChange:
            current = list(case.get("options") or [])
            if existing := same_levers(current):
                raise Unchanged(_summary(existing))
            refuse_if_full(current)
            values: dict[str, Any] = {"options": sorted([*current, plan], key=_rank_key)}
            if not case.get("airports") and document["airports"]:
                values["airports"] = document["airports"]
            return CaseChange(
                values,
                (
                    {
                        **ACTOR,
                        "event": "plan_built",
                        "plan_id": plan["plan_id"],
                        "message": f"Plan {plan['plan_id']}: {summary['route']}; "
                        f"{'; '.join(summary['reasons'])}",
                    },
                ),
            )

        try:
            self._mutate(trace_id, update)
        except Unchanged as unchanged:
            result: dict[str, Any] = unchanged.result
            return result
        return summary

    def what_if(
        self,
        trace_id: str,
        levers: dict[str, Any],
        question: str,
        asked_by: Mapping[str, str],
        entry_id: str,
        assumption: str | None = None,
    ) -> dict[str, Any]:
        """A plan the Case Assistant asks for while the case waits for OCC: built, priced,
        checked and routed by code like any plan, but never run automatically and never the
        recommendation.

        ``question`` and ``asked_by`` are the chat turn's: the duty manager's own words and
        name. ``assumption`` is the Case Assistant's wording of the what-if, kept apart so no
        agent text is ever attributed to a person."""
        # From here ARROW's planning reads belong to this what-if (trail.build_trail).
        started_at = datetime.now(UTC).isoformat()
        try:
            document = self.store.get(trace_id)
        except KeyError as error:
            raise ToolRefused(f"Case {trace_id} does not exist") from error
        if reason := _not_waiting(document):
            raise ToolRefused(reason)
        try:
            chosen = WhatIfLevers(**levers)
        except (TypeError, ValidationError) as error:
            raise ToolRefused(f"Invalid levers: {error}") from error

        def same_levers(plans: list[dict[str, Any]]) -> dict[str, Any] | None:
            asked = {"extra_delay_minutes": 0, **lever_key(chosen.model_dump())}
            return next(
                (
                    p
                    for p in plans
                    if {"extra_delay_minutes": 0, **lever_key(p.get("levers"))} == asked
                ),
                None,
            )

        def refuse_if_full(plans: list[dict[str, Any]]) -> None:
            if sum(1 for p in plans if p.get("what_if")) >= MAX_WHAT_IFS:
                raise ToolRefused(
                    f"This case already has {MAX_WHAT_IFS} what-if plans, the most a case takes"
                )

        plans = list(document.get("options") or [])
        if existing := same_levers(plans):
            return _what_if_summary(existing)
        refuse_if_full(plans)
        limits, document = self._planning_inputs(trace_id, document)
        try:
            built = build_plan(
                self.gateway,
                document,
                chosen,
                limits,
                extra_delay_minutes=chosen.extra_delay_minutes,
            )
        except (CloudDependencyError, ValueError) as error:
            # A system refusing the what-if (HTTP 409 over the network, ValueError in process),
            # for example a storm plan asked to take added delay: the assistant reads why.
            if isinstance(error, CloudDependencyError):
                if error.status_code != 409 or not error.detail:
                    raise
                raise ToolRefused(f"Not built: {error.detail}") from error
            raise ToolRefused(f"Not built: {error}") from error
        if built["rejected_levers"]:
            raise ToolRefused(f"Not built: {'; '.join(built['rejected_levers'])}")
        policy = dict(built["policy"])
        if policy["route"] == "auto":
            # Code, not the assistant, keeps a what-if from running by itself: OCC decides it.
            policy.update(route="occ", reasons=[WHAT_IF_REASON])
        plan = {
            **built,
            "policy": policy,
            "requires_duty_manager": policy["route"] == "occ",
            "recommended": False,
            "what_if": {
                "question": question,
                "asked_by": {"oid": asked_by["oid"], "name": asked_by["name"]},
                **({"assumption": assumption} if assumption else {}),
                "started_at": started_at,
                # Set once built: ARROW's planning reads for it lie between the two.
                "asked_at": datetime.now(UTC).isoformat(),
                "entry_id": entry_id,
            },
        }
        summary = _what_if_summary(plan)

        def update(case: dict[str, Any]) -> CaseChange:
            if reason := _not_waiting(case):
                raise ToolRefused(reason)
            current = list(case.get("options") or [])
            if existing := same_levers(current):
                raise Unchanged(_what_if_summary(existing))
            refuse_if_full(current)
            # The chat names plans by number, so earlier answers stay true: what-ifs follow the
            # orchestrator's ranked plans in the order asked, never ranked into them.
            values: dict[str, Any] = {"options": [*current, plan]}
            if not case.get("airports") and document["airports"]:
                values["airports"] = document["airports"]
            return CaseChange(
                values,
                (
                    {
                        **ASSISTANT,
                        "event": "what_if_built",
                        "plan_id": plan["plan_id"],
                        # The log names the chat turn; the question stays in the conversation.
                        "entry_id": entry_id,
                        "message": f"What-if plan {plan['plan_id']}: "
                        f"{summary['route']}; {'; '.join(summary['reasons'])}",
                    },
                ),
            )

        try:
            self.store.mutate(trace_id, update, statuses=frozenset({"assessment_ready"}))
        except KeyError as error:
            raise ToolRefused(f"Case {trace_id} does not exist") from error
        except AssessmentInactive as error:
            raise ToolRefused(f"{WAITING_ONLY}: it is no longer waiting") from error
        except Unchanged as unchanged:
            result: dict[str, Any] = unchanged.result
            return result
        return summary

    def submit_plan(self, trace_id: str, plan_id: str, reason: str) -> dict[str, Any]:
        if not 1 <= len(reason) <= 300:
            raise ToolRefused("The reason must be 1-300 characters")
        # Read before the case is locked, as for a plan.
        offered = self._offered_now(trace_id)
        result: dict[str, Any] = {}

        def update(case: dict[str, Any]) -> CaseChange:
            plans = list(case.get("options") or [])
            plan = next((p for p in plans if p.get("plan_id") == plan_id), None)
            if plan is None:
                raise ToolRefused(f"Plan {plan_id} was not built for this case")
            submissions = int(case.get("plans_submitted") or 0) + 1
            if submissions > MAX_PLANS:
                raise ToolRefused(f"At most {MAX_PLANS} submissions per case; OCC decides")
            policy = plan.get("policy") or {}
            route, reasons = policy.get("route"), list(policy.get("reasons") or [])
            if route == "blocked":
                result.update(route=route, status="assessing", reasons=reasons)
                return CaseChange(
                    {"plans_submitted": submissions},
                    (
                        {
                            **ACTOR,
                            "event": "plan_refused",
                            "plan_id": plan_id,
                            "message": f"Plan {plan_id} is blocked: {'; '.join(reasons)}",
                        },
                    ),
                )
            stage = (
                "Within every domain's limits; ARROW carries it out and informs OCC."
                if route == "auto"
                else f"OCC decides: {'; '.join(reasons[1:] or reasons)}"
            )
            # Owner: OCC compares complete alternatives. While an option the systems offered,
            # of a kind the playbook allows, has no plan and was not refused, a plan that is not
            # automatic is sent back once per run; submitted again, it is recorded and OCC sees
            # the options not assessed.
            unassessed = _unassessed(case, plans, offered)
            gap = [
                {"option_id": item.get("option_id"), "title": item.get("title")}
                for item in unassessed
            ]
            named = ", ".join(str(item["option_id"]) for item in gap)
            if gap and not case.get("options_reconsidered"):
                result.update(
                    route=route,
                    status="reconsider",
                    unassessed_options=gap,
                    message="Not recorded. The operations control system offered options that"
                    f" have no plan yet: {named}. OCC compares complete alternatives: ask about"
                    " each, one at a time, build its plan, then submit one plan. Submitted again"
                    " before that, this plan is recorded and OCC sees the options not assessed.",
                )
                return CaseChange(
                    {"plans_submitted": submissions, "options_reconsidered": True},
                    (
                        {
                            **ACTOR,
                            "event": "plan_reconsider",
                            "plan_id": plan_id,
                            "message": f"Plan {plan_id} sent back: options not assessed yet:"
                            f" {named}",
                        },
                    ),
                )
            # The objectives put fewer passengers disrupted before lower cost (owner: a
            # recommendation must make sense end to end). A plan that disrupts more passengers
            # than another legal plan is sent back once with the comparison; submitted again,
            # the orchestrator's reason stands and OCC sees the trade-off.
            better = _fewer_disrupted(plan, plans)
            reconsidered = list(case.get("plans_reconsidered") or [])
            if better is not None and plan_id not in reconsidered:
                comparison = _comparison(plan, better)
                result.update(
                    route=route,
                    status="reconsider",
                    better_plan_id=better["plan_id"],
                    message=f"Not recorded. {comparison} ARROW's objectives put fewer passengers"
                    " who lose their flight or connection before lower cost. Submit plan"
                    f" {better['plan_id']}, or submit this plan again with a reason that says"
                    " why it is better despite that.",
                )
                return CaseChange(
                    {
                        "plans_submitted": submissions,
                        "plans_reconsidered": [*reconsidered, plan_id],
                    },
                    (
                        {
                            **ACTOR,
                            "event": "plan_reconsider",
                            "plan_id": plan_id,
                            "message": f"Plan {plan_id} sent back: {comparison}",
                        },
                    ),
                )
            values: dict[str, Any] = {
                "options": [
                    {
                        **p,
                        "recommended": p.get("plan_id") == plan_id,
                        "recommendation_reason": reason if p.get("plan_id") == plan_id else None,
                        # Nothing runs automatically once OCC decides: OCC chooses between every
                        # unblocked plan, including one within every limit.
                        **(
                            {
                                "requires_duty_manager": (p.get("policy") or {}).get("route")
                                in {"occ", "auto"}
                            }
                            if route == "occ"
                            else {}
                        ),
                    }
                    for p in plans
                ],
                "plans_submitted": submissions,
                "status": "assessment_ready",
                "stage": stage,
            }
            events: list[dict[str, Any]] = [
                {
                    **ACTOR,
                    "event": "plan_submitted",
                    "plan_id": plan_id,
                    "message": f"Recommended plan {plan_id} ({route}): {reason}",
                }
            ]
            if gap:
                count = f"{len(gap)} offered option{'s' if len(gap) > 1 else ''}"
                values["unassessed_options"] = gap
                values["stage"] = f"{stage}; {count} not assessed: {named}"
                events.append(
                    {
                        **ACTOR,
                        "event": "options_not_assessed",
                        "message": f"Submitted without a plan for {named}: OCC sees {count} not"
                        " assessed",
                    }
                )
            execution = None
            if route == "auto":
                # Authorised in the same transaction as the submission: never a recommended
                # automatic plan without its execution.
                execution = authorise_execution(
                    {**case, **values, "version": case["version"] + 1},
                    plan_id,
                    route="auto",
                    approved_by="policy",
                )
                events.append({**ACTOR, "event": "auto_execution_authorised", "message": stage})
            result.update(route=route, status="assessment_ready", reasons=reasons)
            if gap:
                result["unassessed_options"] = gap
            return CaseChange(values, tuple(events), execution)

        self._mutate(trace_id, update)
        if result["route"] == "auto":
            self.execute(trace_id)
        return result


def _unassessed(
    case: Mapping[str, Any],
    plans: list[dict[str, Any]],
    offered: list[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """The options the operations control system offers the case now (``offered``), of a kind
    the playbook allows, that have no plan and whose plan code did not refuse; none once a plan
    runs automatically, the case holds as many plans as it takes, or the options could not be
    read."""
    built = [plan for plan in plans if not plan.get("what_if")]
    if (
        offered is None
        or len(built) >= MAX_PLANS
        or any((plan.get("policy") or {}).get("route") == "auto" for plan in built)
    ):
        return []
    cause = str((case.get("disruption") or {}).get("cause"))
    allowed = ALLOWED_AIRCRAFT_KINDS.get(cause, DEFAULT_AIRCRAFT_KINDS)
    done = {system_option_id(plan) for plan in built} | set(case.get("options_refused") or [])
    missing = [
        option
        for option in offered
        if option.get("kind") in allowed and option.get("option_id") not in done
    ]
    return missing[: MAX_PLANS - len(built)]


def _disrupted(plan: Mapping[str, Any]) -> int | None:
    """Passengers a plan disrupts, as the plan cards show it."""
    rebooking = ((plan.get("validation") or {}).get("rebooking")) or {}
    for value in (rebooking.get("disrupted_passengers"), plan.get("misconnected_passengers")):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _fewer_disrupted(plan: Mapping[str, Any], plans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The legal plan that disrupts the fewest passengers, if it disrupts fewer than ``plan``."""
    mine = _disrupted(plan)
    if mine is None:
        return None
    others = [
        (count, other)
        for other in plans
        if other.get("plan_id") != plan.get("plan_id")
        and (other.get("policy") or {}).get("route") in {"auto", "occ"}
        and (count := _disrupted(other)) is not None
        and count < mine
    ]
    return min(others, key=lambda item: item[0])[1] if others else None


def _comparison(plan: Mapping[str, Any], better: Mapping[str, Any]) -> str:
    mine, theirs = _disrupted(plan), _disrupted(better)
    text = (
        f"With plan {better['plan_id']}, {theirs} passengers lose their flight or connection;"
        f" with this plan, {mine}"
    )
    cost, other_cost = ((p.get("cost") or {}).get("total_eur") for p in (plan, better))
    if isinstance(cost, int | float) and isinstance(other_cost, int | float):
        gap = abs(round(other_cost - cost))
        text += f"; it costs €{gap:,} {'more' if other_cost > cost else 'less'}"
    return text + "."


def refusal_text(error: Exception) -> str:
    """What an agent may read about a failed tool call."""
    if isinstance(error, ToolRefused | AssessmentInactive):
        return str(error)
    return "The tool failed; retry once, then stop and report"
