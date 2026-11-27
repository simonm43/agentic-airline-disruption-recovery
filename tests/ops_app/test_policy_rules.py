"""Policy engine (spec §8): management by exception. Plans within every domain's limits run;
anything else goes to OCC or is blocked. Each row sets cost explicitly so delay rows test the
EU261 care thresholds alone."""

import copy
import unittest
from typing import Any

from ops_app.policy import RELEASE_LIMITS, Limits, classify

ORIGINALS = {
    "AR101": {
        "flight_no": "AR101",
        "std_local": "2026-11-08T10:30:00",
        "status": "scheduled",
        "aircraft_reg": "CS-TQA",
    },
    "AR102": {
        "flight_no": "AR102",
        "std_local": "2026-11-08T12:00:00",
        "status": "scheduled",
        "aircraft_reg": "CS-TQA",
    },
}


def option(
    *,
    kind: str = "delay",
    shift: int = 15,
    cancelled: bool = False,
    crew: str = "pass",
    booking: str = "pass",
    capacity_fit: bool = True,
    distance_km: int | None = 300,
    intra_eu: bool = True,
    cost: float | None = 900,
    conflict: str | None = None,
    rejected: list[str] | None = None,
    reg: str = "CS-TQA",
) -> dict[str, Any]:
    def moved(flight_no: str) -> dict[str, Any]:
        hour, minute = (10, 30) if flight_no == "AR101" else (12, 0)
        total = hour * 60 + minute + (0 if cancelled else shift)
        flight: dict[str, Any] = {
            "flight_no": flight_no,
            "std_local": f"2026-11-08T{total // 60:02d}:{total % 60:02d}:00",
            "status": "cancelled" if cancelled else "scheduled",
            "aircraft_reg": reg,
            "intra_eu": intra_eu,
        }
        if distance_km is not None:
            flight["distance_km"] = distance_km
        return flight

    candidate: dict[str, Any] = {
        "option_id": kind,
        "kind": kind,
        "flights": [moved("AR101"), moved("AR102")],
        "domain_assessments": [
            {"domain": "crew", "constraint_status": crew},
            {"domain": "booking", "constraint_status": booking},
            {"domain": "slots", "constraint_status": "pass", "capacity_fit": capacity_fit},
        ],
        "conflict": conflict,
        "rejected_levers": rejected or [],
    }
    if cost is not None:
        candidate["cost"] = {"total_eur": cost}
    return candidate


def validation(
    unplaced: int = 0,
    requests: list[str] | None = None,
    *,
    disrupted: int | None = None,
    rebooked: int = 0,
    unknown_airports: list[str] | None = None,
    breaches: int = 0,
    digest: str | None = "c" * 64,
    reserve: str | None = None,
    standby: list[str] | None = None,
    left: int = 2,
) -> dict[str, Any]:
    """``reserve``: a plan stored before several standby crews (one id, no count of crews left).
    ``standby``: the standby crews a plan calls; after it Lisbon A320 keeps ``left`` complete
    standby crews and Ponta Delgada none."""
    crew_plan: dict[str, Any] = {
        "duties_changed": 1,
        "plan_digest": digest,
        "breaches_unresolved": breaches,
        "reserve_option_id": reserve,
    }
    if standby is not None:
        crew_plan.update(
            reserve_option_id=standby[0] if standby else None,
            reserve_option_ids=standby,
            standby_left=[
                {"base": "LIS", "qualification": "A320F", "crews": left},
                {"base": "PDL", "qualification": "A320F", "crews": 0},
            ],
        )
    return {
        "rebooking": {
            "disrupted_passengers": rebooked + unplaced if disrupted is None else disrupted,
            "rebooked_passengers": rebooked,
            "unplaced_passengers": unplaced,
        },
        "airport_requests": requests or [],
        "airport_rules_unknown": unknown_airports or [],
        "crew_plan": crew_plan,
    }


def counted(candidate: dict[str, Any], misconnected: int) -> dict[str, Any]:
    return {**candidate, "misconnected_passengers": misconnected}


def calling(
    candidate: dict[str, Any], ids: list[str], *, base: str = "LIS", timed: bool = True
) -> dict[str, Any]:
    """The plan calls standby crews ``ids`` of ``base``'s A320 crews, each on time for its first
    departure (``timed=False``: Crew did not say when a crew can take it)."""
    crews = [
        {
            "reserve_option_id": item,
            "base": base,
            "qualification": "A320F",
            "standby_type": "home",
            "called_local": "2026-11-08T11:00:00",
            "report_local": "2026-11-08T12:00:00",
            "departure_local": "2026-11-08T13:00:00",
            **({"earliest_departure_local": "2026-11-08T13:00:00"} if timed else {}),
        }
        for item in ids
    ]
    return {**candidate, "crew_reserves": crews}


