"""The subscription-wide cost gate as code (Task 1 Step 8 rule, A2).

Month-to-date actual cost plus the rest-of-month forecast plus a stage's documented
increment must stay at or below the approved ceiling: Dev's USD 900, or the budget of the
environment being deployed (``--approved-ceiling-usd``). Missing or unreadable cost data
refuses: the gate fails closed. A subscription with no cost history is not missing data: Cost
Management answers with an empty month-to-date table (USD 0) and no forecast (HTTP 204, or no
rows), and the rest of the month is then projected at the month-to-date daily rate. The check
prints which basis it used.

``fetch`` runs one Cost Management query for the subscription of a committed environment
(infra/environments). It retries only throttling (HTTP 429), waiting as long as Cost
Management's own retry-after hints say, within a bounded total wait (D36). Any other failure, or
a spent bound, leaves no result file, so ``check`` refuses with cost_unavailable.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

CEILING_USD = 900.0
API_VERSION = "2025-03-01"
ARM_SCOPE = "https://management.azure.com/.default"
MAX_WAIT_SECONDS = 600.0
DEFAULT_RETRY_SECONDS = 60.0
MIN_RETRY_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 120.0
_COST_URL = re.compile(
    r"https://management\.azure\.com/subscriptions/(?P<subscription>[0-9a-f-]{36})"
    r"/providers/Microsoft\.CostManagement/(?:query|forecast)\?api-version="
    + re.escape(API_VERSION)
)
_COST_MANAGEMENT_HINT = re.compile(
    r"x-ms-ratelimit-microsoft\.costmanagement-[a-z0-9-]+-retry-after"
)
_SECONDS = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,3})?")
# What fetch records when Cost Management has no forecast for the scope (HTTP 204).
NO_FORECAST = "noForecast"


class CostUnavailable(ValueError):
    """Cost Management data is missing, incomplete or not billed in USD."""


class FetchFailed(RuntimeError):
    """A Cost Management query failed, or stayed throttled past the wait bound."""


@dataclass(frozen=True)
class CostResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Post = Callable[[str, bytes], CostResponse]


def query_body() -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
        },
    }


def forecast_body(today: date) -> dict[str, Any]:
    last_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    start = today if today == last_day else today + timedelta(days=1)
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start.isoformat()}T00:00:00+00:00",
            "to": f"{last_day.isoformat()}T23:59:59+00:00",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
        "includeActualCost": False,
        "includeFreshPartialCost": False,
    }


def _table(payload: Mapping[str, Any] | None) -> tuple[list[str], list[list[Any]]]:
    properties = payload.get("properties") if isinstance(payload, Mapping) else None
    if not isinstance(properties, Mapping):
        raise CostUnavailable("Cost Management returned no result table")
    columns = properties.get("columns")
    rows = properties.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise CostUnavailable("Cost Management returned an incomplete result table")
    names: list[str] = []
    for column in columns:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            raise CostUnavailable("Cost Management returned invalid column metadata")
        names.append(str(column["name"]).casefold())
    if not all(isinstance(row, list) and len(row) == len(names) for row in rows):
        raise CostUnavailable("Cost Management returned malformed rows")
    return names, rows


def _usd(row: list[Any], cost_index: int, currency_index: int) -> float:
    cost = row[cost_index]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or row[currency_index] != "USD"
    ):
        raise CostUnavailable("Cost Management returned a cost that is invalid or not in USD")
    return float(cost)


def parse_actual(payload: Mapping[str, Any]) -> float:
    """The single month-to-date ``PreTaxCost`` row, in USD; 0 when the result table lists no
    cost yet this month (a subscription with no cost history)."""
    names, rows = _table(payload)
    if not rows:
        return 0.0
    if len(rows) != 1 or "pretaxcost" not in names or "currency" not in names:
        raise CostUnavailable("Cost Management returned no single month-to-date actual cost")
    actual = _usd(rows[0], names.index("pretaxcost"), names.index("currency"))
    if actual < 0:
        raise CostUnavailable("Cost Management returned a negative actual cost")
    return actual


def parse_forecast(payload: Mapping[str, Any] | None) -> float | None:
    """The sum of the daily forecast rows; None when Cost Management has no forecast for the
    scope (HTTP 204, which fetch records, or no rows). A missing result (``payload`` None: the
    fetch failed) refuses."""
    if payload is None:
        raise CostUnavailable("Cost Management returned no forecast")
    if isinstance(payload, Mapping) and payload.get(NO_FORECAST):
        return None
    names, rows = _table(payload)
    if not rows:
        return None
    cost_name = next((name for name in ("cost", "pretaxcost") if name in names), None)
    if cost_name is None or "currency" not in names:
        raise CostUnavailable("Cost Management returned no usable forecast rows")
    cost_index, currency_index = names.index(cost_name), names.index("currency")
    return sum(_usd(row, cost_index, currency_index) for row in rows)


def projected(actual_usd: float, today: date) -> float:
    """The rest of the month at the month-to-date daily rate, for a scope with no forecast."""
    days = calendar.monthrange(today.year, today.month)[1]
    return actual_usd / today.day * (days - today.day)


def check(
    *,
    actual_usd: float | None,
    forecast_usd: float | None,
    increment_usd: float,
    ceiling_usd: float = CEILING_USD,
) -> list[str]:
    """Refusals for one gated change; an empty list means go."""
    if not math.isfinite(increment_usd) or increment_usd < 0:
        raise ValueError("The cost increment must be a finite, non-negative USD amount")
    if not math.isfinite(ceiling_usd) or ceiling_usd <= 0:
        raise ValueError("The approved ceiling must be a finite, positive USD amount")
    refusals = []
    if actual_usd is None:
        refusals.append("cost_unavailable:actual")
    if forecast_usd is None:
        refusals.append("cost_unavailable:forecast")
    if actual_usd is None or forecast_usd is None:
        return refusals
    total = actual_usd + forecast_usd + increment_usd
    if total > ceiling_usd:
        refusals.append(f"over_ceiling:{total:.2f} > {ceiling_usd:.2f}")
    return refusals


def _seconds(text: str) -> float | None:
    value = text.strip()
    return float(value) if _SECONDS.fullmatch(value) else None


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime) -> float | None:
    """The wait a 429 asks for: the longest valid Cost Management hint (the clienttype
    bucket and its siblings), else ``Retry-After`` in seconds or as an HTTP date, else None."""
    hints: list[float] = []
    retry_after: str | None = None
    for name, value in headers.items():
        key = name.strip().casefold()
        if _COST_MANAGEMENT_HINT.fullmatch(key):
            seconds = _seconds(value)
            if seconds is not None:
                hints.append(seconds)
        elif key == "retry-after":
            retry_after = value
    if hints:
        return max(hints)
    if retry_after is None:
        return None
    seconds = _seconds(retry_after)
    if seconds is not None:
        return seconds
    try:
        when = parsedate_to_datetime(retry_after.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - now).total_seconds())


def fetch(
    url: str,
    body: bytes,
    *,
    post: Post,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
) -> bytes:
    """POST one Cost Management query; retry only HTTP 429, within ``max_wait_seconds``."""
    started = monotonic()
    while True:
        response = post(url, body)
        if response.status == 200:
            try:
                json.loads(response.body.decode("utf-8-sig"))
            except ValueError:
                raise FetchFailed("Cost Management returned HTTP 200 without JSON") from None
            return response.body
        if response.status == 204 and "/forecast?" in url:
            note = "HTTP 204: Cost Management has no forecast for this scope"
            return json.dumps({NO_FORECAST: note}).encode()
        if response.status != 429:
            detail = response.body[:300].decode("utf-8", "replace")
            raise FetchFailed(
                f"Cost Management returned HTTP {response.status}; only throttling (429) is "
                f"retried: {detail}"
            )
        hinted = retry_after_seconds(response.headers, now=datetime.now(UTC))
        delay = max(MIN_RETRY_SECONDS, DEFAULT_RETRY_SECONDS if hinted is None else hinted)
        waited = monotonic() - started
        if waited + delay > max_wait_seconds:
            raise FetchFailed(
                f"Cost Management still throttled (HTTP 429) after {waited:.0f} s; the next "
                f"wait of {delay:.0f} s would pass the {max_wait_seconds:.0f} s bound"
            )
        print(f"Cost Management throttled (HTTP 429); retrying in {delay:.0f} s.", file=sys.stderr)
        sleep(delay)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect must not carry the bearer token anywhere: it fails the fetch instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _arm_post(tenant_id: str) -> Post:
    from azure.identity import AzureCliCredential

    credential = AzureCliCredential(tenant_id=tenant_id)
    opener = urllib.request.build_opener(_NoRedirect)

    def post(url: str, body: bytes) -> CostResponse:
        token = credential.get_token(ARM_SCOPE).token
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return CostResponse(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as error:
            return CostResponse(error.code, dict(error.headers or {}), error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise FetchFailed(f"Cost Management request failed: {type(error).__name__}") from None

    return post


def _fetch_command(
    arguments: argparse.Namespace,
    *,
    post: Post | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> int:
    out: Path = arguments.out
    out.unlink(missing_ok=True)
    target = _COST_URL.fullmatch(arguments.url)
    tenants = _tenants()
    if (
        target is None
        or arguments.subscription not in tenants
        or target["subscription"] != arguments.subscription
    ):
        print(
            "Refused: only a committed environment's Cost Management query or forecast "
            f"(api-version {API_VERSION}) may be fetched.",
            file=sys.stderr,
        )
        return 2
    try:
        body = arguments.body.read_bytes()
        json.loads(body.decode("utf-8-sig"))
    except (OSError, ValueError):
        print(f"Refused: {arguments.body} is not a readable JSON request body.", file=sys.stderr)
        return 2
    try:
        result = fetch(
            arguments.url,
            body,
            post=post or _arm_post(tenants[arguments.subscription]),
            sleep=sleep,
            monotonic=monotonic,
            max_wait_seconds=arguments.max_wait_seconds,
        )
    except FetchFailed as error:
        print(f"cost_unavailable: {error}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result)
    return 0


def _tenants() -> dict[str, str]:
    """The tenant of each committed environment's subscription (infra/environments)."""
    from tools.env.environment import known

    return {subscription: env.tenant_id for subscription, env in known().items()}


