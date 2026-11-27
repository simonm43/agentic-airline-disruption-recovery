"""Foundry evaluation of ARROW's agents on real cases (owner: "add some Foundry eval as well
to evaluate the agents and model").

It judges what the agents actually did: each agent turn recorded in the case's trace (Foundry's own
tracing in Application Insights), not new runs. Nothing is sent to an agent and no case changes.

    python tools/ops_app/evaluate_agents.py run --since 2026-11-21T13:00Z --out <dir>
    python tools/ops_app/evaluate_agents.py results --out <dir>

``run`` finds the cases ARROW assessed in the window (its "Assess case" requests; a case's trace id
is its identity), creates a Foundry trace evaluation over those traces (stage 1, Tool Call Accuracy)
and writes run.json. ``results`` waits for it, then judges the same recorded turns joined into rows
of tool calls, results and answer (stage 2: Task Adherence, Intent Resolution, Tool Output
Utilization; these judges read the tool calls inside the response, which a trace turn keeps apart)
and writes results.json and summary.md: each agent's turns by evaluator. The judge is the project's
gpt-4.1 deployment; the domain agents run gpt-4.1-mini and the orchestrator gpt-4.1 (the same model
as the judge). Runs as the signed-in Azure CLI user.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

PROJECT = (
    "https://aif-airrec-chat-dev-4w7wuej5i2a5m.services.ai.azure.com/api/projects/"
    "proj-airrec-chat-dev"
)
APP_INSIGHTS = (
    "/subscriptions/00000000-0000-4000-8000-000000000013/resourceGroups/rg-airrec-dev-swc/"
    "providers/Microsoft.Insights/components/appi-airrec-dev-swc"
)
JUDGE = "gpt-4.1-eval"
# Per turn: what was asked, the answer, the tool calls made and the tools the agent had.
TURN = {
    "query": "{{item.query}}",
    "response": "{{item.response}}",
    "tool_calls": "{{item.tool_calls}}",
    "tool_definitions": "{{item.tool_definitions}}",
}
# Stage 1, on the traces: were the right tools called with the right arguments?
EVALUATORS = {
    "tool_call_accuracy": (
        "builtin.tool_call_accuracy",
        ("query", "response", "tool_calls", "tool_definitions"),
    ),
}
# Stage 2, on the same recorded turns joined into rows: these judges read the tool calls, their
# results and the answer as one response, which a trace turn keeps apart (tool_calls, response).
JOINED = {
    # Did the agent keep to its instructions: its role, its autonomy level, its output contract?
    "task_adherence": "builtin.task_adherence",
    # Did it answer what it was asked?
    "intent_resolution": "builtin.intent_resolution",
    # Does the answer use what the systems returned, without inventing or leaving out?
    "tool_output_utilization": "builtin.tool_output_utilization",
}


def case_traces(since: str, until: str | None) -> list[dict[str, str]]:
    """The cases ARROW assessed in the window, oldest first: by the workers since ARROW runs as
    three apps (three container apps spec), by the one app before."""
    end = f'datetime("{until}")' if until else "now()"
    query = (
        f'requests | where timestamp between (datetime("{since}") .. {end})'
        ' | where cloud_RoleName in ("arrow-workers", "arrow") and name == "Assess case"'
        " | summarize at = min(timestamp) by operation_Id | order by at asc"
    )
    token = AzureCliCredential().get_token("https://api.loganalytics.io/.default").token
    request = urllib.request.Request(
        f"https://api.loganalytics.io/v1{APP_INSIGHTS}/query",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        rows = json.load(response)["tables"][0]["rows"]
    return [
        {
            "trace_id": trace,
            "case": f"DSR-{at[:10].replace('-', '')}-{trace[:6].upper()}",
            "assessed_at": at,
        }
        for trace, at in rows
    ]


def criteria() -> list[dict[str, Any]]:
    return [
        {
            "type": "azure_ai_evaluator",
            "name": name,
            "evaluator_name": evaluator,
            "initialization_parameters": {"deployment_name": JUDGE},
            "data_mapping": {field: TURN[field] for field in fields},
        }
        for name, (evaluator, fields) in EVALUATORS.items()
    ]


def run(args: argparse.Namespace) -> None:
    out: Path = args.out
    if (out / "run.json").exists():
        raise SystemExit(f"{out / 'run.json'} exists; use a new directory, do not resubmit.")
    cases = case_traces(args.since, args.until)
    if not cases:
        raise SystemExit("No assessed case in the window.")
    client = AIProjectClient(PROJECT, AzureCliCredential()).get_openai_client()
    evaluation = client.evals.create(
        name=args.name,
        data_source_config={"type": "azure_ai_source", "scenario": "traces"},
        testing_criteria=criteria(),  # type: ignore[arg-type]
    )
    result = client.evals.runs.create(
        evaluation.id,
        name=args.name,
        extra_body={"evaluation_level": "turn"},
        data_source={  # type: ignore[arg-type]
            "type": "azure_ai_trace_data_source_preview",
            "trace_source": {
                "type": "trace_id_source",
                "trace_ids": [case["trace_id"] for case in cases],
            },
        },
    )
    out.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(UTC).isoformat(),
        "project": PROJECT,
        "eval_id": evaluation.id,
        "run_id": result.id,
        "name": args.name,
        "judge": JUDGE,
        "evaluation_level": "turn",
        "criteria": criteria(),
        "cases": cases,
    }
    (out / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("RUN", evaluation.id, result.id, len(cases), "cases")


def _text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_text(part) for part in value)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("content") or "")
    return str(value or "")


# A person's sign-in address, such as the duty manager's in the Case Assistant's brief, is not
# evidence: it stays out of the saved results, the joined run's rows included.
SIGN_IN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _wait(client: Any, eval_id: str, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    while True:
        status: dict[str, Any] = client.evals.runs.with_raw_response.retrieve(
            run_id, eval_id=eval_id
        ).http_response.json()
        if status["status"] in ("completed", "failed", "canceled"):
            break
        time.sleep(20)
    items = [
        item.model_dump(mode="json")
        for item in client.evals.runs.output_items.list(run_id, eval_id=eval_id, limit=100)
    ]
    return status, items


def _scores(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scores = {}
    for result in item.get("results") or []:
        error = (result.get("sample") or {}).get("error")
        scores[result["name"]] = {
            "passed": None if error else result.get("passed"),
            "score": result.get("score"),
            "reason": result.get("reason"),
            "error": error.get("message") if error else None,
        }
    return scores


def _row(source: dict[str, Any]) -> dict[str, Any] | None:
    """A recorded turn as one row: the question, then the tool calls, their results and the
    answer as one response; None when Foundry left the turn's content out (too large)."""
    if not source.get("response") or not source.get("query"):
        return None
    answer = {"role": "assistant", "content": [{"type": "text", "text": source["response"]}]}
    return {
        "turn": source.get("span_id"),
        "query": source["query"],
        "response": [*(source.get("tool_calls") or []), answer],
        "tool_definitions": source.get("tool_definitions") or [],
    }