WITHIN = "within every domain's limits"
CASES = [
    ("delay 20 min", option(shift=20), validation(), "auto", WITHIN),
    ("delay 130 min, 300 km", option(shift=130), validation(), "occ", "EU261 care threshold (2 h)"),
    (
        "delay 130 min, 2,000 km, not intra-EU",
        option(shift=130, distance_km=2000, intra_eu=False),
        validation(),
        "auto",
        WITHIN,
    ),
    (
        "delay 190 min, 2,000 km, intra-EU",
        option(shift=190, distance_km=2000, intra_eu=True),
        validation(),
        "occ",
        "EU261 care threshold (3 h)",
    ),
    (
        "delay 250 min, 4,000 km",
        option(shift=250, distance_km=4000, intra_eu=False),
        validation(),
        "occ",
        "EU261 care threshold (4 h)",
    ),
    ("cost 20,001", option(shift=20, cost=20_001), validation(), "occ", "cost above €20,000"),
    (
        "11 rebooked",
        option(shift=20),
        validation(rebooked=11),
        "occ",
        "more than 10 passengers rebooked",
    ),
    ("10 rebooked", option(shift=20), validation(rebooked=10), "auto", WITHIN),
    ("tail swap", option(kind="swap", shift=0, reg="CS-TQB"), validation(), "occ", "aircraft"),
    ("standby aircraft", option(kind="standby", shift=0), validation(), "occ", "aircraft change"),
    ("cancellation", option(kind="cancel", cancelled=True), validation(), "occ", "cancellation"),
    (
        "breach unresolved",
        option(shift=20),
        validation(breaches=1),
        "blocked",
        "flight-time limit exceeded with no standby crew",  # Task 2: was "reserve crew"
    ),
    (
        "crew check failed without a reserve",
        option(shift=20, crew="fail"),
        validation(),
        "blocked",
        "flight-time limit exceeded with no standby crew",  # Task 2: was "reserve crew"
    ),
    (
        "breach resolved by a reserve",
        option(shift=20, crew="fail"),
        validation(reserve="R1"),
        "auto",
        WITHIN,
    ),
    (
        # Case 5's delay breaks two duties; one reserve covers only one (owner).
        "breach left after a reserve",
        option(shift=20, crew="fail"),
        validation(breaches=1, reserve="R1"),
        "blocked",
        "flight-time limit still exceeded after the standby crew",  # Task 2: was "reserve crew"
    ),
    # Standby crews: several in one plan, one per breached duty. More than two, or the
    # last complete standby crew of a base and qualification, is OCC's decision.
    (
        "two standby crews, crews left at the base",
        calling(option(shift=20, crew="fail"), ["R1", "R2"]),
        validation(standby=["R1", "R2"]),
        "auto",
        WITHIN,
    ),
    (
        "three standby crews",
        calling(option(shift=20, crew="fail"), ["R1", "R2", "R3"]),
        validation(standby=["R1", "R2", "R3"], left=1),
        "occ",
        "3 standby crews called (automatic limit 2)",
    ),
    (
        "the last standby crew of a base",
        calling(option(shift=20, crew="fail"), ["R1"]),
        validation(standby=["R1"], left=0),
        "occ",
        "uses the last standby crew at Lisbon A320",
    ),
    (
        # Review: a base already without a complete crew is flagged too.
        "a standby crew of a base left without a complete crew",
        calling(option(shift=20, crew="fail"), ["R1"], base="PDL"),
        validation(standby=["R1"]),
        "occ",
        "uses the last standby crew at Ponta Delgada A320",
    ),
    (
        "breach left after two standby crews",
        calling(option(shift=20, crew="fail"), ["R1", "R2"]),
        validation(breaches=1, standby=["R1", "R2"]),
        "blocked",
        "flight-time limit still exceeded after the standby crews",
    ),
    (
        "standby crew report time unknown",
        calling(option(shift=20, crew="fail"), ["R1"], timed=False),
        validation(standby=["R1"]),
        "blocked",
        "standby crew report time not established",
    ),
    (
        "domains disagree",
        option(shift=20, conflict="crew needs AR102 at 12:20; slots allocated 12:40"),
        validation(),
        "blocked",
        "domains disagree",
    ),
    (
        "lever not offered",
        option(shift=20, rejected=["swap CS-TQZ"]),
        validation(),
        "blocked",
        "lever not offered",
    ),
    (
        "crew duty plan missing",
        option(shift=20),
        validation(digest=None),
        "blocked",
        "crew duty plan not established",
    ),
    ("cost unknown", option(shift=20, cost=None), validation(), "occ", "cost not established"),
    (
        "distance unknown for a delayed flight",
        option(shift=20, distance_km=None),
        validation(),
        "occ",
        "distance unknown",
    ),
    (
        "night-quota request",
        option(shift=0),
        validation(requests=["LIS night quota: AR102 off-block 00:10"]),
        "occ",
        "night quota",
    ),
    (
        "3 passengers outside policy go to the expert, not OCC (D23)",
        option(shift=0, booking="fail"),
        validation(unplaced=3),
        "auto",
        "3 passengers need Passenger Services review",
    ),
    (
        "rebooking declined for disrupted passengers needs OCC",
        {**option(shift=20), "levers": {"aircraft_option_id": "delay", "rebook": False}},
        validation(unplaced=4),
        "occ",
        "rebooking declined for 4 passengers who lose their flight or connection",
    ),
    ("crew check unknown", option(crew="unknown"), validation(), "blocked", "crew check unknown"),
    ("capacity does not fit", option(capacity_fit=False), validation(), "blocked", "LIS capacity"),
    ("validation missing", option(shift=15), None, "blocked", "validation missing"),
    (
        "airport rules unknown for a moved flight",
        option(shift=15),
        validation(unknown_airports=["OPO"]),
        "blocked",
        "airport rules unknown: OPO",
    ),
    (
        "plan and card count different passengers",
        counted(option(shift=15, booking="fail"), 112),
        validation(unplaced=3, disrupted=243),
        "blocked",
        "passenger counts disagree",
    ),
    (
        "plan does not account for every disrupted passenger",
        option(shift=15),
        validation(unplaced=3, disrupted=7),
        "blocked",
        "passenger counts disagree",
    ),
]


