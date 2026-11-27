"use strict";

// Presentation-only helpers that turn case data into the Recovery view.
// State, API calls, polling, decisions and reports stay in app.js. Every value
// here is derived from the case record; nothing is ranked, priced or sent.
const RecoveryView = (() => {
  const ACTIVE = new Set(["assessing", "approved", "executing"]);
  const isCancelled = (flight) => /^(cancelled|canceled)$/i.test(String(flight?.status || "").trim());
  const assessment = (option, domain) => (Array.isArray(option?.domain_assessments)
    ? option.domain_assessments.find(item => item?.domain === domain) : undefined);
  const limits = (item) => Array.isArray(item?.limitations) ? item.limitations.join(" ") : "";

  function hhmm(value) {
    const match = String(value ?? "").match(/(?:T|^)(\d{1,2}):(\d{2})/);
    return match ? `${match[1].padStart(2, "0")}:${match[2]}` : null;
  }

  function visible(options) {
    return (options || []).filter(option => option && option.feasible !== false);
  }

  // ---------- A storm read from a TAF (Task 4): one case for the airport and no single aircraft ----------
  const HAZARDS = { thunderstorm: "Thunderstorms", crosswind: "Strong crosswind", strong_wind: "Strong wind", low_visibility: "Low visibility" };
  // The runway in use, for a crosswind drawn across it: Lisbon (LPPT) flies one runway, 02/20.
  const RUNWAYS = { LIS: "02/20" };
  // Its title, reading line, plain reading and window, from the case's structured fields (never from decoding the TAF);
  // null for any other disruption.
  function storm(disruption) {
    const d = isObj(disruption) ? disruption : {};
    if (d.type !== "weather") return null;
    const hazard = HAZARDS[d.hazard] || String(d.hazard || "Weather").replaceAll("_", " ");
    const place = AIRPORT_NAMES[d.airport] || String(d.airport || "the airport");
    const timed = Boolean(d.window_start && d.window_end);
    const window = timed ? `${d.window_start}–${d.window_end}` : "";
    const rate = isNum(d.hourly_rate) ? `${d.hourly_rate} movements an hour${isNum(d.normal_rate) ? ` (normally ${d.normal_rate})` : ""}` : "";
    // A crosswind's gusts across the runway, as ARROW's code computed them from the forecast's wind (never the model).
    const runway = String(d.runway || RUNWAYS[d.airport] || "");
    // "gusts" only when the wind gusts: code computes the crosswind from the gust then, else from the mean wind.
    const wind = /^(\d{3})\d{2,3}(G\d{2,3})?KT$/.exec(String(d.wind || ""));
    const across = isNum(d.crosswind_kt) ? `${wind?.[2] ? "gusts " : ""}${d.crosswind_kt} kt across ${runway ? `runway ${runway}` : "the runway"}` : "";
    const heading = Number((/^(\d{2})/.exec(runway) || [])[1]) * 10;
    const overlay = d.hazard === "crosswind" && Number.isFinite(heading) && runway
      ? { runway: heading, from: wind ? Number(wind[1]) : (heading + 270) % 360 } : null;
    // The rate's basis as ARROW records it (the data it was set on, in newer cases); an older case reads "assumption; no published
    // Lisbon weather rate" as "an assumption: no published Lisbon weather rate".
    const basis = String(d.rate_basis || "").replace(/^assumption;\s*/i, "an assumption: ");
    return {
      hazard, place, window, rate, basis, across, overlay,
      title: [`${hazard} at ${place}`, window].filter(Boolean).join(" · "),
      reading: ["Weather", place, window, across, rate].filter(Boolean).join(" · "),
      plain: `${hazard} forecast${timed ? ` between ${d.window_start} and ${d.window_end}` : ""}${across ? `, ${across}` : ""}${
        isNum(d.hourly_rate) ? `; ARROW plans ${place} at ${d.hourly_rate} movements an hour${basis ? `, ${basis}` : ""}` : ""}.`,
      forecast: String(d.message || d.description || "")
    };
  }
  // A storm's delay plan follows the reduced airport rate across many aircraft: never "Delay the rotation" (Task 4c).
  const REDUCED_RATE = /^Accept the reduced (.+?) rate\b/i;
  function delayName(option) {
    const reduced = REDUCED_RATE.exec(String(option?.title || ""));
    if (reduced) return `Accept the reduced ${reduced[1]} rate`;
    const aircraft = new Set((option?.flights || []).map(flight => flight?.aircraft_reg).filter(Boolean));
    return aircraft.size > 1 ? String(option?.title || "Delay the flights") : null;
  }

  function optionMode(item) {
    if (!item) return "pending";
    if (item.status === "no_disruption") return "none";
    if (item.status === "failed") return "stopped";
    const count = visible(item.options).length;
    if (!count) return ACTIVE.has(item.status) ? "pending" : "none";
    return count === 1 ? "single" : "compare";
  }

  function baseLabel(option) {
    if (option?.kind === "delay") return delayName(option) || "Delay rotation";
    const cancelled = (option?.flights || []).filter(isCancelled).map(flight => flight.flight_no).filter(Boolean);
    if (option?.kind === "cancel" && cancelled.length) return `Cancel ${cancelled.join(" + ")}`;
    return null;
  }

  function cardLabel(option, options) {
    const label = baseLabel(option);
    const title = String(option?.title || "Untitled option");
    if (!label) return title;
    const shared = (options || []).filter(other => baseLabel(other) === label).length > 1;
    return shared ? title : label;
  }

  function subtitle(option) {
    const flights = option?.flights || [];
    if (option?.kind === "cancel") {
      const count = flights.filter(isCancelled).length;
      return count ? `${count} cancelled` : "";
    }
    const first = flights.filter(flight => !isCancelled(flight)).map(flight => hhmm(flight.std_local)).filter(Boolean).sort()[0];
    return option?.kind === "delay" && first ? `First departure ${first}` : "";
  }

  function crewCheck(option) {
    const crew = assessment(option, "crew");
    const check = { key: "crew", label: "Crew FDP", state: "unknown", value: "unknown",
      detail: limits(crew) || "Crew duty was not assessed for this option." };
    // A plan's standby crews replace the duties that would breach; the check reads as covered only when ARROW's crew
    // plan confirms no breach is left.
    const crews = standbyCrews(option);
    if (crews.length && option?.validation?.crew_plan?.breaches_unresolved === 0) {
      const breaches = (Array.isArray(crew?.duties) ? crew.duties : []).filter(duty => duty?.status === "fail");
      const one = crews.length === 1 && isNum(crews[0].fdp) && isNum(crews[0].max) ? ` · ${crews[0].fdp} / ${crews[0].max} min` : "";
      return { ...check, state: "adjusted", value: `${standbyName(crews)}${one}`,
        detail: `${breaches.map(breach => `Duty ${breach.duty_id} would exceed its modeled limit (${breach.modeled_fdp_minutes} / ${
          breach.modeled_fdp_limit_minutes} min). `).join("")}${crews.map(entry => `${cap(standbyName([entry]))} (${standbyLine(entry).split(" · ").slice(1).join(", ")}) operates ${
          entry.flights.join(", ") || "the affected flights"}.`).join(" ")} ${limits(crew)}`.trim() };
    }
    if (crew?.constraint_status === "fail") return { ...check, state: "disrupts", value: "exceeds limit" };
    if (crew?.constraint_status !== "pass") return check;
    const duties = (Array.isArray(crew.duties) ? crew.duties : []).filter(duty =>
      Number.isFinite(duty?.modeled_fdp_minutes) && Number.isFinite(duty?.modeled_fdp_limit_minutes) &&
      duty.modeled_fdp_limit_minutes > 0);
    const tightest = duties.sort((a, b) => b.modeled_fdp_minutes / b.modeled_fdp_limit_minutes -
      a.modeled_fdp_minutes / a.modeled_fdp_limit_minutes)[0];
    return { ...check, state: "fits",
      value: tightest ? `${tightest.modeled_fdp_minutes} / ${tightest.modeled_fdp_limit_minutes} min` : "within model" };
  }

  // ---------- Standby crews (EASA standby, several per plan) ----------
  // The standby crews a plan calls: crew_reserves and levers.reserve_option_ids since Task 2; plans built before carry one
  // (crew_reserve, levers.reserve_option_id). Each with its id, home or airport standby, the latest time crew control calls
  // it, its report time and the flights it takes.
  const present = (value) => value !== null && value !== undefined && value !== "";
  const cap = (text) => { const value = String(text ?? ""); return value ? value[0].toUpperCase() + value.slice(1) : value; };
  function standbyCrews(option) {
    const listed = Array.isArray(option?.crew_reserves) ? option.crew_reserves.filter(isObj) : isObj(option?.crew_reserve) ? [option.crew_reserve] : [];
    const levers = isObj(option?.levers) ? option.levers : {};
    const ids = Array.isArray(levers.reserve_option_ids) ? levers.reserve_option_ids.filter(present).map(String)
      : present(levers.reserve_option_id) ? [String(levers.reserve_option_id)] : [];
    const crews = listed.map(entry => ({ id: String(entry.reserve_option_id ?? ""),
      type: entry.standby_type === "airport" || entry.standby_type === "home" ? entry.standby_type : "",
      call: hhmm(entry.called_local), report: hhmm(entry.report_local), duty: String(entry.duty_id || ""),
      flights: Array.isArray(entry.flights_for_reserve) ? entry.flights_for_reserve.map(String) : [],
      fdp: entry.fdp_minutes, max: entry.max_fdp_minutes }));
    for (const id of ids) if (!crews.some(entry => entry.id === id)) crews.push({ id, type: "", call: null, report: null, duty: "", flights: [] });
    return crews.filter(entry => entry.id);
  }
  // Crew control calls a standby crew well before it reports (it knows at the report of the disruption): "R1 · home standby ·
  // call by 15:54, report 16:54", never as if called at the last minute.
  function standbyLine(entry) {
    const times = entry.call && entry.report ? `call by ${entry.call}, report ${entry.report}` : entry.report ? `report ${entry.report}` : "";
    return [entry.id, entry.type ? `${entry.type} standby` : "standby", times].filter(Boolean).join(" · ");
  }
  // "standby crews R1, R2", "standby crew R1".
  const standbyName = (crews) => `standby crew${crews.length === 1 ? "" : "s"} ${crews.map(entry => entry.id).join(", ")}`;
  // The complete standby crews left today after the plan, at the bases its standby crews come from (validation.crew_plan.standby_left
  // filtered by crew_reserves' base and qualification; final review): "Ponta Delgada A320: 0 standby crews left". Nothing
  // when the plan calls no standby crew; every base only for an older plan whose crews carry no base.
  const QUALIFICATION = { A320F: "A320" };
  function standbyLeft(option) {
    const plan = option?.validation?.crew_plan;
    const left = Array.isArray(plan?.standby_left) ? plan.standby_left.filter(isObj) : [];
    const called = Array.isArray(option?.crew_reserves) ? option.crew_reserves.filter(isObj) : isObj(option?.crew_reserve) ? [option.crew_reserve] : [];
    if (!called.length) return [];
    const bases = called.every(entry => present(entry.base)) ? new Set(called.map(entry => `${entry.base}|${entry.qualification}`)) : null;
    return left.filter(entry => isNum(entry.crews) && (!bases || bases.has(`${entry.base}|${entry.qualification}`)))
      .map(entry => `${AIRPORT_NAMES[entry.base] || entry.base} ${QUALIFICATION[entry.qualification] || entry.qualification}: ${count(entry.crews, "standby crew")} left`);
  }

  function slotCheck(option) {
    const slots = assessment(option, "slots");
    const check = { key: "slot", label: "LIS capacity", state: "unknown", value: "unknown",
      detail: limits(slots) || "Airport capacity was not assessed for this option. Not an allocated slot or CTOT." };
    const extra = slots?.required_additional_delay_minutes;
    if (Number.isFinite(extra) && extra > 0) return { ...check, state: "adjusted", value: `+${extra} min` };
    if (slots?.capacity_fit === true) return { ...check, state: "fits", value: "fits" };
    if (slots?.capacity_fit === false) return { ...check, state: "disrupts", value: "does not fit" };
    return check;
  }

  function passengers(option) {
    const booking = assessment(option, "booking");
    // Orchestrated plans carry ARROW's own count in validation.rebooking.
    const disrupted = Number.isFinite(option?.misconnected_passengers) ? option.misconnected_passengers
      : option?.validation?.rebooking?.disrupted_passengers;
    const pnrs = booking?.disrupted_booking_count;
    const known = Number.isFinite(disrupted);
    return {
      figure: known ? String(disrupted) : "—",
      note: known && disrupted > 0 && Number.isFinite(pnrs) ? `${pnrs} PNR${pnrs === 1 ? "" : "s"}` : "",
      detail: `Cancelled or missed-connection passengers; delay-only passengers are excluded.${
        known ? "" : " Not assessed for this option."} ${limits(booking)}`.trim()
    };
  }

  const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const totalCost = (option) => Number.isFinite(option?.cost?.total_eur) ? option.cost.total_eur : null;

  function costCheck(option) {
    const total = totalCost(option);
    if (total === null) {
      return { key: "cost", label: "Recovery cost", state: "unknown", value: "unknown",
        detail: "EU261, care and operating cost not established." };
    }
    const cost = option.cost;
    // Bottom-up estimate (passenger cost spec): airline cost, compensation per booking, care and rebooking.
    if (Number.isFinite(cost.airline_eur)) {
      const counts = [Number(cost.meals_passengers) > 0 ? `${plain.format(cost.meals_passengers)} with meals` : "",
        Number(cost.overnight_passengers) > 0 ? `${plain.format(cost.overnight_passengers)} overnight` : ""].filter(Boolean).join(", ");
      const range = Number.isFinite(cost.low_eur) && Number.isFinite(cost.high_eur) && cost.low_eur < cost.high_eur
        ? ` · ${euro.format(cost.low_eur)}–${euro.format(cost.high_eur)} with hotel season` : "";
      const rebooking = Number.isFinite(cost.rebooking_eur) ? ` · Rebooking ${euro.format(cost.rebooking_eur)}` : "";
      return { key: "cost", label: "Cost", state: "estimate", value: `indicative estimate ${euro.format(total)}`,
        detail: `Airline ${euro.format(cost.airline_eur)} · Compensation ${euro.format(cost.compensation_eur)} · Care ${euro.format(cost.care_eur)}${
          counts ? ` (${counts})` : ""}${rebooking}${range} · indicative, not a quote` };
    }
    // Plans stored before it: delay cost plus compensation.
    const { delay_eur: delay, compensation_eur: compensation, basis } = cost;
    const parts = [Number.isFinite(delay) ? `delay ${euro.format(delay)}` : null,
      Number.isFinite(compensation) ? `EU261 compensation ${euro.format(compensation)}` : null].filter(Boolean);
    return { key: "cost", label: "Cost", state: "estimate", value: `indicative estimate ${euro.format(total)}`,
      detail: `${parts.join(" + ")}${parts.length ? ". " : ""}Basis: ${basis || "not recorded"}. Excludes care, hotels and lost revenue; not a quote.` };
  }

  // The orchestrator may recommend a plan other than the cheapest unblocked one; the card says so.
  function notCheapest(option, options) {
    const costs = (options || []).filter(other => other?.policy?.route !== "blocked").map(totalCost)
      .filter(value => value !== null);
    return totalCost(option) !== null && costs.length > 1 && totalCost(option) > Math.min(...costs);
  }

  // ---------- How a plan's cost adds up (owner: "how do we come up with 6,894 eur") ----------
  const plain = new Intl.NumberFormat("en-GB");
  const cents = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", minimumFractionDigits: 2 });
  const isNum = (value) => typeof value === "number" && Number.isFinite(value);
  const isObj = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const count = (n, word) => `${plain.format(n)} ${word}${n === 1 ? "" : "s"}`;
  // More flights than this and a plan reads as numbers, its flights as a list (Task 4c); up to this, its rotation.
  const MANY_FLIGHTS = 6;
  const BODY = { narrow: "narrow-body", wide: "wide-body" };

  function airlineWorkings(airline, total) {
    const flights = (Array.isArray(airline?.flights) ? airline.flights : []).filter(isObj);
    const fuel = (Array.isArray(airline?.fuel) ? airline.fuel : []).filter(isObj).filter(line => isNum(line.made_up_minutes) && isNum(line.eur));
    // Since Task 4: minutes a flight holds in the air before landing (a storm's arrival slot), priced as fuel and CO2.
    const holding = (Array.isArray(airline?.holding) ? airline.holding : []).filter(isObj).filter(line => isNum(line.held_minutes) && isNum(line.eur));
    const rate = airline?.rate_eur_per_minute;
    const late = flights.filter(flight => !flight.cancelled && isNum(flight.delay_minutes));
    const cancelled = flights.filter(flight => flight.cancelled);
    const summary = [];
    const sizes = new Map();
    for (const flight of cancelled) {
      const key = `${flight.aircraft}|${flight.eur}`;
      sizes.set(key, { aircraft: flight.aircraft, eur: flight.eur, n: (sizes.get(key)?.n || 0) + 1 });
    }
    const cancelling = cancelled.reduce((sum, flight) => sum + (isNum(flight.eur) ? flight.eur : 0), 0);
    const fuelling = fuel.reduce((sum, line) => sum + line.eur, 0);
    const held = holding.reduce((sum, line) => sum + line.eur, 0);
    // Plans priced by earlier versions hold the fuel alone in eur; newer ones its fuel and CO2 parts.
    const co2Of = (line) => (isNum(line.co2_eur) ? line.co2_eur : 0);
    const fuelOf = (line) => (isNum(line.fuel_eur) ? line.fuel_eur : line.eur - co2Of(line));
    if (late.length && isNum(rate)) {
      const minutes = late.reduce((sum, flight) => sum + flight.delay_minutes, 0);
      // Per-flight euros are rounded; the delays' share is the airline total less the rest.
      summary.push(`${count(late.length, "flight")}, ${plain.format(minutes)} min late in total × ${cents.format(rate)} a minute (EUROCONTROL) = ${
        euro.format(Math.round(total - cancelling - fuelling - held))}`);
    }
    if (fuel.length) {
      const minutes = fuel.reduce((sum, line) => sum + line.made_up_minutes, 0);
      const co2 = fuel.reduce((sum, line) => sum + co2Of(line), 0);
      // ARROW now words the speed-up itself, each flight's minutes named, and says why no more (owner: "14 minutes faster
      // only, that's weird"); plans priced by earlier versions keep this line.
      const said = typeof airline?.fuel_summary === "string" && airline.fuel_summary.trim() ? airline.fuel_summary.trim() : "";
      summary.push(said || `${count(fuel.length, "flight")} ${fuel.length === 1 ? "flies" : "fly"} faster, making up ${plain.format(minutes)} min in the air: fuel ${
        euro.format(Math.round(fuelling - co2))}${co2 > 0 ? ` + CO2 ${euro.format(Math.round(co2))} = ${euro.format(Math.round(fuelling))}` : ""}`);
      if (typeof airline?.fuel_basis === "string" && airline.fuel_basis.trim()) summary.push(airline.fuel_basis.trim());
    }
    if (holding.length) {
      const minutes = holding.reduce((sum, line) => sum + line.held_minutes, 0);
      const co2 = holding.reduce((sum, line) => sum + co2Of(line), 0);
      summary.push(`${count(holding.length, "flight")} ${holding.length === 1 ? "holds" : "hold"} ${plain.format(minutes)} min in the air before landing: fuel ${
        euro.format(Math.round(held - co2))}${co2 > 0 ? ` + CO2 ${euro.format(Math.round(co2))} = ${euro.format(Math.round(held))}` : ""}`);
    }
    for (const { aircraft, eur, n } of sizes.values()) {
      summary.push(`${count(n, `${BODY[aircraft] || "aircraft"} cancellation`)}${isNum(eur) ? ` × ${euro.format(eur)} (EUROCONTROL) = ${euro.format(n * eur)}` : ""}`);
    }
    if (!summary.length) summary.push("No flight later or cancelled");
    // "AR444 makes up 4 of 5 min in the air · fuel €… + CO2 €… · why" (a plan is a proposal): the minutes it could make up are its speed-up position.
    const fasterLine = (line) => `${line.flight_no || "Flight"} makes up ${plain.format(line.made_up_minutes)}${
      isNum(line.made_up_possible) ? ` of ${plain.format(line.made_up_possible)}` : ""} min in the air · fuel ${euro.format(fuelOf(line))}${
      co2Of(line) > 0 ? ` + CO2 ${euro.format(co2Of(line))}` : ""}${line.reason ? ` · ${line.reason}` : ""}`;
    // "AR864 holds 6 min in the air before landing · fuel €512 + CO2 €106 · fuel at cruise burn (conservative)".
    const holdLine = (line) => `${line.flight_no || "Flight"} holds ${plain.format(line.held_minutes)} min in the air before landing · fuel ${euro.format(fuelOf(line))}${
      co2Of(line) > 0 ? ` + CO2 ${euro.format(co2Of(line))}` : ""}${line.basis ? ` · ${line.basis}` : ""}`;
    const faster = new Map(fuel.map(line => [line.flight_no, line]));
    const holds = new Map(holding.map(line => [line.flight_no, line]));
    const each = flights.flatMap(flight => [`${flight.flight_no || "Flight"} ${flight.cancelled ? `cancelled${BODY[flight.aircraft] ? `, ${BODY[flight.aircraft]}` : ""}`
      : isNum(flight.delay_minutes) ? `+${flight.delay_minutes} min` : ""}${isNum(flight.eur) ? ` · ${euro.format(flight.eur)}` : ""}`,
    ...(faster.has(flight.flight_no) ? [fasterLine(faster.get(flight.flight_no))] : []), ...(holds.has(flight.flight_no) ? [holdLine(holds.get(flight.flight_no))] : [])]);
    const listed = new Set(flights.map(flight => flight.flight_no));
    each.push(...fuel.filter(line => !listed.has(line.flight_no)).map(fasterLine), ...holding.filter(line => !listed.has(line.flight_no)).map(holdLine));
    // A plan moving many flights (a storm) keeps its workings to the totals and the minutes held in the air; each flight's
    // minutes are in Show the flights.
    return { summary, each: flights.length > MANY_FLIGHTS ? holding.map(holdLine) : each };
  }

  function compensationWorkings(list) {
    const lines = (Array.isArray(list) ? list : []).filter(isObj).filter(line => isNum(line.passengers) && isNum(line.eur_each))
      .map(line => `${count(line.passengers, "passenger")} × ${euro.format(line.eur_each)}${line.why ? ` · ${line.why}` : ""}`);
    return lines.length ? lines : ["No passenger is owed compensation"];
  }
  // A storm's plan owes no EU261 compensation (weather is an extraordinary circumstance); its workings say what
  // would otherwise have been owed (cost.workings.compensation_not_owed), never counted in the cost. Null for any other plan.
  const NOT_OWED = "Not owed · weather is an extraordinary circumstance";
  function notOwed(option) {
    const entry = option?.cost?.workings?.compensation_not_owed;
    return isObj(entry) && isNum(entry.passengers) && isNum(entry.eur) ? entry : null;
  }
  const notOwedLines = (entry) => [NOT_OWED, `${count(entry.passengers, "passenger")} · ${euro.format(entry.eur)} if it were owed`, "Care and rebooking still owed"];

  function rebookingWorkings(list) {
    const lines = (Array.isArray(list) ? list : []).filter(isObj).filter(line => isNum(line.passengers) && isNum(line.eur_each))
      .map(line => `${count(line.passengers, "passenger")} × ${euro.format(line.eur_each)}${line.why ? ` · ${line.why}` : ""}`);
    return lines.length ? lines : ["No passenger is rebooked"];
  }

  function careWorkings(workings, cost) {
    const lines = (Array.isArray(workings.meals) ? workings.meals : []).filter(isObj).filter(line => isNum(line.passengers) && isNum(line.eur_each))
      .map(line => `Meals: ${plain.format(line.passengers)} × ${euro.format(line.eur_each)}${line.why ? ` · ${line.why}` : ""}`);
    const night = isObj(workings.overnight) ? workings.overnight : null;
    if (night) {
      lines.push(`Overnight: ${count(night.passengers, "passenger")}, ${count(night.rooms, "room")} × ${euro.format(night.room_eur)}, ${
        count(night.taxis, "taxi")} × ${euro.format(night.taxi_eur)}, meals ${plain.format(night.passengers)} × ${euro.format(night.meals_eur_each)}`);
    }
    if (!lines.length) return ["No passenger is owed care"];
    const care = isObj(cost.care) ? cost.care : {};
    if (night && [care.meals_eur, care.hotel_eur, care.taxi_eur].every(isNum)) {
      lines.push(`Meals ${euro.format(care.meals_eur)} · hotels ${euro.format(care.hotel_eur)} · taxis ${euro.format(care.taxi_eur)}`);
    }
    return lines;
  }

  // The parts of a plan's indicative cost, each with how it was reached. Plans priced before the
  // workings were recorded show their parts and basis; plans with only a total show no parts.
  function costParts(option) {
    const cost = option?.cost;
    if (!isObj(cost) || totalCost(option) === null) return [];
    const workings = isObj(cost.workings) ? cost.workings : null;
    const parts = [];
    if (isNum(cost.airline_eur)) {
      const airline = workings ? airlineWorkings(workings.airline, cost.airline_eur) : null;
      const holds = Array.isArray(workings?.airline?.holding) && workings.airline.holding.length > 0;
      parts.push({ key: "airline", label: "Airline", eur: cost.airline_eur, summary: airline?.summary || [], lines: airline ? [...airline.summary, ...airline.each] : [],
        note: `The airline's own cost of delays and cancellations at EUROCONTROL's published rates, and the fuel and CO2 allowances (EU Emissions Trading System) a late flight buys to make up time in the air, only when the minutes are worth more${
          holds ? ", or burns holding in the air before landing" : ""}.` });
      const excused = notOwed(option);
      const paid = excused ? notOwedLines(excused) : workings ? compensationWorkings(workings.compensation) : [];
      // A storm priced by an earlier version kept compensation as owed, with its weather note; said only where compensation is owed.
      if (!excused && workings?.note && cost.compensation_eur > 0) paid.push(String(workings.note));
      parts.push({ key: "compensation", label: "EU261 compensation", eur: cost.compensation_eur, summary: paid, lines: paid,
        note: excused ? "EU261: the EU air passenger rights regulation. Weather is an extraordinary circumstance (Art 5(3)): no compensation is owed; care and rebooking are."
          : "EU261: the EU air passenger rights regulation. Owed per passenger, by distance and arrival delay, whichever plan runs." });
      const cared = workings ? careWorkings(workings, cost) : [];
      parts.push({ key: "care", label: "Passenger care", eur: cost.care_eur, summary: cared, lines: cared,
        note: "Meals while passengers wait, and a hotel, taxis and meals for those who stay overnight." });
      // Plans priced by earlier versions have no rebooking part.
      if (isNum(cost.rebooking_eur)) {
        const moved = workings ? rebookingWorkings(workings.rebooking) : [];
        parts.push({ key: "rebooking", label: "Rebooking", eur: cost.rebooking_eur, summary: moved, lines: moved,
          note: "Rebooking on our own later flights: a sourced assumption, low confidence. Getting passengers we cannot place to their destination on another airline: derived (the average fare, a third at double), low-medium confidence. Both Steer for the European Commission, 2020, at 2018 prices." });
      }
    } else {
      if (isNum(cost.delay_eur)) parts.push({ key: "delay", label: "Delay", eur: cost.delay_eur, summary: [], lines: [], note: "" });
      if (isNum(cost.compensation_eur)) {
        parts.push({ key: "compensation", label: "EU261 compensation", eur: cost.compensation_eur, summary: [], lines: [],
          note: "EU261: the EU air passenger rights regulation." });
      }
    }
    return parts.filter(part => isNum(part.eur));
  }

  // Hover text for one part, or for the whole cost (part null): the workings when recorded, else
  // the plan's own breakdown and basis.
  function costTip(option, part = null) {
    const cost = option?.cost;
    if (totalCost(option) === null) return costCheck(option).detail;
    const parts = costParts(option);
    const recorded = isObj(cost.workings);
    const older = [costCheck(option).detail, Number.isFinite(cost.airline_eur) && cost.basis ? `Basis: ${cost.basis}` : ""].filter(Boolean).join("\n");
    if (part) return recorded ? [...part.lines, part.note].filter(Boolean).join("\n") : [part.note, older].filter(Boolean).join("\n");
    if (!recorded) return older;
    const range = isNum(cost.low_eur) && isNum(cost.high_eur) && cost.low_eur < cost.high_eur
      ? ` · ${euro.format(cost.low_eur)}–${euro.format(cost.high_eur)} with hotel season` : "";
    return [...parts.flatMap(entry => [`${entry.label} ${euro.format(entry.eur)}`, ...entry.summary.map(line => `• ${line}`)]),
      `Indicative, not a quote${range}`].join("\n");
  }

  // ---------- Why one plan over another, from the plans' own figures (owner) ----------
  // Passengers who lose their flight or connection: ARROW's own count first, then the agents' (as on the plan cards).
  function disrupted(option) {
    const rebooking = option?.validation?.rebooking || {};
    if (isNum(rebooking.disrupted_passengers)) return rebooking.disrupted_passengers;
    return isNum(option?.misconnected_passengers) ? option.misconnected_passengers : null;
  }
  // Passengers owed EU261 compensation: many keep their flight but arrive 3 h or more late.
  const owed = (option) => (isNum(option?.cost?.owed_compensation_passengers) ? option.cost.owed_compensation_passengers : null);
  const LOSE = "lose their flight or connection";
  const cancelledFlights = (option) => (option?.flights || []).filter(isCancelled).map(flight => flight.flight_no).filter(Boolean);
  const listed = (items) => items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
  // Flights by number up to three, then as a count (a storm keeps dozens on time).
  const flightsNamed = (items) => (items.length > 3 ? count(items.length, "flight") : listed(items));
  // How late each flight a plan keeps flying leaves: from the schedule when known, else from the plan's
  // cost workings, which list every late flight (one not listed leaves on time); null when neither says.
  function lateMinutes(option, originals) {
    const lines = option?.cost?.workings?.airline?.flights;
    const priced = Array.isArray(lines) ? new Map(lines.filter(isObj).map(line => [line.flight_no, line])) : null;
    return (option?.flights || []).filter(flight => flight?.flight_no && !isCancelled(flight)).map(flight => {
      const known = lateBy(originals?.get?.(flight.flight_no)?.std_local, flight.std_local);
      if (isNum(known)) return { flight: flight.flight_no, minutes: known };
      if (priced) {
        const minutes = priced.get(flight.flight_no)?.delay_minutes;
        return { flight: flight.flight_no, minutes: isNum(minutes) ? minutes : 0 };
      }
      return { flight: flight.flight_no, minutes: null };
    });
  }
  // The flights a plan makes late. Unknown lateness counts as late, except on a swap, which may list a
  // flight only for its new aircraft.
  const isLate = (option, entry) => (entry.minutes === null ? option?.kind !== "swap" : entry.minutes > 0);
  function lateFlights(option, originals) {
    return lateMinutes(option, originals).filter(entry => isLate(option, entry)).map(entry => entry.flight);
  }
  // A plan moving or cancelling more than six flights (a storm) reads as numbers on its card (Task 4c): "81 flights later · up to
  // 80 min", "4 flights cancelled · 75 later"; null for up to six flights, which keep their rotation.
  function manyFlights(option, originals) {
    const flights = Array.isArray(option?.flights) ? option.flights : [];
    if (flights.length <= MANY_FLIGHTS) return null;
    const cancelled = flights.filter(isCancelled).length;
    const late = lateMinutes(option, originals).filter(entry => isLate(option, entry));
    const minutes = late.map(entry => entry.minutes).filter(isNum);
    const most = minutes.length === late.length && minutes.length ? Math.max(...minutes) : null;
    const flightsWord = (n) => (n === 1 ? "flight" : "flights");
    const counts = { cancelled, later: late.length, most };
    return cancelled
      ? { ...counts, figure: cancelled, words: `${flightsWord(cancelled)} cancelled${late.length ? ` · ${plain.format(late.length)} later` : ""}` }
      : { ...counts, figure: late.length, words: `${flightsWord(late.length)} later${most ? ` · up to ${plain.format(most)} min` : ""}` };
  }
  // Every flight of a plan by scheduled departure, for its Flights table: route, departure (scheduled → planned) and change.
  // Without the schedule at hand, a late flight's scheduled time comes from the plan's cost workings (they list every late
  // flight's minutes); a flight neither names reads as it is planned.
  const earlier = (value, minutes) => {
    const match = /^(.*T)(\d{2}):(\d{2})(.*)$/.exec(String(value || ""));
    if (!match) return value;
    const total = ((Number(match[2]) * 60 + Number(match[3]) - minutes) % 1440 + 1440) % 1440;
    return `${match[1]}${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}${match[4]}`;
  };
  function flightRows(option, originals) {
    const priced = new Map(lateMinutes(option, originals).map(entry => [entry.flight, entry.minutes]));
    return (Array.isArray(option?.flights) ? option.flights : []).filter(isObj).map(flight => {
      const original = originals?.get?.(flight.flight_no);
      const cancelled = isCancelled(flight);
      const late = !original && !cancelled ? priced.get(flight.flight_no) : null;
      const was = original?.std_local || (isNum(late) && late > 0 ? earlier(flight.std_local, late) : flight.std_local);
      const minutes = cancelled ? null : original ? lateBy(was, flight.std_local) : isNum(late) ? late : null;
      // Held in the air for an arrival slot (a storm): it lands late even when it leaves on time.
      const held = !cancelled && isNum(flight.held_airborne_minutes) && flight.held_airborne_minutes > 0 ? flight.held_airborne_minutes : 0;
      const departs = isNum(minutes) && minutes ? `${minutes > 0 ? "+" : "−"}${Math.abs(minutes)} min` : "";
      return { flight: String(flight.flight_no || ""), route: [original?.origin || flight.origin, original?.destination || flight.destination].filter(Boolean).join(" → "),
        departs: String(was || ""), was: hhmm(was) || "", now: cancelled || !minutes ? "" : hhmm(flight.std_local) || "",
        change: cancelled ? "Cancelled" : held ? (departs ? `${departs} · held ${held} min in the air` : `Lands ${held} min late · held in the air`)
          : isNum(minutes) ? departs || "On time" : "" };
    }).sort((a, b) => a.departs.localeCompare(b.departs) || a.flight.localeCompare(b.flight));
  }
  // A plan's flights in a few words for its Flights row: "2 cancelled", "4 later · up to +3 h 25 min", "1 on time".
  function flightsSummary(option, originals) {
    const flights = Array.isArray(option?.flights) ? option.flights : [];
    if (!flights.length) return "";
    const cancelled = flights.filter(isCancelled).length;
    const timed = lateMinutes(option, originals);
    const late = timed.filter(entry => isLate(option, entry));
    const minutes = late.map(entry => entry.minutes).filter(isNum);
    const most = minutes.length === late.length && minutes.length ? Math.max(...minutes) : null;
    const onTime = timed.length - late.length;
    return [cancelled ? `${plain.format(cancelled)} cancelled` : "", late.length ? `${plain.format(late.length)} later${most ? ` · up to +${span(most)}` : ""}` : "",
      onTime ? `${plain.format(onTime)} on time` : ""].filter(Boolean).join(" · ");
  }

  // What option does against other, each fact marked better or worse for option: passengers
  // disrupted, cost, cancellations and the flights other makes late that option keeps on time (a
  // flight absent from a plan's flights is unchanged by it). originals: flight_no → scheduled flight.
  function contrast(option, other, originals = null) {
    const facts = [];
    const mine = new Map((option?.flights || []).map(flight => [flight.flight_no, flight]));
    const ownLate = new Map(lateMinutes(option, originals).map(entry => [entry.flight, entry.minutes]));
    const kept = lateFlights(other, originals).filter(flight => {
      const ours = mine.get(flight);
      if (!ours) return true;
      return !isCancelled(ours) && ownLate.get(flight) === 0;
    });
    if (kept.length) facts.push({ key: "kept", better: true, text: `keeps ${flightsNamed(kept)} on time`, flights: kept });
    const [a, b] = [disrupted(option), disrupted(other)];
    if (a !== null && b !== null && a !== b) {
      facts.push({ key: "passengers", better: a < b, text: `${plain.format(Math.abs(a - b))} ${a < b ? "fewer" : "more"} ${LOSE}` });
    }
    const [o, p] = [owed(option), owed(other)];
    if (o !== null && p !== null && o !== p) {
      facts.push({ key: "owed", better: o < p, text: `${plain.format(Math.abs(o - p))} ${o < p ? "fewer" : "more"} owed compensation` });
    }
    const [x, y] = [totalCost(option), totalCost(other)];
    if (x !== null && y !== null && x !== y) facts.push({ key: "cost", better: x < y, text: `${euro.format(Math.abs(x - y))} ${x < y ? "cheaper" : "more"}` });
    const [own, theirs] = [cancelledFlights(option), cancelledFlights(other)];
    if (!own.length && theirs.length) facts.push({ key: "cancelled", better: true, text: "no cancellations" });
    else if (own.length && !theirs.length) facts.push({ key: "cancelled", better: false, text: `cancels ${listed(own)}` });
    else if (own.length !== theirs.length) {
      facts.push({ key: "cancelled", better: own.length < theirs.length, text: `${count(own.length, "cancellation")} instead of ${theirs.length}` });
    }
    return facts;
  }

  // The first of ARROW's objectives on which the recommended plan is worse than another plan the
  // rules allow, or null. "Protect connections" has no separate figure, so it is not compared; an
  // objective either plan cannot answer stops the comparison rather than skipping to cost.
  function against(option, others) {
    for (const other of others || []) {
      if (other === option || other?.policy?.route === "blocked") continue;
      if (option?.policy?.route === "blocked") return "safe and legal";
      const [a, b] = [disrupted(option), disrupted(other)];
      if (a === null || b === null) continue;
      if (a > b) return "passengers who lose their flight or connection";
      if (a < b) continue;
      const [x, y] = [totalCost(option), totalCost(other)];
      if (x !== null && y !== null && x > y) return "cost";
    }
    return null;
  }

  // ---------- The recommendation on top of the decision (owner: "Recommendation plan 1, …") ----------
  // Every line comes from the plans' own figures: passengers disrupted, the cost and its parts, and the
  // flights each plan cancels, makes late or keeps on time. Nothing here is ranked, priced or invented.
  const signedEuro = (n) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${euro.format(Math.abs(n))}`;
  const lowerFirst = (text) => (/^[A-Z][a-z]/.test(text) ? text[0].toLowerCase() + text.slice(1) : text);
  // On a phone a plan's name and a duration wrap as one ("Cancel AR941 + AR942", "2 h 55 min").
  const keep = (text) => String(text).replace(/ \+ /g, "\u00a0+\u00a0");
  const together = (text) => String(text).replace(/ /g, "\u00a0");

  // A blocked plan's first reason, short and in plain words.
  function blockWords(reason) {
    const text = String(reason || "").replace(/: manual intervention$/i, "").trim();
    if (/flight-time limit still exceeded after the (?:reserve|standby) crews?/i.test(text)) {
      return `crew still over the flight-time limit after the standby crew${/after the standby crews/i.test(text) ? "s" : ""}`;
    }
    if (/flight-time limit/i.test(text)) return /no (?:reserve|standby)/i.test(text) ? "crew over the flight-time limit, no standby crew" : "crew over the flight-time limit";
    if (/crew cannot reach/i.test(text)) return "crew cannot reach a flight in time";
    if (/^LIS capacity (check failed|does not fit)/i.test(text)) return "does not fit Lisbon capacity";
    const check = text.match(/^(crew|LIS capacity|passenger) check (missing|unknown)/i);
    if (check) return `${check[1].replace("LIS", "Lisbon")} check not answered`;
    const conflict = conflictWords(text);
    if (conflict) return lowerFirst(conflict);
    if (/^domains disagree/i.test(text)) return "the agents' answers disagree";
    if (/^standby crews left today not established/i.test(text)) return "standby crews left today unknown";
    if (/^standby crew report time not established/i.test(text)) return "standby crew report time unknown";
    if (/^standby crews changed since the plan/i.test(text)) return "standby crews changed since the plan";
    if (/^validation missing/i.test(text)) return "ARROW's checks are missing";
    return text || "checks incomplete";
  }
  // What the agents' answers disagree on, when the plan names it (Task 2): a standby crew that cannot reach its flight, or a
  // storm plan that cannot wait for one; plans stored before say "reserve crew". Null for any other text.
  function conflictWords(reason) {
    const text = String(reason || "").replace(/^domains disagree:\s*/i, "").trim();
    const reach = /^(?:reserve|standby) (crews?) (.+?) cannot take (\S+) in time, and this plan does not move it$/i.exec(text);
    if (reach) return `Standby ${reach[1].toLowerCase()} ${reach[2]} cannot reach ${reach[3]} in time`;
    const report = /^(?:reserve|standby) (crews?) (.+?) cannot report in time, and a (?:storm|weather) plan takes no added delay$/i.exec(text);
    if (report) {
      const many = report[1].toLowerCase() === "crews";
      return `Standby ${report[1].toLowerCase()} ${report[2]} cannot report in time; a weather plan cannot wait for ${many ? "them" : "it"}`;
    }
    if (/^a (?:storm|weather) plan follows the reduced hourly rate; it takes no added delay$/i.test(text)) return "A weather plan follows the reduced hourly rate and takes no added delay";
    return null;
  }

  // "247 fewer lose their flight or connection than Cancel AR101 + AR102 · 90 more owed compensation · €9,060 more", as
  // segments so the view can mark each figure better (up) or worse (down) for option.
  function versus(option, other, name) {
    const [a, b] = [disrupted(option), disrupted(other)];
    const [o, p] = [owed(option), owed(other)];
    const [x, y] = [totalCost(option), totalCost(other)];
    const fewer = a !== null && b !== null ? Math.sign(b - a) : null;
    const segments = [];
    if (fewer === null) segments.push({ text: `Against ${name}: ` });
    else if (!fewer) segments.push({ text: `As many ${LOSE} as ${name}` });
    else {
      segments.push({ text: `${plain.format(Math.abs(a - b))} ${fewer > 0 ? "fewer" : "more"} ${LOSE}`, tone: fewer > 0 ? "up" : "down" },
        { text: ` than ${name}` });
    }
    const figures = [];
    if (o !== null && p !== null && o !== p) {
      figures.push({ text: `${plain.format(Math.abs(o - p))} ${o < p ? "fewer" : "more"} owed compensation`, tone: o < p ? "up" : "down" });
    }
    if (x !== null && y !== null) {
      figures.push(x === y ? { text: "the same cost" } : { text: `${euro.format(Math.abs(x - y))} ${x < y ? "cheaper" : "more"}`, tone: x < y ? "up" : "down" });
    }
    if (fewer === null && !figures.length) return [...segments, { text: "passengers and cost not established" }];
    figures.forEach((figure, index) => segments.push(...(fewer === null && !index ? [] : [{ text: " · " }]), figure));
    return segments;
  }

  // One plan in plain words: what it cancels, keeps on time and makes late, and whom it leaves to rebook.
  // "Kept on time" is against the flights any other plan of the case makes late.
  function inWords(option, plans, originals, name) {
    const late = lateMinutes(option, originals).filter(entry => isLate(option, entry));
    const minutes = late.map(entry => entry.minutes).filter(isNum);
    const [low, high] = [Math.min(...minutes), Math.max(...minutes)];
    const range = minutes.length && minutes.length === late.length
      ? (low === high ? together(span(low)) : `${together(span(low))} to ${together(span(high))}`) : "";
    const when = range ? `${range} late` : "later";
    const kept = [...new Set(plans.filter(other => other !== option)
      .flatMap(other => contrast(option, other, originals).find(fact => fact.key === "kept")?.flights || []))];
    const cancelled = cancelledFlights(option);
    if (!cancelled.length) {
      const solo = ["delay", "swap"].includes(option?.kind) && plans.filter(other => other?.kind === option.kind).length === 1;
      const leave = late.length === 1 ? `${late[0].flight} leaves` : `${count(late.length, "flight")} leave`;
      return `${solo ? `The ${option.kind}` : name} keeps everyone flying${kept.length ? ` and ${flightsNamed(kept)} on time` : ""}${late.length ? `; ${leave} ${when}` : ""}.`;
    }
    const clauses = [];
    if (kept.length) clauses.push(`keeps ${flightsNamed(kept)} on time`);
    if (late.length) clauses.push(`flies ${late.length > 3 ? count(late.length, "flight") : listed(late.map(entry => entry.flight))} ${when}`);
    const head = `Cancelling ${listed(cancelled)}`;
    const n = disrupted(option);
    if (n === null) return `${head}${clauses.length ? ` ${listed(clauses)}` : ""}.`;
    if (n === 0) return `${head} ${clauses.length ? `${listed(clauses)}, with no passenger losing their flight or connection` : "costs no passenger their flight or connection"}.`;
    const rebook = `leaves ${count(n, "passenger")} to rebook`;
    return clauses.length === 1 && kept.length ? `${head} ${clauses[0]} but ${rebook}.` : `${head} ${listed([...clauses, rebook])}.`;
  }

  // Where the cost difference comes from, part by part (option less other), the largest increase first.
  function costDifference(option, other) {
    const theirs = new Map(costParts(other).map(part => [part.key, part]));
    const [mine, them] = [option?.cost?.overnight_passengers, other?.cost?.overnight_passengers];
    const gap = isNum(mine) && isNum(them) ? mine - them : 0;
    const hotels = !gap ? "" : them === 0 ? `${count(mine, "passenger")} in hotels`
      : `${plain.format(Math.abs(gap))} ${gap > 0 ? "more" : "fewer"} passengers in hotels`;
    return costParts(option).filter(part => theirs.has(part.key)).map(part => {
      const delta = part.eur - theirs.get(part.key).eur;
      return { key: part.key, label: lowerFirst(part.label), mine: part, theirs: theirs.get(part.key), delta, text: signedEuro(delta),
        note: part.key === "care" ? hotels : "" };
    }).filter(part => part.delta !== 0).sort((a, b) => b.delta - a.delta);
  }

  // What a part is made of, from its workings, in groups a driver can name: compensation and rebooking by their
  // reason, care as meals and overnight stays, the airline's cost as cancellations, delays and fuel.
  const firstWhy = (line) => String(line?.why || "").split(" · ")[0];
  function partGroups(option, key) {
    const cost = isObj(option?.cost) ? option.cost : {};
    const workings = isObj(cost.workings) ? cost.workings : {};
    const lines = (list) => (Array.isArray(list) ? list : []).filter(isObj);
    const groups = new Map();
    const add = (group, n, eur, minutes = 0, co2 = 0) => {
      const was = groups.get(group) || { group, n: 0, eur: 0, minutes: 0, co2: 0 };
      groups.set(group, { group, n: was.n + n, eur: was.eur + eur, minutes: was.minutes + minutes, co2: was.co2 + co2 });
    };
    if (key === "compensation" || key === "rebooking") {
      for (const line of lines(workings[key])) if (isNum(line.passengers) && isNum(line.eur_each)) add(firstWhy(line), line.passengers, line.passengers * line.eur_each);
    } else if (key === "care") {
      for (const line of lines(workings.meals)) if (isNum(line.passengers) && isNum(line.eur_each)) add("meals", line.passengers, line.passengers * line.eur_each);
      const night = isObj(workings.overnight) ? workings.overnight : null;
      const care = isObj(cost.care) ? cost.care : {};
      if (night && isNum(night.passengers) && [care.hotel_eur, care.taxi_eur, night.meals_eur_each].every(isNum)) {
        add("overnight", night.passengers, care.hotel_eur + care.taxi_eur + night.passengers * night.meals_eur_each);
      }
    } else if (key === "airline") {
      for (const line of lines(workings.airline?.flights)) {
        if (!isNum(line.eur)) continue;
        if (line.cancelled) add("cancelled", 1, line.eur);
        else if (isNum(line.delay_minutes)) add("late", 1, line.eur, line.delay_minutes);
      }
      for (const line of lines(workings.airline?.fuel)) {
        if (isNum(line.eur) && isNum(line.made_up_minutes)) add("fuel", 1, line.eur, line.made_up_minutes, isNum(line.co2_eur) ? line.co2_eur : 0);
      }
      for (const line of lines(workings.airline?.holding)) {
        if (isNum(line.eur) && isNum(line.held_minutes)) add("holding", 1, line.eur, line.held_minutes, isNum(line.co2_eur) ? line.co2_eur : 0);
      }
    }
    return [...groups.values()];
  }
  const verb = (n, one, many) => (n === 1 ? one : many);
  // A group in words: who is "283 passengers", "42 more passengers" or "2 flights"; n picks the verb.
  const DRIVERS = {
    "arrives 3 h or more late": (who, n) => `${who} ${verb(n, "arrives", "arrive")} 3 h or more late`,
    "no replacement flight": (who, n) => `${who} ${verb(n, "has", "have")} no replacement flight`,
    "cancelled, rebooked": (who, n) => `${who} ${verb(n, "arrives", "arrive")} 2 h or more late after the cancellation`,
    "rerouted on another airline": (who) => `${who} rerouted on other airlines`,
    "rebooked on our own flights": (who) => `${who} rebooked on our own flights`,
    meals: (who, n) => `${who} ${verb(n, "waits", "wait")} past the care threshold`,
    overnight: (who, n) => `${who} ${verb(n, "stays", "stay")} overnight`,
    holding: (who, n) => `${who} ${verb(n, "holds", "hold")} in the air before landing`,
    cancelled: (who) => `${who} cancelled`
  };
  const WHAT = { compensation: "EU261 compensation", rebooking: "rebooking", meals: "meals", overnight: "hotels, taxis and meals",
    cancelled: "airline cost", late: "airline cost", fuel: "extra fuel", holding: "fuel and CO2 in the air" };
  // The dearer plan's group, whole when the cheaper plan has no such group ("283 passengers arrive 3 h or more late, so
  // €72,100 of EU261 compensation"), else as the difference ("42 more passengers … · +€11,750 EU261 compensation"), so
  // the line never credits the gap with euros both plans pay.
  function driverWords(key, group, theirs) {
    const unit = key === "airline" ? "flight" : "passenger";
    const phrase = DRIVERS[group.group] || ((who) => who);
    // Fuel priced by current versions holds its CO2 allowances too.
    const what = group.group === "fuel" && group.co2 > 0 ? "fuel and CO2" : WHAT[group.group] || WHAT[key] || "";
    if (!theirs) {
      const eur = euro.format(Math.round(group.eur));
      if (group.group === "late") return `${count(group.n, "flight")} ${plain.format(group.minutes)} min late in total, ${eur} of airline cost`;
      if (group.group === "fuel") return `${count(group.n, "flight")} ${verb(group.n, "makes", "make")} up ${plain.format(group.minutes)} min in the air, ${eur} of ${what}`;
      const who = phrase(count(group.n, unit), group.n);
      return key === "compensation" ? `${who}, so ${eur} of ${what}` : key === "rebooking" ? `${who}, ${eur}` : `${who}, ${eur} of ${what}`;
    }
    const gap = `${signedEuro(Math.round(group.delta))} ${what}`;
    if (group.group === "late" || group.group === "fuel") {
      const more = group.minutes - theirs.minutes;
      const words = group.group === "late" ? "min late in total" : "min made up in the air";
      return `${more > 0 ? `${plain.format(more)} more ${words}` : `${count(group.n, "flight")} ${group.group === "late" ? "late" : "faster"}`} · ${gap}`;
    }
    const more = group.n - theirs.n;
    const who = more > 0 ? phrase(`${plain.format(more)} more ${unit}${more === 1 ? "" : "s"}`, more) : phrase(count(group.n, unit), group.n);
    return `${who} · ${gap}`;
  }

  // Why the dearer of two plans costs more, in one line (owner): the part that adds most to the difference and,
  // from its workings, the group that adds most to that part. part is that part from the dearer plan's side, for its
  // workings on hover; null when the costs are equal or unknown, or the workings were not recorded.
  function costDriver(option, other, nameOf, labelOf = nameOf) {
    const [x, y] = [totalCost(option), totalCost(other)];
    if (x === null || y === null || x === y) return null;
    const [high, low] = x > y ? [option, other] : [other, option];
    const part = costDifference(high, low)[0];
    if (!part || part.delta <= 0) return null;
    const theirs = new Map(partGroups(low, part.key).filter(group => group.eur > 0).map(group => [group.group, group]));
    const [group] = partGroups(high, part.key).map(entry => ({ ...entry, delta: entry.eur - (theirs.get(entry.group)?.eur || 0) }))
      .filter(entry => entry.delta > 0).sort((a, b) => b.delta - a.delta);
    return group ? { key: part.key, part, plan: high, name: nameOf(high), than: nameOf(low), high: labelOf(high), low: labelOf(low),
      text: driverWords(part.key, group, theirs.get(group.group) || null) } : null;
  }

  // What to show on top of the decision, or null when there is no plan to explain (manual intervention,
  // still assessing). subject is the plan carried out or approved, else the recommended plan; without a
  // recommendation, the least disruptive plan against the cheapest.
  function recommendation(item, originals = null) {
    const shown = visible(item?.options);
    if (!shown.length || item?.manual_intervention || item?.status === "assessing") return null;
    const number = (option) => shown.indexOf(option) + 1;
    const named = (option) => {
      const name = planName(option);
      return keep(shown.filter(other => planName(other) === name).length > 1 ? `Plan ${number(option)} · ${name}` : name);
    };
    const execution = isObj(item.execution) ? item.execution : null;
    // What-ifs asked in chat keep their card numbers but are not weighed here: this explains ARROW's own plans, and a
    // what-if only when OCC approved it.
    const plans = shown.filter(option => !isObj(option?.what_if) || option?.option_id === execution?.option_id);
    const open = plans.filter(option => option?.policy?.route !== "blocked");
    const recommended = plans.find(option => option?.recommended === true) || null;
    const byCost = (a, b) => (totalCost(a) ?? Infinity) - (totalCost(b) ?? Infinity);
    const byPassengers = (a, b) => (disrupted(a) ?? Infinity) - (disrupted(b) ?? Infinity);
    let subject = null, lead, others = null, choose = false;
    if (execution) {
      subject = (item.options || []).find(option => option?.option_id === execution.option_id) || null;
      lead = execution.route === "auto" && execution.approved_by === "policy"
        ? execution.status === "completed" ? "Carried out automatically" : "Running automatically" : "OCC approved";
    } else if (recommended) {
      [subject, lead] = [recommended, "Recommended"];
    } else if (open.length > 1) {
      choose = true;
      lead = `OCC chooses · ${open.length} plans`;
      subject = [...open].sort((a, b) => byPassengers(a, b) || byCost(a, b))[0];
      const cheapest = [...open].sort((a, b) => byCost(a, b) || byPassengers(a, b))[0];
      others = cheapest === subject ? open.filter(option => option !== subject) : [cheapest];
    } else if (open.length === 1) {
      [subject, lead] = [open[0], "One plan"];
    } else {
      lead = "No plan can run";
    }
    others ||= subject && number(subject) ? open.filter(option => option !== subject) : [];
    const route = subject?.policy?.route;
    const chip = execution || choose || !subject ? null
      : route === "auto" ? (subject.requires_duty_manager === true ? { text: "OCC decides", tone: "occ" } : { text: "Runs automatically", tone: "auto" })
      : route === "occ" ? { text: "OCC decides", tone: "occ" } : route === "blocked" ? { text: "Blocked", tone: "blocked" } : null;
    const both = choose && others.length > 1 ? `${named(subject)} is the cheapest and the least disruptive` : "";
    return {
      lead, chip, subject, recommended, choose, both,
      plan: subject && !choose && number(subject) ? `Plan ${number(subject)}` : "",
      name: subject && !choose ? keep(planName(subject)) : "",
      label: subject ? `${number(subject) ? `Plan ${number(subject)} · ` : ""}${planName(subject)}` : "",
      joiner: lead === "OCC approved" ? " " : " · ",
      versus: others.map(other => ({ other, segments: [...(choose && !both ? [{ text: `${named(subject)}: ` }] : []), ...versus(subject, other, named(other))] })),
      tradeoffs: others.length ? [subject, ...others].sort((a, b) => number(a) - number(b)).map(option => inWords(option, plans, originals, named(option))) : [],
      costs: others.map(other => ({ other, name: named(other), number: number(other), parts: costDifference(subject, other),
        driver: costDriver(subject, other, named, (option) => (number(option) ? `Plan ${number(option)} · ${planName(option)}` : planName(option))) }))
        .filter(entry => entry.parts.length),
      blocked: plans.filter(option => option !== subject && option?.policy?.route === "blocked").map(option => ({ option, name: named(option),
        reasons: Array.isArray(option.policy.reasons) ? option.policy.reasons : [], text: blockWords((option.policy.reasons || [])[0]) })),
      against: recommended ? against(recommended, plans) : null,
      orchestrator: recommended && (recommended.recommendation_reason || recommended !== subject)
        ? { reason: String(recommended.recommendation_reason || ""), pick: recommended !== subject ? `Plan ${number(recommended)} · ${keep(planName(recommended))}` : "" } : null
    };
  }

  function checks(option) {
    return [crewCheck(option), slotCheck(option), costCheck(option)];
  }

  function notEstablished(item) {
    const options = item?.options || [];
    const all = options.flatMap(option => Array.isArray(option?.domain_assessments) ? option.domain_assessments : []);
    const items = [];
    if (options.some(option => totalCost(option) === null &&
      (option?.total_eur === null || option?.total_eur === undefined))) items.push("Total recovery cost");
    if (all.some(entry => entry?.domain === "crew")) items.push("Full crew legality");
    if (item?.defect) items.push("Airworthiness release");
    if (all.some(entry => entry?.domain === "slots")) items.push("Allocated slot");
    if (all.some(entry => entry?.domain === "booking" && entry.reaccommodation_status === "not_assessed")) {
      items.push("Re-accommodation and EU261 eligibility");
    }
    return items;
  }

  function leversNotAssessed(options) {
    const kinds = (options || []).map(option => option?.kind);
    return kinds.length && kinds.every(kind => kind === "delay" || kind === "cancel")
      ? "Options not assessed: tail swap, spare aircraft, MEL deferral" : "";
  }

  function coordination(option) {
    const flights = option?.flights || [];
    const disrupted = Number.isFinite(option?.misconnected_passengers) ? option.misconnected_passengers : null;
    const pss = { code: "PSS", label: "reservations", count: disrupted };
    if (option?.kind === "delay") {
      const moved = flights.filter(flight => !isCancelled(flight));
      return [
        { code: "DLA", label: "IFPS", count: moved.length },
        { code: "MVT", label: "handling", count: null },
        { code: "TOBT", label: "A-CDM", count: moved.filter(flight => flight.origin === "LIS").length },
        { code: "Crew", label: "crew control recheck", count: null },
        pss
      ].filter(entry => entry.count === null || entry.count > 0);
    }
    if (option?.kind === "cancel") {
      return [
        { code: "CNL", label: "IFPS", count: flights.filter(isCancelled).length },
        { code: "ASM", label: "schedule", count: null },
        { code: "Slot release", label: "coordinator", count: null },
        { code: "Crew", label: "crew control re-plan", count: null },
        pss
      ].filter(entry => entry.count === null || entry.count > 0);
    }
    return [];
  }

  function chipText(entry) {
    if (entry.code === "PSS") return entry.count === null ? "PSS" : `PSS ${entry.count}`;
    return entry.count > 1 ? `${entry.code} ×${entry.count}` : entry.code;
  }

  function shortBlock(reason) {
    const value = String(reason || "");
    if (/^(booking|passenger) check/i.test(value)) return "passenger check";
    if (/^flight-time limit/i.test(value)) return "crew limit";
    if (/^crew check/i.test(value)) return "crew check";
    if (/^slots check|capacity/i.test(value)) return "slot check";
    return "checks incomplete";
  }


  function optionLabel(optionId, options) {
    const option = (options || []).find(entry => entry?.option_id === optionId);
    return option ? cardLabel(option, options) : "Option not identified";
  }

  function stepContext(input, options) {
    if (!input?.option_id) return "";
    const delay = input.additional_delay_minutes;
    const label = optionLabel(input.option_id, options);
    return Number.isFinite(delay) && delay > 0 ? `${label} (+${delay} min)` : label;
  }

  function callText(tool) {
    const system = String(tool?.system || "System").replace(/^\w/, c => c.toUpperCase());
    const operation = String(tool?.operation || "call")
      .replace(/^get_/, "").replace(/^assess_\w+?_recovery$/, "recovery check").replaceAll("_", " ");
    return `${system} · ${operation}`;
  }

  function finding(output, item) {
    if (!output || typeof output !== "object" || Array.isArray(output)) return "";
    const ids = Array.isArray(output.affected_flight_ids) ? output.affected_flight_ids : [];
    if (typeof output.is_disruption === "boolean") {
      if (!output.is_disruption || !ids.length) return "No departures inside the AOG window";
      const from = hhmm(item?.defect?.reported_at);
      const until = hhmm(item?.defect?.unavailable_until);
      return `${ids.join(", ")} depart${ids.length === 1 ? "s" : ""} during the AOG${from && until ? ` (${from}–${until})` : ""}`;
    }
    if (Array.isArray(output.option_ids)) {
      const labels = output.option_ids.map(id => optionLabel(id, item?.options));
      return labels.length ? `${labels.length} candidate${labels.length === 1 ? "" : "s"}: ${labels.join(" · ")}` : "No candidates supplied";
    }
    const status = output.constraint_status;
    if (output.domain === "slots") {
      const conflict = Array.isArray(output.slot_conflicts) ? output.slot_conflicts[0] : null;
      const extra = output.required_additional_delay_minutes;
      if (conflict) {
        return `${conflict.airport} ${hhmm(conflict.hour_local)} hour full (${conflict.movements}/${conflict.hourly_capacity})${
          Number.isFinite(extra) && extra > 0 ? ` → needs +${extra} min` : ""}`;
      }
      if (output.capacity_fit === true) return "Fits modeled hourly capacity";
    }
    if (output.domain === "booking" && Number.isFinite(output.misconnected_passengers)) {
      const total = Number.isFinite(output.affected_passengers) ? ` of ${output.affected_passengers}` : "";
      const pnrs = output.disrupted_booking_count;
      return `${output.misconnected_passengers}${total} lose flight or connection${
        output.misconnected_passengers > 0 && Number.isFinite(pnrs) ? ` · ${pnrs} PNR${pnrs === 1 ? "" : "s"}` : ""}`;
    }
    if (output.domain === "crew" && status === "pass") {
      const duty = (Array.isArray(output.duties) ? output.duties : []).filter(entry =>
        Number.isFinite(entry?.modeled_fdp_minutes) && Number.isFinite(entry?.modeled_fdp_limit_minutes))
        .sort((a, b) => b.modeled_fdp_minutes / b.modeled_fdp_limit_minutes - a.modeled_fdp_minutes / a.modeled_fdp_limit_minutes)[0];
      return `Duty within modeled limit${duty ? ` · FDP ${duty.modeled_fdp_minutes}/${duty.modeled_fdp_limit_minutes} min` : ""}`;
    }
    if (output.domain === "crew" && status === "fail") return "Duty exceeds modeled limit";
    return status === "pass" ? "Check passed" : status === "fail" ? "Check failed" : status ? "Result unknown" : "";
  }

  function policyBadge(option, execution) {
    const policy = option?.policy;
    if (!policy || typeof policy.route !== "string") return null;
    if (execution) {
      if (execution.option_id !== option.option_id) return null;
      // Only a plan the policy itself authorised ran automatically; a person's approval is "Approved".
      if (execution.route !== "auto" || execution.approved_by !== "policy") return { text: "Approved", tone: "auto" };
      if (execution.status === "completed") return { text: "Ran automatically", tone: "auto" };
      if (execution.status === "needs_attention") return { text: "Stopped · needs attention", tone: "blocked" };
      return { text: "Running automatically", tone: "auto" };
    }
    if (policy.route === "occ") return { text: "Needs your approval", tone: "occ" };
    if (policy.route === "auto") {
      return option.requires_duty_manager === true
        ? { text: "Within limits · you decide", tone: "auto" } : { text: "Runs automatically", tone: "auto" };
    }
    return { text: `Blocked · ${shortBlock((policy.reasons || [])[0])}`, tone: "blocked" };
  }

  function rebookingNote(option) {
    const rebooking = option?.validation?.rebooking;
    if (!rebooking || !Number.isFinite(rebooking.disrupted_passengers) || !rebooking.disrupted_passengers) return "";
    const parts = [];
    if (Number.isFinite(rebooking.rebooked_passengers)) parts.push(`${rebooking.rebooked_passengers} rebookable`);
    if (rebooking.unplaced_passengers > 0) parts.push(`${rebooking.unplaced_passengers} to Passenger Services`);
    return parts.join(" · ");
  }

  const ACTION_RANK = { unknown: 0, failed: 1, refused: 2, blocked: 3, pending: 4, not_started: 5, confirmed: 6 };
  const ACTION_TEXT = { confirmed: "Confirmed", pending: "Pending", refused: "Refused",
    failed: "Failed", unknown: "Outcome unknown", blocked: "Not started", not_started: "Not started" };

  function actionStatusText(status) {
    return ACTION_TEXT[status] || "Unknown";
  }

  function executionGroups(execution) {
    const groups = new Map();
    for (const action of execution?.actions || []) {
      const label = Tips.system(action?.label || "System");
      const group = groups.get(label) || { label, total: 0, confirmed: 0, status: "confirmed" };
      group.total += 1;
      if (action.status === "confirmed") group.confirmed += 1;
      if ((ACTION_RANK[action.status] ?? 4) < (ACTION_RANK[group.status] ?? 4)) group.status = action.status;
      groups.set(label, group);
    }
    return [...groups.values()];
  }

  // ---------- What ARROW did, by outcome (owner: start high level, then drill down) ----------
  // Actions recorded before kinds carry only a label and a message; those say what they were. A weather plan's passenger notice
  // (PAX-NOTICE) reads by its kind, else by its summary.
  const NOTICE = /^Tell passengers on (\d+) flights? their new times$/i;
  function actionKind(action) {
    if (action?.kind) return String(action.kind);
    if (NOTICE.test(String(action?.summary || ""))) return "notify_passengers";
    const label = String(action?.label || "");
    const text = `${action?.summary || ""} ${action?.message || ""}`;
    if (label === "Ops control") return /\bcancel/i.test(text) ? "cancel_flights" : /tail change/i.test(text) ? "change_tail" : "retime_flights";
    if (label === "Network manager") {
      return /\bCNL\b/.test(text) ? "network_manager.fpl_cancel" : /\bCHG\b/.test(text) ? "network_manager.fpl_change" : "network_manager.fpl_delay";
    }
    if (label === "Airport CDM") return "airport_cdm.tobt_update";
    if (label === "Slot coordinator") return /non-use/i.test(text) ? "slot_coordinator.slot_non_use" : "slot_coordinator.slot_change_request";
    if (label === "Crew") return /reserve|standby/i.test(text) ? "assign_reserve_crew" : "update_duties";
    if (label === "Passengers" || label === "Booking") return "rebook_passengers";
    if (label === "Ground handling") return "ground_handling.direct_transfer";
    return "";
  }
  const GROUP_OF = {
    // The reported disruption, recorded first by the system that owns it when a plan runs (owner).
    record_disruption: "report",
    retime_flights: "retimed", cancel_flights: "cancelled", change_tail: "aircraft",
    "network_manager.fpl_delay": "atc", "network_manager.fpl_cancel": "atc", "network_manager.fpl_change": "atc",
    "airport_cdm.tobt_update": "atc", "slot_coordinator.slot_non_use": "atc", "slot_coordinator.slot_change_request": "atc",
    update_duties: "crew", assign_reserve_crew: "crew", rebook_passengers: "passengers", "ground_handling.direct_transfer": "passengers",
    notify_passengers: "passengers",
    // A storm in two stages (case summary and held storm spec §2): the warning, the alerts, a call at call-by, and the
    // stand-down's notice and release.
    warn_passengers: "passengers", notify_no_change: "passengers", alert_standby: "crew", call_standby: "crew", release_standby: "crew"
  };
  const STANDBY_KINDS = new Set(["alert_standby", "call_standby", "release_standby"]);
  const GROUPS = [["report", "Reported disruption"], ["retimed", "Flights"], ["cancelled", "Flights"], ["aircraft", "Aircraft"], ["atc", "Air traffic control and airports"],
    ["crew", "Crew"], ["passengers", "Passengers"], ["other", "Other changes"]];
  const AIRPORT_NAMES = { LIS: "Lisbon", OPO: "Porto", FAO: "Faro", FNC: "Funchal", PDL: "Ponta Delgada" };
  const FLIGHT = /\b([A-Z0-9]{2}\d{1,4})\b/g;
  const airportName = (code) => AIRPORT_NAMES[code] || String(code ?? "");
  const flightsIn = (text) => [...String(text || "").matchAll(FLIGHT)].map(match => match[1]);
  const numberIn = (pattern, ...texts) => {
    for (const text of texts) {
      const match = pattern.exec(String(text || ""));
      if (match) return Number(match[1]);
    }
    return null;
  };
  function span(minutes) {
    if (minutes < 60) return `${minutes} min`;
    return minutes % 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes / 60} h`;
  }
  function lateBy(was, now) {
    if (!was || !now) return null;
    const exact = Date.parse(now) - Date.parse(was);
    if (Number.isFinite(exact) && /T/.test(was) && /T/.test(now)) return Math.round(exact / 60000);
    const [a, b] = [minutesOf(was), minutesOf(now)];
    return a === null || b === null ? null : b - a;
  }

  // A flight change is now one call per flight (native dispatch spec §3), so it can stop part-way: each planned flight
  // with its call's state, "not_started" when it was never sent; null for an action without recorded calls.
  const FLIGHT_PATH = /^\/api\/flights\/([^/?#]+)$/;
  const payloadFlights = (action) => (Array.isArray(action?.payload?.flights) ? action.payload.flights.filter(isObj).map(flight => String(flight.flight_no)) : []);
  const payloadFlightNos = (action) => (Array.isArray(action?.payload?.flight_nos) ? action.payload.flight_nos.map(String) : []);
  const PLANNED_FLIGHTS = { retime_flights: payloadFlights, change_tail: payloadFlights, cancel_flights: payloadFlightNos };
  const FLIGHT_VERB = { retime_flights: "moved", cancel_flights: "cancelled", change_tail: "moved to another aircraft" };
  function flightStates(action) {
    const calls = (Array.isArray(action?.calls) ? action.calls : []).filter(isObj);
    const flights = (PLANNED_FLIGHTS[actionKind(action)] || (() => []))(action);
    if (!calls.length || !flights.length) return null;
    const sent = new Map(calls.map(call => [(FLIGHT_PATH.exec(String(call.path || "")) || [])[1], String(call.status || "unknown")]));
    return new Map(flights.map(flight => [flight, sent.get(flight) || "not_started"]));
  }
  function statesOf(actions) {
    const states = actions.map(flightStates).filter(Boolean);
    return states.length ? new Map(states.flatMap(map => [...map])) : null;
  }
  const NOT_DONE = { refused: "Refused", failed: "Failed", unknown: "Outcome unknown", pending: "Waiting", sending: "Sending", not_started: "Not sent" };
  const notDone = (state) => NOT_DONE[state] || NOT_DONE.unknown;
  // A flight change not done for every flight: how many were, and what became of the others. Null once every flight is done.
  function partFlights(states, verb) {
    const rows = [...(states || [])];
    if (!rows.length || rows.every(([, state]) => state === "confirmed")) return null;
    const done = rows.filter(([, state]) => state === "confirmed").length;
    const others = new Map();
    for (const [flight, state] of rows.filter(([, state]) => state !== "confirmed")) others.set(notDone(state), [...(others.get(notDone(state)) || []), flight]);
    return { headline: [done ? `${done} of ${count(rows.length, "flight")} ${verb}` : `No flight ${verb}`],
      sub: [...others].map(([word, flights]) => `${listed(flights)} ${word.toLowerCase()}`).join(" · ") };
  }

  // unsent(action): the execution stopped before this action was sent, so it reads as what it would have done.
  function retimed(actions, unsent = () => false) {
    const rows = new Map();
    for (const action of actions) {
      const expected = isObj(action.expected?.flights) ? action.expected.flights : {};
      if (Array.isArray(action.payload?.flights)) {
        for (const flight of action.payload.flights.filter(isObj)) {
          const was = isObj(expected[flight.flight_no]) ? expected[flight.flight_no].std_local : null;
          rows.set(flight.flight_no, { flight: flight.flight_no, was, now: flight.std_local });
        }
        continue;
      }
      // "Retimed AR259 11:30; MVT sent": the new times only.
      const pairs = [...String(action.message || "").matchAll(/\b([A-Z0-9]{2}\d{1,4}) (\d{1,2}:\d{2})\b/g)];
      if (pairs.length) pairs.forEach(([, flight, time]) => rows.set(flight, { flight, was: null, now: time }));
      else flightsIn(action.summary).forEach(flight => rows.set(flight, { flight, was: null, now: null }));
    }
    const list = [...rows.values()].map(row => ({ ...row, late: lateBy(row.was, row.now) }));
    const states = statesOf(actions);
    const part = partFlights(states, FLIGHT_VERB.retime_flights);
    if (part) {
      return { ...part, table: { head: ["Flight", "Was", "Planned", "Result"], rows: list.map(row => [row.flight, hhmm(row.was) || "—", hhmm(row.now) || "—",
        states.has(row.flight) ? (states.get(row.flight) === "confirmed" ? "Moved" : notDone(states.get(row.flight))) : "—"]) } };
    }
    const intent = actions.every(unsent);
    const known = list.map(row => row.late).filter(isNum);
    const later = list.length && known.length === list.length && known.every(minutes => minutes > 0);
    const n = list.length;
    const headline = intent ? [n ? `${count(n, "flight")} to retime` : "Flight times to change"]
      : !n ? ["Flight times updated"] : later ? [`${count(n, "flight")} now ${n === 1 ? "leaves" : "leave"} later`] : [`${count(n, "flight")} retimed`];
    const low = Math.min(...known), high = Math.max(...known);
    const sub = later ? (low === high ? `+${span(low)}` : `+${span(low)} to +${span(high)}`) : "";
    return { headline, sub, table: list.length ? { head: ["Flight", "Was", intent ? "Planned" : "Now", "Later by"],
      rows: list.map(row => [row.flight, hhmm(row.was) || "—", hhmm(row.now) || "—", isNum(row.late) ? `${row.late > 0 ? "+" : ""}${span(Math.abs(row.late))}` : "—"]) } : null };
  }

  function cancelled(actions, unsent = () => false) {
    const flights = new Set();
    for (const action of actions) {
      const listedFlights = Array.isArray(action.payload?.flight_nos) ? action.payload.flight_nos : flightsIn(action.message || action.summary);
      listedFlights.forEach(flight => flights.add(String(flight)));
    }
    const intent = actions.every(unsent);
    return partFlights(statesOf(actions), FLIGHT_VERB.cancel_flights) ||
      { headline: [flights.size ? `${count(flights.size, "flight")} ${intent ? "to cancel" : "cancelled"}` : intent ? "Flights to cancel" : "Flights cancelled"],
        sub: listed([...flights]) };
  }

  function aircraft(actions, unsent = () => false) {
    const moves = actions.flatMap(action => (Array.isArray(action.payload?.flights) ? action.payload.flights.filter(isObj) : []))
      .map(flight => `${flight.flight_no} → ${flight.aircraft_reg}`);
    const verb = actions.every(unsent) ? "to move to another aircraft" : FLIGHT_VERB.change_tail;
    return partFlights(statesOf(actions), FLIGHT_VERB.change_tail) ||
      { headline: [moves.length ? `${count(moves.length, "flight")} ${verb}` : `Flights ${verb}`], sub: moves.join(" · ") };
  }

  function messages(actions, unsent = () => false) {
    const tally = new Map();
    const add = (key, text) => tally.set(key, { text, n: (tally.get(key)?.n || 0) + 1 });
    for (const action of actions) {
      const kind = actionKind(action);
      if (kind === "network_manager.fpl_delay") add(kind, (n) => `${count(n, "delay message")} to EUROCONTROL`);
      else if (kind === "network_manager.fpl_cancel") add(kind, (n) => `${count(n, "cancellation message")} to EUROCONTROL`);
      else if (kind === "network_manager.fpl_change") add(kind, (n) => `${count(n, "aircraft change message")} to EUROCONTROL`);
      else if (kind === "airport_cdm.tobt_update") {
        const code = String(action.payload?.airport || (/\bat ([A-Z]{3})\b/.exec(action.message || "") || /\b([A-Z]{3})$/.exec(action.summary || "") || [])[1] || "");
        const name = AIRPORT_NAMES[code] || code;
        add(`${kind}|${code}`, (n) => `${count(n, "departure time")} to ${name ? `${name} airport` : "the airport"}`);
      } else if (kind === "slot_coordinator.slot_non_use") {
        if (unsent(action)) add(`${kind}|unsent`, (n) => `${count(n, "slot")} to release`);
        else add(kind, (n) => `${count(n, "slot")} released`);
      } else add(kind, (n) => `${count(n, "slot change request")}`);
    }
    return { headline: [...tally.values()].map(({ text, n }) => text(n)), sub: "" };
  }

  function crew(actions, unsent = () => false) {
    const parts = [];
    let duties = null;
    // A storm's standby crews: put on alert now, called at their call-by time, released when the storm stands down; or not, and why.
    const STANDBY_WORDS = { alert_standby: [/^Put standby crews? (.+) on alert$/i, "on alert", "put on alert"],
      call_standby: [/^Call standby crews? (.+?)(?: · .*)?$/i, "called", "called"], release_standby: [/^Release standby crews? (.+)$/i, "released", "released"] };
    for (const action of actions) {
      const kind = actionKind(action);
      if (STANDBY_KINDS.has(kind)) {
        const [pattern, word, verb] = STANDBY_WORDS[kind];
        const named = pattern.exec(String(action.summary || ""));
        const who = named ? cap(standbyName(named[1].split(", ").map(id => ({ id })))) : "Standby crews";
        const stopped = ["refused", "failed", "unknown"].includes(action.status);
        parts.push(stopped ? `${who} not ${verb} (${action.status === "unknown" ? "outcome unknown" : action.status})`
          : unsent(action) ? `${who} to be ${verb}` : `${who} ${word}`);
        continue;
      }
      // "Assign standby crews R1, R2" since Task 2; "Assign reserve crew R1" before.
      const reserve = /^Assign (?:reserve|standby) crews? (.+)$/.exec(String(action.summary || ""));
      const done = unsent(action) ? "" : " assigned";
      if (kind === "assign_reserve_crew") {
        parts.push(reserve ? `${cap(standbyName(reserve[1].split(", ").map(id => ({ id }))))}${done}` : `Standby crew${done}`);
      }
      // "Crew duties: D-LIS-127 release 15:55; D-LIS-128 release 16:40"
      const listedDuties = /^Crew duties: (.+)$/.exec(String(action.message || ""));
      if (listedDuties) duties = (duties || 0) + (/^none affected/i.test(listedDuties[1]) ? 0 : listedDuties[1].split(";").filter(item => item.trim()).length);
    }
    const dutyActions = actions.filter(action => !STANDBY_KINDS.has(actionKind(action)));
    if (dutyActions.length) {
      parts.push(dutyActions.every(unsent) ? "Crew duties" : duties === null ? "Crew duties updated"
        : duties ? `${plain.format(duties)} crew ${duties === 1 ? "duty" : "duties"} updated` : "No crew duty changed");
    }
    return { headline: parts, sub: "" };
  }

  function passengersDone(actions, unsent = () => false) {
    const headline = [];
    let table = null;
    for (const action of actions) {
      const kind = actionKind(action);
      const flights = Array.isArray(action.payload?.flights) ? action.payload.flights.length : numberIn(/on (\d+) flights?/, action.summary);
      const on = flights === null ? "" : ` on ${count(flights, "flight")}`;
      const stopped = ["refused", "failed", "unknown"].includes(action.status);
      // A storm's warning ahead of it (no new times yet), and its stand-down's notice that nothing changes.
      if (kind === "warn_passengers" || kind === "notify_no_change") {
        const told = kind === "warn_passengers" ? "warned of possible delays" : "told no change is expected";
        headline.push(stopped ? `Passengers${on} not ${kind === "warn_passengers" ? "warned" : "told"} (${action.status === "unknown" ? "outcome unknown" : action.status})`
          : unsent(action) ? `Passengers${on} to be ${told}` : `Passengers${on} ${told}`);
        continue;
      }
      // "Tell passengers on 85 flights their new times": a weather plan's notice, after the retime (PAX-NOTICE).
      if (kind === "notify_passengers") {
        headline.push(stopped ? `Passengers${on} not told (${action.status === "unknown" ? "outcome unknown" : action.status})`
          : unsent(action) ? `New times to tell passengers${on}` : `Passengers${on} told their new times`);
        continue;
      }
      if (kind === "rebook_passengers") {
        const people = numberIn(/(\d+) passengers?/, action.message, action.summary);
        headline.push(unsent(action) ? (people === null ? "Passenger rebooking" : `Rebooking for ${count(people, "passenger")}`)
          : people === null ? "Passengers rebooked" : `${count(people, "passenger")} rebooked onto later flights`);
        continue;
      }
      const transfers = (Array.isArray(action.payload?.transfers) ? action.payload.transfers : []).filter(isObj);
      const people = transfers.length ? transfers.reduce((sum, item) => sum + (isNum(item.passengers) ? item.passengers : 0), 0)
        : numberIn(/(\d+) passengers?/, action.message, action.summary);
      const links = transfers.length || numberIn(/(\d+) connections?/, action.message);
      const connections = isNum(links) && links ? ` (${count(links, "connection")})` : "";
      headline.push(unsent(action) ? `Cars for ${people === null ? "passengers" : count(people, "passenger")}${connections}`
        : `${people === null ? "Passengers" : count(people, "passenger")} keep their connection with a car${connections}`);
      if (transfers.length) {
        table = { head: ["From", "To", "Passengers", "To connect"], rows: transfers.map(item => [item.from_flight_no || "—", item.to_flight_no || "—",
          isNum(item.passengers) ? plain.format(item.passengers) : "—", isNum(item.minutes) ? `${item.minutes} min${item.passport_control ? ", passport control" : ""}` : "—"]) };
      }
    }
    return { headline, sub: "", table };
  }

  // The reported disruption as the system that owns it recorded it, first of a plan's actions: a delay in operations control, a
  // defect in maintenance (which marks the aircraft out of service), with the event id the system issued then.
  const REPORTED = { schedule: ["Delay", "operations control"], maintenance: ["Defect", "maintenance"] };
  const EVENT = /\bEV-[0-9A-F]{6}\b/;
  function reported(actions, unsent = () => false) {
    const headline = [];
    const events = [];
    for (const action of actions) {
      const [what, where] = REPORTED[String(action.system || "")] || ["Disruption", "the airline system"];
      const stopped = ["refused", "failed", "unknown"].includes(action.status);
      headline.push(action.status === "confirmed" ? `${what} recorded in ${where}`
        : stopped ? `${what} not recorded (${action.status === "unknown" ? "outcome unknown" : action.status})`
          : unsent(action) ? `${what} to record in ${where}` : `Recording the ${what.toLowerCase()} in ${where}`);
      const calls = (Array.isArray(action.calls) ? action.calls : []).filter(isObj);
      const event = calls.map(call => String(call.event_id || "")).find(id => EVENT.test(id)) || (EVENT.exec(String(action.message || "")) || [])[0];
      if (event) events.push(`Event ${event}`);
    }
    return { headline, sub: events.join(" · ") };
  }

  const WORST = ["unknown", "failed", "refused", "blocked", "pending", "not_started", "held", "not_sent", "confirmed"];
  const NEVER_SENT = ["blocked", "not_started", "stood_down"];
  // The changes a completed or running plan made, one group per outcome in plain words, each with
  // its actions for the detail. Once the execution has stopped, what was never sent will not be: it
  // reads as not sent, by what it would have done. A storm's held part reads as held, by what it will do;
  // once stood down, as never sent.
  function outcomeGroups(execution) {
    const actions = (execution?.actions || []).filter(isObj);
    const held = execution?.status === "held";
    const stopped = ["needs_attention", "held", "stood_down"].includes(execution?.status);
    const unsent = (action) => stopped && NEVER_SENT.includes(action?.status);
    const by = new Map();
    for (const action of actions) {
      const key = GROUP_OF[actionKind(action)] || "other";
      by.set(key, [...(by.get(key) || []), action]);
    }
    const read = { report: reported, retimed, cancelled, aircraft, atc: messages, crew, passengers: passengersDone,
      other: (list) => ({ headline: [`${count(list.length, "other change")}`], sub: "" }) };
    return GROUPS.filter(([key]) => by.has(key)).map(([key, title]) => {
      const list = by.get(key);
      // One state per flight of a flight change once its calls are recorded, else one per action.
      const statuses = list.flatMap(action => {
        const flights = flightStates(action);
        return flights ? [...flights.values()] : [String(action.status || "unknown")];
      }).map(state => (state === "sending" ? "pending" : stopped && NEVER_SENT.includes(state) ? (held ? "held" : "not_sent") : state));
      const status = WORST.find(state => statuses.includes(state)) || (statuses.every(state => state === "confirmed") ? "confirmed" : "unknown");
      const confirmed = statuses.filter(state => state === "confirmed").length;
      return { key, title, actions: list, status, confirmed, total: statuses.length, stopped, held,
        partly: confirmed > 0 && ["refused", "failed", "unknown"].includes(status), unsent: statuses.every(state => state === "not_sent" || state === "held"),
        table: null, sub: "", ...read[key](list, unsent) };
    });
  }

  // "All 10 changes confirmed by the airline systems", or what is still open. A stopped execution in labels and numbers:
  // what was done (a flight change stopped part-way by its flights), what stopped it and what was not sent. A storm held until
  // confirmed, stood down or back with OCC says so.
  function outcomeLine(execution) {
    const actions = (execution?.actions || []).filter(isObj);
    const n = actions.length;
    const of = (states) => actions.filter(action => states.includes(action.status)).length;
    const confirmed = of(["confirmed"]);
    if (isObj(execution?.storm)) {
      if (execution.status === "held") {
        const now = actions.filter(action => !["when_confirmed", "stand_down"].includes(action.stage));
        const waiting = actions.filter(action => action.stage === "when_confirmed" && NEVER_SENT.includes(action.status)).length;
        return { tone: "occ", text: `Now: ${now.filter(action => action.status === "confirmed").length} of ${now.length} confirmed · ${plain.format(waiting)} held until the forecast is confirmed` };
      }
      if (execution.status === "stood_down") return { tone: "pass", text: `Stood down · ${confirmed} sent · ${plain.format(of(NEVER_SENT))} never sent` };
      if (execution.status === "needs_attention" && execution.storm.returned) {
        return { tone: "fail", text: `Back to OCC · ${confirmed} confirmed · ${plain.format(of(NEVER_SENT))} not sent` };
      }
    }
    if (n && confirmed === n) {
      return { tone: "pass", text: n === 1 ? "The change is confirmed by the airline system" : `All ${n} changes confirmed by the airline systems` };
    }
    const unknown = actions.filter(action => !WORST.includes(action.status)).length + of(["unknown"]);
    if (execution?.status === "needs_attention") {
      const moved = new Map();
      for (const action of actions.filter(action => action.status !== "confirmed")) {
        const done = [...(flightStates(action)?.values() || [])].filter(state => state === "confirmed").length;
        const verb = FLIGHT_VERB[actionKind(action)];
        if (done) moved.set(verb, (moved.get(verb) || 0) + done);
      }
      const parts = [[confirmed, "confirmed"], [of(["refused"]), "refused"], [of(["failed"]), "failed"], [unknown, "outcome unknown"],
        [of(["pending"]), "waiting"], [of(NEVER_SENT), "not sent"]].filter(([k]) => k).map(([k, word]) => `${k} ${word}`);
      return { tone: "fail", text: [confirmed || moved.size ? "Stopped part-way" : "Stopped",
        ...[...moved].map(([verb, k]) => `${count(k, "flight")} ${verb}`), ...parts].join(" · ") };
    }
    const open = [[of(["pending", ...NEVER_SENT]), "waiting"], [of(["refused"]), "refused"], [of(["failed"]), "failed"],
      [unknown, "outcome unknown"]].filter(([k]) => k);
    return { tone: of(["refused", "failed", "unknown"]) ? "fail" : "occ",
      text: [`${confirmed} of ${count(n, "change")} confirmed`, ...open.map(([k, word]) => `${k} ${word}`)].join(" · ") };
  }

  function awaitingReview(execution) {
    return (execution?.reviews || []).find(review => review?.status === "awaiting_expert") || null;
  }

  function handledReview(execution) {
    return (execution?.reviews || []).find(review => review?.status === "handled") || null;
  }
  // Passengers still waiting for Passenger Services, as every screen says it (owner: "should be clearer that there is still
  // action required").
  const actionNeeded = (passengers) => `Action needed · ${isNum(Number(passengers)) ? `${plain.format(Number(passengers))} ` : ""}passenger${
    Number(passengers) === 1 ? "" : "s"} for Passenger Services`;

  // One name per plan on every screen: the case page, its banner and the map. A what-if that holds the delay back says by how much.
  function planName(option) {
    if (!option) return "";
    if (option.kind === "delay") {
      const own = delayName(option);
      if (own) return own;
      const extra = option.levers?.extra_delay_minutes;
      return isNum(extra) && extra > 0 ? `Delay the rotation, ${extra} min more` : "Delay the rotation";
    }
    if (option.kind === "cancel") {
      const cancelled = (option.flights || []).filter(isCancelled).map(flight => flight.flight_no).filter(Boolean);
      return `Cancel ${cancelled.join(" + ") || "flights"}`;
    }
    if (option.kind === "swap") return "Swap the aircraft";
    return option.title || "Plan";
  }

  // A what-if plan asked for in the case chat (case chat spec §5): who asked, when (Lisbon time) and what; null otherwise.
  const lisbon = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Lisbon" });
  function lisbonTime(value) {
    const time = Date.parse(value || "");
    return Number.isFinite(time) ? lisbon.format(new Date(time)) : "";
  }
  function whatIfLine(option) {
    const asked = isObj(option?.what_if) ? option.what_if : null;
    if (!asked) return null;
    const at = lisbonTime(asked.asked_at);
    return `Asked in chat by ${asked.asked_by?.name || "an operator"}${at ? ` at ${at}` : ""}: ${String(asked.question || "")}`;
  }

  function executionHeading(execution) {
    const actions = execution?.actions || [];
    const review = awaitingReview(execution);
    const waitingOn = review ? ` · ${review.passengers} passengers await Passenger Services` : "";
    const first = (statuses) => actions.find(action => statuses.includes(action.status));
    // A storm plan (case summary and held storm spec §2): its Now part done and the rest held, stood down, or back with OCC.
    const storm = isObj(execution?.storm) ? execution.storm : null;
    if (storm && execution.status === "held") {
      return { heading: storm.decide_by ? `Held · decide by ${storm.decide_by}` : "Held until confirmed",
        message: "Passengers warned and standby crews on alert; the rest waits for the forecast to be confirmed.", tone: "waiting" };
    }
    if (storm && execution.status === "stood_down") {
      // Worded from the stand-down actions actually confirmed (review), as the executor's own stood-down message.
      const done = (kind) => actions.find(action => action?.kind === kind && action.stage === "stand_down" && action.status === "confirmed") || null;
      const released = done("release_standby");
      const told = done("notify_no_change");
      const crews = (/^Release (standby crews? .+)$/i.exec(String(released?.summary || "")) || [])[1];
      const flights = Array.isArray(told?.payload?.flights) ? told.payload.flights.length : numberIn(/on (\d+) flights?/, told?.summary);
      const parts = [released ? `${crews || "standby crews"} released` : "",
        told ? `passengers on ${flights === null ? "the warned flights" : count(flights, "flight")} told no change is expected` : ""].filter(Boolean);
      return { heading: "Stood down", tone: "done",
        message: parts.length ? `The forecast dropped: ${parts.join("; ")}; nothing held was sent.` : "The forecast dropped before anything was sent; nothing changes." };
    }
    if (storm && execution.status === "needs_attention" && storm.returned) {
      return { heading: "Back to OCC", message: `${cap(storm.returned)}. Nothing more was sent.`, tone: "attention" };
    }
    if (execution?.status === "completed") {
      const automatic = execution.route === "auto" && execution.approved_by === "policy";
      return { heading: `${automatic ? "Executed automatically · OCC informed" : "Recovery executed · all systems confirmed"}${waitingOn}`,
        message: automatic ? "Within every agent's limits; receipts on the plan." : "Airline systems updated; receipts on the option.",
        tone: review ? "review" : "done" };
    }
    if (execution?.status === "needs_attention") {
      const stopped = first(["unknown", "failed", "refused"]);
      const name = Tips.system(stopped?.label);
      if (stopped?.status === "unknown") {
        return { heading: `Needs attention · ${name} outcome unknown`,
          message: `Check ${name} before acting; dependent actions were not sent.`, tone: "attention" };
      }
      // As the OCC alert says it: a change that went through for some flights was partly carried out.
      if ((Array.isArray(stopped?.calls) ? stopped.calls : []).some(call => call?.status === "confirmed")) {
        return { heading: `Needs attention · ${name} partly carried out`,
          message: "Dependent actions were not sent; what was done stays in place.", tone: "attention" };
      }
      return { heading: stopped ? `Needs attention · ${name} ${stopped.status}` : "Needs attention · plan changed",
        message: stopped ? "Dependent actions were not sent." : "Nothing further was sent; Reset the day to assess it again.", tone: "attention" };
    }
    if (execution?.status === "pending_external") {
      const waiting = first(["pending", "not_started"]);
      return { heading: `Waiting for ${waiting ? Tips.system(waiting.label) : "confirmation"}`,
        message: "A request stays pending until the counterparty confirms.", tone: "waiting" };
    }
    const confirmed = actions.filter(action => action.status === "confirmed").length;
    return { heading: actions.length ? `Executing · ${confirmed} of ${actions.length} confirmed` : "Approved · starting",
      message: "ARROW rechecks the sources before each update.", tone: "waiting" };
  }

  function sourceFreshness(events) {
    const latest = new Map();
    for (const event of events || []) {
      if (event?.event !== "agent_completed") continue;
      for (const tool of event?.evidence?.tools || []) {
        if (tool?.status === "completed" && tool.system) latest.set(String(tool.system), event.at);
      }
    }
    return [...latest.entries()].map(([system, at]) => ({ system, at }))
      .sort((a, b) => a.system.localeCompare(b.system));
  }

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  function minutesOf(value) {
    const time = hhmm(value);
    if (!time) return null;
    const [hours, minutes] = time.split(":").map(Number);
    return hours * 60 + minutes;
  }
  // An arrival earlier than its departure lands after midnight.
  function arrivalOf(flight) {
    const departs = minutesOf(flight?.std_local);
    const arrives = minutesOf(flight?.sta_local);
    return departs !== null && arrives !== null && arrives < departs ? arrives + 1440 : arrives;
  }
  function legStatus(flight, row, opts) {
    if (row.scheduled) return opts.affected?.has(flight.flight_no) ? "affected" : "scheduled";
    if (isCancelled(flight)) return "cancelled";
    const original = opts.originals?.get(flight.flight_no);
    if (!original || minutesOf(original.std_local) === null) return "planned";
    return minutesOf(original.std_local) === minutesOf(flight.std_local) ? "unchanged" : "changed";
  }

  function timeRange(flights) {
    const times = (flights || []).flatMap(flight => [minutesOf(flight?.std_local), arrivalOf(flight)])
      .filter(Number.isFinite);
    return times.length ? [Math.min(...times), Math.max(...times)] : null;
  }

  // rows: [{ label?, flights, scheduled? }]; opts: { axis, labels, band: { from, until }, originals: Map, affected: Set, width }
  function rotationSvg(rows, opts = {}) {
    const width = opts.width || 560;
    const labelWidth = opts.labels ? 200 : 0;
    const rowHeight = opts.rowHeight || 44;
    const barHeight = 30;
    const top = opts.axis ? 22 : 4;
    const timed = (flight) => minutesOf(flight?.std_local) !== null && minutesOf(flight?.sta_local) !== null;
    const times = [];
    rows.forEach(row => (row.flights || []).filter(timed).forEach(flight => {
      times.push(minutesOf(flight.std_local), arrivalOf(flight));
      const original = !row.scheduled && opts.originals?.get(flight.flight_no);
      if (original && timed(original)) times.push(minutesOf(original.std_local), arrivalOf(original));
    }));
    const band = opts.band && minutesOf(opts.band.from) !== null && minutesOf(opts.band.until) !== null
      ? [minutesOf(opts.band.from), minutesOf(opts.band.until)] : null;
    if (!times.length) return "";
    if (band) times.push(...band);
    if (Array.isArray(opts.range)) times.push(...opts.range.filter(Number.isFinite));
    const start = Math.floor(Math.min(...times) / 60) * 60;
    const end = Math.max(start + 60, Math.ceil(Math.max(...times) / 60) * 60);
    const x = (m) => labelWidth + 6 + (m - start) / (end - start) * (width - labelWidth - 12);
    const height = top + rows.length * rowHeight + (band && opts.axis ? 18 : 4);
    let out = "";
    if (opts.axis) {
      for (let m = start; m <= end; m += 60) {
        out += `<line class="hour" x1="${x(m)}" y1="16" x2="${x(m)}" y2="${top + rows.length * rowHeight}"/>`;
        out += `<text class="hour-label" x="${x(m)}" y="11" text-anchor="middle">${String((m / 60) % 24).padStart(2, "0")}:00</text>`;
      }
    }
    if (band) {
      out += `<rect class="band" x="${x(band[0])}" y="${top - 4}" width="${Math.max(0, x(band[1]) - x(band[0]))}" height="${rows.length * rowHeight}" rx="6"/>`;
      if (opts.axis) {
        out += `<text class="band-label" x="${x(band[0]) + 4}" y="${height - 4}">unavailable ${hhmm(opts.band.from)}–${hhmm(opts.band.until)}</text>`;
      }
    }
    rows.forEach((row, index) => {
      const y = top + index * rowHeight + (rowHeight - barHeight) / 2;
      out += `<g class="row">`;
      if (opts.labels) out += `<text class="row-label" x="8" y="${y + 19}">${esc(row.label || "")}</text>`;
      (row.flights || []).filter(timed).forEach(flight => {
        const original = opts.originals?.get(flight.flight_no);
        if (legStatus(flight, row, opts) === "changed" && timed(original)) {
          out += `<rect class="ghost" data-flight="${esc(flight.flight_no)}" x="${x(minutesOf(original.std_local))}" y="${y}" width="${Math.max(4, x(arrivalOf(original)) - x(minutesOf(original.std_local)))}" height="${barHeight}" rx="6"><title>${esc(`${flight.flight_no} originally ${hhmm(original.std_local)}`)}</title></rect>`;
        }
      });
      (row.flights || []).filter(timed).forEach(flight => {
        const status = legStatus(flight, row, opts);
        const x0 = x(minutesOf(flight.std_local));
        const w = Math.max(4, x(arrivalOf(flight)) - x0);
        const second = status === "cancelled" ? "✕" : hhmm(flight.std_local);
        out += `<g class="leg ${status}" data-flight="${esc(flight.flight_no)}"><title>${esc(`${flight.flight_no} ${flight.origin || ""}→${flight.destination || ""} ${status === "cancelled" ? "cancelled" : `${hhmm(flight.std_local)}–${hhmm(flight.sta_local)}`}`)}</title>
          <rect x="${x0}" y="${y}" width="${w}" height="${barHeight}" rx="6"/>
          <text x="${x0 + w / 2}" y="${y + 13}" text-anchor="middle" class="leg-flight">${esc(flight.flight_no)}</text>
          <text x="${x0 + w / 2}" y="${y + 25}" text-anchor="middle" class="leg-time">${esc(second)}</text></g>`;
      });
      out += "</g>";
    });
    const summary = rows.map(row => `${row.label || "Proposed"}: ${(row.flights || []).map(flight =>
      `${flight.flight_no} ${isCancelled(flight) ? "cancelled" : hhmm(flight.std_local) || "time unknown"}`).join(", ")}`).join("; ");
    return `<svg class="rotation" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(summary)}">${out}</svg>`;
  }

  // ---------- An airline system call in words (owner): what was done, with its key detail ----------
  // The label is the trail's (trail.OPERATIONS); the aircraft, flight, option and minutes come from what the call recorded.
  const OPTION_OPERATIONS = new Set(["assess_crew", "assess_booking", "assess_slots", "get_reserve_options", "get_rebooking_plan", "get_duty_plan"]);
  function callWords(call) {
    const operation = String(call?.operation || "");
    const request = isObj(call?.request) ? call.request : {};
    const label = String(call?.label || operation.replaceAll("_", " ") || "Call");
    if (operation === "get_aircraft_rotation" && request.aircraft_reg) return label.replace("the aircraft's", `${request.aircraft_reg}'s`);
    if (operation === "record_delay" && request.flight_no) {
      return `${label} · ${request.flight_no}${isNum(request.delay_minutes) ? ` +${request.delay_minutes} min` : ""}`;
    }
    if (operation === "record_defect" && request.aircraft_reg) {
      return `${label} · ${request.aircraft_reg}${isNum(request.unavailable_minutes) ? ` out of service ${span(request.unavailable_minutes)}` : ""}`;
    }
    if (operation === "apply_action" && request.kind) return `${label} · ${String(request.kind).replaceAll("_", " ")}`;
    const named = /^Option: ([^·]+)/.exec(String(call?.detail || ""));
    const option = OPTION_OPERATIONS.has(operation) ? String(named ? named[1].trim() : request.option_id || "").replace(/[_-]/g, " ") : "";
    const added = isNum(request.additional_delay_minutes) ? request.additional_delay_minutes : Number((/\+(\d+) min/.exec(String(call?.detail || "")) || [])[1] || 0);
    const text = !option ? label : operation === "assess_slots" ? `${label} for the ${option === "cancel" ? "cancellation" : option}` : `${label} · ${option}`;
    return added > 0 ? `${text} · +${added} min` : text;
  }
  // A route as called: its {placeholders} filled from the recorded request (GET /api/defects/DSR-…/assessment).
  const ROUTE_KEYS = { reg: "aircraft_reg", trace: "case" };
  function filledRoute(route, request) {
    const values = isObj(request) ? request : {};
    return String(route || "").replace(/\{(\w+)\}/g, (whole, key) => {
      const value = Object.hasOwn(values, key) ? values[key] : Object.hasOwn(ROUTE_KEYS, key) ? values[ROUTE_KEYS[key]] : undefined;
      return value !== undefined && value !== null && value !== "" && typeof value !== "object" ? String(value) : whole;
    });
  }

  // ---------- Each call ARROW sent (native dispatch spec §4), in What ARROW did and in the trail ----------
  // Named by its recipient and what it says, then its state (owner). Opened: what was sent and what came back, then
  // the Interface line: method and path, the call's reference and, in the trail, the identity and backend the recipient
  // recorded. The hover card explains the recipient and the abbreviations; it carries no ids.
  const CALL_STATE = { confirmed: ["pass", "✓", "Confirmed"], pending: ["occ", "…", "Waiting"], sending: ["occ", "…", "Sending"],
    refused: ["fail", "✕", "Refused"], failed: ["fail", "!", "Failed"], unknown: ["unknown", "?", "Outcome unknown"] };
  const NM_CODE = /^\((DLA|CHG|CNL)-/;
  // The fields a recipient takes, in words (airline_systems.audit call_request); original rows are never shown.
  const SENT_FIELDS = [["flight_no", "Flight"], ["std_local", "Departs"], ["sta_local", "Arrives"], ["status", "Status"],
    ["aircraft_reg", "Aircraft"], ["airport", "Airport"], ["tobt", "TOBT"], ["type", "Request"], ["request", "Change"],
    ["option_id", "Option"], ["additional_delay_minutes", "Extra delay (min)"], ["reserve_option_id", "Standby crew"], ["reserve_option_ids", "Standby crews"]];

  function sentWords(sent) {
    if (!isObj(sent)) return '<span class="call-none">Not recorded</span>';
    if (typeof sent.text === "string") {
      const code = (NM_CODE.exec(sent.text) || [])[1];
      return `<code class="call-msg"${Tips.attrs(esc, code ? Tips.explain("", `${code} EOBT`) : "")}>${esc(sent.text)}</code>`;
    }
    const rows = SENT_FIELDS.filter(([key]) => sent[key] !== undefined && sent[key] !== null && sent[key] !== "")
      .map(([key, name]) => [name, /_local$|^tobt$/.test(key) ? hhmm(sent[key]) || String(sent[key])
        : Array.isArray(sent[key]) ? sent[key].map(String).join(", ") : String(sent[key]).replaceAll("_", " ")]);
    if (Array.isArray(sent.flights)) {
      rows.push(["Flights", sent.flights.filter(isObj).map(flight => [flight.flight_no, isCancelled(flight) ? "cancelled"
        : [hhmm(flight.std_local), hhmm(flight.sta_local)].filter(Boolean).join("–"), flight.aircraft_reg].filter(Boolean).join(" ")).join(" · ")]);
    }
    if (Array.isArray(sent.transfers)) {
      rows.push(["Cars", sent.transfers.filter(isObj).map(item => `${item.from_flight_no || "—"} → ${item.to_flight_no || "—"}${
        isNum(item.passengers) ? `, ${count(item.passengers, "passenger")}` : ""}`).join(" · ")]);
    }
    if (isNum(sent.passengers)) rows.push(["Passengers", plain.format(sent.passengers)]);
    return rows.length ? `<dl class="call-kv">${rows.map(([name, value]) => `<dt${Tips.attrs(esc, Tips.explain("", name))}>${esc(name)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`
      : '<span class="call-none">Not recorded</span>';
  }

  // The hover card explains the recipient and the abbreviations in what it says.
  function callTip(call) {
    const code = (NM_CODE.exec(String(call.sent?.text || "")) || [])[1] || "";
    const terms = [call.recipient, call.says, code, code ? "EOBT" : "", call.sent?.tobt ? "TOBT" : ""].filter(Boolean).join(" ");
    return Tips.explain(String(call.recipient || ""), terms);
  }

  // opts: { identity: { name, id } } from the recipient's own record (the trail only); opts.data: data-* attributes for
  // the row (open state kept across redraws).
  function callRow(call, opts = {}) {
    if (!isObj(call)) return "";
    const status = String(call.status || "unknown");
    const [tone, glyph, word] = CALL_STATE[status] || CALL_STATE.unknown;
    const message = String(call.message || "");
    const recipient = Tips.system(call.recipient);
    // A protocol answer (ACK, REJ) is what the recipient said; a longer reply waits behind the row.
    const code = /^[A-Z]{2,4}$/.test(message) ? ` · ${esc(message)}` : "";
    const why = ["refused", "failed", "unknown"].includes(status) && message && !code ? `<span class="call-why">${esc(message)}</span>` : "";
    const iface = [call.method && call.path ? `${call.method} ${call.path}` : "", call.call_id ? `Reference ${call.call_id}` : "",
      call.request_id && call.request_id !== call.call_id ? `Request ${call.request_id}` : "",
      opts.identity?.id ? `${opts.identity.name || "Caller"} ID ${opts.identity.id}` : ""].filter(Boolean).join(" · ");
    const data = Object.entries(opts.data || {}).map(([name, value]) => ` data-${esc(name)}="${esc(value)}"`).join("");
    return `<details class="call"${data}><summary${Tips.attrs(esc, callTip(call), recipient)}>
        <span class="mark ${tone}">${glyph}</span>
        <span class="call-what"><span class="call-says"><b>${esc(recipient)}</b>${call.says ? ` · ${esc(call.says)}` : ""}${code}</span>${why}</span>
        <span class="state">${esc(word)}</span></summary>
      <div class="call-io"><section><h6>Sent</h6>${sentWords(call.sent)}</section>
        <section><h6>Returned</h6>${message ? `<p class="call-reply ${tone}">${esc(message)}</p>` : '<span class="call-none">No answer yet</span>'}</section></div>
      ${iface ? `<p class="call-iface iface"><span>Interface:</span> <code>${esc(iface)}</code></p>` : ""}
    </details>`;
  }

  return Object.freeze({ hhmm, visible, optionMode, cardLabel, subtitle, checks, passengers, notEstablished,
    leversNotAssessed, coordination, chipText, shortBlock, stepContext, callText, finding, rotationSvg, timeRange,
    policyBadge, rebookingNote, executionGroups, executionHeading, awaitingReview, handledReview, actionNeeded, planName, actionStatusText,
    sourceFreshness, notCheapest, costCheck, costParts, costTip, disrupted, owed, notOwed, NOT_OWED, costDriver, contrast, against, recommendation, outcomeGroups, outcomeLine, lateMinutes,
    callRow, callWords, filledRoute, whatIfLine, lisbonTime, storm, manyFlights, flightRows, flightsSummary, lateFlights, standbyCrews, standbyLine, standbyName, standbyLeft, conflictWords,
    airportName,
    MANY_FLIGHTS });
})();
