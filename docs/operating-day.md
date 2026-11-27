# Operating day: facts, assumptions and measured counts

The five airline systems (Schedule, Maintenance, Crew, Booking and Slots) share one generated
operating day. `src/airline_systems/operating_day.py` builds it from a fixed seed (`20261106`), so
every day has the same flights, crews, bookings and slots. Only the date changes.
`ScheduleService.ensure_day` loads the day on the first request of each Lisbon day, and
`tools/systems/load_day.py` reloads it on demand. Each load replaces the previous day in all
five systems in one SQL transaction (`infra/systems/sql/002_operating_day.sql`).

## Scenario facts used

| Fact | Source |
|---|---|
| Lisbon-based airline, 900 daily flights | The scenario |

Everything below is an assumption made for a realistic fictional airline. It is not taken from TAP
or any other carrier's data.

## Assumptions

| Area | Assumption | Rationale |
|---|---|---|
| Times | Every time is a Lisbon local wall-clock time, also abroad. | Plan simplification: one clock for the demo. |
| Published times | Each flight keeps its published departure and arrival (`published_std_local`, `published_sta_local`) beside its current estimate (`std_local`, `sta_local`). The day load sets both; a retime moves only the estimate. Booking measures passengers' delay (EU261) from the published times. | A second disruption on an already-delayed rotation must not hide the delay passengers already have (review, 20 November; migration `013`). |
| Fleet | 180 aircraft in two families: 100 A320neo (174 seats), 50 A321neo (216 seats) and 30 A330-900neo (298 seats). Registrations: CS-TQ\*/TR\*/TS\*/TT\* (A320neo), CS-TJ\*/TK\* (A321neo), CS-TU\*/TV\* (A330neo). | A realistic hub carrier flies one or two families. Seat counts are typical single-airline configurations. |
| Bases | Narrow-body A320neo/A321neo: LIS 57/36, OPO 27/10, FAO 6/2, FNC 6/0, PDL 4/2. Every A330neo is based at LIS. | Hub-and-spoke with Portuguese outstations. |
| Utilisation | All 180 aircraft fly. There is no idle spare fleet; recovery has to use reserves, swaps and delays. | User direction: no invented unlimited spares. |
| Network | Narrow-body: 52 LIS routes plus point-to-point routes from OPO, FAO, FNC and PDL (domestic, islands, Europe, North and West Africa). Wide-body: one LIS round trip per A330neo (Brazil 13, North America 12, Africa 5). | A Lisbon hub with Portuguese, European, Brazilian, North American and African traffic. |
| Night stops | 24 LIS aircraft spend the night at an outstation. Each flies an evening LIS→X leg and an X→LIS morning feeder. | Feeds the morning bank without exceeding LIS slots at 06:00. |
| Block time | 30 min + distance / 750 km/h (≥ 3,000 km: / 828 km/h), rounded to 5 min. LIS–OPO is 55 min and LIS–FAO 45 min. The A320neo flies sectors of ≤ 4 h only. | Great-circle distance; winds ignored, so both directions take the same time. |
| Turnaround | Minimum 35 min narrow-body and 60 min wide-body. Each aircraft's next flight departs from its previous arrival airport. | Typical short-haul and long-haul minimum ground times. |
| Operating window | Departures 06:00–23:59. Narrow-body arrivals by 00:30. Wide-body overnight returns arrive the next morning. | Night-time noise restrictions at LIS. |
| Flight numbers | AR101/102/131/132 for the demo aircraft, AR201–260 wide-body, AR301 upward narrow-body (odd numbers outbound). | Unique per day. |
| Demo aircraft | CS-TQA (A320neo, LIS) flies exactly AR101 LIS-OPO 10:30–11:25, AR102 OPO-LIS 12:00–12:55, AR131 LIS-FAO 14:10–14:55 and AR132 FAO-LIS 15:35–16:20. | Plan: defect at 09:45, out of service 4 h. |
| Load factor | 100 % on popular routes in peak hours (06–08, 17–20 h). Otherwise uniformly 85–99 %. | User direction: popular flights run near full. |
| Bookings | Each PNR has a party size of 1–4 (55/28/9/8 %). Passengers on a flight equal the sum of its PNR segments and never exceed its seats. Exactly one single-flight AR101 PNR is marked with a demo-only recovery opt-in. | Disruption handling works per itinerary. The marker is not real passenger consent or a modeled airline policy. |
| Connections | Via LIS only, on the same day, 60 min minimum and 5 h maximum connection time. No return to the origin, and no same-region connection except within Portugal. Up to 35 % of an inbound hub flight's passengers are offered an onward connection. | LIS minimum connection time assumed to be 60 min. |
| Slots | One slot per departure and arrival at coordinated airports. Each airport has a declared hourly capacity for departures plus arrivals, counted per clock hour (after-midnight movements count in the early hours): LIS 40, OPO 24, FAO 20, FNC 12, PDL 10, plus 25 European and long-haul airports. | Treated as the airline's own allocation; other carriers are not modelled. |
| Crew complement | 2 pilots (CPT, FO), plus a third pilot (FO) when block time is over 10 h or the 2-pilot FDP limit is exceeded. Cabin crew: A320neo 4, A321neo 5, A330neo 9 (one SC, the rest CC). | Above the EASA 1-per-50-seats minimum. 3 pilots is a company policy for augmented long haul. |
| Crew qualification | Pilots and cabin crew are qualified on A320F (A320neo and A321neo) or A330. Each crew member has one base. | Common family type rating. |
| FDP limit | EASA ORO.FTL.205(b)(1) (Commission Regulation (EU) No 83/2014): 13 h for a report time of 06:00–13:29 and 1–2 sectors, by report-time band, minus 30 min per sector from the third, never below 9 h. An augmented crew (3 pilots, class 1 rest facility, CS FTL.1.205(c)) has 16 h. Report is 60 min before the first departure. FDP runs to the last arrival; release is 30 min after it. | Deterministic legality check outside the LLM. Crew are treated as acclimatised to Lisbon time. |
| Duties | Narrow-body: each aircraft day is split into duties that change crew only at the crew base. Wide-body: one duty per sector, with LIS crew laying over at the outstation. Each crew member works at most one duty a day. | Single-day model. Rest periods, days off and cumulative limits are not modelled. |
| Reserves (standby) | 84 crew on same-day standby (from 22 November; the 6 November count below had 82): LIS A320F 6/6/4/16 and A330 2/2/2/8; OPO 3/3/3/8; FAO, FNC and PDL 1/1/1/4 each (CPT/FO/SC/CC). Home standby for 8 h: LIS 06:00–14:00, 11:00–19:00 and 16:00–24:00, OPO 06:00–14:00 and 13:00–21:00, each island 08:00–16:00; one complete LIS A320F crew on airport standby 14:00–18:00; no one overnight. Each window holds a complete crew where the totals allow. Home standby answers a call in 60 min, then reports; airport standby reports at the call; report 60 min before the first departure (75 on the A330). A standby is called within its window, so a home standby may report up to 60 min after it ends and an airport standby reports inside it; the duty may end after the window. A crew that cannot report in time for its departure is not assigned. The maximum FDP falls by the standby time before report beyond 6 h (home) or 4 h (airport), not counting home standby hours between 23:00 and 07:00 before the call; airport standby plus FDP at most 16 h. A plan may call several standby crews, one per breached duty, nobody twice. | Research 2026-11-22 (EASA CS FTL.1.225, TAP's agreements). Not modelled: splitting flight deck and cabin, positioning from another base, the split-duty guard, cumulative and rest limits, the 8 h threshold with in-flight rest. |
| Maintenance | All aircraft are serviceable at the start of the day; return-to-service is empty. | The demo raises the CS-TQA defect at 09:45. |

## Measured counts

Results of `tools/systems/load_day.py` on 2026-11-06, read back from `airrec-ops`. The day was
built in 0.7 s and loaded in 9.7 s.

| Check | Result |
|---|---|
| Flights | 900 |
| Flights per fleet | A320neo 550, A321neo 290, A330neo 60 |
| Flights touching LIS | 64.2 % |
| LIS maximum movements per hour vs capacity | 40 / 40 |
| Airport-hours over declared capacity | 0 |
| Flights at coordinated airports without their slots | 0 |
| Passengers / seats | 165,051 / 176,220 (93.7 %) |
| Flights with passengers above seats | 0 |
| Connecting PNRs | 7,288 (12,300 passengers; 14.9 % of passenger-segments) |
| Flights without a legal crew | 0 |
| Duties / crew members on duty / reserves | 383 (10 with 3 pilots) / 2,715 / 82 |
| Serviceable aircraft | 180 / 180 |
| CS-TQA rotation | AR101 LIS-OPO 10:30–11:25, AR102 OPO-LIS 12:00–12:55, AR131 LIS-FAO 14:10–14:55, AR132 FAO-LIS 15:35–16:20 |

## Known limitations

- **LIS capacity.** The real Lisbon airport declares roughly 40 movements an hour for all airlines.
  Here 40 an hour is the airline's own allocation, because the case's 900 flights at a single hub
  carrier would not fit otherwise.
- **Connections.** Connecting passengers are 14.9 % of passenger-segments, which is low for a hub
  carrier. Onward seat availability caps the share: raising the parameter to 80 % gives only
  23 %. Overnight wide-body arrivals do not connect.
- **Crew.** The crew model covers a single day. It ignores rest before and after the day, days
  off, cumulative duty limits and acclimatisation abroad.
- **First request of a day.** Loading takes about 10 s and happens in the request that first
  sees the new date.

## How the systems check a plan

The sample systems check each candidate plan against the same operating-day snapshot. Crew
recalculates the single-day FDP of the affected duties from their recorded report time and offers
standby crews for a breached duty; Booking reports the passengers who lose their flight or
connection, who can be rebooked, who are kept by a car at Lisbon and who fall outside the
rebooking policy; Slots checks the proposed movements against the airline's hourly allocation and
may ask for a five-minute adjustment. ARROW's code then prices the plan, checks its rules and
routes it; see [the rules engine](../README.md#the-rules-engine). Crew rest, cumulative duty,
days off and acclimatisation remain outside the model. All counts and constraints are generated
scenario results, not measured airline outcomes.

A demo-only 15-minute seat hold for the marked AR101 demo party exists in the Booking system but
is switched off (`passenger_holds_enabled` is false), at the owner's request on 8 November.