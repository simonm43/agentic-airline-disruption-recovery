"""The generated operating day in memory, with the SQL store's result shapes and write rules.

Built on ``ScenarioStore``'s compare-and-set writes; filled from ``build_operating_day`` as the
day loader fills SQL. Airport rules mirror migrations 008 and 009: LIS has its night period and
every other flown station an explicit "no rule modelled" row. Passenger notices of each kind and
standby alerts (migration 019) are ``ScenarioStore``'s records.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from arrow_scenario import ScenarioStore, empty_records

from airline_systems.operating_day import OperatingDay, build_operating_day
from airline_systems.schedule import lisbon_today


class OperatingDayStore(ScenarioStore):
    def __init__(self, op_day: OperatingDay | None = None) -> None:
        op_day = op_day or build_operating_day(lisbon_today())
        self.op_day = op_day
        self.day = op_day.day
        self.flights = {flight.flight_no: flight.as_json() for flight in op_day.flights}
        status = {item.registration: item.status for item in op_day.statuses}
        self.aircraft = {
            a.registration: {
                "registration": a.registration,
                "fleet_type": a.fleet_type,
                "seats": a.seats,
                "base": a.base,
                "status": status[a.registration],
            }
            for a in op_day.aircraft
        }
        self.segments = {pnr.locator: list(pnr.flight_nos) for pnr in op_day.pnrs}
        self.party = {pnr.locator: pnr.party_size for pnr in op_day.pnrs}
        self.seats = {item.flight_no: item.seats for item in op_day.inventory}
        self.distances = {item.flight_no: item.distance_km for item in op_day.inventory}
        self.duties = {
            duty.duty_id: {
                "duty_id": duty.duty_id,
                "base": duty.base,
                "start_airport": duty.start_airport,
                "qualification": duty.qualification,
                "report_local": duty.report_local.isoformat(),
                "release_local": duty.release_local.isoformat(),
                "fdp_minutes": duty.fdp_minutes,
                "max_fdp_minutes": duty.max_fdp_minutes,
                "pilots_required": duty.pilots_required,
                "cabin_required": duty.cabin_required,
                "flight_nos": list(duty.flight_nos),
                "members": [{"crew_id": m.crew_id, "rank": m.rank} for m in duty.members],
            }
            for duty in op_day.duties
        }
        crew = {member.crew_id: member for member in op_day.crew}
        self.reserves = [
            {
                "crew_id": reserve.crew_id,
                "rank": crew[reserve.crew_id].rank,
                "base": reserve.base,
                "qualification": crew[reserve.crew_id].qualification,
                "start_local": reserve.start_local.isoformat(),
                "end_local": reserve.end_local.isoformat(),
                "standby_type": reserve.standby_type,
            }
            for reserve in op_day.reserves
        ]
        self.defect: dict[str, Any] = {"trace_id": None}
        self.actions: dict[str, dict[str, Any]] = {}
        self.delays: dict[str, dict[str, Any]] = {}
        self.weather: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str | None, str]] = []
        self.records = empty_records()

    def record_defect(self, defect: dict[str, Any]) -> dict[str, Any]:
        # As SQL: Maintenance marks the aircraft unserviceable when it records the defect.
        self.aircraft[defect["aircraft_reg"]]["status"] = "unserviceable"
        return super().record_defect(defect)

    def domain_inventory(self, day: date, domain: str) -> dict[str, Any]:
        if domain == "booking":
            inventory = super().domain_inventory(day, domain)
            for row in inventory["inventory"]:
                row["seats"] = self.seats[row["flight_no"]]
                row["distance_km"] = self.distances[row["flight_no"]]
            return inventory
        if domain == "slots":
            return {
                "capacity": dict(self.op_day.slot_capacity),
                "slots": [
                    {
                        "airport": slot.airport,
                        "flight_no": slot.flight_no,
                        "movement": slot.movement,
                        "slot_local": slot.slot_local.isoformat(),
                    }
                    for slot in self.op_day.slots
                ],
            }
        return super().domain_inventory(day, domain)

    def airport_rules(self) -> list[dict[str, Any]]:
        flown = {f["origin"] for f in self.flights.values()} | {
            f["destination"] for f in self.flights.values()
        }
        none = {
            "coordination_level": None,
            "a_cdm": False,
            "night_departure_from": None,
            "night_departure_until": None,
            "night_arrival_from": None,
            "night_arrival_until": None,
        }
        rules = [
            {
                "iata": "LIS",
                "coordination_level": 3,
                "a_cdm": True,
                "night_departure_from": "23:55",
                "night_departure_until": "05:50",
                "night_arrival_from": "00:05",
                "night_arrival_until": "06:00",
                "source": "LIS night period per airline-ops KB §12.2",
            }
        ]
        rules += [
            {"iata": iata, **none, "source": "no rule modelled"} for iata in sorted(flown - {"LIS"})
        ]
        return copy.deepcopy(rules)
