"""Check the Test Bench's five cases step by step from their saved trails, after guide step 23.

    .\\tools\\systems\\exec_py.ps1 -Environment <env> -App ca-airrec-ops-<env> `
        -Script tools\\testbench\\case_trails.py -Arg <id>_<id>_... > .local\\e2e\\<env>.dump.txt
    python -m tools.testbench.check_cases <env> .local\\e2e\\<env>.dump.txt [--out <file>]

Read-only. Each case's decision trail is rebuilt with ARROW's own code (ops_app.trail), with the
agents' identities as Foundry holds them, and checked against the Test Bench preset it came from:
- the outcome the Test Bench shows (testbench.service.outcome) is the preset's expected one, and
  an executed plan has every action confirmed by its recipient;
- no refusal (a plan_refused event or a non-empty refused_actions) and no tool error (a trail step
  or an agent's ARROW tool call that failed), and no failed airline-system call; a plan ARROW
  sent back once (plan_reconsider) is expected and reported, as are the offered options OCC sees
  as not assessed;
- one plan_built per stored plan, each from one build_plan call, and no two plans with one title;
- no ARROW case id or trace in an airline-system call's endpoint or request;
- options one at a time (the agents asked about one option, then that plan built) and no plan
  built after an automatic one;
- the Detection agent recorded the report on ARROW's case with a typed tool, and no airline
  system issued an event id for it (owner: the systems are written only when a plan runs);
- every audited write by the workers' identity, the only writer (three container apps);
- no airline-system write outside an executed plan's own calls, and an executed plan's first
  action records the reported delay or defect, confirmed with the event id its system issued
  (a weather forecast is recorded in no airline system);
- the Passenger agent's EU261 exposure for every option built.
Per case it also gives who was asked about each option and the times from the report: to the
first assessment step (queued), to the orchestrator's last step (the trail's own figure) and, for
an executed plan, to its last action confirmed.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import zlib
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ops_app.trail import build_trail
from testbench.presets import PRESETS
from testbench.service import outcome
from tools.env.environment import load
from tools.env.rest import Rest
from tools.env.wiring import AGENTS, Live

OPTION = re.compile(r"\bThe ([a-z0-9-]+) option\b")
PLAN = re.compile(r"^Plan ([0-9a-f]+):")
EVENT_ID = re.compile(r"^EV-[0-9A-F]{6}$")
WRITES = ("POST ", "PATCH ", "PUT ", "DELETE ")
RECORD = "record_disruption"


def cases_of(text: str) -> list[dict[str, Any]]:
    """The cases case_trails.py printed, refused unless every line arrived."""
    lines = [line.strip() for line in text.splitlines()]
    joined = "".join(line[2:] for line in lines if line.startswith("D "))
    footer = next((line.split() for line in lines if line.startswith("N ")), None)
    if footer is None or int(footer[2]) != len(joined):
        raise SystemExit(f"dump incomplete: {len(joined)} characters, footer {footer}")
    cases: list[dict[str, Any]] = json.loads(zlib.decompress(base64.b64decode(joined)))
    if len(cases) != int(footer[1]):
        raise SystemExit(f"dump incomplete: {len(cases)} cases, footer {footer}")
    return cases


def _at(value: Any) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _json(value: Any) -> Any:
    """A tool's output: the first JSON value of its text (Foundry may append a second)."""
    if not isinstance(value, str):
        return value
    try:
        return json.JSONDecoder().raw_decode(value.strip())[0]
    except ValueError:
        return value


def _args(item: Mapping[str, Any]) -> dict[str, Any]:
    value = _json(item.get("arguments"))
    return dict(value) if isinstance(value, Mapping) else {}


def _request_id(call: Mapping[str, Any]) -> str:
    """The X-Request-Id a system audited a call under; "" when it recorded none."""
    request = _json(call.get("request"))
    return str(request.get("request_id") or "") if isinstance(request, Mapping) else ""


def preset_of(document: Mapping[str, Any]) -> Any:
    """The Test Bench preset whose report opened the case."""
    day = date.fromisoformat(str(document.get("operating_date")))
    text = str((document.get("disruption") or {}).get("message") or "").strip()
    for preset in PRESETS:
        if str(preset.data_on(day).get("text") or "").strip() == text:
            return preset
    return None


