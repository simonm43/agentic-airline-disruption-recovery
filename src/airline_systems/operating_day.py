"""Today's operating day of the fictional Lisbon-hub airline, for all five systems.

Deterministic: a fixed seed builds one daily pattern; every day has the same flights, crews,
bookings and slots, only the date changes. All times are Lisbon wall-clock times, also abroad
(documented simplification). Every assumption is listed in docs/operating-day.md; the only
scenario fact used here is the 900 daily flights.
"""

from __future__ import annotations

import heapq
import math
import random
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from airline_systems.schedule import Flight

SEED = 20261106
DAILY_FLIGHTS = 900  # scenario fact
HUB = "LIS"
FIRST_DEPARTURE = 6 * 60
LAST_DEPARTURE = 23 * 60 + 59
LAST_NARROW_ARRIVAL = 24 * 60 + 30
NIGHT_END = 24 * 60 + 6 * 60
REPORT_BEFORE_STD = 60
RELEASE_AFTER_STA = 30
MIN_CONNECTION = 60
MAX_CONNECTION = 300
CONNECTING_SHARE = 0.35  # of an inbound hub flight's passengers, where onward options exist
AUGMENTED_BLOCK = 600  # company policy: block above 10 h flies with a third pilot
AUGMENTED_MAX_FDP = 16 * 60  # CS FTL.1.205(c): one extra pilot, class 1 rest facility

# fleet type: seats, cabin crew, pilot type rating, minimum turnaround (min)
FLEET: dict[str, tuple[int, int, str, int]] = {
    "A320neo": (174, 4, "A320F", 35),
    "A321neo": (216, 5, "A320F", 35),
    "A330neo": (298, 9, "A330", 60),
}

# The demo aircraft's rotation is fixed (defect at 09:45, out of service 4 h).
DEMO_AIRCRAFT = "CS-TQA"
DEMO_ROTATION: tuple[tuple[str, str, str, int, int], ...] = (
    ("AR101", "LIS", "OPO", 10 * 60 + 30, 11 * 60 + 25),
    ("AR102", "OPO", "LIS", 12 * 60, 12 * 60 + 55),
    ("AR131", "LIS", "FAO", 14 * 60 + 10, 14 * 60 + 55),
    ("AR132", "FAO", "LIS", 15 * 60 + 35, 16 * 60 + 20),
)

# iata: latitude, longitude, region (PT, EU, AF narrow-body Africa, LH long-haul)
AIRPORTS: dict[str, tuple[float, float, str]] = {
    "LIS": (38.77, -9.13, "PT"),
    "OPO": (41.24, -8.68, "PT"),
    "FAO": (37.01, -7.97, "PT"),
    "FNC": (32.70, -16.77, "PT"),
    "PDL": (37.74, -25.70, "PT"),
    "TER": (38.76, -27.09, "PT"),
    "MAD": (40.47, -3.56, "EU"),
    "BCN": (41.30, 2.08, "EU"),
    "SVQ": (37.42, -5.89, "EU"),
    "AGP": (36.67, -4.50, "EU"),
    "BIO": (43.30, -2.91, "EU"),
    "VLC": (39.49, -0.48, "EU"),
    "LHR": (51.47, -0.45, "EU"),
    "LGW": (51.15, -0.19, "EU"),
    "MAN": (53.35, -2.27, "EU"),
    "BHX": (52.45, -1.75, "EU"),
    "EDI": (55.95, -3.37, "EU"),
    "DUB": (53.42, -6.27, "EU"),
    "CDG": (49.01, 2.55, "EU"),
    "ORY": (48.72, 2.38, "EU"),
    "NCE": (43.66, 7.22, "EU"),
    "LYS": (45.73, 5.08, "EU"),
    "MRS": (43.44, 5.21, "EU"),
    "TLS": (43.63, 1.36, "EU"),
    "BOD": (44.83, -0.72, "EU"),
    "NTE": (47.15, -1.61, "EU"),
    "BRU": (50.90, 4.48, "EU"),
    "AMS": (52.31, 4.76, "EU"),
    "LUX": (49.63, 6.21, "EU"),
    "FRA": (50.03, 8.57, "EU"),
    "MUC": (48.35, 11.79, "EU"),
    "DUS": (51.29, 6.77, "EU"),
    "HAM": (53.63, 9.99, "EU"),
    "BER": (52.37, 13.50, "EU"),
    "CPH": (55.62, 12.66, "EU"),
    "ARN": (59.65, 17.92, "EU"),
    "OSL": (60.19, 11.10, "EU"),
    "GVA": (46.24, 6.11, "EU"),
    "ZRH": (47.46, 8.55, "EU"),
    "FCO": (41.80, 12.25, "EU"),
    "MXP": (45.63, 8.72, "EU"),
    "VCE": (45.51, 12.35, "EU"),
    "BLQ": (44.53, 11.29, "EU"),
    "PRG": (50.10, 14.26, "EU"),
    "VIE": (48.11, 16.57, "EU"),
    "WAW": (52.17, 20.97, "EU"),
    "BUD": (47.44, 19.26, "EU"),
    "CMN": (33.37, -7.59, "AF"),
    "RAK": (31.61, -8.04, "AF"),
    "DSS": (14.67, -17.07, "AF"),
    "RAI": (14.92, -23.49, "AF"),
    "SID": (16.74, -22.95, "AF"),
    "BKO": (12.53, -7.95, "AF"),
    "GRU": (-23.43, -46.47, "LH"),
    "GIG": (-22.81, -43.25, "LH"),
    "REC": (-8.13, -34.92, "LH"),
    "FOR": (-3.78, -38.53, "LH"),
    "SSA": (-12.91, -38.33, "LH"),
    "BSB": (-15.87, -47.92, "LH"),
    "CNF": (-19.62, -43.97, "LH"),
    "BEL": (-1.38, -48.48, "LH"),
    "NAT": (-5.77, -35.37, "LH"),
    "VCP": (-23.01, -47.13, "LH"),
    "EWR": (40.69, -74.17, "LH"),
    "JFK": (40.64, -73.78, "LH"),
    "BOS": (42.36, -71.01, "LH"),
    "IAD": (38.95, -77.46, "LH"),
    "MIA": (25.79, -80.29, "LH"),
    "ORD": (41.98, -87.90, "LH"),
    "YYZ": (43.68, -79.63, "LH"),
    "YUL": (45.47, -73.74, "LH"),
    "ATL": (33.64, -84.43, "LH"),
    "LAD": (-8.86, 13.23, "LH"),
    "MPM": (-25.92, 32.57, "LH"),
    "ACC": (5.61, -0.17, "LH"),
    "ABJ": (5.26, -3.93, "LH"),
}

