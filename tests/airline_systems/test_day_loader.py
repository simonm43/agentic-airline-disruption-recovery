"""The nightly job prepares each new operating day before anyone opens ARROW."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from airline_systems.day_loader import prepare_day

DAY = date(2026, 11, 10)


class DayStore:
    def __init__(self, loaded: set[date]) -> None:
        self.loaded = loaded
        self.loads: list[date] = []

    def has_day(self, day: date) -> bool:
        return day in self.loaded

    def load_operating_day(self, day: date, op_day: Any, *, force: bool = False) -> bool:
        self.loads.append(day)
        self.loaded.add(day)
        return True


def check_new_day_is_loaded_once_and_then_left_alone() -> None:
    store = DayStore(loaded={date(2026, 11, 9)})

    first = prepare_day(store, DAY)
    second = prepare_day(store, DAY)

    assert first["loaded"] is True and first["flights"] == 900, first
    assert second["loaded"] is False, second
    assert store.loads == [DAY]


def check_test_bench_reset_reloads_a_day_already_loaded() -> None:
    store = DayStore(loaded={DAY})

    result = prepare_day(store, DAY, force=True)

    assert result["loaded"] is True and result["flights"] == 900, result
    assert store.loads == [DAY]


class DayLoaderTests(unittest.TestCase):
    def test_new_day_is_loaded_once_and_then_left_alone(self) -> None:
        check_new_day_is_loaded_once_and_then_left_alone()

    def test_test_bench_reset_reloads_a_day_already_loaded(self) -> None:
        check_test_bench_reset_reloads_a_day_already_loaded()
