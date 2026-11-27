# ADR 0029: A storm plan is held until the storm is confirmed

## Status

Accepted (D126).

The demo's weather case is now a crosswind on Lisbon's single runway (D129); the decision is
unchanged.

## Context

ARROW reads a Terminal Aerodrome Forecast (TAF) and plans a Lisbon storm across the
network (D122, D125). The approved plan then sent everything at once, including EUROCONTROL
messages, retimes and rebookings for a storm that might not come. The owner: "it does not
make sense to already update eurocontrol, i mean we dont even know if storm will hit that hard or
not". The early, reversible steps are where acting at a forecast pays: a home standby crew needs an
hour to answer and reports an hour before an A320 departure.

## Decision

- A weather plan's actions carry a stage. One OCC approval sends only the reversible part now:
  passengers on the exposed flights are warned of possible delays, and the plan's standby crews are
  alerted with call-by times. Holding-fuel and alternates advice is shown with the plan.
- Everything else waits: retimes, EUROCONTROL messages, standby calls, rebooking, cars and
  new-time notices.
- A METAR, an amended TAF or a EUROCONTROL regulation arrives as a typed weather update
  (`com.arrow.weather.update`, `confirmed` with the rate, or `dropped`) on the disruption topic. It
  never opens a case; code matches it to today's open storm case at that airport.
- Confirmed inside the planned window, at no lower rate, with every source re-check passing: the
  held actions are sent. Otherwise nothing more is sent and OCC decides.
- Dropped: the alerts are released, the warned passengers are told no change is expected and the
  case is closed "Stood down". The latest update decides until the held part is released.
- A standby crew whose call-by time passes while the storm is unconfirmed is called; nothing else
  is sent.

## Alternatives rejected

- **Send the whole plan on approval:** commits outside parties and passengers to a storm that may
  not come.
- **Wait for the storm before planning:** too late to bring standby crews in.
- **An "Act now" plan** that cancelled early, offered earlier flights, held hotel rooms and swapped
  slots: priced offline at EUR 329,219 with 611 passengers losing their flight or connection, against
  EUR 58,533 and none for delaying with standby crews; dropped by the owner (D125).
- **Reading a weather feed now:** a weather watch on TAF, METAR and SIGMET is a design only; the
  Test Bench sends the forecast and its updates.

## Consequences

- OCC approves once; the case shows what was sent, what is held and what confirms or stands it
  down.
- A worse confirmation, or a changed source, returns the case to OCC instead of sending a plan
  built for another storm.
- The held flow is tested offline end to end. It is not yet shown live: it needs the owner's
  approval of the storm plan. The Dev clock is fixed at 09:45, so the call-by call is tested offline
  only.
- No EU261 compensation is owed when weather is an extraordinary circumstance (recitals 14–15;
  C-74/19), which the carrier must still be able to prove; care and rebooking are still owed and
  priced.

## Evidence

- Decisions D122, D125 and D126 in the decision log;
  spec;
  evidence.
- Code: [`src/ops_app/actions.py`](../../../src/ops_app/actions.py) (stages),
  [`src/ops_app/executor.py`](../../../src/ops_app/executor.py) (held, released, stood down),
  [`src/ops_app/events.py`](../../../src/ops_app/events.py) (weather updates).
- Research: proactive storm recovery,
  Lisbon storm and ATC history.
