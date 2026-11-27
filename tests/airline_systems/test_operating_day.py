"""Step 2 business checks: the generated operating day is a realistic, legal airline day."""

from __future__ import annotations

import unittest
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

from airline_systems.operating_day import FLEET, build_operating_day, max_daily_fdp_minutes
from airline_systems.schedule import lisbon_today

TODAY = lisbon_today()
DAY = build_operating_day(TODAY)
SEATS = {a.registration: a.seats for a in DAY.aircraft}
FLEET_OF = {a.registration: a.fleet_type for a in DAY.aircraft}
FLIGHTS = {f.flight_no: f for f in DAY.flights}


class OperatingDayTests(unittest.TestCase):
    def test_900_flights_fly_continuous_hub_rotations_on_two_families(self) -> None:
        self.assertEqual(len(DAY.flights), 900)
        self.assertEqual(len(FLIGHTS), 900)
        self.assertEqual({FLEET_OF[f.aircraft_reg] for f in DAY.flights}, set(FLEET))
        self.assertEqual(len({a.registration for a in DAY.aircraft}), len(DAY.aircraft))
        hub = sum(1 for f in DAY.flights if "LIS" in (f.origin, f.destination))
        self.assertGreaterEqual(hub / 900, 0.55)
        rotations = defaultdict(list)
        for f in sorted(DAY.flights, key=lambda f: f.std_local):
            rotations[f.aircraft_reg].append(f)
        for reg, legs in rotations.items():
            turn = timedelta(minutes=FLEET[FLEET_OF[reg]][3])
            for prev, nxt in zip(legs, legs[1:], strict=False):
                self.assertEqual(prev.destination, nxt.origin, reg)
                self.assertGreaterEqual(nxt.std_local - prev.sta_local, turn, reg)
        self.assertTrue(all(f.std_local.date() == TODAY for f in DAY.flights))
        self.assertTrue(all(f.std_local.hour >= 6 for f in DAY.flights))

    def test_every_flight_has_a_legal_qualified_crew(self) -> None:
        crew = {c.crew_id: c for c in DAY.crew}
        covered = Counter(no for d in DAY.duties for no in d.flight_nos)
        self.assertEqual(covered, Counter(FLIGHTS.keys()))
        working = [m.crew_id for d in DAY.duties for m in d.members]
        working += [r.crew_id for r in DAY.reserves]
        self.assertEqual(len(working), len(set(working)), "a crew member has two duties")
        for d in DAY.duties:
            legs = [FLIGHTS[no] for no in d.flight_nos]
            fdp = (legs[-1].sta_local - d.report_local).total_seconds() / 60
            self.assertEqual(fdp, d.fdp_minutes)
            limit = max_daily_fdp_minutes(d.report_local.time(), len(legs), d.pilots_required)
            self.assertLessEqual(d.fdp_minutes, limit, d.duty_id)
            ranks = Counter(m.rank for m in d.members)
            _, cabin, qualification, _ = FLEET[FLEET_OF[legs[0].aircraft_reg]]
            self.assertEqual(ranks["CPT"], 1, d.duty_id)
            self.assertEqual(ranks["CPT"] + ranks["FO"], d.pilots_required, d.duty_id)
            self.assertEqual(ranks["SC"] + ranks["CC"], cabin, d.duty_id)
            for m in d.members:
                self.assertEqual(crew[m.crew_id].qualification, qualification, d.duty_id)
                self.assertEqual(crew[m.crew_id].base, d.base, d.duty_id)
            for prev, nxt in zip(legs, legs[1:], strict=False):
                self.assertEqual(prev.aircraft_reg, nxt.aircraft_reg, d.duty_id)
        self.assertTrue({r.base for r in DAY.reserves} >= {"LIS", "OPO", "FAO", "FNC", "PDL"})

    def test_bookings_fill_seats_and_slots_respect_declared_capacity(self) -> None:
        booked: Counter[str] = Counter()
        for pnr in DAY.pnrs:
            for no in pnr.flight_nos:
                booked[no] += pnr.party_size
            for first, onward in zip(pnr.flight_nos, pnr.flight_nos[1:], strict=False):
                a, b = FLIGHTS[first], FLIGHTS[onward]
                self.assertEqual((a.destination, b.origin), ("LIS", "LIS"), pnr.locator)
                self.assertGreaterEqual(b.std_local - a.sta_local, timedelta(minutes=60))
        self.assertTrue(any(len(p.flight_nos) == 2 for p in DAY.pnrs))
        for inv in DAY.inventory:
            self.assertEqual(booked[inv.flight_no], inv.passengers, inv.flight_no)
            self.assertLessEqual(inv.passengers, SEATS[FLIGHTS[inv.flight_no].aircraft_reg])
            self.assertGreaterEqual(inv.passengers, int(0.85 * inv.seats), inv.flight_no)
        expected = {(f.flight_no, "D", f.origin, f.std_local) for f in DAY.flights} | {
            (f.flight_no, "A", f.destination, f.sta_local) for f in DAY.flights
        }
        slots = {(s.flight_no, s.movement, s.airport, s.slot_local) for s in DAY.slots}
        self.assertEqual(slots, {m for m in expected if m[2] in DAY.slot_capacity})
        self.assertTrue({"LIS", "OPO", "FAO", "FNC"} <= set(DAY.slot_capacity))
        hourly = Counter((s.airport, s.slot_local.hour) for s in DAY.slots)
        for (airport, hour), used in hourly.items():
            self.assertLessEqual(used, DAY.slot_capacity[airport], (airport, hour))
        self.assertTrue(all(s.status == "serviceable" for s in DAY.statuses))
        self.assertTrue(all(s.return_to_service_local is None for s in DAY.statuses))

    def test_seeded_day_marks_only_the_demo_ar101_party_as_synthetic_opted_in(self) -> None:
        opted_in = [pnr for pnr in DAY.pnrs if getattr(pnr, "synthetic_recovery_opt_in", False)]

        self.assertEqual(len(opted_in), 1)
        self.assertEqual(opted_in[0].flight_nos, ("AR101",))

    def test_demo_rotation_is_fixed_and_every_day_is_the_same_pattern(self) -> None:
        tqa = sorted(
            (f for f in DAY.flights if f.aircraft_reg == "CS-TQA"), key=lambda f: f.std_local
        )
        self.assertEqual(
            [
                (
                    f.flight_no,
                    f.origin,
                    f.destination,
                    f.std_local.strftime("%H:%M"),
                    f.sta_local.strftime("%H:%M"),
                )
                for f in tqa
            ],
            [
                ("AR101", "LIS", "OPO", "10:30", "11:25"),
                ("AR102", "OPO", "LIS", "12:00", "12:55"),
                ("AR131", "LIS", "FAO", "14:10", "14:55"),
                ("AR132", "FAO", "LIS", "15:35", "16:20"),
            ],
        )
        other_day = date(2027, 2, 14)
        shift = other_day - TODAY
        other = build_operating_day(other_day)
        self.assertEqual(
            other.flights,
            tuple(
                replace(
                    f,
                    flight_date=other_day,
                    std_local=f.std_local + shift,
                    sta_local=f.sta_local + shift,
                    published_std_local=f.std_local + shift,
                    published_sta_local=f.sta_local + shift,
                )
                for f in DAY.flights
            ),
        )
        self.assertEqual(other.pnrs, DAY.pnrs)
        self.assertEqual(other.crew, DAY.crew)
        self.assertEqual([d.flight_nos for d in other.duties], [d.flight_nos for d in DAY.duties])


if __name__ == "__main__":
    unittest.main()
