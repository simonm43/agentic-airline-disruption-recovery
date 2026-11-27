from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tools.env import costgate

ROOT = Path(__file__).resolve().parents[2]


def _actual(cost: float = 500.0, currency: str = "USD") -> dict[str, Any]:
    return {
        "properties": {
            "columns": [
                {"name": "PreTaxCost", "type": "Number"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [[cost, currency]],
        }
    }


def _forecast(*rows: tuple[float, str], cost_column: str = "Cost") -> dict[str, Any]:
    return {
        "properties": {
            "columns": [
                {"name": cost_column, "type": "Number"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "CostStatus", "type": "String"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": [
                [cost, 20261106 + index, "Forecast", currency]
                for index, (cost, currency) in enumerate(rows)
            ],
        }
    }


def test_check_passes_within_ceiling() -> None:
    assert costgate.check(actual_usd=500.0, forecast_usd=300.0, increment_usd=80.0) == []


def test_check_refuses_over_ceiling() -> None:
    refusals = costgate.check(actual_usd=500.0, forecast_usd=330.0, increment_usd=80.0)
    assert len(refusals) == 1
    assert refusals[0].startswith("over_ceiling:910.00")
    assert refusals[0] == "over_ceiling:910.00 > 900.00"


def test_check_fails_closed_when_cost_unavailable() -> None:
    assert costgate.check(actual_usd=None, forecast_usd=300.0, increment_usd=80.0) == [
        "cost_unavailable:actual"
    ]
    assert costgate.check(actual_usd=500.0, forecast_usd=None, increment_usd=80.0) == [
        "cost_unavailable:forecast"
    ]


def test_approved_ceiling_raises_limit() -> None:
    assert (
        costgate.check(actual_usd=500.0, forecast_usd=330.0, increment_usd=80.0, ceiling_usd=1000.0)
        == []
    )
    assert costgate.CEILING_USD == 900.0
    assert costgate.API_VERSION == "2025-03-01"


def test_query_body_is_month_to_date_actual_cost() -> None:
    assert costgate.query_body() == {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
        },
    }


def test_forecast_body_covers_rest_of_month() -> None:
    body = costgate.forecast_body(date(2026, 11, 5))
    assert body == {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": "2026-11-06T00:00:00+00:00",
            "to": "2026-11-30T23:59:59+00:00",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
        "includeActualCost": False,
        "includeFreshPartialCost": False,
    }
    last_day = costgate.forecast_body(date(2026, 11, 30))
    assert last_day["timePeriod"] == {
        "from": "2026-11-30T00:00:00+00:00",
        "to": "2026-11-30T23:59:59+00:00",
    }
    december = costgate.forecast_body(date(2026, 12, 31))
    assert december["timePeriod"]["to"] == "2026-12-31T23:59:59+00:00"


def test_parse_actual_reads_one_usd_row() -> None:
    assert costgate.parse_actual(_actual(512.25)) == 512.25
    for payload in (
        _actual(1.0, "EUR"),
        _actual(-1.0),
        {},
    ):
        with pytest.raises(costgate.CostUnavailable):
            costgate.parse_actual(payload)


def test_parse_forecast_sums_cost_rows() -> None:
    assert costgate.parse_forecast(_forecast((10.0, "USD"), (12.5, "USD"))) == 22.5
    assert costgate.parse_forecast(_forecast((3.0, "USD"), cost_column="PreTaxCost")) == 3.0


def test_parse_forecast_refuses_unknown_columns_or_currency() -> None:
    for payload in (
        _forecast((10.0, "USD"), (1.0, "EUR")),
        _forecast((10.0, "USD"), cost_column="Amount"),
        None,
    ):
        with pytest.raises(costgate.CostUnavailable):
            costgate.parse_forecast(payload)


def test_a_subscription_with_no_cost_history_passes_on_its_own_numbers(tmp_path: Path) -> None:
    """A new subscription: an empty month-to-date table is USD 0 and Cost Management has no
    forecast (HTTP 204), so the rest of the month is projected at the month-to-date rate; a
    failed fetch (no file) still refuses."""
    forecast_url = QUERY_URL.replace("/query?", "/forecast?")
    no_content = costgate.CostResponse(204, {}, b"")
    marker = costgate.fetch(forecast_url, b"{}", post=lambda url, body: no_content)
    assert costgate.parse_forecast(json.loads(marker)) is None
    assert costgate.parse_forecast(_forecast()) is None
    empty = {"properties": {"columns": _actual()["properties"]["columns"], "rows": []}}
    assert costgate.parse_actual(empty) == 0.0
    assert costgate.projected(10.0, date(2026, 11, 10)) == 20.0

    actual, forecast = tmp_path / "cost-actual.json", tmp_path / "cost-forecast.json"
    actual.write_text(json.dumps(empty), encoding="utf-8")
    forecast.write_bytes(marker)
    common = ["check", "--actual", str(actual), "--forecast", str(forecast)]
    passing = _run(*common, "--increment-usd", "290", "--approved-ceiling-usd", "600")
    assert passing.returncode == 0, passing.stderr
    shown = json.loads(passing.stdout)
    assert shown["total_usd"] == 290.0 and shown["forecast_basis"].startswith("no forecast")
    forecast.unlink()
    assert _run(*common, "--increment-usd", "0").returncode == 3


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.env.costgate", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_bodies_and_check(tmp_path: Path) -> None:
    result = _run("bodies", "--out-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "cost-query.json").read_text(encoding="utf-8")) == (
        costgate.query_body()
    )
    forecast_body = json.loads((tmp_path / "cost-forecast-body.json").read_text("utf-8"))
    assert forecast_body["timeframe"] == "Custom"

    actual = tmp_path / "cost-actual.json"
    forecast = tmp_path / "cost-forecast.json"
    actual.write_text(json.dumps(_actual(500.0)), encoding="utf-8")
    forecast.write_text(json.dumps(_forecast((300.0, "USD"))), encoding="utf-8")
    common = ["check", "--actual", str(actual), "--forecast", str(forecast)]

    passing = _run(*common, "--increment-usd", "80")
    assert passing.returncode == 0, passing.stderr

    refused = _run(*common, "--increment-usd", "110")
    assert refused.returncode == 3
    assert "over_ceiling:910.00 > 900.00" in refused.stderr

    approved = _run(*common, "--increment-usd", "110", "--approved-ceiling-usd", "1000")
    assert approved.returncode == 0, approved.stderr

    forecast.unlink()
    missing = _run(*common, "--increment-usd", "0")
    assert missing.returncode == 3
    assert "cost_unavailable:forecast" in missing.stderr

    forecast.write_text("not json", encoding="utf-8")
    unparsable = _run(*common, "--increment-usd", "0")
    assert unparsable.returncode == 3
    assert "cost_unavailable:forecast" in unparsable.stderr

    negative = _run(*common, "--increment-usd", "-1")
    assert negative.returncode == 2


# D36: the cost-gate fetch honours Cost Management's throttling hints, within a bound.

DEV = "00000000-0000-4000-8000-000000000013"
QUERY_URL = (
    f"https://management.azure.com/subscriptions/{DEV}"
    "/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
)
CLIENTTYPE = "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after"
OK_BODY = json.dumps(_actual(12.5)).encode()


class FakeCostManagement:
    """Scripted Cost Management responses on a fake clock; records every call and sleep."""

    def __init__(self, *responses: costgate.CostResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, bytes]] = []
        self.sleeps: list[float] = []
        self.clock = 1000.0

    def post(self, url: str, body: bytes) -> costgate.CostResponse:
        self.calls.append((url, body))
        return self.responses[min(len(self.calls), len(self.responses)) - 1]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def monotonic(self) -> float:
        return self.clock


def _throttled(**headers: str) -> costgate.CostResponse:
    return costgate.CostResponse(429, headers, b'{"error":{"code":"429"}}')


def _ok() -> costgate.CostResponse:
    return costgate.CostResponse(200, {}, OK_BODY)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({CLIENTTYPE: "14", "Retry-After": "60"}, 14.0),
        ({CLIENTTYPE.upper(): "14"}, 14.0),
        (
            {
                CLIENTTYPE: "14",
                "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "30",
            },
            30.0,
        ),
        ({"Retry-After": "20"}, 20.0),
        ({CLIENTTYPE: "soon", "Retry-After": "20"}, 20.0),
        ({"Retry-After": "Fri, 06 Nov 2026 10:00:45 GMT"}, 45.0),
        ({"Retry-After": "-5"}, None),
        ({}, None),
    ],
)
def test_retry_after_prefers_the_cost_management_hint(
    headers: dict[str, str], expected: float | None
) -> None:
    now = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
    assert costgate.retry_after_seconds(headers, now=now) == expected