def one_at_a_time(items: list[Mapping[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    """Departures from "ask about one option, build it, then the next", and who was asked about
    each option built."""
    problems: list[str] = []
    asked: dict[str, list[str]] = {}
    current: str | None = None
    pending: list[str] = []
    automatic = False
    for item in items:
        kind = str(item.get("type") or "")
        if "a2a" in kind and kind.endswith("_call"):
            agent = str(item.get("name") or "").removeprefix("a2a-")
            if agent == "detection":
                continue
            pending.append(agent)
            found = OPTION.search(json.dumps(_args(item)))
            if found:
                if current is not None and found.group(1) != current:
                    problems.append(f"asked about {found.group(1)} before building {current}")
                current = found.group(1)
        elif kind == "mcp_call" and item.get("name") == "build_plan":
            levers = _args(item).get("levers") or {}
            option = str(levers.get("aircraft_option_id") or "")
            if automatic:
                problems.append(f"built {option} after an automatic plan")
            if current is not None and option != current:
                problems.append(f"built {option} after asking about {current}")
            asked[option] = pending
            pending, current = [], None
            out = _json(item.get("output"))
            automatic = automatic or (isinstance(out, Mapping) and out.get("route") == "auto")
    return problems, asked


def check(
    case: Mapping[str, Any], prompt: Any, arrow_oids: Collection[str], *, writer_oid: str
) -> dict[str, Any]:
    """``writer_oid``: the workers' object id, the only identity that may write."""
    doc = case["document"]
    calls = case["calls"]
    trace, shown = str(doc["trace_id"]), str(doc["display_id"])
    events = [e for e in doc.get("events") or [] if isinstance(e, Mapping)]
    items = [i for i in (doc.get("_trail") or {}).get("items") or [] if isinstance(i, Mapping)]
    options = [o for o in doc.get("options") or [] if isinstance(o, Mapping)]
    trail = build_trail(doc, calls, prompt, arrow_oids)
    preset = preset_of(doc)
    created = _at(doc.get("created_at"))
    shows = outcome(dict(doc), created or datetime.now(UTC), datetime.now(UTC))

    builds = [i for i in items if i.get("type") == "mcp_call" and i.get("name") == "build_plan"]
    outputs = [_json(b.get("output")) for b in builds]
    built = [
        m.group(1)
        for e in events
        if e.get("event") == "plan_built"
        if (m := PLAN.match(str(e.get("message") or "")))
    ]
    plan_ids = [str(o.get("plan_id")) for o in options]
    titles = [str(o.get("title")) for o in options]
    screen_titles = [str(p.get("title")) for p in trail["plans"]]
    refusals = [str(e.get("message"))[:160] for e in events if e.get("event") == "plan_refused"]
    refusals += [
        f"{out.get('plan_id')}: refused_actions {out.get('refused_actions')}"
        for out in outputs
        if isinstance(out, Mapping) and out.get("refused_actions")
    ]
    tool_errors = [
        f"{s.get('title')}: {str(s.get('result') or s.get('detail'))[:140]}"
        for s in trail["orchestrator"]["steps"]
        if not s.get("ok")
    ]
    tool_errors += [
        f"{e.get('actor')} {e.get('tool')}: {str(e.get('message'))[:140]}"
        for e in events
        if e.get("event") == "agent_tool_call" and e.get("ok") is False
    ]
    failed_calls = [
        f"{c['system']} {c['operation']} {c['outcome']}: {str(c.get('detail'))[:120]}"
        for c in calls
        if c.get("outcome") != "ok"
    ]
    leaks = [
        f"{c['system']} {c['operation']} {c.get('endpoint')}"
        for c in calls
        if shown in f"{c.get('endpoint')} {c.get('request')}"
        or trace in f"{c.get('endpoint')} {c.get('request')}".lower()
    ]
    order, asked = one_at_a_time(items)
    recorded = [
        e
        for e in events
        if e.get("actor") == "detection"
        and e.get("event") == "agent_tool_call"
        and str(e.get("tool") or "").startswith("record_")
    ]
    disruption = doc.get("disruption") or {}
    # Recorded on ARROW's case only: no airline system issued an event id for it.
    detection_ok = (
        len(recorded) == 1
        and recorded[0].get("ok") is True
        and not (recorded[0].get("response") or {}).get("event_id")
        and not disruption.get("event_id")
        and any(e.get("event") == "disruption_recorded" for e in events)
    )
    execution = doc.get("execution") or {}
    actions = [a for a in execution.get("actions") or [] if isinstance(a, Mapping)]
    confirmed = sum(1 for a in actions if a.get("status") == "confirmed")
    # Every write a system audited is one of the executed plan's own calls.
    planned = {
        str(call.get(key))
        for action in actions
        for call in action.get("calls") or []
        if isinstance(call, Mapping)
        for key in ("call_id", "request_id")
        if call.get(key)
    }
    unplanned = [
        f"{c['system']} {c['operation']} {c.get('endpoint')}"
        for c in calls
        if str(c.get("endpoint") or "").startswith(WRITES) and _request_id(c) not in planned
    ]
    # Three container apps: every audited write is the workers', the only writer.
    not_by_workers = [
        f"{c['system']} {c['operation']} {c.get('endpoint')} by {c.get('caller_oid')}"
        for c in calls
        if str(c.get("endpoint") or "").startswith(WRITES)
        and str(c.get("caller_oid") or "").lower() != writer_oid.lower()
    ]
    # An executed plan records the reported delay or defect first; a forecast nowhere.
    record = next((a for a in actions if a.get("kind") == RECORD), None)
    receipts = [c for c in (record or {}).get("calls") or [] if isinstance(c, Mapping)]
    issued = next((str(c.get("event_id")) for c in receipts if c.get("event_id")), None)
    if not actions or disruption.get("type") == "weather":
        record_ok = record is None
    else:
        record_ok = (
            record is actions[0]
            and record.get("status") == "confirmed"
            and bool(issued and EVENT_ID.match(issued))
        )
    exposure = sorted(
        str((e.get("request") or {}).get("option_id"))
        for e in events
        if e.get("actor") == "passenger" and e.get("tool") == "get_compensation_exposure"
    )
    options_built = sorted(str(o.get("aircraft_option_id") or o.get("kind")) for o in options)
    first_step = next(
        (_at(e.get("at")) for e in events if e.get("event") == "assessment_progress"), None
    )
    reported = _at((doc.get("source") or {}).get("event_time"))
    completed = next(
        (_at(e.get("at")) for e in events if e.get("event") == "execution_completed"), None
    )
    recommended = next((o for o in options if o.get("recommended")), {})
    found = {
        "case": preset.case if preset else None,
        "label": preset.label if preset else None,
        "id": shown,
        "expected": preset.expected if preset else None,
        "outcome": shows,
        "actions": f"{confirmed} of {len(actions)} confirmed" if actions else None,
        "plans": [
            {
                "title": o.get("title"),
                "route": next(
                    (
                        out.get("route")
                        for out in outputs
                        if isinstance(out, Mapping) and out.get("plan_id") == o.get("plan_id")
                    ),
                    None,
                ),
                "cost_eur": (o.get("cost") or {}).get("total_eur"),
                "recommended": bool(o.get("recommended")),
            }
            for o in options
        ],
        "recommended_cost_eur": (recommended.get("cost") or {}).get("total_eur"),
        "asked": asked,
        "refusals": refusals,
        # Expected, reported only: a plan ARROW sent back once (offered options not assessed
        # yet, or another legal plan disrupting fewer passengers), and any options OCC sees as
        # not assessed because the orchestrator submitted again before building them.
        "reconsidered": [
            str(e.get("message"))[:160] for e in events if e.get("event") == "plan_reconsider"
        ],
        "unassessed_options": [
            str(o.get("option_id"))
            for o in doc.get("unassessed_options") or []
            if isinstance(o, Mapping)
        ],
        "tool_errors": tool_errors,
        "failed_system_calls": failed_calls,
        "system_calls": len(calls),
        "case_id_or_trace_sent": leaks,
        "plan_built_per_plan": sorted(built) == sorted(plan_ids) and len(builds) == len(options),
        "titles_unique": len(set(titles)) == len(titles)
        and len(set(screen_titles)) == len(screen_titles),
        "one_option_at_a_time": order,
        "detection_recorded": {
            "tool": recorded[0].get("tool") if recorded else None,
            "event_id": disruption.get("event_id"),
            "ok": detection_ok,
        },
        "writes_outside_plan": unplanned,
        "writes_not_by_workers": not_by_workers,
        "disruption_record": {
            "action": record.get("summary") if record else None,
            "status": record.get("status") if record else None,
            "event_id": issued,
            "ok": record_ok,
        },
        "passenger_exposure": {"options_built": options_built, "exposure_for": exposure},
        "trail_steps": len(trail["orchestrator"]["steps"]),
        "queued_seconds": round((first_step - reported).total_seconds(), 1)
        if first_step and reported
        else None,
        # The trail's own figure: the report to the orchestrator's last step.
        "seconds_to_decision": trail["stats"].get("seconds"),
        "seconds_to_actions_confirmed": round((completed - reported).total_seconds(), 1)
        if completed and reported
        else None,
    }
    found["passed"] = bool(
        preset
        and shows == preset.expected
        and (not actions or confirmed == len(actions))
        and not (
            refusals or tool_errors or failed_calls or leaks or order or unplanned or not_by_workers
        )
        and found["plan_built_per_plan"]
        and found["titles_unique"]
        and detection_ok
        and record_ok
        and exposure == options_built
    )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.testbench.check_cases", description=__doc__
    )
    parser.add_argument("environment")
    parser.add_argument("dump", help="case_trails.py's output")
    parser.add_argument("--out", help="write the findings here as JSON")
    arguments = parser.parse_args(argv)
    env = load(arguments.environment)
    cases = cases_of(Path(arguments.dump).read_text(encoding="utf-8", errors="replace"))
    live = Live(Rest(env.tenant_id), env)
    agents = {name: live.agent(name) for name in AGENTS}

    def prompt(name: str) -> dict[str, Any]:
        agent = agents.get(name) or {}
        identity = {k: agent.get(k) for k in ("principal_id", "client_id")}
        return {"version": agent.get("version"), "identity": {**identity, "name": name}}

    # Every ARROW app's identity: the workers' (the one writer) and, once ARROW runs as three
    # apps, the operator app's and the tools app's (three container apps spec).
    arrow_oids = frozenset(
        found.principal_id.lower()
        for found in (live.optional_identity(app) for app in ("ops", "operator", "tools"))
        if found is not None
    )
    # The workers' identity (id-airrec-ops), the only one that may write.
    writer = live.identity("ops").principal_id
    findings = sorted(
        (check(case, prompt, arrow_oids, writer_oid=writer) for case in cases),
        key=lambda f: f["case"] or 9,
    )
    for f in findings:
        confirmed = f["seconds_to_actions_confirmed"]
        gap = ", ".join(f["unassessed_options"])
        print(
            f"Case {f['case']} {f['id']}: {f['outcome']} (expected {f['expected']})"
            f"{'; ' + f['actions'] if f['actions'] else ''}; {len(f['plans'])} plan(s);"
            f"{' sent back once;' if f['reconsidered'] else ''}"
            f"{f' not assessed: {gap};' if gap else ''}"
            f" queued {f['queued_seconds']} s, decided after {f['seconds_to_decision']} s"
            f"{f', confirmed after {confirmed} s' if confirmed else ''};"
            f" {'PASS' if f['passed'] else 'CHECK'}"
        )
    summary = {
        "environment": env.name,
        "versions": {name: agent.get("version") for name, agent in agents.items()},
        "cases": findings,
        "all_passed": all(f["passed"] for f in findings) and len(findings) == 5,
        "counts": dict(
            Counter(
                key
                for f in findings
                for key in ("refusals", "tool_errors", "failed_system_calls")
                for _ in f[key]
            )
        ),
    }
    if arguments.out:
        Path(arguments.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