# Rule checks (control tower and rules spec §4): what each rule checked, against which limit, and
# its result.
CHECKED = [
    ("auto", option(shift=20), validation(), "auto"),
    ("OCC by cost", option(shift=20, cost=21_250), validation(), "occ"),
    ("OCC by long delay", option(shift=195, distance_km=277), validation(), "occ"),
    ("blocked by crew", option(shift=20), validation(breaches=1), "blocked"),
    ("expert passengers", option(shift=0, booking="fail"), validation(unplaced=3), "auto"),
    (
        "OCC by standby crews",
        calling(option(shift=20, crew="fail"), ["R1", "R2", "R3"]),
        validation(standby=["R1", "R2", "R3"]),
        "occ",
    ),
]
ALWAYS_CHECKED = {
    "cost",
    "rebooking",
    "aircraft",
    "cancellation",
    "airport",
    "crew",
    "eu261",
    "capacity",
    "answered",
    "passenger_services",
}


class PolicyRuleTests(unittest.TestCase):
    def test_each_check_shows_its_input_limit_and_result_and_agrees_with_the_route(self) -> None:
        checks: dict[str, dict[str, dict[str, Any]]] = {}
        for label, candidate, checked, route in CHECKED:
            with self.subTest(label):
                verdict = classify(
                    candidate, originals=ORIGINALS, validation=checked, limits=Limits(1, 20_000, 10)
                )
                results = {check["result"] for check in verdict.checks}

                self.assertEqual(verdict.route, route, verdict.reasons)
                self.assertLessEqual(ALWAYS_CHECKED, {check["key"] for check in verdict.checks})
                self.assertEqual(verdict.route == "blocked", "blocked" in results, verdict.checks)
                self.assertEqual(
                    verdict.route == "occ",
                    "blocked" not in results and "occ" in results,
                    verdict.checks,
                )
                checks[label] = {check["key"]: check for check in verdict.checks}

        cost = checks["OCC by cost"]["cost"]
        self.assertEqual(cost["input"], "€21,250")
        self.assertEqual(cost["limit"], "€20,000 (rules v1)")
        self.assertEqual(cost["result"], "occ")
        self.assertEqual(cost["measure"], {"value": 21250, "limit": 20000})
        long_delay = checks["OCC by long delay"]["eu261"]
        self.assertEqual(long_delay["result"], "occ")
        self.assertIn("+195 min", long_delay["input"])
        self.assertEqual(checks["blocked by crew"]["crew"]["result"], "blocked")
        self.assertEqual(checks["expert passengers"]["passenger_services"]["result"], "expert")
        standby = checks["OCC by standby crews"]["standby"]
        self.assertEqual((standby["result"], standby["measure"]), ("occ", {"value": 3, "limit": 2}))
        self.assertEqual(checks["OCC by standby crews"]["crew"]["result"], "pass")
        self.assertNotIn("standby", checks["auto"])

    def test_policy_routes(self) -> None:
        for label, candidate, checked, route, reason in CASES:
            with self.subTest(label):
                verdict = classify(
                    candidate, originals=ORIGINALS, validation=checked, limits=RELEASE_LIMITS
                )
                self.assertEqual(verdict.route, route, verdict)
                self.assertTrue(any(reason in text for text in verdict.reasons), verdict.reasons)

    def test_unknown_original_time_blocks(self) -> None:
        originals = copy.deepcopy(ORIGINALS)
        del originals["AR102"]

        verdict = classify(
            option(shift=15), originals=originals, validation=validation(), limits=RELEASE_LIMITS
        )

        self.assertEqual(verdict.route, "blocked")
        self.assertTrue(any("added delay unknown" in text for text in verdict.reasons))