# Public ICAO location indicators, for the messages to EUROCONTROL's Network Manager.
ICAO: dict[str, str] = {
    "LIS": "LPPT",
    "OPO": "LPPR",
    "FAO": "LPFR",
    "FNC": "LPMA",
    "PDL": "LPPD",
    "TER": "LPLA",
    "MAD": "LEMD",
    "BCN": "LEBL",
    "SVQ": "LEZL",
    "AGP": "LEMG",
    "BIO": "LEBB",
    "VLC": "LEVC",
    "LHR": "EGLL",
    "LGW": "EGKK",
    "MAN": "EGCC",
    "BHX": "EGBB",
    "EDI": "EGPH",
    "DUB": "EIDW",
    "CDG": "LFPG",
    "ORY": "LFPO",
    "NCE": "LFMN",
    "LYS": "LFLL",
    "MRS": "LFML",
    "TLS": "LFBO",
    "BOD": "LFBD",
    "NTE": "LFRS",
    "BRU": "EBBR",
    "AMS": "EHAM",
    "LUX": "ELLX",
    "FRA": "EDDF",
    "MUC": "EDDM",
    "DUS": "EDDL",
    "HAM": "EDDH",
    "BER": "EDDB",
    "CPH": "EKCH",
    "ARN": "ESSA",
    "OSL": "ENGM",
    "GVA": "LSGG",
    "ZRH": "LSZH",
    "FCO": "LIRF",
    "MXP": "LIMC",
    "VCE": "LIPZ",
    "BLQ": "LIPE",
    "PRG": "LKPR",
    "VIE": "LOWW",
    "WAW": "EPWA",
    "BUD": "LHBP",
    "CMN": "GMMN",
    "RAK": "GMMX",
    "DSS": "GOBD",
    "RAI": "GVNP",
    "SID": "GVAC",
    "BKO": "GABS",
    "GRU": "SBGR",
    "GIG": "SBGL",
    "REC": "SBRF",
    "FOR": "SBFZ",
    "SSA": "SBSV",
    "BSB": "SBBR",
    "CNF": "SBCF",
    "BEL": "SBBE",
    "NAT": "SBSG",
    "VCP": "SBKP",
    "EWR": "KEWR",
    "JFK": "KJFK",
    "BOS": "KBOS",
    "IAD": "KIAD",
    "MIA": "KMIA",
    "ORD": "KORD",
    "YYZ": "CYYZ",
    "YUL": "CYUL",
    "ATL": "KATL",
    "LAD": "FNLU",
    "MPM": "FQMA",
    "ACC": "DGAA",
    "ABJ": "DIAP",
}

# The city each airport serves, for plain words in plans (an airport's own name where a city
# has two of ours).
CITY: dict[str, str] = {
    "LIS": "Lisbon",
    "OPO": "Porto",
    "FAO": "Faro",
    "FNC": "Funchal",
    "PDL": "Ponta Delgada",
    "TER": "Terceira",
    "MAD": "Madrid",
    "BCN": "Barcelona",
    "SVQ": "Seville",
    "AGP": "Málaga",
    "BIO": "Bilbao",
    "VLC": "Valencia",
    "LHR": "London Heathrow",
    "LGW": "London Gatwick",
    "MAN": "Manchester",
    "BHX": "Birmingham",
    "EDI": "Edinburgh",
    "DUB": "Dublin",
    "CDG": "Paris Charles de Gaulle",
    "ORY": "Paris Orly",
    "NCE": "Nice",
    "LYS": "Lyon",
    "MRS": "Marseille",
    "TLS": "Toulouse",
    "BOD": "Bordeaux",
    "NTE": "Nantes",
    "BRU": "Brussels",
    "AMS": "Amsterdam",
    "LUX": "Luxembourg",
    "FRA": "Frankfurt",
    "MUC": "Munich",
    "DUS": "Düsseldorf",
    "HAM": "Hamburg",
    "BER": "Berlin",
    "CPH": "Copenhagen",
    "ARN": "Stockholm",
    "OSL": "Oslo",
    "GVA": "Geneva",
    "ZRH": "Zurich",
    "FCO": "Rome",
    "MXP": "Milan",
    "VCE": "Venice",
    "BLQ": "Bologna",
    "PRG": "Prague",
    "VIE": "Vienna",
    "WAW": "Warsaw",
    "BUD": "Budapest",
    "CMN": "Casablanca",
    "RAK": "Marrakesh",
    "DSS": "Dakar",
    "RAI": "Praia",
    "SID": "Sal",
    "BKO": "Bamako",
    "GRU": "São Paulo",
    "GIG": "Rio de Janeiro",
    "REC": "Recife",
    "FOR": "Fortaleza",
    "SSA": "Salvador",
    "BSB": "Brasília",
    "CNF": "Belo Horizonte",
    "BEL": "Belém",
    "NAT": "Natal",
    "VCP": "Campinas",
    "EWR": "Newark",
    "JFK": "New York",
    "BOS": "Boston",
    "IAD": "Washington Dulles",
    "MIA": "Miami",
    "ORD": "Chicago",
    "YYZ": "Toronto",
    "YUL": "Montreal",
    "ATL": "Atlanta",
    "LAD": "Luanda",
    "MPM": "Maputo",
    "ACC": "Accra",
    "ABJ": "Abidjan",
}

# Regulation (EC) 261/2004 territory used for its intra-Community distance band: EU, EEA and
# Switzerland. The United Kingdom left in 2020 (UK261 applies separately).
UK_AIRPORTS = frozenset({"LHR", "LGW", "MAN", "BHX", "EDI"})
EU261_AREA = (
    frozenset(code for code, (_, _, region) in AIRPORTS.items() if region in {"PT", "EU"})
    - UK_AIRPORTS
)
# Owner (D108): at the hub a car keeps a connection a delay left this short; passport
# control (crossing the Schengen border) needs longer. Ireland and the UK are outside Schengen.
SCHENGEN_AREA = EU261_AREA - {"DUB"}
DIRECT_TRANSFER_MINUTES = 15
DIRECT_TRANSFER_PASSPORT_MINUTES = 20


