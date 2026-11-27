"""The figures of one load-test run: from ARROW's own records of each case
(tools/load/report.py, run inside ARROW), the send log and the model metrics of the environment's
Foundry account.

    tools\\systems\\exec_py.ps1 -Environment <env> -App ca-airrec-ops-<env> `
        -Script tools\\load\\report.py -Arg <run> > .local\\load\\<run>.rows.txt
    python -m tools.load.summarize <env> <run> [--out <file>]

The send log (.local/load/<run>.jsonl) says which phase each report belongs to.

Per phase: reports sent, cases opened, how they ended, queue wait (opened to the first assessment
step), assessment time (first step to the decision), end to end (enqueued on the topic to the
decision), the largest backlog of cases waiting for a worker, throughput, retries and the cases
whose 15-minute window ended. Over the run: model tokens, HTTP 429 answers and the token cost at
the regional list prices below. The read is read-only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tools.env.environment import Environment, load
from tools.env.rest import Rest

LOGS = Path(__file__).resolve().parents[2] / ".local" / "load"
METRICS_API = "2023-10-01"
# USD per million tokens, Azure Retail Prices API, Sweden Central, regional (Standard) deployment.
PRICES = {
    "gpt-4.1": {"input": 2.42, "cached": 0.605, "output": 9.68},
    "gpt-4.1-mini": {"input": 0.484, "cached": 0.121, "output": 1.936},
}
EXPIRED = "15-minute window ended"
FIELDS = (
    "id",
    "event_id",
    "kind",
    "event_time",
    "enqueued",
    "opened",
    "started",
    "decided",
    "attempts",
    "retries",
    "status",
    "stage",
)


def rows(text: str) -> list[dict[str, Any]]:
    """The cases of report.py's output: one ROW line each, then ROWS with their count."""
    found = [
        dict(zip(FIELDS, json.loads(line[4:]), strict=True))
        for line in text.splitlines()
        if line.startswith("ROW ")
    ]
    total = next((line for line in text.splitlines() if line.startswith("ROWS ")), None)
    if total is None or int(total.split()[1]) != len(found):
        raise SystemExit(f"report output incomplete: {len(found)} rows, footer {total!r}")
    return found


def _at(value: str | None) -> datetime | None:
    """A moment as UTC: the decision log's times are SQL datetime2 in UTC without a zone."""
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _seconds(start: str | None, end: str | None) -> float | None:
    begin, finish = _at(start), _at(end)
    return (finish - begin).total_seconds() if begin and finish else None