def _wait_bound(text: str) -> float:
    value = _amount(text)
    if not 0 < value <= 3600:
        raise argparse.ArgumentTypeError(f"must be between 0 and 3600 seconds: {text}")
    return value


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _amount(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be a finite, non-negative USD amount: {text}")
    return value


def _ceiling(text: str) -> float:
    value = _amount(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive USD amount: {text}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.env.costgate",
        description=(
            "Build Cost Management bodies, fetch an environment's cost query, or gate a change "
            "on its cost ceiling."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bodies = commands.add_parser("bodies", help="Write the query and forecast request bodies.")
    bodies.add_argument("--out-dir", type=Path, required=True)
    fetcher = commands.add_parser(
        "fetch", help="POST one Cost Management query; retry only throttling, bounded."
    )
    fetcher.add_argument("--subscription", required=True)
    fetcher.add_argument("--url", required=True)
    fetcher.add_argument("--body", type=Path, required=True)
    fetcher.add_argument("--out", type=Path, required=True)
    fetcher.add_argument("--max-wait-seconds", type=_wait_bound, default=MAX_WAIT_SECONDS)
    gate = commands.add_parser("check", help="Refuse if actual + forecast + increment > ceiling.")
    gate.add_argument("--actual", type=Path, required=True)
    gate.add_argument("--forecast", type=Path, required=True)
    gate.add_argument("--increment-usd", type=_amount, required=True)
    gate.add_argument(
        "--approved-ceiling-usd",
        type=_ceiling,
        default=CEILING_USD,
        help="The environment's budget, or a higher ceiling the owner approved (default Dev's).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    post: Post | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    if arguments.command == "fetch":
        return _fetch_command(arguments, post=post, sleep=sleep, monotonic=monotonic)
    if arguments.command == "bodies":
        out_dir: Path = arguments.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date()
        for name, body in (
            ("cost-query.json", query_body()),
            ("cost-forecast-body.json", forecast_body(today)),
        ):
            (out_dir / name).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        return 0

    actual: float | None
    forecast: float | None
    basis = "forecast"
    try:
        actual = parse_actual(_load(arguments.actual))
    except CostUnavailable:
        actual = None
    try:
        forecast = parse_forecast(_load(arguments.forecast))
        if forecast is None and actual is not None:
            forecast = round(projected(actual, datetime.now(UTC).date()), 2)
            basis = "no forecast: the month-to-date daily rate"
    except CostUnavailable:
        forecast = None
    refusals = check(
        actual_usd=actual,
        forecast_usd=forecast,
        increment_usd=arguments.increment_usd,
        ceiling_usd=arguments.approved_ceiling_usd,
    )
    shown: dict[str, object] = {
        "actual_usd": actual,
        "forecast_usd": forecast,
        "forecast_basis": basis,
        "increment_usd": arguments.increment_usd,
        "ceiling_usd": arguments.approved_ceiling_usd,
    }
    if actual is not None and forecast is not None:
        shown["total_usd"] = round(actual + forecast + arguments.increment_usd, 2)
    print(json.dumps(shown))
    for refusal in refusals:
        print(refusal, file=sys.stderr)
    return 3 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