def passport_control(origin: str, destination: str) -> bool:
    """A hub connection from ``origin`` to ``destination`` crosses the Schengen border."""
    return (origin in SCHENGEN_AREA) != (destination in SCHENGEN_AREA)


# Carbon pricing for aviation (cost index and time value research §3). The EU Emissions Trading
# System (Directive 2003/87/EC as amended by Directive (EU) 2023/958) covers flights within the EEA
# and departures from it to the UK or Switzerland. Arrivals from the UK or Switzerland pay under
# those countries' own schemes, priced here at the EU allowance price (assumption, low confidence).
# Exempt until the end of 2030: flights between mainland Portugal and Madeira (FNC) or the Azores
# (PDL, TER), and, as domestic flights of Portugal's outermost regions, flights between or within
# the islands (Ponta Delgada-Funchal, Ponta Delgada-Terceira). Flights to and from Africa and the
# Americas pay nothing.
SWISS_AIRPORTS = frozenset({"GVA", "ZRH"})
EEA_AREA = EU261_AREA - SWISS_AIRPORTS
PORTUGUESE_OUTERMOST = frozenset({"FNC", "PDL", "TER"})


def emissions_trading(origin: str, destination: str) -> bool:
    """A flight from ``origin`` to ``destination`` pays for its CO2 (allowances at the EU price)."""
    abroad = UK_AIRPORTS | SWISS_AIRPORTS
    if origin in abroad:
        return destination in EEA_AREA
    if origin not in EEA_AREA:
        return False
    if destination in abroad:
        return True
    if destination not in EEA_AREA:
        return False
    domestic = AIRPORTS[origin][2] == AIRPORTS[destination][2] == "PT"
    return not (domestic and {origin, destination} & PORTUGUESE_OUTERMOST)


# Fuel to make up a minute in the air and its carbon, one source for operations control (which
# decides the minutes) and ARROW (which prices them): about 70 kg a minute for an A320-family
# aircraft (35-125; Airbus 1998 Tables 21-22, Cook et al. 2007 Table 4) and an assumed 130 kg for
# a wide body (A330ceo data; no A330neo data), wide from 250 seats, at €1.22 a kilogram (IATA Jet
# Fuel Price Monitor, November 2026, at the ECB rate). Each tonne of fuel emits 3.15 t of CO2
# (Commission Implementing Regulation (EU) 2018/2066, Annex III), each tonne an allowance at about
# €80 from 2026, when aircraft operators' free allocation ends (cost index and time value
# research §3).
WIDE_BODY_FROM_SEATS = 250
FUEL_KG_PER_MINUTE = {"narrow": 70, "wide": 130}
FUEL_EUR_PER_KG = 1.22
CO2_T_PER_T_FUEL = 3.15
EUA_EUR_PER_T = 80


# Declared movements (departures + arrivals) per clock hour at coordinated airports,
# treated as the airline's usable allocation (assumption; other carriers not modelled).
SLOT_CAPACITY: dict[str, int] = {
    "LIS": 40,
    "OPO": 24,
    "FAO": 20,
    "FNC": 12,
    "PDL": 10,
    "MAD": 100,
    "BCN": 78,
    "LHR": 88,
    "LGW": 55,
    "MAN": 60,
    "DUB": 48,
    "CDG": 120,
    "ORY": 70,
    "NCE": 50,
    "AMS": 106,
    "FRA": 104,
    "MUC": 90,
    "DUS": 47,
    "BER": 50,
    "BRU": 74,
    "GVA": 40,
    "ZRH": 66,
    "FCO": 90,
    "MXP": 70,
    "VCE": 40,
    "VIE": 68,
    "CPH": 83,
    "GRU": 50,
    "JFK": 81,
    "EWR": 76,
}

# Weather playbook: the share of the airline's Lisbon allocation OCC plans with while a forecast
# hazard lasts. Lisbon publishes no bad-weather rate (TAF weather case research
# §2.4, §4.3), so the thunderstorm rate is calibrated on history: 60%, 40 -> 24 movements an
# hour, was chosen because the research's runway queue on our day then delays each movement it
# makes later by 26 min (§4.4), about the 25 minutes per delayed arrival of Lisbon's
# thunderstorm evenings in EUROCONTROL's airport delay data 2015-2025 (Lisbon storm and ATC
# history research §2). In the delay plan, with knock-on delays, delayed Lisbon arrivals average
# about 33 minutes.
# Crosswind: Lisbon has one runway in use, 02/20, so a wind across it is never a closure: the
# airline's OCC playbook plans the same 60% while gusts cross it (wider spacing, go-arounds).
# Low visibility: 70% (40 -> 28), conservatively twice the ~15% average cut EUROCONTROL's
# peak-service-rate study found for low visibility, as Lisbon's single runway cannot absorb the
# wider low-visibility spacing on a second runway.
WEATHER_PLAYBOOK_PERCENT: dict[str, int] = {
    "thunderstorm": 60,
    "crosswind": 60,
    "low_visibility": 70,
}
# Why each hazard's rate, as a weather case and its plans say it (owner: not "an
# assumption", "based on historical data"). Never a published Lisbon rate.
WEATHER_RATE_BASIS: dict[str, str] = {
    "thunderstorm": "set to match Lisbon's thunderstorm evenings in EUROCONTROL delay data"
    " 2015–2025 (about 25 min per delayed arrival)",
    "crosswind": "the airline's OCC playbook rate for crosswind on Lisbon's single runway; the"
    " A320 family's demonstrated crosswind is 38 kt including gusts (35 kt take-off for the"
    " A320neo's engines), so operations continue with care",
    "low_visibility": "twice the 15% average cut EUROCONTROL found for low visibility, as"
    " Lisbon has one runway",
}


def weather_rate(airport: str, hazard: str) -> int:
    """Movements an hour the playbook plans at ``airport`` while ``hazard`` lasts."""
    return SLOT_CAPACITY[airport] * WEATHER_PLAYBOOK_PERCENT[hazard] // 100