def test_fetch_honours_each_hint_then_returns_the_body() -> None:
    fake = FakeCostManagement(
        _throttled(**{CLIENTTYPE: "14"}), _throttled(**{"Retry-After": "20"}), _ok()
    )
    body = costgate.fetch(
        QUERY_URL, b"{}", post=fake.post, sleep=fake.sleep, monotonic=fake.monotonic
    )
    assert body == OK_BODY
    assert fake.sleeps == [14.0, 20.0]
    assert [url for url, _ in fake.calls] == [QUERY_URL] * 3


def test_fetch_without_a_hint_waits_the_default() -> None:
    fake = FakeCostManagement(_throttled(), _ok())
    costgate.fetch(QUERY_URL, b"{}", post=fake.post, sleep=fake.sleep, monotonic=fake.monotonic)
    assert fake.sleeps == [costgate.DEFAULT_RETRY_SECONDS]


def test_fetch_fails_closed_when_the_next_wait_would_pass_the_bound() -> None:
    fake = FakeCostManagement(_throttled(**{CLIENTTYPE: "250"}))
    with pytest.raises(costgate.FetchFailed, match=r"429.*600 s"):
        costgate.fetch(
            QUERY_URL,
            b"{}",
            post=fake.post,
            sleep=fake.sleep,
            monotonic=fake.monotonic,
            max_wait_seconds=600,
        )
    assert fake.sleeps == [250.0, 250.0]
    assert len(fake.calls) == 3
    assert sum(fake.sleeps) <= 600