def _joined(client: Any, record: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    evaluation = client.evals.create(
        name=record["name"] + "-joined",
        data_source_config={
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {
                    "turn": {"type": "string"},
                    "query": {"type": "string"},
                    "response": {"type": "array"},
                    "tool_definitions": {"type": "array"},
                },
                "required": ["query", "response"],
            },
            "include_sample_schema": False,
        },
        testing_criteria=[
            {
                "type": "azure_ai_evaluator",
                "name": name,
                "evaluator_name": evaluator,
                "initialization_parameters": {"deployment_name": JUDGE},
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{item.response}}",
                    "tool_definitions": "{{item.tool_definitions}}",
                },
            }
            for name, evaluator in JOINED.items()
        ],
    )
    result = client.evals.runs.create(
        evaluation.id,
        name=record["name"] + "-joined",
        extra_body={"evaluation_level": "turn"},
        data_source={
            "type": "jsonl",
            "source": {"type": "file_content", "content": [{"item": row} for row in rows]},
        },
    )
    record["joined"] = {"eval_id": evaluation.id, "run_id": result.id, "rows": len(rows)}


def results(args: argparse.Namespace) -> None:
    out: Path = args.out
    record = json.loads((out / "run.json").read_text(encoding="utf-8"))
    client = AIProjectClient(PROJECT, AzureCliCredential()).get_openai_client()
    status, items = _wait(client, record["eval_id"], record["run_id"])
    sources = [item.get("datasource_item") or {} for item in items]
    if "joined" not in record:
        rows = [row for row in map(_row, sources) if row is not None]
        _joined(client, record, rows)
        (out / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print("JOINED", record["joined"]["eval_id"], record["joined"]["run_id"])
    second, used = _wait(client, record["joined"]["eval_id"], record["joined"]["run_id"])
    by_turn = {(item.get("datasource_item") or {}).get("turn"): _scores(item) for item in used}
    cases = {case["trace_id"]: case["case"] for case in record["cases"]}
    names = [*EVALUATORS, *JOINED]
    not_scored = {"passed": None, "score": None, "reason": None, "error": "content omitted"}
    turns: list[dict[str, Any]] = []
    for item, source in zip(items, sources, strict=True):
        scores = _scores(item)
        joined = by_turn.get(source.get("span_id")) or {}
        scores.update({name: joined.get(name) or not_scored for name in JOINED})
        turns.append(
            {
                "agent": source.get("agent_name"),
                "case": cases.get(source.get("trace_id"), source.get("trace_id")),
                "turn": source.get("span_id"),
                "query": _text(source.get("query"))[-600:],
                "response": _text(source.get("response"))[:600],
                "tool_calls": len(source.get("tool_calls") or []) // 2,
                "usage": source.get("usage"),
                "omitted": source.get("_raisvc_content_omission"),
                "scores": scores,
            }
        )
    table: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    for turn in turns:
        for name, score in turn["scores"].items():
            cell = table[turn["agent"]][name]
            cell[0 if score["passed"] else 1 if score["passed"] is False else 2] += 1
    for run_status in (status, second):
        for key in ("created_by", "createdBy"):
            run_status.pop(key, None)  # a person's display name; not evidence
    report = {"runs": {"traces": status, "joined": second}, "turns": turns}
    saved = SIGN_IN.sub("[sign-in address removed]", json.dumps(report, indent=2))
    (out / "results.json").write_text(saved, encoding="utf-8")
    lines = [
        f"Runs {record['run_id']} ({status['status']}) and {record['joined']['run_id']} "
        f"({second['status']}), judge {record['judge']}, {len(record['cases'])} cases, "
        f"{len(turns)} agent turns.",
        "",
        "| Agent | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for agent in sorted(table, key=str):
        cells = [
            "{}/{} pass{}".format(
                table[agent][name][0],
                sum(table[agent][name][:2]),
                f", {table[agent][name][2]} not scored" if table[agent][name][2] else "",
            )
            for name in names
        ]
        lines.append(f"| {agent} | " + " | ".join(cells) + " |")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    for run_status in (status, second):
        if run_status.get("report_url"):
            print("Foundry:", run_status["report_url"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run")
    start.add_argument("--since", required=True, help="UTC, e.g. 2026-11-21T13:00Z")
    start.add_argument("--until")
    start.add_argument(
        "--name", default="arrow-agents-" + datetime.now(UTC).strftime("%Y%m%d-%H%M")
    )
    start.add_argument("--out", type=Path, required=True)
    read = commands.add_parser("results")
    read.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    {"run": run, "results": results}[args.command](args)


if __name__ == "__main__":
    main()