# The OCC playbook's dispatch advice (OCC playbook plan; proactive storm recovery research §4.2),
# assumptions, not published figures. Exposure window: the forecast window widened by 90 min
# before and 180 min after (the queue clears), 14:30-21:00 for a 16:00-18:00 storm.
EXPOSURE_BEFORE = timedelta(minutes=90)
EXPOSURE_AFTER = timedelta(minutes=180)
# Alternates the dispatcher screens for a Lisbon storm; no forecast hazard at them in this demo.
STORM_ALTERNATES = ("OPO", "FAO")


# Narrow-body routes per base: destination, daily round-trip weight, popular (runs full at peaks)
NARROW_ROUTES: dict[str, tuple[tuple[str, int, bool], ...]] = {
    "LIS": (
        ("OPO", 7, True),
        ("FAO", 4, True),
        ("FNC", 7, True),
        ("PDL", 4, True),
        ("TER", 2, False),
        ("MAD", 6, True),
        ("BCN", 5, True),
        ("SVQ", 2, False),
        ("AGP", 2, False),
        ("BIO", 2, False),
        ("VLC", 2, False),
        ("LHR", 6, True),
        ("LGW", 3, False),
        ("MAN", 2, False),
        ("BHX", 1, False),
        ("EDI", 1, False),
        ("DUB", 3, False),
        ("CDG", 6, True),
        ("ORY", 3, True),
        ("NCE", 3, False),
        ("LYS", 3, False),
        ("MRS", 2, False),
        ("TLS", 2, False),
        ("BOD", 2, False),
        ("NTE", 2, False),
        ("BRU", 4, False),
        ("AMS", 4, True),
        ("LUX", 3, False),
        ("FRA", 4, True),
        ("MUC", 3, False),
        ("DUS", 2, False),
        ("HAM", 2, False),
        ("BER", 3, False),
        ("CPH", 2, False),
        ("ARN", 1, False),
        ("OSL", 1, False),
        ("GVA", 4, False),
        ("ZRH", 3, False),
        ("FCO", 4, True),
        ("MXP", 3, False),
        ("VCE", 2, False),
        ("BLQ", 2, False),
        ("PRG", 1, False),
        ("VIE", 2, False),
        ("WAW", 1, False),
        ("BUD", 1, False),
        ("CMN", 2, False),
        ("RAK", 2, False),
        ("DSS", 1, False),
        ("RAI", 1, False),
        ("SID", 1, False),
        ("BKO", 1, False),
    ),
    "OPO": (
        ("FNC", 5, True),
        ("PDL", 3, True),
        ("TER", 2, False),
        ("MAD", 3, False),
        ("BCN", 3, False),
        ("LHR", 4, True),
        ("LGW", 2, False),
        ("MAN", 2, False),
        ("CDG", 4, True),
        ("ORY", 3, True),
        ("LYS", 3, False),
        ("MRS", 2, False),
        ("TLS", 2, False),
        ("NTE", 2, False),
        ("BRU", 3, False),
        ("AMS", 3, False),
        ("LUX", 3, False),
        ("FRA", 3, False),
        ("DUS", 2, False),
        ("MUC", 2, False),
        ("GVA", 3, False),
        ("ZRH", 3, False),
        ("FCO", 2, False),
        ("MXP", 2, False),
        ("NCE", 2, False),
        ("BOD", 2, False),
    ),
    "FAO": (
        ("LHR", 3, True),
        ("LGW", 3, True),
        ("MAN", 3, True),
        ("BHX", 2, False),
        ("EDI", 1, False),
        ("DUB", 3, True),
        ("AMS", 2, False),
        ("FRA", 2, False),
        ("DUS", 2, False),
        ("CDG", 2, False),
        ("BRU", 2, False),
    ),
    "FNC": (
        ("OPO", 3, True),
        ("LHR", 2, True),
        ("CDG", 2, False),
        ("MAD", 2, False),
        ("AMS", 1, False),
        ("FRA", 1, False),
        ("BRU", 1, False),
        ("PDL", 1, False),
    ),
    "PDL": (
        ("OPO", 3, True),
        ("TER", 4, False),
        ("FNC", 1, False),
        ("LHR", 1, False),
        ("FRA", 1, False),
        ("BOS", 1, False),
    ),
}
A320_MAX_BLOCK = 240  # longer narrow-body sectors are flown by the A321neo only

# One A330neo each; each flies one round trip from LIS per day.
WIDE_DESTINATIONS = (
    "GRU",
    "GRU",
    "GRU",
    "GIG",
    "GIG",
    "REC",
    "FOR",
    "SSA",
    "BSB",
    "CNF",
    "BEL",
    "NAT",
    "VCP",
    "EWR",
    "EWR",
    "JFK",
    "JFK",
    "BOS",
    "BOS",
    "IAD",
    "MIA",
    "ORD",
    "YYZ",
    "YUL",
    "ATL",
    "LAD",
    "LAD",
    "MPM",
    "ACC",
    "ABJ",
)
POPULAR_LONG_HAUL = frozenset({"GRU", "GIG", "EWR", "JFK", "BOS", "LAD", "REC", "FOR"})
PEAK_HOURS = frozenset({6, 7, 8, 17, 18, 19, 20})

# base: number of A320neo, A321neo
NARROW_BASES: tuple[tuple[str, int, int], ...] = (
    ("LIS", 57, 36),
    ("OPO", 27, 10),
    ("FAO", 6, 2),
    ("FNC", 6, 0),
    ("PDL", 4, 2),
)
WIDE_COUNT = len(WIDE_DESTINATIONS)

# Hub aircraft that spend the night at an outstation: early feeder into LIS, last flight back out.
NIGHT_STOPS = (
    "OPO",
    "OPO",
    "OPO",
    "FNC",
    "FNC",
    "FNC",
    "PDL",
    "PDL",
    "FAO",
    "FAO",
    "TER",
    "MAD",
    "MAD",
    "BCN",
    "LHR",
    "LHR",
    "CDG",
    "CDG",
    "FRA",
    "AMS",
    "FCO",
    "GVA",
    "BRU",
    "MXP",
)