def percentiles(values: Iterable[float | None]) -> dict[str, float] | None:
    """Nearest-rank p50, p90, p95 and the maximum, in seconds."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None

    def rank(p: float) -> float:
        return round(ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)], 1)

    return {"p50": rank(50), "p90": rank(90), "p95": rank(95), "max": round(ordered[-1], 1)}


def backlog(cases: list[dict[str, Any]]) -> int:
    """The most cases opened and not yet started by a worker at any moment."""
    marks = []
    for case in cases:
        opened = _at(case["opened"])
        if opened is None:
            continue
        marks.append((opened, 1))
        ended = _at(case["started"]) or _at(case["decided"])
        if ended is not None:
            marks.append((ended, -1))
    depth = peak = 0
    for _, change in sorted(marks, key=lambda mark: (mark[0], mark[1])):
        depth += change
        peak = max(peak, depth)
    return peak


def outcome(case: dict[str, Any]) -> str:
    if EXPIRED in str(case["stage"]):
        return "window_ended"
    if case["status"] == "assessment_ready":
        return "plans_ready"
    if case["status"] == "assessing":
        return "still_assessing"
    return str(case["status"])


def phase_figures(cases: list[dict[str, Any]], sent: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [moment for c in cases if (moment := _at(c["decided"])) is not None]
    opened = [moment for c in cases if (moment := _at(c["opened"])) is not None]
    hours = (max(decided) - min(opened)).total_seconds() / 3600 if decided and opened else 0.0
    failed = [c for c in cases if c["status"] == "failed" and EXPIRED not in str(c["stage"])]
    return {
        "reports_sent": sum(1 for item in sent if item.get("sent")),
        "send_failures": sum(1 for item in sent if not item.get("sent")),
        "cases_opened": len(cases),
        "outcomes": dict(Counter(outcome(c) for c in cases)),
        "queue_wait_seconds": percentiles(_seconds(c["opened"], c["started"]) for c in cases),
        "assessment_seconds": percentiles(_seconds(c["started"], c["decided"]) for c in cases),
        "end_to_end_seconds": percentiles(
            _seconds(c["enqueued"] or c["event_time"], c["decided"]) for c in cases
        ),
        "largest_backlog": backlog(cases),
        "decisions_per_hour": round(len(decided) / hours, 1) if hours else None,
        "cases_retried": sum(1 for c in cases if (c["retries"] or 0) > 0),
        "retries": sum(c["retries"] or 0 for c in cases),
        "failures_by_stage": dict(Counter(str(c["stage"])[:70] for c in failed)),
    }


def _metric(
    rest: Rest, resource: str, names: str, start: datetime, end: datetime, dimension: str
) -> dict[tuple[str, str], float]:
    """Totals per metric and dimension value over the span."""
    span = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
    every = quote(f"{dimension} eq '*'", safe="'")
    query = f"metricnames={names}&timespan={span}&interval=PT1H&aggregation=Total&$filter={every}"
    body = rest.arm(
        "GET",
        f"{resource}/providers/Microsoft.Insights/metrics?{query}",
        api_version=METRICS_API,
    ).body
    totals: dict[tuple[str, str], float] = {}
    for metric in body.get("value", []):
        for series in metric.get("timeseries", []):
            key = next((m["value"] for m in series.get("metadatavalues", [])), "")
            total = sum(point.get("total") or 0 for point in series.get("data", []))
            totals[(metric["name"]["value"], key)] = total
    return totals


def tokens(rest: Rest, env: Environment, start: datetime, end: datetime) -> dict[str, Any]:
    names = env.names()
    account = (
        f"{env.group_id}/providers/Microsoft.CognitiveServices/accounts/{names['foundryAccount']}"
    )
    used = _metric(
        rest,
        account,
        "InputTokens,OutputTokens,cacheReadInputTokens",
        start,
        end,
        "ModelDeploymentName",
    )
    requests = _metric(rest, account, "ModelRequests", start, end, "StatusCode")
    models = {model.name: model.model for model in env.models}
    per_deployment: dict[str, Any] = {}
    total = 0.0
    for deployment in sorted({key for _, key in used}):
        price = PRICES.get(models.get(deployment, deployment))
        given = used.get(("InputTokens", deployment), 0.0)
        cached = used.get(("cacheReadInputTokens", deployment), 0.0)
        output = used.get(("OutputTokens", deployment), 0.0)
        # InputTokens includes the cached part, which is billed at the cached price.
        cost = (
            (
                (given - cached) * price["input"]
                + cached * price["cached"]
                + output * price["output"]
            )
            / 1e6
            if price
            else 0.0
        )
        total += cost
        per_deployment[deployment] = {
            "input_tokens": int(given),
            "cached_input_tokens": int(cached),
            "output_tokens": int(output),
            "cost_usd": round(cost, 2) if price else None,
        }
    return {
        "deployments": per_deployment,
        "model_requests_by_status": {key: int(v) for (_, key), v in sorted(requests.items())},
        "token_cost_usd": round(total, 2),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.load.summarize", description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("run")
    parser.add_argument("--rows", help="exec output with the ROWS line")
    parser.add_argument("--out", help="write the figures here as JSON")
    arguments = parser.parse_args(argv)
    env = load(arguments.environment)
    run = arguments.run
    cases = rows(Path(arguments.rows or LOGS / f"{run}.rows.txt").read_text(encoding="utf-8"))
    sent = [
        json.loads(line)
        for line in (LOGS / f"{run}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    phase_of = {item["id"]: item["phase"] for item in sent}
    phases = {
        phase: phase_figures(
            [c for c in cases if phase_of.get(c["event_id"]) == phase],
            [item for item in sent if item["phase"] == phase],
        )
        for phase in sorted(set(phase_of.values()))
    }
    times = [_at(item["sent_at"]) for item in sent] + [_at(c["decided"]) for c in cases]
    known = [moment for moment in times if moment is not None]
    start = min(known) - timedelta(minutes=5)
    end = max(known) + timedelta(minutes=5)
    used = tokens(Rest(env.tenant_id), env, start, end)
    decided = sum(1 for c in cases if c["decided"])
    figures = {
        "run": run,
        "environment": env.name,
        "window_utc": [start.isoformat(), end.isoformat()],
        "phases": phases,
        "tokens": used,
        "token_cost_per_decided_case_usd": round(used["token_cost_usd"] / decided, 3)
        if decided
        else None,
    }
    text = json.dumps(figures, indent=2)
    print(text)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