def test_fetch_refuses_a_hint_longer_than_the_bound_without_waiting() -> None:
    fake = FakeCostManagement(_throttled(**{CLIENTTYPE: "3600"}), _ok())
    with pytest.raises(costgate.FetchFailed):
        costgate.fetch(QUERY_URL, b"{}", post=fake.post, sleep=fake.sleep, monotonic=fake.monotonic)
    assert fake.sleeps == []
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [204, 400, 401, 403, 500, 503])
def test_fetch_retries_only_throttling(status: int) -> None:
    fake = FakeCostManagement(costgate.CostResponse(status, {"Retry-After": "5"}, b"denied"), _ok())
    with pytest.raises(costgate.FetchFailed, match=f"HTTP {status}"):
        costgate.fetch(QUERY_URL, b"{}", post=fake.post, sleep=fake.sleep, monotonic=fake.monotonic)
    assert len(fake.calls) == 1
    assert fake.sleeps == []


def _fetch_cli(
    tmp_path: Path, fake: FakeCostManagement, *, url: str = QUERY_URL, subscription: str = DEV
) -> tuple[int, Path]:
    body = tmp_path / "cost-query.json"
    body.write_text(json.dumps(costgate.query_body()), encoding="utf-8")
    out = tmp_path / "cost-actual.json"
    out.write_text("stale", encoding="utf-8")
    code = costgate.main(
        [
            "fetch",
            "--subscription",
            subscription,
            "--url",
            url,
            "--body",
            str(body),
            "--out",
            str(out),
        ],
        post=fake.post,
        sleep=fake.sleep,
        monotonic=fake.monotonic,
    )
    return code, out


@pytest.mark.parametrize(
    ("responses", "calls", "written"),
    [
        (["ok"], 1, True),
        (["429", "429", "ok"], 3, True),
        (["429"], 11, False),
        (["403", "ok"], 1, False),
    ],
)
def test_cli_fetch_writes_only_a_successful_result(
    tmp_path: Path, responses: list[str], calls: int, written: bool
) -> None:
    """A2 found Cost Management throttling (HTTP 429). Only throttling is retried, within the
    bound; any other failure, or a spent bound, leaves no result file (a stale one is removed
    first), so `costgate check` refuses with cost_unavailable."""
    scripted = {
        "ok": _ok(),
        "429": _throttled(**{CLIENTTYPE: "60"}),
        "403": costgate.CostResponse(403, {}, b"AuthorizationFailed"),
    }
    fake = FakeCostManagement(*(scripted[name] for name in responses))
    code, out = _fetch_cli(tmp_path, fake)
    assert len(fake.calls) == calls
    assert fake.calls[0][1] == json.dumps(costgate.query_body()).encode()
    assert sum(fake.sleeps) <= costgate.MAX_WAIT_SECONDS
    assert out.exists() is written
    assert code == (0 if written else 1)
    if written:
        assert costgate.parse_actual(json.loads(out.read_text(encoding="utf-8"))) == 12.5


@pytest.mark.parametrize(
    ("url", "subscription"),
    [
        (QUERY_URL.replace(DEV, "11111111-2222-3333-4444-555555555555"), DEV),
        (QUERY_URL, "11111111-2222-3333-4444-555555555555"),
        (QUERY_URL.replace("https://", "http://"), DEV),
        (QUERY_URL.replace("management.azure.com", "example.com"), DEV),
        (QUERY_URL.replace("/query?", "/exports?"), DEV),
        (QUERY_URL.replace("2025-03-01", "2023-03-01"), DEV),
    ],
)
def test_cli_fetch_refuses_any_other_target(tmp_path: Path, url: str, subscription: str) -> None:
    fake = FakeCostManagement(_ok())
    code, out = _fetch_cli(tmp_path, fake, url=url, subscription=subscription)
    assert code == 2
    assert fake.calls == []
    assert not out.exists()