# Standby crew (reserve crew practice research §4; assumption): per base and qualification, rows of
# standby type, window (minutes after midnight) and CPT, FO, SC, CC. The daily totals by rank are
# Lisbon A320 6/6/4/16, Lisbon A330 2/2/2/8, Porto 3/3/3/8 and each island 1/1/1/4: the earlier
# pool plus one senior cabin crew member at Porto and on the Lisbon A330. Home standby lasts 8 h,
# airport standby 4 h (TAP's agreements' limits); no crew is on standby overnight. Each window
# holds a complete crew (CPT, FO, SC and four CC on the A320 family, eight on the A330) where the
# totals allow; one complete Lisbon A320 crew waits at the airport in the afternoon bank.
STANDBY_ROSTER: dict[
    tuple[str, str], tuple[tuple[str, int, int, tuple[int, int, int, int]], ...]
] = {
    ("LIS", "A320F"): (
        ("home", 6 * 60, 14 * 60, (2, 2, 1, 4)),
        ("home", 11 * 60, 19 * 60, (2, 2, 1, 4)),
        ("airport", 14 * 60, 18 * 60, (1, 1, 1, 4)),
        ("home", 16 * 60, 24 * 60, (1, 1, 1, 4)),
    ),
    ("LIS", "A330"): (
        ("home", 11 * 60, 19 * 60, (1, 1, 1, 8)),
        ("home", 16 * 60, 24 * 60, (1, 1, 1, 0)),
    ),
    ("OPO", "A320F"): (
        ("home", 6 * 60, 14 * 60, (1, 1, 1, 4)),
        ("home", 13 * 60, 21 * 60, (2, 2, 2, 4)),
    ),
    ("FAO", "A320F"): (("home", 8 * 60, 16 * 60, (1, 1, 1, 4)),),
    ("FNC", "A320F"): (("home", 8 * 60, 16 * 60, (1, 1, 1, 4)),),
    ("PDL", "A320F"): (("home", 8 * 60, 16 * 60, (1, 1, 1, 4)),),
}

# Commission Regulation (EU) No 83/2014, Annex III ORO.FTL.205(b)(1): maximum daily FDP for
# acclimatised crew. Start of FDP (reference time) -> limit for 1-2 sectors; the limit falls by
# 30 min for each sector from the 3rd, never below 9 h.
_FDP_TABLE = (
    (0, 660),
    (5 * 60, 720),
    (5 * 60 + 15, 735),
    (5 * 60 + 30, 750),
    (5 * 60 + 45, 765),
    (6 * 60, 780),
    (13 * 60 + 30, 765),
    (14 * 60, 750),
    (14 * 60 + 30, 735),
    (15 * 60, 720),
    (15 * 60 + 30, 705),
    (16 * 60, 690),
    (16 * 60 + 30, 675),
    (17 * 60, 660),
)


def max_daily_fdp_minutes(report: time, sectors: int, pilots: int = 2) -> int:
    """EASA maximum daily flight duty period, in minutes, for a duty reporting at ``report``."""
    if pilots >= 3:
        return AUGMENTED_MAX_FDP
    minute = report.hour * 60 + report.minute
    limit = [value for start, value in _FDP_TABLE if start <= minute][-1]
    return max(9 * 60, limit - 30 * max(0, sectors - 2))


@dataclass(frozen=True)
class Aircraft:
    registration: str
    fleet_type: str
    seats: int
    base: str


@dataclass(frozen=True)
class AircraftStatus:
    registration: str
    status: str
    return_to_service_local: datetime | None


@dataclass(frozen=True)
class FlightInventory:
    flight_no: str
    seats: int
    distance_km: int
    passengers: int


@dataclass(frozen=True)
class CrewMember:
    crew_id: str
    rank: str  # CPT, FO (cockpit); SC senior cabin, CC cabin
    base: str
    qualification: str  # A320F (A320neo/A321neo) or A330


@dataclass(frozen=True)
class DutyMember:
    crew_id: str
    rank: str


@dataclass(frozen=True)
class Duty:
    duty_id: str
    base: str
    start_airport: str
    qualification: str
    report_local: datetime
    release_local: datetime
    flight_nos: tuple[str, ...]
    fdp_minutes: int
    max_fdp_minutes: int
    pilots_required: int
    cabin_required: int
    members: tuple[DutyMember, ...]


@dataclass(frozen=True)
class Reserve:
    crew_id: str
    base: str
    start_local: datetime
    end_local: datetime
    standby_type: str  # home or airport (EASA ORO.FTL.225)


@dataclass(frozen=True)
class Pnr:
    locator: str
    party_size: int
    flight_nos: tuple[str, ...]
    synthetic_recovery_opt_in: bool = False


@dataclass(frozen=True)
class Slot:
    airport: str
    flight_no: str
    movement: str  # D departure, A arrival
    slot_local: datetime


@dataclass(frozen=True)
class OperatingDay:
    day: date
    aircraft: tuple[Aircraft, ...]
    statuses: tuple[AircraftStatus, ...]
    flights: tuple[Flight, ...]
    inventory: tuple[FlightInventory, ...]
    crew: tuple[CrewMember, ...]
    duties: tuple[Duty, ...]
    reserves: tuple[Reserve, ...]
    pnrs: tuple[Pnr, ...]
    slot_capacity: dict[str, int]
    slots: tuple[Slot, ...]


# Day-independent pattern in minutes after midnight (values above 1440 are the next day).
@dataclass(frozen=True)
class _Leg:
    flight_no: str
    origin: str
    destination: str
    std: int
    sta: int
    registration: str


@dataclass(frozen=True)
class _DutyPlan:
    duty_id: str
    base: str
    start_airport: str
    qualification: str
    report: int
    release: int
    flight_nos: tuple[str, ...]
    fdp: int
    max_fdp: int
    pilots: int
    cabin: int
    members: tuple[DutyMember, ...]


@dataclass(frozen=True)
class _Pattern:
    aircraft: tuple[Aircraft, ...]
    legs: tuple[_Leg, ...]
    inventory: tuple[FlightInventory, ...]
    crew: tuple[CrewMember, ...]
    duties: tuple[_DutyPlan, ...]
    reserves: tuple[tuple[str, str, int, int, str], ...]
    pnrs: tuple[Pnr, ...]


def distance_km(origin: str, destination: str) -> int:
    lat1, lon1, _ = AIRPORTS[origin]
    lat2, lon2, _ = AIRPORTS[destination]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)))


def block_minutes(origin: str, destination: str) -> int:
    """Scheduled block time: 30 min taxi/climb allowance plus cruise, rounded to 5 min."""
    pair = frozenset((origin, destination))
    if pair == frozenset(("LIS", "OPO")):
        return 55
    if pair == frozenset(("LIS", "FAO")):
        return 45
    km = distance_km(origin, destination)
    minutes = 30 + km / (12.5 if km < 3000 else 13.8)
    return int(5 * round(minutes / 5))


class _Slots:
    def __init__(self) -> None:
        self.used: dict[str, list[int]] = defaultdict(lambda: [0] * 24)

    def fits(self, movements: list[tuple[str, int]]) -> bool:
        tally: dict[tuple[str, int], int] = defaultdict(int)
        for airport, minute in movements:
            if airport in SLOT_CAPACITY:
                tally[(airport, minute // 60 % 24)] += 1
        return all(self.used[ap][hour] + n <= SLOT_CAPACITY[ap] for (ap, hour), n in tally.items())

    def book(self, movements: list[tuple[str, int]], sign: int = 1) -> None:
        for airport, minute in movements:
            if airport in SLOT_CAPACITY:
                self.used[airport][minute // 60 % 24] += sign


def _round_trip_moves(
    base: str, dest: str, times: tuple[int, int, int, int]
) -> list[tuple[str, int]]:
    d, a, r, ra = times
    return [(base, d), (dest, a), (dest, r), (base, ra)]


def _registrations(prefixes: str, count: int) -> list[str]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    regs = [f"CS-T{p}{s}" for p in prefixes for s in letters]
    return regs[:count]


def _fleet() -> list[Aircraft]:
    a320 = iter(_registrations("QRST", 100))
    a321 = iter(_registrations("JK", 50))
    fleet: list[Aircraft] = []
    for base, n320, n321 in NARROW_BASES:
        fleet += [Aircraft(next(a320), "A320neo", FLEET["A320neo"][0], base) for _ in range(n320)]
        fleet += [Aircraft(next(a321), "A321neo", FLEET["A321neo"][0], base) for _ in range(n321)]
    fleet += [
        Aircraft(reg, "A330neo", FLEET["A330neo"][0], HUB)
        for reg in _registrations("UV", WIDE_COUNT)
    ]
    return fleet


RoundTrip = tuple[str, str, tuple[int, int, int, int]]  # base, destination, (d, a, r, ra)


def _wide_rotations(rng: random.Random, slots: _Slots) -> list[RoundTrip]:
    trips: list[RoundTrip] = []
    for i, dest in enumerate(WIDE_DESTINATIONS):
        block = block_minutes(HUB, dest)
        offset = rng.randrange(0, 61, 5)
        starts = list(range(7 * 60 + offset, 12 * 60 + 31, 5)) + list(range(7 * 60, 9 * 60, 5))
        for d in starts:
            found = None
            for turn in range(90, 241, 5):
                r = d + block + turn
                ra = r + block
                night_ok = ra <= LAST_NARROW_ARRIVAL or NIGHT_END <= ra <= d + 1440 - 60
                times = (d, d + block, r, ra)
                if r <= LAST_DEPARTURE and night_ok:
                    if slots.fits(_round_trip_moves(HUB, dest, times)):
                        found = times
                        break
            if found:
                slots.book(_round_trip_moves(HUB, dest, found))
                trips.append((HUB, dest, found))
                break
        else:
            raise RuntimeError(f"no wide-body rotation fits for {dest} ({i})")
    return trips


def _place(
    slots: _Slots, base: str, dest: str, ready: int, latest: int
) -> tuple[int, int, int, int] | None:
    block = block_minutes(base, dest)
    turn = FLEET["A320neo"][3]
    for d in range(ready, ready + 91, 5):
        a = d + block
        if d > LAST_DEPARTURE or not slots.fits([(base, d), (dest, a)]):
            continue
        for r in range(a + turn, a + turn + 61, 5):
            ra = r + block
            if r > LAST_DEPARTURE or ra > latest:
                break
            times = (d, a, r, ra)
            if slots.fits(_round_trip_moves(base, dest, times)):
                return times
    return None


def _night_stop(slots: _Slots, station: str) -> RoundTrip:
    """Evening LIS->station and next-morning station->LIS (the morning leg flies first)."""
    block = block_minutes(HUB, station)
    for r in range(FIRST_DEPARTURE, 8 * 60 + 1, 5):
        if not slots.fits([(station, r), (HUB, r + block)]):
            continue
        last = min(LAST_DEPARTURE, LAST_NARROW_ARRIVAL - block) // 5 * 5
        for d in range(last, 21 * 60 - 1, -5):
            times = (d, d + block, r, r + block)
            if slots.fits(_round_trip_moves(HUB, station, times)):
                return (HUB, station, times)
    raise RuntimeError(f"no night-stop fits at {station}")


def _narrow_rotations(
    rng: random.Random,
    slots: _Slots,
    narrow: list[Aircraft],
    target: int,
    nightstops: dict[str, RoundTrip],
) -> dict[str, list[RoundTrip]]:
    rotations: dict[str, list[RoundTrip]] = {ac.registration: [] for ac in narrow}
    used: dict[tuple[str, str], int] = defaultdict(int)
    latest = {ac.registration: LAST_NARROW_ARRIVAL for ac in narrow}
    start = {ac.registration: FIRST_DEPARTURE for ac in narrow}
    hub_aircraft = [ac for ac in narrow if ac.base == HUB]
    for ac, station in zip(reversed(hub_aircraft), NIGHT_STOPS, strict=False):
        trip = _night_stop(slots, station)
        slots.book(_round_trip_moves(HUB, station, trip[2]))
        nightstops[ac.registration] = trip
        d, _, _, ra = trip[2]
        start[ac.registration] = ra + FLEET[ac.fleet_type][3]
        latest[ac.registration] = d - FLEET[ac.fleet_type][3]
    heap = [(start[ac.registration], i) for i, ac in enumerate(narrow)]
    heapq.heapify(heap)
    while heap:
        ready, i = heapq.heappop(heap)
        ac = narrow[i]
        routes = [
            (dest, weight)
            for dest, weight, _ in NARROW_ROUTES[ac.base]
            if used[(ac.base, dest)] < 2 * weight + 1
            and (ac.fleet_type == "A321neo" or block_minutes(ac.base, dest) <= A320_MAX_BLOCK)
        ]
        routes.sort(key=lambda route: rng.random() ** (1 / route[1]), reverse=True)
        for dest, _ in routes:
            times = _place(slots, ac.base, dest, ready, latest[ac.registration])
            if times:
                slots.book(_round_trip_moves(ac.base, dest, times))
                used[(ac.base, dest)] += 1
                rotations[ac.registration].append((ac.base, dest, times))
                heapq.heappush(heap, (times[3] + FLEET[ac.fleet_type][3], i))
                break
        else:
            if ready + 30 <= 22 * 60:
                heapq.heappush(heap, (ready + 30, i))

    total = 2 * sum(len(trips) for trips in rotations.values()) + 2 * len(nightstops)
    if total < target:
        raise RuntimeError(f"narrow-body pattern yields {total} flights, needs {target}")
    while total > target:
        reg = max(
            (r for r in rotations if rotations[r]),
            key=lambda r: (len(rotations[r]) + (r in nightstops), rotations[r][-1][2][3]),
        )
        base, dest, times = rotations[reg].pop()
        slots.book(_round_trip_moves(base, dest, times), -1)
        total -= 2
    return rotations


def _load_factor(rng: random.Random, popular: bool, std: int) -> float:
    if popular and std // 60 % 24 in PEAK_HOURS:
        return 1.0
    return rng.uniform(0.85, 0.99)


def _duty_split(legs: list[_Leg], base: str) -> list[list[_Leg]]:
    """Greedy: crews change only at their base, and a duty grows while the FTL table allows."""
    blocks: list[list[_Leg]] = [[]]
    for leg in legs:
        blocks[-1].append(leg)
        if leg.destination == base:
            blocks.append([])
    duties: list[list[_Leg]] = []
    current: list[_Leg] = []
    for block in (b for b in blocks if b):
        candidate = current + block
        report = candidate[0].std - REPORT_BEFORE_STD
        limit = max_daily_fdp_minutes(_clock(report), len(candidate))
        if current and candidate[-1].sta - report > limit:
            duties.append(current)
            current = list(block)
        else:
            current = candidate
    if current:
        duties.append(current)
    return duties


def _clock(minute: int) -> time:
    minute %= 1440
    return time(minute // 60, minute % 60)


@lru_cache(maxsize=1)
def _pattern() -> _Pattern:
    rng = random.Random(SEED)
    fleet = _fleet()
    by_reg = {ac.registration: ac for ac in fleet}
    slots = _Slots()
    demo_moves = [(o, s) for _, o, _, s, _ in DEMO_ROTATION] + [
        (d, a) for _, _, d, _, a in DEMO_ROTATION
    ]
    slots.book(demo_moves)
    wide = _wide_rotations(rng, slots)
    narrow = [ac for ac in fleet if ac.fleet_type != "A330neo" and ac.registration != DEMO_AIRCRAFT]
    target = DAILY_FLIGHTS - 2 * len(wide) - len(DEMO_ROTATION)
    nightstops: dict[str, RoundTrip] = {}
    rotations = _narrow_rotations(rng, slots, narrow, target, nightstops)
    for reg, trip in nightstops.items():
        rotations[reg].append(trip)
    wide_regs = [ac.registration for ac in fleet if ac.fleet_type == "A330neo"]
    for reg, trip in zip(wide_regs, wide, strict=True):
        rotations[reg] = [trip]

    legs_by_reg: dict[str, list[_Leg]] = {
        DEMO_AIRCRAFT: [_Leg(no, o, d, s, a, DEMO_AIRCRAFT) for no, o, d, s, a in DEMO_ROTATION]
    }
    numbers = {"A330neo": 201, "narrow": 301}
    ordered = sorted((trip[2][0], reg, trip) for reg, trips in rotations.items() for trip in trips)
    for _, reg, (base, dest, (d, a, r, ra)) in ordered:
        key = "A330neo" if by_reg[reg].fleet_type == "A330neo" else "narrow"
        n = numbers[key]
        numbers[key] += 2
        legs_by_reg.setdefault(reg, []).extend(
            [_Leg(f"AR{n}", base, dest, d, a, reg), _Leg(f"AR{n + 1}", dest, base, r, ra, reg)]
        )
    for legs in legs_by_reg.values():
        legs.sort(key=lambda leg: leg.std)
    legs_all = sorted((leg for legs in legs_by_reg.values() for leg in legs), key=lambda x: x.std)

    crew, duties, reserves = _crew(legs_by_reg, by_reg)
    inventory, pnrs = _bookings(rng, legs_all, by_reg)
    return _Pattern(tuple(fleet), tuple(legs_all), inventory, crew, duties, reserves, pnrs)


def _crew(
    legs_by_reg: dict[str, list[_Leg]], by_reg: dict[str, Aircraft]
) -> tuple[
    tuple[CrewMember, ...], tuple[_DutyPlan, ...], tuple[tuple[str, str, int, int, str], ...]
]:
    crew: list[CrewMember] = []
    counters = {"P": 0, "C": 0}

    def hire(rank: str, base: str, qualification: str) -> str:
        prefix = "P" if rank in ("CPT", "FO") else "C"
        counters[prefix] += 1
        crew_id = f"{prefix}{counters[prefix]:05d}"
        crew.append(CrewMember(crew_id, rank, base, qualification))
        return crew_id

    duties: list[_DutyPlan] = []
    seq: dict[str, int] = defaultdict(int)
    for reg in sorted(legs_by_reg, key=lambda r: (by_reg[r].base, r)):
        ac = by_reg[reg]
        _, cabin, qualification, _ = FLEET[ac.fleet_type]
        legs = legs_by_reg[reg]
        if ac.fleet_type == "A330neo":
            groups = [[leg] for leg in legs]  # long-haul crews lay over at the outstation
        else:
            groups = _duty_split(legs, ac.base)
        for group in groups:
            report = group[0].std - REPORT_BEFORE_STD
            fdp = group[-1].sta - report
            longest = max(leg.sta - leg.std for leg in group)
            two_pilot_limit = max_daily_fdp_minutes(_clock(report), len(group), 2)
            pilots = 3 if longest > AUGMENTED_BLOCK or fdp > two_pilot_limit else 2
            seq[ac.base] += 1
            ranks = ["CPT"] + ["FO"] * (pilots - 1) + ["SC"] + ["CC"] * (cabin - 1)
            members = tuple(DutyMember(hire(rank, ac.base, qualification), rank) for rank in ranks)
            duties.append(
                _DutyPlan(
                    f"D-{ac.base}-{seq[ac.base]:03d}",
                    ac.base,
                    group[0].origin,
                    qualification,
                    report,
                    group[-1].sta + RELEASE_AFTER_STA,
                    tuple(leg.flight_no for leg in group),
                    fdp,
                    max_daily_fdp_minutes(_clock(report), len(group), pilots),
                    pilots,
                    cabin,
                    members,
                )
            )
    reserves: list[tuple[str, str, int, int, str]] = []
    for (base, qualification), rows in STANDBY_ROSTER.items():
        for standby_type, start, end, counts in rows:
            for rank, n in zip(("CPT", "FO", "SC", "CC"), counts, strict=True):
                for _ in range(n):
                    crew_id = hire(rank, base, qualification)
                    reserves.append((crew_id, base, start, end, standby_type))
    return tuple(crew), tuple(duties), tuple(reserves)


def _bookings(
    rng: random.Random, legs: list[_Leg], by_reg: dict[str, Aircraft]
) -> tuple[tuple[FlightInventory, ...], tuple[Pnr, ...]]:
    popular = {
        (base, dest) for base, routes in NARROW_ROUTES.items() for dest, _, pop in routes if pop
    } | {(HUB, dest) for dest in POPULAR_LONG_HAUL}
    seats = {leg.flight_no: by_reg[leg.registration].seats for leg in legs}
    pax: dict[str, int] = {}
    for leg in legs:
        route_popular = (leg.origin, leg.destination) in popular or (
            leg.destination,
            leg.origin,
        ) in popular
        pax[leg.flight_no] = min(
            seats[leg.flight_no],
            round(seats[leg.flight_no] * _load_factor(rng, route_popular, leg.std)),
        )
    remaining = dict(pax)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    locators: set[str] = set()

    def locator() -> str:
        while True:
            value = "".join(rng.choices(alphabet, k=6))
            if value not in locators:
                locators.add(value)
                return value

    def party() -> int:
        return rng.choices((1, 2, 3, 4), weights=(55, 28, 9, 8))[0]

    pnrs: list[Pnr] = []
    region = {iata: AIRPORTS[iata][2] for iata in AIRPORTS}
    outbound = [leg for leg in legs if leg.origin == HUB]
    for inbound in (leg for leg in legs if leg.destination == HUB and leg.sta < 1440):
        onward = [
            leg
            for leg in outbound
            if inbound.sta + MIN_CONNECTION <= leg.std <= inbound.sta + MAX_CONNECTION
            and leg.destination != inbound.origin
            and (
                region[leg.destination] != region[inbound.origin] or region[leg.destination] == "PT"
            )
        ]
        want = round(CONNECTING_SHARE * pax[inbound.flight_no])
        while want > 0 and onward:
            size = min(party(), want, remaining[inbound.flight_no])
            options = [leg for leg in onward if remaining[leg.flight_no] >= size]
            if size <= 0 or not options:
                break
            nxt = rng.choice(options)
            pnrs.append(Pnr(locator(), size, (inbound.flight_no, nxt.flight_no)))
            remaining[inbound.flight_no] -= size
            remaining[nxt.flight_no] -= size
            want -= size
    for leg in legs:
        while remaining[leg.flight_no] > 0:
            size = min(party(), remaining[leg.flight_no])
            pnrs.append(Pnr(locator(), size, (leg.flight_no,)))
            remaining[leg.flight_no] -= size
    demo_pnr = next(
        (index for index, pnr in enumerate(pnrs) if pnr.flight_nos == ("AR101",)),
        None,
    )
    if demo_pnr is None:
        raise RuntimeError("The seeded operating day has no single-flight AR101 booking")
    pnrs[demo_pnr] = replace(pnrs[demo_pnr], synthetic_recovery_opt_in=True)
    inventory = tuple(
        FlightInventory(
            leg.flight_no,
            seats[leg.flight_no],
            distance_km(leg.origin, leg.destination),
            pax[leg.flight_no],
        )
        for leg in legs
    )
    return inventory, tuple(pnrs)


def build_operating_day(day: date) -> OperatingDay:
    """The airline's operating day for ``day``: same pattern every day, only the date changes."""
    pattern = _pattern()
    midnight = datetime.combine(day, time())

    def at(minute: int) -> datetime:
        return midnight + timedelta(minutes=minute)

    flights = tuple(
        Flight(
            day,
            leg.flight_no,
            leg.origin,
            leg.destination,
            at(leg.std),
            at(leg.sta),
            leg.registration,
        )
        for leg in pattern.legs
    )
    slots = tuple(
        slot
        for leg in pattern.legs
        for slot in (
            Slot(leg.origin, leg.flight_no, "D", at(leg.std)),
            Slot(leg.destination, leg.flight_no, "A", at(leg.sta)),
        )
        if slot.airport in SLOT_CAPACITY
    )
    duties = tuple(
        Duty(
            d.duty_id,
            d.base,
            d.start_airport,
            d.qualification,
            at(d.report),
            at(d.release),
            d.flight_nos,
            d.fdp,
            d.max_fdp,
            d.pilots,
            d.cabin,
            d.members,
        )
        for d in pattern.duties
    )
    return OperatingDay(
        day=day,
        aircraft=pattern.aircraft,
        statuses=tuple(
            AircraftStatus(ac.registration, "serviceable", None) for ac in pattern.aircraft
        ),
        flights=flights,
        inventory=pattern.inventory,
        crew=pattern.crew,
        duties=duties,
        reserves=tuple(Reserve(c, b, at(s), at(e), t) for c, b, s, e, t in pattern.reserves),
        pnrs=pattern.pnrs,
        slot_capacity=dict(SLOT_CAPACITY),
        slots=slots,
    )
