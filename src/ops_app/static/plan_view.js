"use strict";

// The plans and the decision (the Case journey, owner-approved case screen prototype, journey.html):
// the calm plan cards, the recommendation line, the Decision and Plans steps, What ARROW did and the approval preview, with
// each plan's OCC playbook. Presentation only: app.js reads the case, keeps what the operator opened and posts decisions.
// Every inserted string is escaped. Each entry point takes the case and the operator's context (ctx below).
const PlanView = (() => {
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const plain = new Intl.NumberFormat("en-GB");
  const lisbonClock = (value) => {
    const time = Date.parse(value || "");
    return Number.isFinite(time) ? new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit",
      timeZone: "Europe/Lisbon" }).format(new Date(time)) : "";
  };
  const hhmm = (value) => RecoveryView.hhmm(value) || "";
  const isObj = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  function duration(minutes) {
    if (!Number.isFinite(minutes)) return "an unknown time";
    if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? "" : "s"}`;
    return minutes < 60 ? `${minutes} minutes` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }
  // What the operator may do and has open, set by each entry point from its ctx: the case's flights as scheduled
  // (originals), whether this user approves or marks Passenger Services reviews, whether the case read is fresh with no
  // decision being previewed (ready), the other options unfolded, a review being confirmed.
  const EMPTY = { originals: new Map(), approve: false, review: false, ready: false, othersOpen: false, reviewConfirm: false };
  let env = { ...EMPTY };
  function use(item, ctx = {}) {
    env = { ...EMPTY, ...ctx };
    return RecoveryView.visible(item?.options || []);
  }
  // One name per plan on every screen: the plan's own (RecoveryView's, as the tower, map and chat name it).
  const planName = (option) => RecoveryView.planName(option);

  // ---------- The plans: calm cards, their details on hover or focus (owner) ----------
  function crewWords(option) {
    const check = RecoveryView.checks(option).find(c => c.key === "crew");
    if (!check) return ["unknown", "Not checked"];
    if (check.state === "fits") return ["pass", check.value.replace(/^(\d+) \/ (\d+) min$/, "Within limit · $1 of $2 min")];
    if (check.state === "adjusted") {
      return ["pass", RecoveryView.standbyCrews(option).length === 1 ? "Standby crew covers the duty" : "Standby crews cover the duties"];
    }
    if (check.state === "disrupts") return ["fail", "Over the limit"];
    return ["unknown", "Not established"];
  }
  function capacityWords(option) {
    const check = RecoveryView.checks(option).find(c => c.key === "slot");
    if (!check) return ["unknown", "Not checked"];
    if (check.state === "fits") return ["pass", "Fits"];
    if (check.state === "adjusted") return ["adjusted", check.value];
    if (check.state === "disrupts") return ["fail", "Does not fit"];
    return ["unknown", "Not established"];
  }
  function passengerWords(option) {
    const r = option.validation?.rebooking || {};
    const disrupted = Number.isFinite(r.disrupted_passengers) ? r.disrupted_passengers : option.misconnected_passengers;
    if (!Number.isFinite(disrupted)) return ["unknown", "Not established"];
    const cars = r.transferred_passengers > 0 ? `${r.transferred_passengers} kept by car` : "";
    if (!disrupted) return ["pass", cars || "No one misses a connection"];
    const parts = [];
    if (Number.isFinite(r.rebooked_passengers)) parts.push(`${r.rebooked_passengers} rebooked`);
    if (r.unplaced_passengers > 0) parts.push(`${r.unplaced_passengers} to Passenger Services`);
    if (cars) parts.push(cars);
    return [r.unplaced_passengers > 0 ? "expert" : "pass", parts.join(" · ") || `${disrupted} lose flight or connection`];
  }
  // Who loses their flight or connection, explained on hover (cancellation and rebooking cost research §5).
  const LOSE_TIP = "Passengers whose flight is cancelled or who miss a connection: rebooked on our flights or left for Passenger Services. Passengers a car keeps on their connection are not counted.";
  function reasonWords(reason) {
    const text = String(reason || "");
    if (/^aircraft change/i.test(text)) return "the aircraft changes (the Aircraft agent only recommends)";
    if (/^cancellation/i.test(text)) return "a flight is cancelled";
    if (/^weather: /i.test(text)) return "a weather forecast moves flights across the network";
    // Task 2: several standby crews; plans stored before name one reserve crew.
    if (/flight-time limit still exceeded after the (?:reserve|standby) crews?/i.test(text)) {
      return `crew would still exceed their flight-time limit after the standby crew${/after the standby crews/i.test(text) ? "s" : ""}`;
    }
    if (/flight-time limit/i.test(text)) return "crew would exceed their flight-time limit and no standby crew is available";
    const called = text.match(/^(\d+) standby crews called \(automatic limit (\d+)\)/i);
    if (called) return `${called[1]} standby crews are called (automatic limit ${called[2]})`;
    if (/^uses the last (?:complete )?standby crew/i.test(text)) return `the plan ${text}`;
    if (/^standby crews left today not established/i.test(text)) return "standby crews left today unknown";
    if (/^standby crew report time not established/i.test(text)) return "a standby crew's report time is unknown";
    if (/^standby crews changed since the plan/i.test(text)) return "the standby crews changed since the plan was built";
    // What the agents disagree on, when the plan names it (a standby crew that cannot reach its flight in time).
    const conflict = RecoveryView.conflictWords(text);
    if (conflict && /^domains disagree:/i.test(text)) return conflict[0].toLowerCase() + conflict.slice(1);
    const care = text.match(/^(\w+) departs (\d+) min late: EU261 care threshold/i);
    if (care) return `${care[1]} leaves ${duration(Number(care[2]))} late, so passengers are owed care under EU261`;
    const cost = text.match(/^estimated cost above (€[\d,]+)/i);
    if (cost) return `the cost is above the ${cost[1]} limit for automatic plans`;
    const rebooked = text.match(/^more than (\d+) passengers rebooked \((\d+)\)/i);
    if (rebooked) return `${rebooked[2]} passengers would be rebooked (automatic limit ${rebooked[1]})`;
    if (/^asked in chat/i.test(text)) return "a what-if never runs automatically";
    return text;
  }
  // EU261 care reasons, one per late flight, read as one sentence (a Dev run); so do a storm's late arrivals in an airport's
  // night quota (Task 4c), when there are several. More than one cancelled flight is counted (option given).
  const CITY = { LIS: "Lisbon", OPO: "Porto", FAO: "Faro" };
  const NIGHT_QUOTA = /^([A-Z]{3}) night quota: (\w+) in-block (\d{2}):(\d{2})$/;
  // Several late arrivals in one airport's night quota, as their count, city and first to last in-block time; null for one
  // arrival or several airports, which read as written.
  function quotaSpan(texts) {
    const found = texts.map(text => NIGHT_QUOTA.exec(String(text || ""))).filter(Boolean);
    if (found.length < 2 || new Set(found.map(m => m[1])).size > 1) return null;
    // In-block times before noon are after midnight: they sort after the evening's.
    const minutes = (m) => Number(m[3]) * 60 + Number(m[4]) + (Number(m[3]) < 12 ? 1440 : 0);
    const sorted = [...found].sort((a, b) => minutes(a) - minutes(b));
    return { n: found.length, city: CITY[found[0][1]] || found[0][1], from: `${sorted[0][3]}:${sorted[0][4]}`, until: `${sorted.at(-1)[3]}:${sorted.at(-1)[4]}` };
  }
  function reasonList(reasons, option = null) {
    const care = [], quota = [], rest = [];
    const cancelled = (option?.flights || []).filter(f => f.status === "cancelled").length;
    for (const reason of reasons) {
      const late = String(reason || "").match(/^(\w+) departs (\d+) min late: EU261 care threshold/i);
      if (late) care.push([late[1], Number(late[2])]);
      else if (NIGHT_QUOTA.test(String(reason || ""))) quota.push(reason);
      else if (/^cancellation/i.test(String(reason || "")) && cancelled > 1) rest.push(`${cancelled} flights are cancelled`);
      else rest.push(reasonWords(reason));
    }
    const span = quotaSpan(quota);
    if (span) rest.push(`${span.n} arrivals fall in ${span.city}'s night quota (${span.from}–${span.until})`);
    else rest.push(...quota.map(reasonWords));
    if (care.length === 1) return [reasonWords(`${care[0][0]} departs ${care[0][1]} min late: EU261 care threshold`), ...rest];
    if (!care.length) return rest;
    const minutes = care.map(([, value]) => value);
    const low = Math.min(...minutes), high = Math.max(...minutes);
    const names = care.length <= 3 ? care.map(([flight]) => flight).join(", ") : `${care.length} flights`;
    const range = low === high ? duration(low) : `${duration(low)} to ${duration(high)}`;
    return [`${names} leave ${range} late, so passengers are owed care under EU261`, ...rest];
  }
  function whoDecides(option, item) {
    const route = option.policy?.route;
    const reasons = (option.policy?.reasons || []).filter(r => !/^(OCC decides|within every domain)/i.test(r))
      .filter(r => !/Passenger Services review/i.test(r));
    if (item.execution?.option_id === option.option_id) {
      // Carried out only once every change is confirmed; a plan stopped part-way is approved, not done.
      const done = item.execution.status === "completed";
      return item.execution.approved_by === "policy"
        ? ["auto", `${done ? "Carried out" : "Approved"} automatically: within every limit, OCC informed`]
        : ["auto", done ? "Approved by OCC and carried out" : "Approved by OCC"];
    }
    if (item.execution) return ["", "Not chosen"];
    if (route === "blocked") return ["blocked", `Blocked: ${reasonList(reasons, option).join("; ") || "checks incomplete"}`];
    if (route === "occ") return ["occ", `Needs OCC approval because ${reasonList(reasons, option).join("; ")}`];
    if (route === "auto") {
      return option.requires_duty_manager
        ? ["auto", "Within every limit · you choose between the plans"]
        : ["auto", "Runs automatically: within every limit, OCC informed"];
    }
    return ["", "Route not established"];
  }
  const slug = (value) => String(value ?? "plan").replace(/[^\w-]/g, "_");
  const capFirst = (text) => (text ? text[0].toUpperCase() + text.slice(1) : text);
  const isNum = (value) => typeof value === "number" && Number.isFinite(value);
  const signed = (n, format) => `${n > 0 ? "+" : n < 0 ? "−" : "±"}${format(Math.abs(n))}`;
  // Approve on the plan ARROW recommends, Choose on the others (prototype); the inspector's Decision step adds Reject. A plan card's
  // button says only the verb (prototype rev 2), its plan in its accessible name.
  // approve: false leaves only Reject (the Decision step's Who decides, under the plan cards that carry Approve and Choose).
  function decisionControls(option, item, number, { reject = false, primary = false, short = false, approve = true } = {}) {
    if (option.requires_duty_manager !== true || item.execution) return "";
    const decision = (item.events || []).filter(e => e.event === "duty_manager_decision" && e.option_id === option.option_id).at(-1);
    if (decision) return `<p class="note">You ${decision.decision === "approve" ? "approved" : "rejected"} this plan.</p>`;
    if (item.status !== "assessment_ready") return "";
    if (env.approve !== true) return '<p class="note">Only the OCC Duty Manager can decide.</p>';
    if (option.policy?.route === "blocked") return "";
    if (!env.ready) return '<p class="note">Refreshing…</p>';
    const id = escapeHtml(option.option_id);
    const verb = primary ? "Approve" : "Choose";
    const rejectButton = reject ? `<button class="reject" type="button" data-option-id="${id}" data-decision="reject"${
      approve ? "" : ` aria-label="Reject plan ${escapeHtml(number)}"`}>Reject</button>` : "";
    if (!approve) return rejectButton;
    return `<button class="${primary ? "primary" : "secondary"} approve" type="button" data-option-id="${id}" data-decision="approve"${
      short ? ` aria-label="${verb} plan ${escapeHtml(number)}">${verb}` : `>${verb} plan ${escapeHtml(number)}`}</button>${rejectButton ? ` ${rejectButton}` : ""}`;
  }
  // Flights: a few words, opening to a compact table by departure (and, up to six flights, the rotation picture).
  function flightsBlock(option) {
    const flights = option.flights || [];
    if (!flights.length) return "";
    const originals = env.originals;
    const rows = RecoveryView.flightRows(option, originals).map(row => `<tr data-departs="${escapeHtml(row.departs)}"${
      row.change === "Cancelled" ? ' class="cancelled"' : ""}><td>${escapeHtml(row.flight)}</td><td>${escapeHtml(row.route)}</td><td>${
      escapeHtml(row.now ? `${row.was} → ${row.now}` : row.was)}</td><td>${escapeHtml(row.change)}</td></tr>`).join("");
    const svg = RecoveryView.manyFlights(option, originals) ? "" : RecoveryView.rotationSvg([{ flights }], { originals });
    return `<details class="plan-flights" data-keep="flights-${escapeHtml(slug(option.option_id))}"><summary><b>Flights</b> <span>${
      escapeHtml(RecoveryView.flightsSummary(option, originals))}</span></summary>${svg ? `<div class="mini-rotation">${svg}</div>` : ""}
      <div class="flight-scroll"><table><thead><tr><th>Flight</th><th>Route</th><th>Departs</th><th>Change</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
  }
  // How each plan's cost adds up (owner): hover or focus on the total or a part opens a card with the workings
  // (tips.js data-card); the inspector's Decision step holds each part and the breakdown.
  const cardId = (scope, option, key) => `cost-${scope}-${slug(option.option_id)}-${key}`;
  function costCard(option, part, id) {
    const total = option.cost?.total_eur;
    const parts = RecoveryView.costParts(option);
    const recorded = parts.some(entry => entry.lines.length);
    const lines = (list) => list.map(line => `<p class="cc-line">${escapeHtml(line)}</p>`).join("");
    const body = part
      ? lines(part.lines)
      : `${parts.map(entry => `<div class="cc-row"><span>${escapeHtml(entry.label)}</span><b>${escapeHtml(euro.format(entry.eur))}</b></div>${lines(entry.summary)}`).join("")}
        ${parts.length > 1 ? `<div class="cc-row cc-total"><span>Total</span><b>${escapeHtml(euro.format(total))}</b></div>` : ""}`;
    // Recorded workings end with the part's note or the hotel-season range; older plans show their breakdown and basis.
    const foot = part ? (recorded ? part.note : RecoveryView.costTip(option, part))
      : recorded ? RecoveryView.costTip(option).split("\n").at(-1) : RecoveryView.costTip(option);
    return `<template id="${escapeHtml(id)}"><div class="cost-card">
      <p class="cc-head">${escapeHtml(part ? `${part.label} ${euro.format(part.eur)}` : `How ${euro.format(total)} adds up`)}</p>${body}
      ${foot ? `<p class="cc-foot">${escapeHtml(foot)}</p>` : ""}</div></template>`;
  }
  function costHover(option, scope, part = null) {
    if (!Number.isFinite(option.cost?.total_eur)) return { attrs: "", template: "" };
    const id = cardId(scope, option, part?.key || "total");
    return { attrs: ` data-card="${escapeHtml(id)}" tabindex="0"`, template: costCard(option, part, id) };
  }
  function costDetail(option, number) {
    if (!Number.isFinite(option.cost?.total_eur)) return "";
    const parts = RecoveryView.costParts(option).filter(part => part.lines.length);
    return `<details class="cost-detail" data-plan-no="${escapeHtml(number)}" data-keep="cost-${escapeHtml(slug(option.option_id))}"><summary>${
      escapeHtml(`Plan ${number}: ${planName(option)}`)}</summary><p>${escapeHtml(RecoveryView.costCheck(option).detail)}</p>
      ${parts.map(part => `<p class="cost-part"><b>${escapeHtml(part.label)} ${escapeHtml(euro.format(part.eur))}</b></p>
        <ul>${part.lines.map(line => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`).join("")}</details>`;
  }
  // A hover card of labels and numbers: a long value wraps below its label, never over it (owner).
  function infoCard(id, { title, sub = "", rows = [], listTitle = "", list = [], foot = "", body = "", tail = "", wide = false }) {
    return `<template id="${escapeHtml(id)}"><div class="info-card${wide ? " wide" : ""}">
      <p class="ic-head">${escapeHtml(title)}${sub ? ` <span>· ${escapeHtml(sub)}</span>` : ""}</p>${body}
      ${rows.length ? `<dl class="ic-kv">${rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd></div>`).join("")}</dl>` : ""}
      ${list.length ? `${listTitle ? `<p class="ic-sub">${escapeHtml(listTitle)}</p>` : ""}<ul class="ic-list">${list.map(entry =>
        `<li>${escapeHtml(entry)}</li>`).join("")}</ul>` : ""}${tail}
      ${foot ? `<p class="ic-foot">${escapeHtml(foot)}</p>` : ""}</div></template>`;
  }
  // Facts in a line read as a list (owner: no "word · word" chains).
  const SEP = ", ";
  // What each plan other than the reference does better or differently, in words (the Decision step and the vs line's card).
  const FIRST = ["kept", "cost", "passengers", "owed", "cancelled"];
  function factLine(facts, order) {
    const sorted = [...facts].sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
    return sorted.map((fact, index) => `<span class="${fact.better ? "up" : "down"}">${escapeHtml(index ? fact.text : fact.text[0].toUpperCase() + fact.text.slice(1))}</span>`).join(SEP);
  }
  function factsOf(option, reference, item) {
    return RecoveryView.contrast(option, reference, env.originals).filter(fact => fact.key !== "cancelled" || fact.better);
  }
  const OBJECTIVES = "ARROW's objectives, in order: safe and legal, protect connections, fewest passengers losing their flight or connection, lowest cost.";
  // Two or more plans side by side: who loses their flight or connection, who is owed compensation, the cost and its parts.
  // With two, the second against the first (lower is better).
  const COST_PARTS = [["airline", "Airline"], ["compensation", "EU261 compensation"], ["care", "Passenger care"], ["rebooking", "Rebooking"], ["delay", "Delay"]];
  function compareTable(columns) {
    const two = columns.length === 2;
    const partOf = (option, key) => RecoveryView.costParts(option).find(part => part.key === key)?.eur;
    const rows = [["Lose flight or connection", (o) => RecoveryView.disrupted(o), plain, ""],
      ["Owed compensation", (o) => RecoveryView.owed(o), plain, ""], ["Indicative cost", (o) => o.cost?.total_eur, euro, "sum"],
      ...COST_PARTS.filter(([key]) => columns.some(([, o]) => isNum(partOf(o, key)))).map(([key, label]) => [label, (o) => partOf(o, key), euro, "part"])];
    const fmt = (value, format) => (isNum(value) ? format.format(value) : "—");
    return `<table class="cmp-t"><thead><tr><th></th>${columns.map(([label]) => `<th>${escapeHtml(label)}</th>`).join("")}${two ? "<th>Difference</th>" : ""}</tr></thead>
      <tbody>${rows.filter(([, get]) => columns.some(([, o]) => isNum(get(o)))).map(([label, get, format, cls]) => {
        const values = columns.map(([, o]) => get(o));
        const gap = two && values.every(isNum) ? values[1] - values[0] : null;
        return `<tr class="${cls}"><td>${escapeHtml(label)}</td>${values.map(value => `<td>${escapeHtml(fmt(value, format))}</td>`).join("")}${
          two ? `<td class="${gap < 0 ? "up" : gap > 0 ? "down" : ""}">${gap === null ? "—" : escapeHtml(signed(gap, (n) => format.format(n)))}</td>` : ""}</tr>`;
      }).join("")}</tbody></table>`;
  }
  // The route of a plan as a chip (Automatic · OCC decides · Blocked) with why on hover.
  const ROUTE_CHIP = { auto: "Automatic", occ: "OCC decides", blocked: "Blocked" };
  const ROUTE_FOOT = "OCC: Operations Control Centre · EU261: the EU passenger-rights rule";
  function routeText(option) {
    const route = option.policy?.route;
    return route === "auto" && option.requires_duty_manager === true ? "Within limits" : ROUTE_CHIP[route] || "Not routed";
  }
  // A plan card's chip reads short (prototype rev 2: OCC · Auto · Blocked); its card says why in full.
  const ROUTE_SHORT = { auto: "Auto", occ: "OCC", blocked: "Blocked" };
  function routeChip(option, item, id) {
    const route = option.policy?.route;
    const [, who] = whoDecides(option, item);
    const reasons = (option.policy?.reasons || []).filter(r => !/^(OCC decides|within every domain)/i.test(r));
    const review = reasons.find(r => /Passenger Services review/i.test(r));
    const executed = item.execution?.option_id === option.option_id;
    const title = executed ? "How it was approved" : route === "blocked" ? "Why it is blocked" : route === "occ" ? "Why OCC decides" : "Why it runs automatically";
    const list = executed || !["occ", "blocked"].includes(route) ? [who]
      : [...reasonList(reasons.filter(r => r !== review), option), ...(review ? [review.replace(/ need Passenger Services review$/, " go to Passenger Services")] : [])].map(capFirst);
    const full = routeText(option);
    const short = full === "Within limits" ? full : ROUTE_SHORT[route] || full;
    return `<span class="route ${escapeHtml(route || "none")}" data-card="${escapeHtml(id)}" tabindex="0" aria-label="${escapeHtml(full)}">${escapeHtml(short)}</span>${
      infoCard(id, { title, list: list.length ? list : ["Checks incomplete"], foot: ROUTE_FOOT })}`;
  }
  // Crew · Lisbon · Passengers: a mark and a word, the detail on hover or focus.
  const MARK = { pass: "✓", fail: "✕", adjusted: "↻", expert: "→", unknown: "?" };
  const RESULT = { pass: "Pass", fail: "Fails", adjusted: "Adjusted", expert: "Passenger Services decides", unknown: "Not established" };
  const FDP_FOOT = "FDP: flight duty period, the crew's working time from report to the last landing, capped by EU rules.";
  function crewCard(option, id) {
    const [mark, text] = crewWords(option);
    const duties = (option.domain_assessments || []).find(entry => entry?.domain === "crew")?.duties || [];
    const crews = RecoveryView.standbyCrews(option);
    const left = option.validation?.crew_plan?.breaches_unresolved;
    // A duty over its limit that the standby crews take over is covered, not over (ARROW's crew plan confirms none is left).
    const covered = crews.length > 0 && left === 0;
    const rows = duties.filter(duty => isNum(duty?.modeled_fdp_minutes) && isNum(duty?.modeled_fdp_limit_minutes)).map(duty =>
      [`${duty.duty_id}${duty.status === "fail" ? (covered ? " · would be over · covered" : " · over") : ""}`, `${duty.modeled_fdp_minutes} of ${duty.modeled_fdp_limit_minutes} min`]);
    const remaining = RecoveryView.standbyLeft(option);
    const warn = crews.length && isNum(left) && left > 0 ? `<p class="ic-warn">${escapeHtml(`${left} ${left === 1 ? "duty" : "duties"} still over ${
      left === 1 ? "its" : "their"} limit after the ${crews.length === 1 ? "standby crew" : "standby crews"}`)}</p>` : "";
    // The standby crews left today at the bases the plan draws on (Task 2: the Standby crews rule keeps one at each base).
    const after = remaining.length ? `<p class="ic-sub">Left today after this plan</p><ul class="ic-list">${remaining.map(line => `<li>${escapeHtml(line)}</li>`).join("")}</ul>` : "";
    return infoCard(id, { title: "Crew flight-time limits", sub: text, rows: rows.slice(0, 6),
      listTitle: crews.length ? capFirst(RecoveryView.standbyName(crews)) : "", list: crews.map(RecoveryView.standbyLine),
      body: warn, tail: after,
      foot: [mark === "fail" && !crews.length ? "No standby crew covers the duty." : "", FDP_FOOT, crews.length ? Tips.explain("", "standby crew") : ""].filter(Boolean).join("\n") });
  }
  function lisbonCard(option, id) {
    const [, text] = capacityWords(option);
    const extra = (option.domain_assessments || []).find(entry => entry?.domain === "slots")?.required_additional_delay_minutes;
    return infoCard(id, { title: "Lisbon capacity", sub: text, rows: isNum(extra) && extra > 0 ? [["Extra delay to fit", `+${extra} min`]] : [],
      foot: "Movements each hour at Lisbon against its declared capacity." });
  }
  function passengersCard(option, id) {
    const [mark] = passengerWords(option);
    const r = option.validation?.rebooking || {};
    const lose = RecoveryView.disrupted(option);
    const rows = [["Lose flight or connection", lose], ["Rebooked", r.rebooked_passengers], ["To Passenger Services", r.unplaced_passengers],
      ["Kept by car", r.transferred_passengers], ["Overnight", option.cost?.overnight_passengers], ["Owed compensation", RecoveryView.owed(option)]]
      .filter(([key, value]) => isNum(value) && (value > 0 || key === "Lose flight or connection")).map(([key, value]) => [key, plain.format(value)]);
    return infoCard(id, { title: "Passengers", sub: RESULT[mark], rows,
      foot: mark === "expert" ? "Passenger Services decides for those without a same-day seat" : "" });
  }
  function checkChips(option, base) {
    const chips = [["crew", "Crew", crewWords(option)[0], crewCard], ["lisbon", "Lisbon", capacityWords(option)[0], lisbonCard],
      ["passengers", "Passengers", passengerWords(option)[0], passengersCard]];
    return `<div class="chips" role="list" aria-label="Checks">${chips.map(([key, label, mark, card]) => {
      const id = `${base}-${key}`;
      return `<span class="chk" role="listitem" data-check="${key}" data-card="${escapeHtml(id)}" tabindex="0" aria-label="${escapeHtml(`${label}: ${RESULT[mark]}`)}"><i class="mark ${
        mark}" aria-hidden="true">${MARK[mark]}</i> ${escapeHtml(label)}</span>${card(option, id)}`;
    }).join("")}</div>`;
  }
  // Lose flight · no seat today · cost (prototype rev 2), each with its workings on hover or focus.
  const NO_SEAT_TIP = "Could not be rebooked within policy today. Passenger Services rebooks them, often on another airline.";
  function numberTiles(option, base) {
    const lose = RecoveryView.disrupted(option);
    const noSeat = unplaced(option);
    const cost = option.cost?.total_eur;
    const r = option.validation?.rebooking || {};
    const loseRows = [["Rebooked", r.rebooked_passengers], ["To Passenger Services", r.unplaced_passengers], ["Kept by car, not counted", r.transferred_passengers]]
      .filter(([, value]) => isNum(value) && value > 0).map(([key, value]) => [key, plain.format(value)]);
    const tile = (key, value, label, attrs, template, cls = "") => `<div class="nb${cls}" data-num="${key}"${attrs}><b>${value}</b><span>${escapeHtml(label)}</span>${template}</div>`;
    const cardAttrs = (id) => ` data-card="${escapeHtml(id)}" tabindex="0"`;
    const total = costHover(option, "plan");
    return `<div class="nums-c"><div class="numbers">
      ${tile("lose", lose !== null ? escapeHtml(plain.format(lose)) : "?", "lose flight", cardAttrs(`${base}-lose`),
        infoCard(`${base}-lose`, { title: "Lose flight or connection", sub: lose !== null ? `${plain.format(lose)} passengers` : "", rows: loseRows, foot: LOSE_TIP }),
        lose === 0 ? " zero" : "")}
      ${tile("noseat", noSeat !== null ? escapeHtml(plain.format(noSeat)) : "?", "no seat today", cardAttrs(`${base}-noseat`),
        infoCard(`${base}-noseat`, { title: "No seat today", sub: noSeat !== null ? `${plain.format(noSeat)} passengers` : "", body: `<p class="ic-msg">${escapeHtml(NO_SEAT_TIP)}</p>` }),
        noSeat > 0 ? " ps" : noSeat === 0 ? " zero" : "")}
      ${tile("cost", Number.isFinite(cost) ? escapeHtml(euro.format(cost)) : "?", "cost", total.attrs, total.template)}
    </div></div>`;
  }
  // What the card's footer offers: Approve on the recommended plan, Choose on the others; the plan carried out says how far.
  function cardActions(option, item, number, recommended) {
    const execution = item.execution;
    if (execution?.option_id === option.option_id) {
      const actions = execution.actions || [];
      const confirmed = actions.filter(action => action.status === "confirmed").length;
      const auto = execution.route === "auto" && execution.approved_by === "policy";
      const counted = `${confirmed} of ${actions.length} confirmed`;
      const [tone, text] = execution.status === "completed" ? ["good", `✓ Carried out${auto ? " automatically" : ""} · ${counted}`]
        : execution.status === "needs_attention" ? ["bad", `! ${auto ? "Ran automatically" : "Approved by OCC"} · stopped part-way`]
        : ["", `${auto ? "Running automatically" : "Approved by OCC"} · ${counted}`];
      // Passengers still waiting for Passenger Services say so on the status line, in amber (owner), not here.
      return `<span class="done-note ${tone}">${escapeHtml(text)}</span> <button class="ghost" type="button" data-step="carried">Details ›</button>`;
    }
    if (execution) return "";
    if (option.policy?.route === "blocked") return '<span class="note">Not possible</span>';
    if (option.requires_duty_manager !== true && option.policy?.route === "auto") return '<span class="note">Runs automatically</span>';
    return decisionControls(option, item, number, { primary: option === recommended, short: true });
  }
  // A plan in three numbers and what it does (prototype rev 2): its checks, the flights it moves and how it compares are in
  // the Plans and Decision steps and on the summary's hover cards.
  function planCard(option, item, shown, { other = "" } = {}) {
    const number = shown.indexOf(option) + 1;
    const base = `pc-${slug(option.option_id)}`;
    const whatIf = RecoveryView.whatIfLine(option);
    const recommended = shown.find(entry => entry.recommended && !entry.what_if) || null;
    const isRecommended = option === recommended && !item.execution;
    const executed = item.execution?.option_id === option.option_id;
    const auto = item.execution?.route === "auto" && item.execution?.approved_by === "policy";
    const ribbon = executed ? (auto ? (item.execution.status === "completed" ? "Ran automatically" : "Running automatically") : "Approved by OCC")
      : whatIf ? "What-if" : isRecommended ? "Recommended" : other;
    const classes = ["plan", isRecommended ? "recommended" : "", whatIf ? "what-if" : "", executed ? "chosen" : "", other ? "other" : "",
      option.policy?.route === "blocked" ? "blocked" : ""].filter(Boolean).join(" ");
    return `<article class="${classes}" data-plan-no="${number}" aria-label="${escapeHtml(`Plan ${number}: ${planName(option)}`)}">
      ${ribbon ? `<span class="ribbon">${escapeHtml(ribbon)}</span>` : ""}
      <header class="plan-head"><span class="rank">${number}</span><h4>${escapeHtml(planName(option))}</h4>${routeChip(option, item, `${base}-route`)}</header>
      ${numberTiles(option, base)}
      <div class="pc-checks">${stagedStrip(option, base) || playbookStrip(option, base) || (option.flights?.length ? "" : '<p class="plan-none">No flight changes</p>')}</div>
      <div class="pc-mid">${whatIf ? `<p class="what-if-note">${escapeHtml(whatIf)}</p>` : ""}</div>
      <div class="plan-actions">${cardActions(option, item, number, recommended)}</div>
    </article>`;
  }
  // An automatic plan that ran (owner: the cancellations are "useless analysis"): the plan that ran, and the plans also
  // built folded into one line each until asked for.
  function othersFold(others, item, shown, executed) {
    const auto = item.execution?.route === "auto" && item.execution?.approved_by === "policy";
    const why = auto ? "Not needed: the plan ran within every limit" : `Not chosen: OCC approved Plan ${shown.indexOf(executed) + 1}`;
    return `<section class="others-fold" aria-label="Other options checked"><h3>Other options checked (${others.length})</h3>
      <p class="why">${escapeHtml(why)}</p>
      <ul>${others.map(option => `<li><b>${escapeHtml(`Plan ${shown.indexOf(option) + 1}: ${planName(option)}`)}</b> <span class="c">${
        Number.isFinite(option.cost?.total_eur) ? `${escapeHtml(euro.format(option.cost.total_eur))} ` : ""}<span class="route ${escapeHtml(option.policy?.route || "none")}">${
        escapeHtml(routeText(option))}</span>${option.recommended && !option.what_if ? ' <span class="pl-tag rec">Recommended</span>' : ""}</span></li>`).join("")}</ul>
      <div><button class="secondary sm" type="button" data-others="show" aria-expanded="false">Show them</button></div></section>`;
  }
  // The cards for #plans: { className, html }, or { className, text } when there is no plan. A plan that ran folds the others
  // into one line each until they are unfolded (ctx.othersOpen).
  function cards(item, ctx) {
    const shown = use(item, ctx);
    if (!shown.length) {
      return { className: "plans empty", text: item.status === "assessing" ? "Plans appear when the orchestrator has built them."
        : item.status === "no_disruption" ? "No plan is needed."
        : item.manual_intervention ? "No plan can run: see above."
        : "No plan was built. Nothing was changed." };
    }
    const executed = item.execution ? shown.find(option => option.option_id === item.execution.option_id) || null : null;
    // A storm plan OCC approved: its Now part and its held part, side by side (prototype rev 2 storm views).
    if (executed && isObj(item.execution.storm)) return { className: "plans cols-2 stages", html: stormStages(item, shown) };
    if (executed && shown.length > 1) {
      const others = shown.filter(option => option !== executed);
      const auto = item.execution.route === "auto" && item.execution.approved_by === "policy";
      if (!env.othersOpen) return { className: "plans cols-2 folded", html: planCard(executed, item, shown) + othersFold(others, item, shown, executed) };
      return { className: `plans cols-${Math.min(shown.length, 3)}`,
        html: `<div class="plans-head"><button class="ghost" type="button" data-others="hide" aria-expanded="true">Hide other options (${others.length})</button></div>${
          shown.map(option => planCard(option, item, shown, option === executed ? {} : { other: auto ? "Not needed" : "Not chosen" })).join("")}` };
    }
    return { className: `plans cols-${shown.length === 4 ? 2 : Math.min(shown.length, 3)}`,
      html: shown.map(option => planCard(option, item, shown)).join("") };
  }
  // ---------- The case summary (owner-approved case summary prototype rev 2; owner: "would be better
  // to really have a summary", "a summary of why we should choose plan X over plan Y"): what happened, its impact, the one thing OCC
  // must do and why this plan rather than the next, in labels and numbers, each figure's detail on hover, focus or tap. Every figure
  // is the case's own: its plans' flights, passengers, crews and cost, its execution and the rules in force. ----------
  const unplaced = (option) => (isNum(option?.validation?.rebooking?.unplaced_passengers) ? option.validation.rebooking.unplaced_passengers : null);
  const cancelledCount = (option) => (Array.isArray(option?.flights) ? option.flights.filter(flight => flight?.status === "cancelled").length : null);
  // The crew duties a plan takes over their flight-time limit before any standby crew, with the base of the standby crew offered for each.
  function overLimit(option) {
    const duties = (option?.domain_assessments || []).find(entry => entry?.domain === "crew")?.duties;
    const reserves = (Array.isArray(option?.crew_reserves) ? option.crew_reserves : []).filter(isObj);
    return (Array.isArray(duties) ? duties : []).filter(duty => duty?.status === "fail" && isNum(duty.modeled_fdp_minutes) &&
      isNum(duty.modeled_fdp_limit_minutes) && duty.modeled_fdp_minutes > duty.modeled_fdp_limit_minutes)
      .map(duty => ({ duty: String(duty.duty_id || ""), minutes: duty.modeled_fdp_minutes - duty.modeled_fdp_limit_minutes,
        base: reserves.find(crew => crew.duty_id === duty.duty_id)?.base || null }));
  }
  // What happened, in a few words: the cause, or the storm's hazard.
  const CAUSE = { aircraft_defect: "Aircraft defect", late_inbound: "Late inbound aircraft", staff_shortage: "Staff shortage", crew_shortage: "Crew shortage",
    border_control: "Border-control queue", baggage: "Baggage" };
  function disruptionWords(item) {
    const d = item.disruption || {};
    const storm = RecoveryView.storm(d);
    if (storm) return `${storm.hazard} forecast`;
    const cause = String(d.cause || (d.type === "message" ? "" : d.type) || "");
    return CAUSE[cause] || capFirst(cause.replaceAll("_", " ")) || "Disruption report";
  }
  function whatTile(item) {
    const d = item.disruption || {};
    const storm = RecoveryView.storm(d);
    // A storm confirmed or stood down by a weather update (Task 2): the update as received, on hover.
    const update = storm ? stormUpdate(item) : null;
    if (update && ["confirmed", "dropped"].includes(update.status)) {
      const report = reportOf(update);
      const confirmed = update.status === "confirmed";
      const window = update.window || storm.window;
      const said = confirmed ? `${storm.hazard} ${window}${isNum(update.rate_per_hour) ? ` · rate ${update.rate_per_hour} an hour` : ""}` : `No ${storm.hazard.toLowerCase()} ${window}`;
      const card = infoCard("sum-what", { title: report.title, wide: true,
        body: `${update.description ? `<pre class="ic-code">${escapeHtml(update.description)}</pre>` : ""}<p class="ic-msg">${escapeHtml(said)}</p>`,
        rows: [["Received", lisbonClock(update.at)], ["Forecast", storm.title]].filter(([, value]) => value),
        foot: item.execution ? "" : "Nothing is sent until OCC approves a plan." });
      return `<div class="tile t-what"><p class="tl">Disruption</p>
        <p class="big dotted" data-card="sum-what" tabindex="0">${escapeHtml(confirmed ? `${storm.hazard} confirmed` : "Forecast dropped")}</p>${card}
        <p class="sub">${escapeHtml([report.name, lisbonClock(update.at)].filter(Boolean).join(" · "))}</p></div>`;
    }
    const source = item.source?.system || (["event", "fabric"].includes(item.source?.kind) ? "A system event" : "OCC");
    const read = d.type === "flight_delay" ? `${d.flight_no} · ${d.delay_minutes} min late`
      : d.type === "aircraft_defect" ? `${d.scope?.aircraft_reg || item.defect?.aircraft_reg || ""} · ${duration(item.defect?.unavailable_minutes)}` : storm ? storm.reading : "";
    const message = d.message || d.text || "";
    const body = storm ? `${storm.forecast ? `<pre class="ic-code">${escapeHtml(storm.forecast)}</pre>` : ""}<p class="ic-msg">${escapeHtml(storm.plain)}</p>`
      : message ? `<p class="ic-msg">${escapeHtml(message)}</p>` : "";
    const card = infoCard("sum-what", { title: storm ? "TAF (Terminal Aerodrome Forecast) as received" : disruptionWords(item), wide: Boolean(storm), body,
      rows: [["Reported by", source], ["At", lisbonClock(item.created_at)], ["Recorded as", read]].filter(([, value]) => value),
      foot: storm ? "A TAF is the airport's official weather forecast for the next 24–30 hours: a forecast, not an observation." : "" });
    return `<div class="tile t-what"><p class="tl">Disruption</p>
      <p class="big dotted" data-card="sum-what" tabindex="0">${escapeHtml(disruptionWords(item))}</p>${card}</div>`;
  }
  // The passengers a plan affects, in its own figures (the most of these is the summary's count).
  function affectedBy(option) {
    const told = matched(factLines(rowsOf(option).find(row => row.id === "PAX-NOTICE")), /(\d[\d,]*) passengers told\b/);
    return [["Lose flight or connection", RecoveryView.disrupted(option)], ["Owed care", option?.cost?.meals_passengers], ["Owed compensation", RecoveryView.owed(option)],
      ["Overnight", stranded(option)], ["Told their new times", told]].filter(([, n]) => isNum(n) && n > 0);
  }
  const stat = (key, value, label, card = "", tone = "") => `<div class="stat${tone ? ` ${tone}` : ""}" data-stat="${key}"${
    card ? ` data-card="sum-${key}" tabindex="0"` : ""}><b>${escapeHtml(value)}</b> <span>${escapeHtml(label)}</span>${card}</div>`;
  function impactTile(item, plans, subject, number) {
    const flights = new Map();
    for (const option of plans) for (const flight of option.flights || []) if (flight?.flight_no && !flights.has(flight.flight_no)) flights.set(flight.flight_no, flight);
    if (!flights.size) for (const no of item.disruption?.scope?.flights || []) flights.set(no, { flight_no: no });
    const names = [...flights.keys()];
    const stats = [stat("flights", plain.format(names.length), names.length === 1 ? "flight" : "flights", names.length ? infoCard("sum-flights", {
      title: nOf(names.length, "flight", "flights"), sub: "in the case's plans",
      body: `<p class="ic-msg">${escapeHtml(names.length > 24 ? `${names.slice(0, 24).join(" · ")} · ${names.length - 24} more` : names.join(" · "))}</p>` }) : "")];
    const affected = subject ? affectedBy(subject) : [];
    if (affected.length) {
      // The most the plan affects in its own figures, never the passengers booked: said so on the figure and its card (review).
      const most = Math.max(...affected.map(([, n]) => n));
      stats.push(stat("passengers", plain.format(most), most === 1 ? "passenger affected" : "passengers affected", infoCard("sum-passengers", {
        title: `At least ${nOf(most, "passenger", "passengers")}`, sub: `affected by Plan ${number(subject)} · ${planName(subject)}`,
        rows: affected.map(([label, n]) => [label, plain.format(n)]),
        foot: "The most the plan affects in its own figures; passengers who keep their times are not counted, nor are all passengers booked." })));
    }
    // The crews the disruption takes over their duty limit in the plan that moves the most, before any standby crew: they need cover
    // (a standby crew) or a cancellation. Never "over limit": after the plan's standby crews none may be (plan.outcomes).
    const over = plans.map(overLimit).sort((a, b) => b.length - a.length)[0] || [];
    stats.push(stat("crews", plain.format(over.length), over.length === 1 ? "crew needs cover" : "crews need cover", over.length ? infoCard("sum-crews", {
      title: over.length === 1 ? "1 crew needs cover" : `${over.length} crews need cover`, sub: "a standby crew or a cancellation",
      list: over.map(entry => `${entry.base ? `${RecoveryView.airportName(entry.base)} crew` : entry.duty} · ${entry.minutes} min over its duty limit`), foot: FDP_FOOT }) : "",
    over.length ? "warn" : ""));
    if (subject && isNum(subject.cost?.total_eur)) stats.push(stat("cost", euro.format(subject.cost.total_eur), `plan ${number(subject)}`, costCard(subject, null, "sum-cost")));
    return `<div class="tile t-impact"><p class="tl">Impact</p>
      <div class="stats">${stats.join(" ")}</div></div>`;
  }
  // When OCC must decide by: a storm plan in two stages by its first standby crew's call-by time; any other plan by the first standby
  // crew's call time, else the first departure the plan changes, as scheduled.
  function decideBy(option) {
    if (stagesOf(option) && callBys(option).length) return `By ${callBys(option)[0].callBy} · standby call`;
    const calls = RecoveryView.standbyCrews(option).map(crew => crew.call).filter(Boolean).sort();
    if (calls.length) return `Standby call ${calls[0]}`;
    const first = RecoveryView.flightRows(option, env.originals).find(row => row.change && row.change !== "On time" && row.was);
    return first ? `Before ${first.was} · ${first.flight}` : "";
  }
  // The one thing OCC must do: approve the plan, act on passengers still waiting for Passenger Services, or nothing. The live
  // announcement is a node outside the band (app.js #summary-live): the band's markup is replaced on each redraw.
  function actionTile(item, model, shown, subject, number) {
    const execution = item.execution;
    const tile = (tone, label, body, sub = "", pulse = true) => `<div class="tile t-todo${tone ? ` ${tone}` : ""}"><p class="tl">${
      pulse ? '<i class="pulse" aria-hidden="true"></i>' : ""}${escapeHtml(label)}</p>
      ${body}${sub ? ` <p class="sub">${escapeHtml(sub)}</p>` : ""}</div>`;
    const big = (text, cls = "") => `<p class="big${cls ? ` ${cls}` : ""}">${escapeHtml(text)}</p>`;
    if (item.manual_intervention) {
      return tile("bad", "Your action", `<p class="big dotted" data-card="sum-manual" tabindex="0">Needs manual action</p>${infoCard("sum-manual",
        { title: "Needs manual action", body: `<p class="ic-msg">${escapeHtml(item.manual_intervention.message || item.stage || "")}</p>` })}`, "No plan can run in ARROW");
    }
    if (execution) {
      const auto = execution.route === "auto" && execution.approved_by === "policy";
      const review = RecoveryView.awaitingReview(execution);
      const heading = RecoveryView.executionHeading(execution);
      // Passengers waiting for Passenger Services from the start of the execution, whatever else is still open (review),
      // unless the plan stopped: then OCC's first action is the stop.
      if (review && execution.status !== "needs_attention") {
        return tile("action", "Action needed", `<button class="amber-btn" type="button" data-step="carried">Review ${
          escapeHtml(plain.format(Number(review.passengers) || 0))} passengers</button>`,
          execution.status === "completed" ? "Passenger Services · no seat today" : heading.heading.replace(/ · \d[\d,]* passengers await Passenger Services$/, ""));
      }
      // A storm plan in two stages: held with its decide-by time, carried out once confirmed, back with OCC, or stood down.
      const storm = isObj(execution.storm) ? execution.storm : null;
      if (storm && execution.status === "held") {
        const ran = shown.find(option => option.option_id === execution.option_id);
        return tile("wait", ran ? `Approved · plan ${number(ran)}` : "Approved by OCC", big(storm.decide_by ? `Held · decide by ${storm.decide_by}` : "Held until confirmed"),
          "Now done · rest held");
      }
      if (storm && execution.status === "stood_down" && storm.confirmed_after_stand_down) {
        // The storm came after all, after the stand-down: OCC decides again (trail.case_state "needs").
        return tile("", "Your action", big("Forecast confirmed after stand-down · decide again"));
      }
      if (storm && execution.status === "stood_down") {
        // What the stand-down actually sent, on hover (review): nothing, or the releases and notices confirmed.
        const sent = (execution.actions || []).some(action => action?.status === "confirmed");
        return tile("done", "Your action", `<p class="big good dotted" data-card="sum-stood-down" tabindex="0">✓ Stood down</p>${infoCard("sum-stood-down",
          { title: "Stood down", body: `<p class="ic-msg">${escapeHtml(heading.message)}</p>` })}`, sent ? "Nothing held was sent" : "Nothing was sent", false);
      }
      if (storm && execution.status === "needs_attention" && storm.returned) return tile("bad", "Your action", big("Back to you"), capFirst(String(storm.returned)));
      if (storm && execution.status === "completed") {
        const sent = lisbonClock(lastEvent(item, "storm_released")?.at);
        return tile("done", "Your action", big("✓ Forecast confirmed · carried out", "good"), sent ? `Sent ${sent}` : "", false);
      }
      if (execution.status === "completed") {
        const handled = RecoveryView.handledReview(execution);
        return tile("done", "Your action", big("✓ Nothing to do", "good"), [auto ? "Ran automatically · OCC informed" : "Approved by OCC · carried out",
          handled ? `${plain.format(Number(handled.passengers) || 0)} passengers handled` : ""].filter(Boolean).join(" · "), false);
      }
      // A stopped plan: what stopped it, and what OCC should check before acting on hover or focus (the status line is hidden while
      // the summary shows; review).
      if (execution.status === "needs_attention") {
        return tile("bad", "Your action", `<p class="big dotted" data-card="sum-attention" tabindex="0">Needs attention</p>${infoCard("sum-attention",
          { title: heading.heading, body: `<p class="ic-msg">${escapeHtml(heading.message)}</p>` })}`, heading.heading.replace(/^Needs attention · /, ""));
      }
      return tile("wait", auto ? "Running automatically" : "Approved by OCC", big(heading.heading), "Nothing to do now");
    }
    if (item.status !== "assessment_ready") return tile("", "Status", big(capFirst(String(item.stage || item.status || "").replaceAll("_", " "))), "", false);
    const open = shown.filter(option => option.policy?.route !== "blocked");
    if (!open.length) return tile("bad", "Your action", big("No plan can run"), "ARROW's rules block every plan", false);
    if (model?.choose || !subject) return tile("", "Your action", big("Choose a plan"), `OCC chooses · ${open.length} plans`);
    if (subject.policy?.route === "auto" && subject.requires_duty_manager !== true) return tile("wait", "Your action", big("Runs automatically"), "Within every limit · OCC informed", false);
    const by = decideBy(subject);
    // A weather update recorded before OCC decided (Task 2): shown, and nothing is sent until a plan is approved.
    const early = isObj(item.storm) ? item.storm.state : null;
    const note = early === "confirmed" ? "Forecast confirmed · nothing sent yet" : early === "dropped" ? "Forecast dropped · nothing sent yet" : by;
    const decided = (item.events || []).filter(event => event.event === "duty_manager_decision" && event.option_id === subject.option_id).at(-1);
    if (decided) return tile("wait", "Your action", big(`You ${decided.decision === "approve" ? "approved" : "rejected"} plan ${number(subject)}`), "", false);
    if (env.approve !== true) return tile("", "Your action", big("Waiting for OCC"), note);
    return tile("", "Your action", `<button class="primary" type="button" data-option-id="${escapeHtml(subject.option_id)}" data-decision="approve"${
      env.ready ? "" : " disabled"}>Approve plan ${number(subject)}</button>`, note);
  }
  // Why this plan, not the next: a verdict in a few words and three to five trade-offs, each signed and marked better or worse.
  const KIND = { delay: "delay", cancel: "cancel", swap: "swap" };
  function whyTitle(subject, other, number) {
    const [mine, theirs] = [KIND[subject.kind], KIND[other.kind]];
    return mine && theirs && mine !== theirs ? `Why plan ${number(subject)} · ${capFirst(mine)}, not ${theirs}` : `Why plan ${number(subject)}, not plan ${number(other)}`;
  }
  function verdictWords(subject, other) {
    const [a, b] = [RecoveryView.disrupted(subject), RecoveryView.disrupted(other)];
    const [x, y] = [subject.cost?.total_eur, other.cost?.total_eur];
    const money = !isNum(x) || !isNum(y) ? "" : x > y ? `for ${euro.format(x - y)} more` : x < y ? `for ${euro.format(y - x)} less` : "at the same cost";
    if (a === null || b === null) return capFirst(money.replace(/^for /, "")) || "As the orchestrator recommends";
    // As the prototype words it: "247 more keep their flight, €9,060 more"; "Nobody loses a flight, for €183,489 less".
    if (a < b) return a === 0 ? `Nobody loses a flight${money ? `, ${money}` : ""}` : `${plain.format(b - a)} more keep their flight${money ? `, ${money.replace(/^for /, "")}` : ""}`;
    if (a > b) return x < y ? `${euro.format(y - x)} less, but ${plain.format(a - b)} more lose their flight` : `${plain.format(a - b)} more lose their flight${money ? `, ${money}` : ""}`;
    return money ? `As many keep their flight, ${money}` : "As many keep their flight";
  }
  // Each trade-off: the figure of a plan, its format and whether more of it is better (passengers who keep their flight).
  const TRADES = [
    { key: "keep", label: "Keep flight", of: (o) => RecoveryView.disrupted(o), fmt: plain, keep: true, what: "Lose flight or connection" },
    { key: "cost", label: "Cost", of: (o) => (isNum(o?.cost?.total_eur) ? o.cost.total_eur : null), fmt: euro, what: "Indicative cost" },
    { key: "owed", label: "Compensation", of: (o) => RecoveryView.owed(o), fmt: plain, what: "Passengers owed EU261 compensation" },
    { key: "overnight", label: "Overnight", of: (o) => stranded(o), fmt: plain, what: "Passengers overnight" },
    { key: "noseat", label: "No seat today", of: unplaced, fmt: plain, what: "No seat today: to Passenger Services" },
    { key: "standby", label: "Standby crews", of: (o) => RecoveryView.standbyCrews(o).length, fmt: plain, what: "Standby crews called" },
    { key: "cancel", label: "Cancellations", of: cancelledCount, fmt: plain, what: "Flights cancelled" }
  ];
  const deltaTile = (key, tone, label, value, sub, card) => `<li class="dt ${tone}" data-delta="${key}" data-card="sum-dt-${key}" tabindex="0" aria-label="${
    escapeHtml(`${label} ${value}${sub ? ` ${sub}` : ""}, ${tone === "good" ? "better" : "worse"}`)}"><i class="wm ${tone}" aria-hidden="true">${tone === "good" ? "✓" : "!"}</i> <span class="dl">${
    escapeHtml(label)}</span> <b>${escapeHtml(value)}</b>${sub ? ` <small>${escapeHtml(sub)}</small>` : ""}${card}</li>`;
  function tradeoffs(subject, other, number) {
    return TRADES.map(trade => {
      const [mine, theirs] = [trade.of(subject), trade.of(other)];
      if (!isNum(mine) || !isNum(theirs) || mine === theirs) return null;
      const gap = trade.keep ? theirs - mine : mine - theirs;
      const tone = (trade.keep ? gap > 0 : gap < 0) ? "good" : "bad";
      return deltaTile(trade.key, tone, trade.label, signed(gap, (n) => trade.fmt.format(n)), "", infoCard(`sum-dt-${trade.key}`, { title: trade.what,
        sub: tone === "good" ? "better" : "worse", rows: [[`Plan ${number(subject)} · ${planName(subject)}`, trade.fmt.format(mine)],
          [`Plan ${number(other)} · ${planName(other)}`, trade.fmt.format(theirs)]] }));
    }).filter(Boolean).slice(0, 4);
  }
  // A plan that ran by itself: the automatic limits it stayed within, the ones it was routed under (policy.limits), else the rules in force.
  function ranTiles(subject) {
    const own = subject.policy?.limits;
    const recorded = isObj(own) && isNum(own.cost_cap_eur) && isNum(own.rebook_limit);
    const limits = recorded ? own : isObj(env.limits) ? env.limits : {};
    const late = Math.max(0, ...RecoveryView.lateMinutes(subject, env.originals).map(entry => entry.minutes).filter(isNum));
    const rebooked = subject.validation?.rebooking?.rebooked_passengers;
    const rows = [
      ["cost", "Cost", isNum(subject.cost?.total_eur) ? euro.format(subject.cost.total_eur) : null, isNum(limits.cost_cap_eur) ? `of ${euro.format(limits.cost_cap_eur)}` : "",
        isNum(limits.cost_cap_eur) ? `Cost limit for automatic plans ${euro.format(limits.cost_cap_eur)}${isNum(limits.version) ? ` · rules v${limits.version}` : ""}` : "Cost limit for automatic plans"],
      ["rebooked", "Rebooked", isNum(rebooked) ? plain.format(rebooked) : null, isNum(limits.rebook_limit) ? `of ${plain.format(limits.rebook_limit)}` : "",
        isNum(limits.rebook_limit) ? `Rebooking limit ${nOf(limits.rebook_limit, "passenger", "passengers")}` : "Rebooking limit"],
      ["cancel", "Cancellations", plain.format(cancelledCount(subject) || 0), "always OCC", "A cancellation always goes to OCC"],
      ["delay", "Longest delay", late > 0 ? `+${late} min` : null, "", "Delays past the EU261 care thresholds (2, 3 or 4 h by distance) go to OCC"]];
    return rows.filter(([, , value]) => value !== null).map(([key, label, value, sub, tip]) => deltaTile(key, "good", label, value, sub,
      infoCard(`sum-dt-${key}`, { title: label, sub: "within the automatic limit", body: `<p class="ic-msg">${escapeHtml(tip)}</p>` })));
  }
  // The recommendation's full comparison, on the verdict's hover: the orchestrator's reason, the plans side by side, what is done ahead
  // of a storm, the plans the rules block and a choice against ARROW's objectives.
  function recCard(item, model, shown, id) {
    const subject = model.subject;
    const number = (option) => shown.indexOf(option) + 1;
    const orchestrator = model.orchestrator;
    const said = orchestrator ? [orchestrator.pick ? `Recommended ${orchestrator.pick}.` : "", orchestrator.reason].filter(Boolean).join(" ") : "";
    const others = model.versus.map(entry => entry.other);
    const columns = subject && others.length ? (others.length === 1 ? [[`Plan ${number(others[0])}`, others[0]], [`Plan ${number(subject)}`, subject]]
      : [subject, ...others].sort((a, b) => number(a) - number(b)).map(option => [`Plan ${number(option)}`, option])) : [];
    const ahead = aheadOf(item, subject);
    const body = `${said ? `<p class="hc-q"><b>Orchestrator</b>${escapeHtml(said)}</p>` : ""}${columns.length ? compareTable(columns) : ""}${ahead}${
      model.blocked.map(entry => `<p class="ic-warn">${escapeHtml(`Not possible: ${entry.name} — ${entry.text}`)}</p>`).join("")}${
      model.against ? `<p class="ic-warn">${escapeHtml(`The orchestrator's choice goes against ARROW's objectives on ${model.against}.`)}</p>` : ""}`;
    return columns.length || said || model.blocked.length || ahead
      ? infoCard(id, { title: subject && others.length ? `${model.label} against ${others.length === 1 ? `Plan ${number(others[0])}` : "the other plans"}` : model.label || model.lead,
        wide: true, body, foot: OBJECTIVES }) : "";
  }
  function whyTile(item, model, shown, subject, number, plans) {
    const execution = item.execution;
    const agents = isNum(env.agents) ? `${nOf(env.agents, "agent", "agents")} · graph ›` : "Agents · graph ›";
    const links = `<div class="why-links"><button class="linkish" type="button" data-step="assessed">${escapeHtml(agents)}</button></div>`;
    const head = (title, verdict, card = "", flag = "") => `<div class="why-h"><p class="tl">${escapeHtml(title)}</p>
      <p class="verdict">${card ? `<span class="dotted" data-card="sum-verdict" tabindex="0">${escapeHtml(verdict)}</span>` : escapeHtml(verdict)}${flag}</p>${card}</div>`;
    const tile = (cls, top, tiles) => `<div class="tile t-why${cls}">${top}
      ${tiles.length ? `<ul class="deltas">${tiles.join(" ")}</ul>` : '<div class="deltas none"></div>'}
      ${links}</div>`;
    if (!model || !subject) {
      const reasons = plans.flatMap(option => reasonList((option.policy?.reasons || []).filter(r => !/^(OCC decides|within every domain)/i.test(r)), option).slice(0, 1)
        .map(reason => `Plan ${number(option)} · ${planName(option)}: ${reason}`));
      return tile(" stopped", head("Why", "No plan can run", infoCard("sum-verdict", { title: "No plan can run", list: reasons.length ? reasons : ["Checks incomplete"] })), []);
    }
    const card = recCard(item, model, shown, "sum-verdict");
    const flag = model.against ? ` <span class="rec-flag"${Tips.attrs(escapeHtml, `The orchestrator's choice goes against ARROW's objectives on ${model.against}.\n${OBJECTIVES}`,
      "Against ARROW's objectives")}>Against ARROW's objectives</span>` : "";
    if (execution?.option_id === subject.option_id && execution.route === "auto" && execution.approved_by === "policy") {
      return tile(" ran", head(`Why plan ${number(subject)} ran without OCC`, "Within every automatic limit · OCC informed", card), ranTiles(subject));
    }
    const others = plans.filter(option => option !== subject && option.policy?.route !== "blocked");
    const byObjectives = (a, b) => (RecoveryView.disrupted(a) ?? Infinity) - (RecoveryView.disrupted(b) ?? Infinity) ||
      (a.cost?.total_eur ?? Infinity) - (b.cost?.total_eur ?? Infinity);
    // OCC chooses: the least disruptive plan against the plan the comparison on hover and in the Decision step names (the cheapest).
    const versus = model.choose ? model.versus?.[0]?.other : null;
    const other = versus && others.includes(versus) ? versus : [...others].sort(byObjectives)[0];
    if (!other) {
      const route = subject.policy?.route;
      const verdict = route === "blocked" ? "Blocked by ARROW's rules" : model.blocked.length ? "The only plan the rules allow"
        : route === "auto" && subject.requires_duty_manager !== true ? "Within every limit" : "The only plan built";
      return tile(route === "blocked" ? " stopped" : "", head(`Why plan ${number(subject)}`, verdict, card, flag), []);
    }
    const verdict = verdictWords(subject, other);
    const title = model.choose ? `OCC chooses · ${nOf(others.length + 1, "plan", "plans")}` : whyTitle(subject, other, number);
    return tile(model.choose ? " choose" : "", head(title, model.choose ? `Plan ${number(subject)} is the least disruptive: ${verdict[0].toLowerCase()}${verdict.slice(1)}` : verdict,
      card, flag), tradeoffs(subject, other, number));
  }
  // The summary band for #summary: its tiles' markup, or "" while ARROW is still assessing or no plan was built.
  function summary(item, ctx = {}) {
    const shown = use(item, ctx);
    if (!item || item.status === "assessing" || !shown.length) return "";
    const own = shown.filter(option => !option.what_if);
    const plans = own.length ? own : shown;
    const model = recModel(item);
    const subject = model?.subject && shown.includes(model.subject) ? model.subject : null;
    const number = (option) => shown.indexOf(option) + 1;
    return [whatTile(item), impactTile(item, plans, subject, number), actionTile(item, model, shown, subject, number),
      whyTile(item, model, shown, subject, number, plans)].join("\n");
  }
  // Why the other plans (owner, Case 4: "people think why would you even consider cancelling"): when the recommended (or
  // first) delay plan takes a crew over its duty limit, one short line says by how much and what covers it, in labels and numbers;
  // "" for a storm, once a plan ran, or when no duty is over.
  function whyOthers(item, ctx) {
    const shown = use(item, ctx);
    if (item?.disruption?.type === "weather" || item?.execution || shown.length < 2) return "";
    const recommended = shown.find(option => option.recommended && !option.what_if);
    const delay = recommended?.kind === "delay" ? recommended : shown.find(option => option.kind === "delay" && !option.what_if);
    if (!delay) return "";
    const over = overLimit(delay);
    if (!over.length) return "";
    // The base of the standby crew taking each duty over its limit, when one base covers them all.
    const bases = over.map(entry => entry.base);
    const place = bases.every(Boolean) && new Set(bases).size === 1 ? `${RecoveryView.airportName(bases[0])} ` : "";
    const most = Math.max(...over.map(entry => entry.minutes));
    const one = over.length === 1;
    const crews = RecoveryView.standbyCrews(delay);
    const left = delay.validation?.crew_plan?.breaches_unresolved;
    const covered = crews.length > 0 && (left === 0 || (!isNum(left) && delay.policy?.route !== "blocked"));
    const cancels = shown.filter(option => option.kind === "cancel");
    const cancel = !cancels.length ? "" : cancels.some(option => (option.flights || []).filter(f => f?.status === "cancelled").length === 2)
      ? "a round trip is cancelled" : "flights are cancelled";
    const head = one ? `The delay puts a ${place}crew ${most} min over its duty limit` : `The delay puts ${over.length} ${place}crews up to ${most} min over their duty limits`;
    if (covered) return `${head}: ${one ? "a standby crew covers it" : "standby crews cover them"}${cancel ? `, or ${cancel}` : ""}.`;
    // Standby crews that cover only some of the duties (review): how many they cover and how many are still over.
    const still = isNum(left) ? Math.min(left, over.length) : over.length;
    if (crews.length && still < over.length) {
      return `${head}: ${crews.length === 1 ? "a standby crew covers" : "standby crews cover"} ${over.length - still}; ${still} still over${cancel ? `, so ${cancel}` : ""}.`;
    }
    return `${head}: ${one ? "no standby crew covers it" : "no standby crews cover them"}${cancel ? `, so ${cancel}` : ""}.`;
  }
  // The recommendation in full, in the Decision step (owner: "Recommendation plan 1, …"): the plan, against each other
  // plan in passengers and cost, the trade-off in words and where the cost difference comes from, each part's workings on hover
  // or focus. All from RecoveryView.recommendation, from the plans' own figures.
  function diffCard(model, entry, part, id) {
    return sidesCard(id, `${part.mine.label} ${part.text}`, [[model.label, part.mine], [`Plan ${entry.number} · ${planName(entry.other)}`, part.theirs]], part.mine.note);
  }
  // A cost part of two plans side by side, each with its workings.
  function sidesCard(id, head, sides, note) {
    const lines = (list) => list.map(line => `<p class="cc-line">${escapeHtml(line)}</p>`).join("");
    const side = ([label, own]) => `<div class="cc-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(euro.format(own.eur))}</b></div>${lines(own.summary)}`;
    return `<template id="${escapeHtml(id)}"><div class="cost-card">
      <p class="cc-head">${escapeHtml(head)}</p>
      ${sides.map(side).join("")}
      ${note ? `<p class="cc-foot">${escapeHtml(note)}</p>` : ""}</div></template>`;
  }
  function costDiffLine(model, entry, many) {
    const parts = entry.parts.map(part => {
      const id = `cost-rec-${slug(model.subject.option_id)}-${slug(entry.other.option_id)}-${part.key}`;
      return `<span class="part" data-part="${escapeHtml(part.key)}" data-card="${escapeHtml(id)}" tabindex="0">${escapeHtml(part.label)} <b>${
        escapeHtml(part.text)}</b></span>${part.note ? ` <span class="note">(${escapeHtml(part.note)})</span>` : ""}${diffCard(model, entry, part, id)}`;
    });
    return `<li class="rec-cost"><span class="lbl">${escapeHtml(many ? `Cost difference vs ${entry.name}:` : "Cost difference:")}</span> ${parts.join(SEP)}</li>`;
  }
  // Why the dearer plan costs more, in one line from the workings (owner); hover shows that part for both plans,
  // the dearer first and the difference signed from its side.
  function driverLine(model, entry, many) {
    const driver = entry.driver;
    const part = driver.part;
    const id = `cost-why-${slug(model.subject.option_id)}-${slug(entry.other.option_id)}`;
    return `<li class="rec-why"><span class="lbl">${escapeHtml(`Why ${driver.name} costs more${many ? ` than ${driver.than}` : ""}:`)}</span> <span class="why" data-card="${
      escapeHtml(id)}" tabindex="0">${escapeHtml(String(driver.text).replaceAll(" · ", ", "))}</span>${
      sidesCard(id, `${part.mine.label} ${part.text}`, [[driver.high, part.mine], [driver.low, part.theirs]], part.mine.note)}</li>`;
  }
  // The recommendation in full for #recommendation: its markup, or "" when there is nothing to recommend.
  function recommendation(item, ctx) {
    const shown = use(item, ctx);
    const model = recModel(item);
    if (!model) return "";
    const subject = model.subject;
    const lead = model.lead === "Recommended" ? `<span class="rec-lead"${Tips.attrs(escapeHtml, `ARROW's orchestrator recommends one plan; ARROW's code checks every plan against the rules. ${OBJECTIVES}`, "Recommended")}>Recommended</span>`
      : `<span class="rec-lead">${escapeHtml(String(model.lead).replace(" · ", ": "))}</span>`;
    const plan = model.plan ? `${model.joiner === " " ? " " : ": "}<span class="rec-no">${escapeHtml(model.plan)}</span>${model.name ? ", " : ""}` : model.name ? ": " : "";
    const chip = model.chip ? `<span class="rec-chip ${escapeHtml(model.chip.tone)}"${Tips.attrs(escapeHtml, Tips.explain("", model.chip.text))}>${escapeHtml(model.chip.text)}</span>` : "";
    const lines = [];
    if (model.both) lines.push(`<li>${escapeHtml(model.both)}</li>`);
    if (subject && !model.versus.length && subject.policy?.route !== "blocked") {
      const route = subject.policy?.route;
      const note = route === "auto" ? "Within every limit" : route === "occ" && !item.execution ? whoDecides(subject, item)[1] : "";
      if (note) lines.push(`<li class="rec-note">${escapeHtml(note)}</li>`);
    }
    for (const entry of model.versus) {
      lines.push(`<li class="rec-vs">${entry.segments.map(segment => (segment.text === " · " ? ", " : segment.tone
        ? `<span class="${escapeHtml(segment.tone)}">${escapeHtml(segment.text)}</span>` : escapeHtml(segment.text))).join("")}</li>`);
    }
    for (const entry of model.costs) if (entry.driver) lines.push(driverLine(model, entry, model.costs.length > 1));
    if (model.tradeoffs.length) lines.push(`<li class="rec-trade">${escapeHtml(model.tradeoffs.join(" "))}</li>`);
    for (const entry of model.costs) lines.push(costDiffLine(model, entry, model.costs.length > 1 || model.choose));
    for (const entry of model.blocked) {
      const why = reasonList(entry.reasons, entry.option).join("\n");
      lines.push(`<li class="rec-blocked"${Tips.attrs(escapeHtml, why, "Blocked by ARROW's rules")}><span class="lbl">Not possible:</span> ${
        escapeHtml(entry.name)} — ${escapeHtml(entry.text)}</li>`);
    }
    const orchestrator = model.orchestrator;
    const said = orchestrator ? [orchestrator.pick ? `Recommended ${orchestrator.pick.replace(" · ", ": ")}.` : "", orchestrator.reason].filter(Boolean).join(" ") : "";
    return `<div class="rec-head"><h3 id="rec-title">${lead}${plan}${model.name ? `<span class="rec-name">${escapeHtml(model.name)}</span>` : ""}</h3>${chip}</div>
      ${lines.length ? `<ul class="rec-lines">${lines.join("")}</ul>` : ""}
      ${model.against ? `<p class="against"${Tips.attrs(escapeHtml, OBJECTIVES, "ARROW's objectives")}>The orchestrator's choice goes against ARROW's objectives on ${escapeHtml(model.against)}.</p>` : ""}
      ${said ? `<p class="said"><span class="by">Orchestrator</span> ${TrailView.aiMark()} ${escapeHtml(said)}</p>` : ""}`;
  }
  // The Decision step below the recommendation: each plan by part (each part's workings on hover or focus), what each other
  // plan does differently, who decides each plan with its controls, each plan's playbook and its cost breakdown.
  function decisionStep(item, ctx) {
    const shown = use(item, ctx);
    if (!shown.length) return `<p class="muted">${item.status === "assessing" ? "The decision follows once plans are built." : "No plan to decide."}</p>`;
    const number = (option) => shown.indexOf(option) + 1;
    const reference = shown.find(option => option.recommended && !option.what_if) || null;
    const keys = COST_PARTS.map(([key]) => key).filter(key => shown.some(option => RecoveryView.costParts(option).some(part => part.key === key)));
    const cell = (option, key) => {
      if (key === "total") {
        const hover = costHover(option, "cmp");
        return `<td data-plan-no="${number(option)}" data-part="total"${hover.attrs}>${Number.isFinite(option.cost?.total_eur) ? escapeHtml(euro.format(option.cost.total_eur)) : "?"}${hover.template}</td>`;
      }
      const part = RecoveryView.costParts(option).find(entry => entry.key === key);
      if (!part) return "<td>—</td>";
      const hover = costHover(option, "cmp", part);
      return `<td data-plan-no="${number(option)}" data-part="${escapeHtml(key)}"${hover.attrs}>${escapeHtml(euro.format(part.eur))}${hover.template}</td>`;
    };
    const count = (value) => `<td>${value === null ? "?" : escapeHtml(plain.format(value))}</td>`;
    // Passengers stranded overnight in a storm and crews still over their limit after the standby crews, when a plan has any.
    const weather = item.disruption?.type === "weather";
    const row = (key, label, get, format, { cls = "", tip = "" } = {}) => (shown.some(option => isNum(get(option)))
      ? `<tr data-row="${key}"${cls ? ` class="${cls}"` : ""}><th${Tips.attrs(escapeHtml, tip, label)}>${escapeHtml(label)}</th>${shown.map(option => {
        const value = get(option);
        return `<td>${isNum(value) ? escapeHtml(format.format(value)) : "—"}</td>`;
      }).join("")}</tr>` : "");
    const table = `<section class="dr-sec"><h3>Plans by part</h3><div class="cmp-wrap"><table class="cmp-parts">
      <thead><tr><th></th>${shown.map(option => `<th${Tips.attrs(escapeHtml, planName(option))}>Plan ${number(option)}</th>`).join("")}</tr></thead>
      <tbody>${weather && shown.some(option => stranded(option) > 0) ? row("overnight", "Stranded overnight", stranded, plain) : ""}
        <tr data-row="lose"><th>Lose flight or connection</th>${shown.map(option => count(RecoveryView.disrupted(option))).join("")}</tr>
        <tr data-row="owed"><th>Owed compensation</th>${shown.map(option => count(RecoveryView.owed(option))).join("")}</tr>
        ${shown.some(option => crewsOver(option) > 0) ? row("crews", "Crews over limit", crewsOver, plain, { tip: CREWS_OVER_TIP }) : ""}
        <tr data-row="cost" class="sum"><th>Indicative cost</th>${shown.map(option => cell(option, "total")).join("")}</tr>
        ${keys.map(key => `<tr data-row="${escapeHtml(key)}" class="part"><th>${escapeHtml(COST_PARTS.find(([k]) => k === key)[1])}</th>${shown.map(option => cell(option, key)).join("")}</tr>`).join("")}
      </tbody></table></div></section>`;
    const others = reference ? shown.filter(option => option !== reference) : [];
    const different = others.map(option => [option, factsOf(option, reference, item)]).filter(([, facts]) => facts.length);
    const differently = different.length ? `<section class="dr-sec"><h3>What each other plan does differently</h3><ul class="plan-whys">${different.map(([option, facts]) =>
      `<li class="plan-why" data-plan-no="${number(option)}"><b>${escapeHtml(`Plan ${number(option)}: ${planName(option)}`)}</b> <span class="vs">vs Plan ${number(reference)}</span> <span class="facts">${
        factLine(facts, FIRST)}</span></li>`).join("")}</ul></section>` : "";
    const who = `<section class="dr-sec"><h3>Who decides</h3>${shown.map(option => {
      const [tone, text] = whoDecides(option, item);
      const controls = decisionControls(option, item, number(option), { primary: option === reference, reject: true, approve: false });
      // OCC's comment on its decision (owner), as kept on the audited decision.
      const said = (item.events || []).filter(e => e.event === "duty_manager_decision" && e.option_id === option.option_id).at(-1)?.comment;
      // The checks each plan passed, with their detail on hover or focus (on the plan cards until the summary).
      return `<div class="who-plan" data-plan-no="${number(option)}"><p class="who-name"><b>${escapeHtml(`Plan ${number(option)}: ${planName(option)}`)}</b></p>
        <p class="who ${escapeHtml(tone)}">${escapeHtml(text)}</p>${said ? `<p class="who-said"><b>OCC comment</b>“${escapeHtml(said)}”</p>` : ""}${
        checkChips(option, `dc-${slug(option.option_id)}`)}${controls ? `<div class="plan-actions">${controls}</div>` : ""}</div>`;
    }).join("")}</section>`;
    const playbooks = shown.map(option => checklist(option, number(option))).filter(Boolean).join("");
    const breakdowns = shown.map(option => costDetail(option, number(option))).filter(Boolean).join("");
    return `${stagePlan(item, shown, reference)}${table}${differently}${who}${playbooks ? `<section class="dr-sec pb-sec"><h3>Playbook</h3>${playbooks}</section>` : ""}${
      breakdowns ? `<section class="dr-sec"><h3>Cost breakdown</h3>${breakdowns}</section>` : ""}`;
  }
  // The Plans step: every plan in one row, its numbers and route; a row leads to its card.
  function plansStep(item, ctx) {
    const shown = use(item, ctx);
    const executed = item.execution?.option_id;
    const note = shown.length < 2 ? ""
      : shown.some(option => option.what_if) ? "ARROW's plans ranked by indicative cost; what-ifs follow in the order asked."
      : `Ranked by indicative cost${shown.some(option => option.recommended) ? "; ARROW's orchestrator recommends one" : ""}.`;
    if (!shown.length) {
      return `<p class="muted">${item.status === "assessing" ? "Plans appear when the orchestrator has built them." : "No plan was built."}</p><p id="plans-note" class="dr-foot"></p>`;
    }
    const tag = (option) => option.option_id === executed ? ["ran", item.execution.approved_by === "policy" ? "Ran" : "Approved"]
      : option.what_if ? ["what-if", "What-if"] : option.recommended && !executed ? ["rec", "Recommended"] : executed ? ["", "Not needed"] : null;
    return `<div class="pl-rows">${shown.map((option, index) => {
      const t = tag(option);
      const lose = RecoveryView.disrupted(option), noSeat = unplaced(option);
      const nums = [lose !== null ? `<span><b>${escapeHtml(plain.format(lose))}</b> lose</span>` : "", noSeat !== null ? `<span><b>${escapeHtml(plain.format(noSeat))}</b> no seat</span>` : "",
        Number.isFinite(option.cost?.total_eur) ? `<span><b>${escapeHtml(euro.format(option.cost.total_eur))}</b></span>` : "",
        `<span class="route ${escapeHtml(option.policy?.route || "none")}">${escapeHtml(routeText(option))}</span>`].filter(Boolean).join(" ");
      // Each plan's flights (on the plan cards until the summary): a few words, opening to a compact table.
      const flights = flightsBlock(option);
      return `<div class="pl-plan" data-plan-no="${index + 1}"><button class="pl-row" type="button" data-plan="${index + 1}"><span class="pl-top"><span class="rank">${index + 1}</span> <b>${escapeHtml(planName(option))}</b>${
        t ? ` <span class="pl-tag ${t[0]}">${escapeHtml(t[1])}</span>` : ""}</span>
        <span class="pl-nums">${nums}</span></button>${flights}</div>`;
    }).join("")}</div>
      <p id="plans-note" class="dr-foot">${escapeHtml(note)}</p>`;
  }
  // What ARROW did (owner: "start from a higher level and drill down"): one line overall, one
  // row per outcome in plain words, and each row opens to the calls ARROW sent (older executions: their
  // actions), explained on hover.
  const DONE_MARK = { confirmed: ["pass", "✓"], pending: ["occ", "…"], not_started: ["occ", "…"], blocked: ["occ", "…"],
    refused: ["fail", "✕"], failed: ["fail", "!"], unknown: ["unknown", "?"], not_sent: ["unknown", "–"], held: ["occ", "…"] };
  const DONE_STATE = { confirmed: "Confirmed", pending: "Waiting", not_started: "Not started", blocked: "Not started",
    refused: "Refused", failed: "Failed", unknown: "Outcome unknown", not_sent: "Not sent", held: "Held" };
  function doneGroup(group) {
    const [cls, glyph] = DONE_MARK[group.status] || DONE_MARK.unknown;
    const n = group.total;
    const word = DONE_STATE[group.status] || "Outcome unknown";
    const state = group.status === "confirmed" || n === 1 || group.unsent ? word
      : group.partly ? "Partly done" : `${group.confirmed} of ${n} confirmed · ${word.toLowerCase()}`;
    const table = group.table ? `<div class="done-scroll"><table class="done-table"><thead><tr>${group.table.head.map(cell =>
      `<th>${escapeHtml(cell)}</th>`).join("")}</tr></thead><tbody>${group.table.rows.map(row =>
      `<tr>${row.map(cell => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    const actions = group.actions.map(action => {
      // Each action keeps the calls ARROW sent (not those of earlier versions): one row per call, named by its recipient.
      if (Array.isArray(action.calls) && action.calls.length) {
        return action.calls.map(call => `<li class="call-item">${RecoveryView.callRow(call, { data: { call: call?.call_id || "" } })}</li>`).join("");
      }
      // Once the execution has stopped, an action never sent will not be; a storm's held action waits for the storm.
      const unsent = group.stopped && ["blocked", "not_started", "stood_down"].includes(action.status);
      const shownAs = unsent ? (group.held ? "held" : "not_sent") : action.status;
      const [aClass, aGlyph] = DONE_MARK[shownAs] || ["unknown", "·"];
      const name = Tips.system(action.label);
      const said = Tips.systemWords(action.message || action.summary || "");
      const tip = Tips.attrs(escapeHtml, Tips.explain(name, said), name);
      return `<li${tip}><span class="mark ${aClass}">${aGlyph}</span><span><b>${escapeHtml(name)}</b> · ${escapeHtml(said)}</span>
        <span class="state">${escapeHtml(unsent ? DONE_STATE[shownAs] : RecoveryView.actionStatusText(action.status))}</span></li>`;
    }).join("");
    return `<details class="done-group" data-group="${escapeHtml(group.key)}"><summary>
        <span class="mark ${cls}">${glyph}</span>
        <span class="what"><span class="title">${escapeHtml(group.title)}</span> <b>${escapeHtml(group.headline.join(" · "))}</b>${
          group.sub ? ` <span class="sub">${escapeHtml(group.sub)}</span>` : ""}</span>
        <span class="state">${escapeHtml(state)}</span></summary>
      <div class="done-detail">${table}<ul>${actions}</ul></div></details>`;
  }
  // What ARROW did for #done: its markup, or null when nothing was sent. ctx.review: this user marks Passenger Services
  // reviews; ctx.reviewConfirm: the mark is being confirmed.
  function done(item, ctx) {
    use(item, ctx);
    const execution = item.execution;
    if (!execution || !(execution.actions || []).length) return null;
    const review = RecoveryView.awaitingReview(execution);
    const handled = RecoveryView.handledReview(execution);
    const recorded = (item.events || []).filter(e => e.event === "passenger_review_recorded").at(-1);
    const confirming = review && env.reviewConfirm;
    const control = !env.review ? ""
      : confirming ? `<button id="review-confirm" class="primary" type="button">Yes, ${escapeHtml(review.passengers)} handled</button>
          <button id="review-cancel" class="secondary" type="button">Cancel</button>`
      : `<button id="review-handled" class="secondary" type="button">Mark ${escapeHtml(review?.passengers)} passengers handled</button>`;
    const line = RecoveryView.outcomeLine(execution);
    // A storm plan: its stages, each with its outcomes (case summary and held storm spec §2).
    const groups = isObj(execution.storm) ? stormDone(item, execution) : `<div class="done-groups">${RecoveryView.outcomeGroups(execution).map(doneGroup).join("")}</div>`;
    // Before it wrote, ARROW read its sources again (the trail's rechecks): one folded line above the writes (owner: "I again
    // see repetitive calls").
    const again = (Array.isArray(env.rechecks) ? env.rechecks : []).filter(isObj);
    const rechecked = again.length ? `<details class="done-again" data-group="again"><summary${Tips.attrs(escapeHtml,
      "Before it writes, ARROW reads every source the plan was checked against again, and stops if one has changed since the approval.",
      "Re-checked before writing")}><span class="mark pass">✓</span><span class="what">${escapeHtml(`Re-checked ${again.length} source${again.length === 1 ? "" : "s"} before writing`)}</span></summary>
      <ul class="done-again-list">${again.map(call => `<li><b>${escapeHtml(Tips.system(call.system))}</b> · ${escapeHtml(call.label || "")}${
        call.summary ? `<span class="sub"> · ${escapeHtml(call.summary)}</span>` : ""}</li>`).join("")}</ul></details>` : "";
    return `<h3 id="done-title">What ARROW did in the airline systems</h3>
      <p class="done-line"><span class="mark ${line.tone}">${{ pass: "✓", occ: "…", fail: "!" }[line.tone]}</span> <b>${escapeHtml(line.text)}</b></p>
      ${rechecked}${groups}
      ${review ? `<div class="review">${escapeHtml(review.passengers)} passengers could not be rebooked within policy and wait for Passenger Services.
        ${control}</div>` : ""}
      ${handled ? `<p class="review handled">✓ ${escapeHtml(handled.passengers)} passengers handled by Passenger Services${
        recorded?.at ? ` · ${escapeHtml(lisbonClock(recorded.at))}` : ""}</p>` : ""}`;
  }

  // ---------- The approval preview: what approving a plan would change, by recipient ----------
  // The recipient each planned action goes to, named as What ARROW did names it (Tips.system); ready times by their airport.
  function recipientOf(action) {
    const airport = action.label === "Airport CDM" ? (/^TOBT \S+ (\S+)$/.exec(String(action.summary || "")) || [])[1] : null;
    return airport && airport !== "LIS" ? `${airport} airport (A-CDM)` : Tips.system(action.label);
  }
  // A weather plan's passenger notice (the executor's summary; kind notify_passengers).
  const NOTICE = /^Tell passengers on (\d+) flights? their new times$/i;
  const ACTION_WORDS = [
    // The reported disruption, recorded first by the system that owns it ("Record the AR259 delay: 20 min, border-control queue").
    [/^Record the (.+)$/, "record the", (m) => m[1]],
    [/^Retime (.+)$/, "retime", (m) => m[1]],
    [/^Cancel (.+)$/, "cancel", (m) => m[1]],
    [/^Tail change (.+)$/, "move to another aircraft:", (m) => m[1]],
    [/^DLA (\S+)$/, "delay messages for", (m) => m[1]],
    [/^CNL (\S+)$/, "cancellation messages for", (m) => m[1]],
    [/^CHG (\S+)$/, "aircraft change messages for", (m) => m[1]],
    [/^TOBT (\S+) (\S+)$/, "new ready times for", (m) => m[1]],
    [/^Slot non-use (\S+) (\S+)$/, "release the slots of", (m) => `${m[1]} at ${m[2]}`],
    [/^Assign (?:reserve|standby) crews? (.+)$/, "assign standby crew", (m) => m[1]],
    [/^Update crew duties$/, "update the crew duties", () => ""],
    [/^Rebook (\d+) passengers$/, "rebook", (m) => `${m[1]} passengers`],
    [/^Direct transfers for (\d+) passengers$/, "drive", (m) => `${m[1]} passengers to their connecting flight`],
    // A weather plan's passenger notice (PAX-NOTICE), after the retime and before the crew.
    [NOTICE, "tell passengers", (m) => m[1]],
    // A storm's Now part (case summary and held storm spec §2): passengers warned of possible delays, standby crews put on alert.
    [/^Warn passengers on (\d+) flights? of possible delays$/i, "warn passengers", (m) => m[1]],
    [/^Put standby crews? (.+) on alert$/i, "put on alert", (m) => m[1]]
  ];
  // Actions read by their kind first (the executor's own), their summary second.
  const KIND_WORDS = { notify_passengers: ["tell passengers", (action) => (Array.isArray(action.payload?.flights) ? String(action.payload.flights.length)
    : (NOTICE.exec(String(action.summary || "")) || [])[1] || "")] };
  // A group of more than six flights (a storm, Task 4c) reads as a count, by what its verb counts; up to six, by number.
  const COUNTED = { retime: "flights", cancel: "flights", "move to another aircraft:": "flights", "delay messages for": "departures",
    "cancellation messages for": "departures", "new ready times for": "departures", "release the slots of": "flights" };
  const sum = (items) => items.reduce((total, item) => total + (Number(item) || 0), 0);
  const nOf = (n, one, more) => `${plain.format(n)} ${n === 1 ? one : more}`;
  function groupWords(system, verb, items, option) {
    // "assign standby crews R1, R2" (Task 2), "assign standby crew R1".
    if (verb === "assign standby crew") return `assign standby crew${items.length === 1 ? "" : "s"} ${items.join(", ")}`;
    if (verb === "night quota") {
      const span = quotaSpan(items);
      return span ? `request night-quota slots for ${span.n} ${span.city} arrivals (${span.from}–${span.until})` : items.map(item => `ask: ${item}`).join("; ");
    }
    if (verb === "tell passengers") {
      const flights = sum(items);
      return flights ? `tell passengers on ${nOf(flights, "flight", "flights")} their new times` : "tell passengers their new times";
    }
    if (verb === "warn passengers") return `warn passengers on ${nOf(sum(items), "flight", "flights")} of possible delays`;
    if (verb === "put on alert") return `put standby crew${items.length === 1 ? "" : "s"} ${items.join(", ")} on alert`;
    if (items.length <= RecoveryView.MANY_FLIGHTS || !COUNTED[verb]) return `${verb} ${items.join(", ")}`.trim();
    // "Lisbon airport (A-CDM)" sets ready times for Lisbon departures.
    const city = verb === "new ready times for" ? `${system.replace(/ airport \(A-CDM\)$/, "")} ` : "";
    const most = verb === "retime" ? RecoveryView.manyFlights(option, env.originals)?.most : null;
    return `${verb} ${plain.format(items.length)} ${city}${COUNTED[verb]}${most ? `, up to ${plain.format(most)} min later` : ""}`;
  }
  // What approving the plan would change, as [recipient, words, stage] (the approval dialog); a storm plan's stage is "now" or
  // "when_confirmed", any other plan's none.
  function effects(option, item, ctx) {
    use(item, ctx);
    const r = option.validation?.rebooking || {};
    if (!Array.isArray(option.planned_actions)) return fallbackEffects(option);
    const staged = Boolean(stagesOf(option));
    const groups = new Map();
    for (const action of option.planned_actions) {
      const system = recipientOf(action);
      const summary = String(action.summary || "");
      const stage = staged ? stageOf(action) : undefined;
      // Night-quota requests (a storm's late arrivals) group as the plan's reasons do.
      const night = NIGHT_QUOTA.test(summary);
      const byKind = night || !Object.hasOwn(KIND_WORDS, String(action.kind)) ? null : KIND_WORDS[action.kind];
      const rule = night || byKind ? null : ACTION_WORDS.find(([pattern]) => pattern.test(summary));
      const match = rule ? summary.match(rule[0]) : null;
      const [verb, what] = night ? ["night quota", summary] : byKind ? [byKind[0], byKind[1](action)]
        : rule ? [rule[1], rule[2](match)] : [`ask: ${summary}`, ""];
      const key = `${system}|${verb}|${stage || ""}`;
      const entry = groups.get(key) || { system, verb, stage, items: [] };
      // One action may name many flights ("Retime AR532, AR608, …"): each counts.
      if (what) entry.items.push(...String(what).split(", "));
      groups.set(key, entry);
    }
    const list = [...groups.values()].map(({ system, verb, stage, items }) => [system, groupWords(system, verb, items, option), stage]);
    if (r.unplaced_passengers > 0) list.push(["Passenger Services", `${r.unplaced_passengers} passengers outside the rebooking policy go to an expert`, staged ? "when_confirmed" : undefined]);
    return list;
  }
  function fallbackEffects(option) {
    const flights = option.flights || [];
    const moved = flights.filter(f => f.status !== "cancelled");
    const cancelled = flights.filter(f => f.status === "cancelled").map(f => f.flight_no);
    const r = option.validation?.rebooking || {};
    const effects = [];
    // A storm's plan (Task 4c) cancels a few flights and moves dozens on many aircraft, whatever its kind: the moves are counted.
    const many = RecoveryView.manyFlights(option, env.originals);
    if (cancelled.length) effects.push(["Operations control", `cancel ${cancelled.join(", ")}`]);
    if (many) {
      if (many.later) {
        effects.push(["Operations control", `retime ${plain.format(many.later)} flights${many.most ? `, up to ${plain.format(many.most)} min later` : ""}`]);
        effects.push(["EUROCONTROL Network Manager", "delay messages for the moved departures"]);
        effects.push(["Lisbon airport (A-CDM)", "new ready times for Lisbon departures"]);
      }
    } else {
      if (moved.length && option.kind !== "cancel") effects.push(["Operations control", `retime ${moved.map(f => `${f.flight_no} to ${hhmm(f.std_local)}`).join(", ")}`]);
      if (option.kind === "delay") {
        effects.push(["EUROCONTROL Network Manager", "delay messages for the moved departures"]);
        effects.push(["Lisbon airport (A-CDM)", "new ready times for Lisbon departures"]);
      }
    }
    const crews = RecoveryView.standbyCrews(option);
    effects.push(["Crew system", crews.length ? `assign ${RecoveryView.standbyName(crews)} and update duties` : "update the crew duties"]);
    if (r.disrupted_passengers > 0) {
      effects.push(["Passenger system", `rebook ${r.rebooked_passengers || 0} passengers${r.unplaced_passengers ? `; ${r.unplaced_passengers} go to Passenger Services` : ""}`]);
    }
    return effects;
  }

  // ---------- The OCC playbook (OCC playbook spec): what each plan does, its checklist, the catalogue on Rules ----------
  // The plan card says what the plan does in labels and numbers and the checklist names each action shortly; the hover cards and
  // Rules keep the catalogue's names.
  const SHORT = { RETIME: "Retime", "GROUND-HOLD": "Hold at origin", THIN: "Cancel early", "SWAP-AC": "Swap aircraft", STANDBY: "Standby crews",
    REBOOK: "Rebook", CAR: "Car transfers", "PAX-NOTICE": "Passenger notice", "FUEL-FLAG": "Fuel", "ALT-FLAG": "Alternates", "MX-ETR": "Maintenance estimate" };
  const AUTONOMY = { auto: "Automatic within limits", occ: "OCC decides", advice: "Advice only" };
  const STATUS = { in_plan: "In this plan", advice: "Advice", not_needed: "Not in this plan", refused: "Refused" };
  const APPLIES = { weather: "Weather", aircraft_defect: "Defect", flight_delay: "Delay" };
  const PHASE = { prepare: "Prepares · reversible, hours ahead", commit: "Commits · cannot be undone" };
  const ADVICE_FOOT = "Advice never changes the plan: the dispatcher, commander or maintenance decides.";
  // One line icon per desk (24 px grid, stroked in the text colour).
  const DESK_ICON = {
    "Aircraft control": '<path d="M12 2.5v19"/><path d="M3 13.5l9-4.5 9 4.5"/><path d="M8.5 20.5 12 19l3.5 1.5"/>',
    "Crew control": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 4.8a3.2 3.2 0 0 1 0 6.4"/><path d="M18 14.2a6 6 0 0 1 3 5.8"/>',
    "Hub and passengers": '<rect x="4" y="7" width="16" height="13" rx="2"/><path d="M9 7V4.5h6V7"/><path d="M4 12.5h16"/>',
    Dispatch: '<path d="M4.5 17.5a8 8 0 1 1 15 0"/><path d="m12 15.5 3.5-4.5"/><circle cx="12" cy="15.5" r="1"/>',
    "Maintenance control": '<path d="M4.5 19.5 12 12"/><path d="M14.5 4.5a4.6 4.6 0 0 0-2.9 6.9l.4.6m2.5-7.5 2.2 2.2-.5 2.3 2.3-.5 2.2 2.2a4.6 4.6 0 0 1-6.9 2.9"/>',
    other: '<circle cx="12" cy="12" r="7.5"/><path d="M12 9v6M9 12h6"/>'
  };
  const deskIcon = (desk) => `<svg class="pb-ico" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${DESK_ICON[desk] || DESK_ICON.other}</svg>`;
  const rowsOf = (option) => (Array.isArray(option?.playbook) ? option.playbook.filter(isObj) : []);
  const shortName = (row) => SHORT[row.id] || String(row.name || row.id || "Action");
  const statusWords = (row) => (row.status === "refused" ? `Refused: ${row.reason || "does not apply"}` : STATUS[row.status] || "Not established");
  // What an action does in this plan, as ARROW recorded it: a line of labels and numbers, always read as text, one line per part
  // ("R1 called 09:45 · …; R2 called 10:05 · …"). Anything else (an object's keys) is never shown.
  function factLines(row) {
    const facts = row?.facts;
    const lines = typeof facts === "string" ? [facts] : isNum(facts) ? [plain.format(facts)]
      : Array.isArray(facts) ? facts.filter(line => typeof line === "string") : [];
    return lines.flatMap(line => line.split(/;\s+/)).map(line => line.trim()).filter(Boolean);
  }
  // Actions in a hover card: each action's short name, its facts below it.
  const actionList = (rows) => `<dl class="ic-acts">${rows.map(row => `<div><dt>${escapeHtml(shortName(row))}</dt>${factLines(row).length
    ? `<dd>${factLines(row).map(line => `<span>${escapeHtml(line)}</span>`).join("")}</dd>` : ""}</div>`).join("")}</dl>`;
  // The plan card's strip (owner: "passenger number (2) is a bit misleading"): what the plan does in labels and numbers,
  // one chip per kind of action (Retime 4 · Cancel 2 · Standby 1 · Rebook 39 · Car 33), each number from the plan's own figures
  // (flights moved or cancelled, standby crews called, passengers rebooked or driven), else from the action's recorded facts, and
  // Advice only when an advice row says something. Each chip's facts on hover, focus or tap. Refusals and actions not in the plan
  // stay in the Decision step's checklist. A plan built before the playbook says what it does from its own figures.
  // The number a fact gives for its own kind ("4 flights, up to +205 min" → 4), or null when it does not match.
  const matched = (lines, pattern) => {
    for (const line of lines) {
      const match = pattern.exec(line);
      if (match) return Number(match[1].replaceAll(",", ""));
    }
    return null;
  };
  const DOES = [
    { id: "RETIME", label: "Retime", name: "Retime flights", desk: "Aircraft control",
      count: (o) => RecoveryView.lateFlights(o, env.originals).length, facts: (lines) => matched(lines, /^(\d[\d,]*) flights?\b/),
      lines: (o, n) => [`${nOf(n, "flight", "flights")} later`] },
    { id: "GROUND-HOLD", label: "Hold", name: "Hold departures at origin", desk: "Aircraft control", facts: (lines) => matched(lines, /^(\d[\d,]*) arrivals?\b/) },
    { id: "THIN", label: "Cancel", name: "Cancel flights", desk: "Aircraft control",
      count: (o) => (o.flights || []).filter(f => f?.status === "cancelled").length,
      facts: (lines) => { const named = lines.map(line => /^(.+) cancelled$/.exec(line)).find(Boolean); return named ? named[1].split(", ").length : null; },
      lines: (o) => [`${(o.flights || []).filter(f => f?.status === "cancelled").map(f => f.flight_no).join(", ")} cancelled`], computed: true },
    // Flights moved to another aircraft: by the case's own aircraft for each flight, else one per "AR965 → CS-TNB" in the facts.
    { id: "SWAP-AC", label: "Swap", name: "Swap aircraft", desk: "Aircraft control",
      count: (o) => {
        const known = (o.flights || []).filter(f => f?.status !== "cancelled" && env.originals.get(f?.flight_no)?.aircraft_reg);
        return known.length ? known.filter(f => f.aircraft_reg && f.aircraft_reg !== env.originals.get(f.flight_no).aircraft_reg).length : null;
      },
      facts: (lines) => lines.filter(line => /^\S+ → \S+$/.test(line)).length || null },
    { id: "STANDBY", label: "Standby", name: "Call standby crews", desk: "Crew control",
      count: (o) => RecoveryView.standbyCrews(o).length, facts: (lines) => lines.filter(line => /^\S+ (?:called|call time)\b/.test(line)).length || null,
      lines: (o) => RecoveryView.standbyCrews(o).map(RecoveryView.standbyLine) },
    { id: "REBOOK", label: "Rebook", name: "Rebook passengers", desk: "Hub and passengers",
      count: (o) => o.validation?.rebooking?.rebooked_passengers, facts: (lines) => matched(lines, /^(\d[\d,]*) passengers? rebooked\b/), computed: true,
      lines: (o, n) => [`${nOf(n, "passenger", "passengers")} rebooked`, ...(o.validation?.rebooking?.unplaced_passengers > 0
        ? [`${plain.format(o.validation.rebooking.unplaced_passengers)} to Passenger Services`] : [])] },
    { id: "CAR", label: "Car", name: "Drive a short connection at Lisbon", desk: "Hub and passengers",
      count: (o) => o.validation?.rebooking?.transferred_passengers, facts: (lines) => matched(lines, /^(\d[\d,]*) passengers? by car\b/), computed: true,
      lines: (o, n) => {
        const transfers = Array.isArray(o.validation?.rebooking?.transfers) ? o.validation.rebooking.transfers.length : 0;
        return [`${nOf(n, "passenger", "passengers")} by car${transfers ? ` · ${nOf(transfers, "connection", "connections")}` : ""}`];
      } },
    // "85 flights · 9,412 passengers told their new times"; none when the passengers are "not established".
    { id: "PAX-NOTICE", label: "Notify", name: "Tell passengers their new times", desk: "Hub and passengers",
      facts: (lines) => matched(lines, /(\d[\d,]*) passengers told\b/) }
  ];
  function planDoes(option) {
    const rows = rowsOf(option);
    return DOES.map(kind => {
      const row = rows.find(entry => entry.id === kind.id) || null;
      const facts = factLines(row);
      const figure = kind.count ? kind.count(option) : null;
      const known = isNum(figure);
      // The plan's own figure when it has one; an action in the plan without one reads its number from its facts by its own
      // pattern, else shows none (review: never another number in the facts, a flight number or a count of flights).
      const n = known ? figure : row?.status === "in_plan" ? kind.facts(facts) : null;
      if (!(known ? figure > 0 : row?.status === "in_plan")) return null;
      const own = isNum(n) && kind.lines ? kind.lines(option, n).filter(line => line && !/^ ?cancelled$/.test(line)) : [];
      // Figures ARROW counted read first for what it counts (cancelled, rebooked, by car); recorded facts first otherwise.
      const lines = kind.computed ? (own.length ? own : facts) : facts.length ? facts : own;
      return { ...kind, row, n, lines };
    }).filter(Boolean);
  }
  // One chip: its label and number, its card on hover, focus or tap.
  function chipMarkup(base, action, label, n, card, cls = "", aria = null) {
    const id = `${base}-pb-${slug(action)}`;
    return `<span class="pb-chip${cls}" role="listitem" data-action="${escapeHtml(action)}" data-card="${escapeHtml(id)}" tabindex="0" aria-label="${
      escapeHtml(aria || `${label}${isNum(n) ? `: ${plain.format(n)}` : ""}`)}"><span class="pb-dn">${escapeHtml(label)}</span>${
      isNum(n) ? ` <b>${escapeHtml(plain.format(n))}</b>` : ""}</span>${infoCard(id, card)}`;
  }
  const linesBody = (name, lines) => (lines.length ? `<dl class="ic-acts"><div><dt>${escapeHtml(name)}</dt><dd>${lines.map(line => `<span>${escapeHtml(line)}</span>`).join("")}</dd></div></dl>` : "");
  function doesChip(entry, base, label = entry.label) {
    const short = entry.row ? shortName(entry.row) : SHORT[entry.id] || entry.label;
    return chipMarkup(base, entry.id, label, entry.n, { title: String(entry.row?.name || entry.name), sub: String(entry.row?.desk || entry.desk), body: linesBody(short, entry.lines) });
  }
  function playbookStrip(option, base) {
    const does = planDoes(option);
    const advice = rowsOf(option).filter(row => row.status === "advice" && factLines(row).length);
    if (!does.length && !advice.length) return "";
    const chips = does.map(entry => doesChip(entry, base));
    if (advice.length) chips.push(adviceChip(advice, base));
    return `<div class="pb" role="list" aria-label="What the plan does">${chips.join("")}</div>`;
  }
  const adviceChip = (advice, base) => chipMarkup(base, "advice", "Advice", null, { title: `Advice · ${advice.length}`,
    sub: [...new Set(advice.map(row => row.desk).filter(Boolean))].join(" · "), body: actionList(advice), foot: ADVICE_FOOT }, " advice", `Advice: ${advice.length}`);

  // ---------- The storm in two stages (owner: "it does not make sense to already update eurocontrol ... better to already
  // inform passengers somehow"; case summary and held storm spec §2, prototype rev 2): a weather plan's actions carry a stage.
  // Now, carried out when OCC approves: passengers warned of possible delays, standby crews on alert, dispatch's fuel and alternates
  // advice. When confirmed: everything else, held until the storm is confirmed and ARROW's re-check passes. One approval. ----------
  const stageOf = (action) => (action?.stage === "when_confirmed" || action?.stage === "stand_down" ? action.stage : "now");
  // A plan whose planned actions run in two stages; null for any other plan (and a plan built by an earlier version).
  function stagesOf(option) {
    const planned = Array.isArray(option?.planned_actions) ? option.planned_actions.filter(isObj) : [];
    if (!planned.some(action => stageOf(action) === "when_confirmed")) return null;
    return { now: planned.filter(action => stageOf(action) === "now"), later: planned.filter(action => stageOf(action) === "when_confirmed") };
  }
  const STAGE = {
    now: ["Now", "Carried out when OCC approves: reversible. No flight, crew duty or EUROCONTROL change; passengers get no new times yet."],
    when_confirmed: ["When confirmed", "Held until the forecast is confirmed and ARROW's re-check passes: then the flight changes, EUROCONTROL messages, standby calls and new times go out."]
  };
  const RULE_TIP = "OCC approves once. The Now part runs on approval. When the forecast is confirmed, ARROW re-checks the plan and sends the held part only if nothing " +
    "got worse; otherwise the case comes back to OCC. A standby crew still on alert at its call-by time is called, crew only.";
  const ruleLine = (words = "One approval · re-check before sending · standby called at call-by") =>
    `<p class="stage-rule"${Tips.attrs(escapeHtml, RULE_TIP, "One OCC approval")}>${escapeHtml(words)}</p>`;
  // A standby crew is called by its report time less 60 minutes (the executor's rule: home standby answers within the hour).
  const CALL_BEFORE = 60;
  const minus = (time, minutes) => {
    const [h, m] = String(time).split(":").map(Number);
    const t = (((h * 60 + m - minutes) % 1440) + 1440) % 1440;
    return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
  };
  const callBys = (option) => RecoveryView.standbyCrews(option).filter(crew => crew.report).map(crew => ({ ...crew, callBy: minus(crew.report, CALL_BEFORE) }))
    .sort((a, b) => a.callBy.localeCompare(b.callBy));
  const crewCallLine = (crew) => [crew.id, `call by ${crew.callBy}`, crew.report ? `reports ${crew.report}` : "", crew.flights?.length ? crew.flights.join(", ") : ""].filter(Boolean).join(" · ");
  const WARN = /^Warn passengers on (\d[\d,]*) flights?\b/i;
  const ALERT = /^Put standby crews? (.+) on alert$/i;
  const EUROCONTROL_FOOT = "EUROCONTROL's Network Manager balances European air traffic with airport capacity; a delay or cancellation message tells it a departure moves.";
  // Dispatch's fuel and alternates advice: never sent, never changes the plan.
  const dispatchAdvice = (option) => rowsOf(option).filter(row => ["FUEL-FLAG", "ALT-FLAG"].includes(row.id) && row.status === "advice" && factLines(row).length);
  const alternatesOf = (option) => [...new Set(factLines(rowsOf(option).find(row => row.id === "ALT-FLAG"))
    .flatMap(line => [...line.matchAll(/([A-Z][a-z]+(?: [A-Z][a-z]+)*) \d[\d,]* km/g)].map(match => match[1])))];
  const arrivalsOf = (option) => matched(factLines(rowsOf(option).find(row => row.id === "FUEL-FLAG")), /^(\d[\d,]*) arrivals?\b/);
  // What is done now, before OCC approves: the warning, the alerts and the advice, as chips.
  function nowChips(option, stages, base) {
    const chips = [];
    const warn = stages.now.find(action => WARN.test(String(action.summary || "")));
    if (warn) {
      const flights = Number(WARN.exec(warn.summary)[1].replaceAll(",", ""));
      chips.push(chipMarkup(base, "WARN", "Warn", flights, { title: "Warn passengers of possible delays", sub: "Hub and passengers",
        body: linesBody("Warn", [`${nOf(flights, "flight", "flights")} · no new times yet`]), foot: "Their new times follow once the forecast is confirmed." }));
    }
    const alert = stages.now.find(action => ALERT.test(String(action.summary || "")));
    if (alert) {
      const ids = ALERT.exec(alert.summary)[1].split(", ");
      const crews = callBys(option).filter(crew => ids.includes(crew.id));
      chips.push(chipMarkup(base, "ALERT", "Standby", ids.length, { title: "Standby crews on alert", sub: "Crew control",
        body: linesBody("Standby", crews.length ? crews.map(crewCallLine) : ids), foot: "An alert is not a call: a crew is called when the forecast is confirmed, or at its call-by time." }));
    }
    const advice = dispatchAdvice(option);
    if (advice.length) {
      // Advice: its figures on hover (the words in view stay within the approved prototype's).
      chips.push(chipMarkup(base, "FUEL", "Fuel", null, { title: "Fuel and alternates", sub: "Dispatch", body: actionList(advice), foot: ADVICE_FOOT },
        " fuel", `Fuel and alternates: ${isNum(arrivalsOf(option)) ? `${plain.format(arrivalsOf(option))} arrivals` : "advice"}`));
    }
    return chips;
  }
  // EUROCONTROL's messages among a plan's held actions (planned, or as the executor recorded them).
  function networkMessages(actions) {
    const said = actions.map(action => `${action.kind || ""} ${action.summary || ""}`);
    return { delay: said.filter(text => /fpl_delay|\bDLA\b/.test(text)).length, cancel: said.filter(text => /fpl_cancel|\bCNL\b/.test(text)).length,
      change: said.filter(text => /fpl_change|\bCHG\b/.test(text)).length };
  }
  // What waits until the storm is confirmed: what the plan does, its standby crews already on alert, EUROCONTROL's messages before the
  // cars and the new times.
  function laterChips(option, actions, base) {
    const does = planDoes(option).filter(entry => entry.id !== "STANDBY").map(entry => doesChip(entry, base, entry.id === "PAX-NOTICE" ? "New times" : entry.label));
    const messages = networkMessages(actions);
    if (messages.delay + messages.cancel + messages.change) {
      const lines = [[messages.delay, "delay message", "delay messages"], [messages.cancel, "cancellation message", "cancellation messages"],
        [messages.change, "aircraft change message", "aircraft change messages"]].filter(([n]) => n).map(([n, one, more]) => nOf(n, one, more));
      const chip = chipMarkup(base, "EUROCONTROL", "EUROCONTROL", null, { title: "EUROCONTROL Network Manager", sub: "Air traffic flow · Europe",
        body: linesBody("Messages", lines), foot: EUROCONTROL_FOOT });
      const at = planDoes(option).filter(entry => entry.id !== "STANDBY").findIndex(entry => ["CAR", "PAX-NOTICE"].includes(entry.id));
      does.splice(at < 0 ? does.length : at, 0, chip);
    }
    const other = rowsOf(option).filter(row => row.status === "advice" && factLines(row).length && !["FUEL-FLAG", "ALT-FLAG"].includes(row.id));
    if (other.length) does.push(adviceChip(other, base));
    return does;
  }
  const stageRow = (stage, chips, { labelled = true, cls = "" } = {}) => `<div class="pb-stage${cls}" data-stage="${stage}">${labelled
    ? `<span class="pb-pt"${Tips.attrs(escapeHtml, STAGE[stage][1], STAGE[stage][0])}>${escapeHtml(STAGE[stage][0])}</span>` : ""}<div class="pb" role="list" aria-label="${
    escapeHtml(`${STAGE[stage][0]}: ${stage === "now" ? "carried out when OCC approves" : "held until the forecast is confirmed"}`)}">${chips.join("")}</div></div>`;
  // A storm plan's card: what it does now and when confirmed; "" for any other plan.
  function stagedStrip(option, base) {
    const stages = stagesOf(option);
    if (!stages) return "";
    return `<div class="pb-stages">${stageRow("now", nowChips(option, stages, base))}${stageRow("when_confirmed", laterChips(option, stages.later, base))}</div>`;
  }
  // The Decision step: the plan OCC approves once, in its two stages.
  function stagePlan(item, shown, reference) {
    if (item.execution) return "";
    const option = reference && stagesOf(reference) ? reference : shown.find(entry => stagesOf(entry));
    if (!option) return "";
    return `<section class="dr-sec stage-plan"><h3>${escapeHtml(`Plan ${shown.indexOf(option) + 1} · approve once`)}</h3>${
      stagedStrip(option, `dc-${slug(option.option_id)}`)}${ruleLine()}</section>`;
  }
  // METAR, TAF and EUROCONTROL explained on hover or focus wherever a storm's confirmation is described (owner: abbreviations spelled out).
  const TERM_TIPS = {
    METAR: ["METAR (routine airport weather report)", "The weather observed at the airport, issued every 30 minutes: an observation, not a forecast."],
    TAF: ["TAF (Terminal Aerodrome Forecast)", "The airport's official weather forecast for the next 24–30 hours; amended when the forecast changes."],
    EUROCONTROL: ["EUROCONTROL", "Its Network Manager balances European air traffic with capacity: a regulation limits arrivals into an airport, for example in bad weather."]
  };
  const termed = (text) => escapeHtml(text).replace(/\b(METAR|TAF|EUROCONTROL)\b/g, (word) =>
    `<span class="term"${Tips.attrs(escapeHtml, TERM_TIPS[word][1], TERM_TIPS[word][0])}>${word}</span>`);
  // What confirms the held part and what stands it down, in words; the executor's exact conditions (window, rate) on hover.
  function signals(storm, item) {
    const s = RecoveryView.storm(item?.disruption) || { hazard: "the forecast weather", place: "the airport" };
    const hazard = String(s.hazard).toLowerCase();
    const exact = (list) => (Array.isArray(list) ? list.map(String).join("\n") : "");
    const confirms = `METAR or amended TAF with ${hazard} · EUROCONTROL regulation for ${s.place} arrivals`;
    return `<dl class="sig"><dt>Confirms</dt> <dd${Tips.attrs(escapeHtml, exact(storm.confirm_by), "Confirms the forecast")}>${termed(confirms)}</dd>
    <dt>Stands down</dt> <dd${Tips.attrs(escapeHtml, exact(storm.stand_down_by), "Stands the plan down")}>${termed(`Amended TAF without ${hazard}`)}</dd></dl>`;
  }
  const lastEvent = (item, name) => (item.events || []).filter(event => event?.event === name).at(-1) || null;
  // The weather update a storm case received last: the approved plan's (execution.storm), else the case's own before OCC decided.
  function stormUpdate(item) {
    const storm = isObj(item?.execution?.storm) ? item.execution.storm : isObj(item?.storm) ? item.storm : null;
    return (Array.isArray(storm?.updates) ? storm.updates.filter(isObj) : []).at(-1) || null;
  }
  const REPORTS = [[/^METAR\b/, "METAR", "METAR (routine airport weather report) as received"],
    [/^TAF AMD\b/, "Amended TAF", "Amended TAF (Terminal Aerodrome Forecast) as received"], [/^TAF\b/, "TAF", "TAF (Terminal Aerodrome Forecast) as received"]];
  const reportOf = (update) => {
    const found = REPORTS.find(([pattern]) => pattern.test(String(update?.description || "")));
    return found ? { name: found[1], title: found[2] } : { name: "Weather update", title: "Weather update as received" };
  };
  // After OCC approved a storm plan, the main view shows its two stages (prototype rev 2 storm views): the Now part and the held part.
  function stormStages(item, shown) {
    const execution = item.execution;
    const storm = execution.storm;
    const option = shown.find(entry => entry.option_id === execution.option_id) || null;
    const actions = (execution.actions || []).filter(isObj);
    const [now, later, down] = ["now", "when_confirmed", "stand_down"].map(stage => actions.filter(action => stageOf(action) === stage));
    const status = execution.status;
    const at = (name) => lisbonClock(lastEvent(item, name)?.at);
    const ok = (list) => list.length > 0 && list.every(action => action.status === "confirmed");
    const ofKind = (list, kind) => list.find(action => action.kind === kind) || null;
    const row = (icon, label, detail) => `<li class="stage-row"><span class="pi ${icon}" aria-hidden="true"></span><b>${escapeHtml(label)}</b> ${detail}</li>`;
    const small = (text) => `<small>${escapeHtml(text)}</small>`;
    const rows = [];
    let nowTitle;
    let nowNote = "";
    // A stand-down (the forecast dropped) reads from what was actually sent: nothing, undone, still undoing, or stopped.
    const dropped = storm.state === "dropped" || down.length > 0;
    const failed = (list) => list.some(action => ["refused", "failed", "unknown"].includes(action.status));
    const sentNow = now.some(action => action.status === "confirmed");
    if (dropped) {
      nowTitle = !sentNow && !down.length ? "Now · nothing sent" : down.length && ok(down) ? `Now · undone ${at("storm_stood_down")}`.trim()
        : failed(down) ? "Now · stand-down stopped" : "Now · undoing";
      if (!sentNow && !down.length) nowNote = "The forecast dropped before anything was sent";
    } else {
      nowTitle = ok(now) ? `Now · ✓ done ${lisbonClock(storm.held_at)}`.trim() : failed(now) ? "Now · stopped" : "Now · sending";
    }
    const statusWord = (action) => (action.status === "unknown" ? "outcome unknown" : String(action.status || ""));
    const told = ofKind(down, "notify_no_change");
    const warn = ofKind(now, "warn_passengers");
    if (told) {
      const flights = Array.isArray(told.payload?.flights) ? told.payload.flights.length : (/on (\d+) flights?/.exec(told.summary || "") || [])[1];
      const detail = `no change expected${flights ? ` · ${flights} flights` : ""}`;
      rows.push(told.status === "confirmed" ? row("pax", "Passengers told", small(detail))
        : failed([told]) ? row("pax", "Passengers not told", small(`${statusWord(told)} · ${detail}`)) : row("pax", "Telling passengers", small(detail)));
    } else if (warn && (warn.status !== "stood_down" || !dropped)) {
      const said = /Told ([\d,]+) passengers on (\d[\d,]*) flights?/.exec(warn.message || "");
      const flights = said ? said[2] : Array.isArray(warn.payload?.flights) ? plain.format(warn.payload.flights.length) : "";
      rows.push(row("pax", warn.status === "confirmed" ? "Passengers warned" : `Warning ${RecoveryView.actionStatusText(warn.status).toLowerCase()}`,
        small(said ? `${said[1]} on ${flights} flights` : flights ? `${flights} flights` : "")));
    }
    const released = ofKind(down, "release_standby");
    const alert = ofKind(now, "alert_standby");
    if (released) {
      const ids = (/^Release standby crews? (.+)$/i.exec(released.summary || "") || [])[1] || "";
      rows.push(released.status === "confirmed" ? row("crew", "Standby released", small(ids))
        : failed([released]) ? row("crew", "Standby not released", small(`${statusWord(released)}${ids ? ` · ${ids}` : ""}`)) : row("crew", "Releasing standby", small(ids)));
    } else if (alert && (alert.status !== "stood_down" || !dropped)) {
      const crews = (Array.isArray(alert.payload?.crews) ? alert.payload.crews : []).filter(isObj)
        .map(crew => ({ id: String(crew.reserve_option_id || ""), callBy: hhmm(crew.call_by_local) })).sort((a, b) => a.callBy.localeCompare(b.callBy));
      const called = new Set(actions.filter(action => action.kind === "call_standby" && action.status === "confirmed")
        .flatMap(action => (Array.isArray(action.payload?.reserve_option_ids) ? action.payload.reserve_option_ids.map(String) : [])));
      const planned = option ? RecoveryView.standbyCrews(option) : [];
      const chips = crews.map((crew, index) => {
        const own = planned.find(entry => entry.id === crew.id);
        const tip = [`call by ${crew.callBy}`, own?.report ? `reports ${own.report}` : "", own?.flights?.length ? own.flights.join(", ") : ""].filter(Boolean).join(" · ");
        return `<span class="sbx${index === 0 ? " first" : ""}${called.has(crew.id) ? " called" : ""}"${Tips.attrs(escapeHtml, tip, `Standby ${crew.id}`)}>${
          escapeHtml(called.has(crew.id) ? `${crew.id} called` : `${crew.id} ${crew.callBy}`)}</span>`;
      });
      rows.push(row("crew", alert.status === "confirmed" ? "Standby on alert" : `Standby alert ${RecoveryView.actionStatusText(alert.status).toLowerCase()}`,
        `<span class="chips-in">${chips.join(" ")}</span>`));
    }
    if (!dropped && option && dispatchAdvice(option).length) {
      const arrivals = arrivalsOf(option);
      const detail = [isNum(arrivals) ? `${plain.format(arrivals)} arrivals` : "", alternatesOf(option).join(", ")].filter(Boolean).join(" · ");
      rows.push(row("disp", "Fuel and alternates", `<span${Tips.attrs(escapeHtml, dispatchAdvice(option).flatMap(factLines).join("\n"), "Advice to dispatch")}>${escapeHtml(detail || "advice")}</span>`));
    }
    const nowCard = `<article class="stage-card${dropped ? "" : ok(now) ? " ok" : ""}" data-stage="now" aria-label="Now">
      <p class="stage-t">${escapeHtml(nowTitle)}</p>${rows.length ? `<ul class="stage-rows">${rows.join("")}</ul>` : ""}${
      nowNote ? `<p class="stage-sub">${escapeHtml(nowNote)}</p>` : ""}</article>`;
    const chips = option ? laterChips(option, later, `st-${slug(option.option_id)}`) : [];
    const sentLater = later.length > 0 && ok(later);
    const chipRow = chips.length ? stageRow("when_confirmed", chips, { labelled: false, cls: sentLater ? " sent" : "" }) : "";
    const kept = [ofKind(now, "warn_passengers")?.status === "confirmed" ? "passengers stay warned" : "",
      ofKind(now, "alert_standby")?.status === "confirmed" ? "standby crews on alert" : ""].filter(Boolean);
    let laterCard;
    if (status === "held") {
      const first = ofKind(now, "alert_standby") ? "first standby call" : "first change";
      laterCard = `<article class="stage-card held" data-stage="when_confirmed" aria-label="When confirmed">
        <p class="stage-t"><i class="pulse" aria-hidden="true"></i>When confirmed · held</p>
        <p class="stage-big">${escapeHtml(storm.decide_by ? `Decide by ${storm.decide_by}` : "Held until confirmed")}${storm.decide_by ? ` <small>${escapeHtml(first)}</small>` : ""}</p>
        ${chipRow}${signals(storm, item)}${ruleLine("Re-check before sending · standby called at call-by")}</article>`;
    } else if (dropped) {
      laterCard = `<article class="stage-card never" data-stage="when_confirmed" aria-label="When confirmed">
        <p class="stage-t">When confirmed · never sent</p>${chipRow}<p class="stage-sub">Nothing to undo at EUROCONTROL or the airport</p></article>`;
    } else if (status === "needs_attention") {
      const returned = storm.returned ? capFirst(String(storm.returned)) : "";
      const sentSome = later.some(action => action.status === "confirmed");
      laterCard = `<article class="stage-card returned" data-stage="when_confirmed" aria-label="When confirmed">
        <p class="stage-t">${escapeHtml(sentSome ? "When confirmed · stopped part-way" : "When confirmed · not sent")}</p>
        <p class="stage-big bad">${escapeHtml(returned ? "Back to you" : "Needs attention")}</p>
        <p class="stage-why">${escapeHtml(returned || RecoveryView.outcomeLine(execution).text)}</p>${chipRow}
        ${returned ? `<p class="stage-sub">${escapeHtml(["Nothing more was sent", ...kept].join(" · "))}</p>` : ""}</article>`;
    } else {
      const released = storm.state === "confirmed" && storm.rechecked;
      const title = sentLater ? `When confirmed · ✓ sent ${at("storm_released")}`.trim() : released ? "When confirmed · sending" : storm.state === "confirmed" ? "When confirmed · re-checking" : "When confirmed · waiting";
      const recheck = "ARROW re-checked before sending: the confirmed weather inside the planned window, its rate not lower than planned, and every source unchanged since " +
        "the approval (flights, crew duties, rebooking, slots).";
      laterCard = `<article class="stage-card${sentLater ? " ok" : ""}" data-stage="when_confirmed" aria-label="When confirmed">
        <p class="stage-t">${escapeHtml(title)}</p>
        ${released ? `<p class="stage-big good"${Tips.attrs(escapeHtml, recheck, "Re-checked")}>Re-checked: nothing worse</p>` : ""}${chipRow}</article>`;
    }
    return nowCard + laterCard;
  }
  // The approved storm plan's stages in Carried out: each stage's outcomes, with what confirms the held part and what stands it down.
  function stormDone(item, execution) {
    const storm = execution.storm;
    const actions = (execution.actions || []).filter(isObj);
    const option = (item.options || []).find(entry => entry?.option_id === execution.option_id) || null;
    const at = (name) => lisbonClock(lastEvent(item, name)?.at);
    const groups = (stage) => RecoveryView.outcomeGroups({ ...execution, actions: actions.filter(action => stageOf(action) === stage) })
      .map(group => doneGroup({ ...group, key: `${stage}-${group.key}` })).join("");
    const ok = (stage) => { const list = actions.filter(action => stageOf(action) === stage); return list.length > 0 && list.every(action => action.status === "confirmed"); };
    const status = execution.status;
    const advice = option ? dispatchAdvice(option) : [];
    const arrivals = option ? arrivalsOf(option) : null;
    const dropped = storm.state === "dropped" || actions.some(action => stageOf(action) === "stand_down");
    const adviceLine = advice.length && !dropped ? `<p class="stage-advice"${Tips.attrs(escapeHtml, advice.flatMap(factLines).join("\n"), "Advice to dispatch")}><b>Fuel and alternates</b> <span>${
      escapeHtml(["advice to dispatch", isNum(arrivals) ? `${plain.format(arrivals)} arrivals` : "", alternatesOf(option).join(", ")].filter(Boolean).join(" · "))}</span></p>` : "";
    const sentAny = (stage) => actions.some(action => stageOf(action) === stage && action.status === "confirmed");
    const failedIn = (stage) => actions.some(action => stageOf(action) === stage && ["refused", "failed", "unknown"].includes(action.status));
    const nowTitle = ok("now") ? `Now · ✓ done ${lisbonClock(storm.held_at)}`.trim() : dropped && !sentAny("now") ? "Now · nothing sent" : failedIn("now") ? "Now · stopped" : "Now";
    const laterTitle = status === "held" ? `When confirmed · held${storm.decide_by ? ` · decide by ${storm.decide_by}` : ""}`
      : dropped && !sentAny("when_confirmed") ? "When confirmed · never sent"
      : status === "needs_attention" ? (storm.returned ? "When confirmed · not sent · back to you" : "When confirmed · stopped")
      : ok("when_confirmed") ? `When confirmed · ✓ sent ${at("storm_released")}`.trim() : "When confirmed · sending";
    const section = (stage, title, body) => `<section class="stage-sec" data-stage="${stage}"><h4>${escapeHtml(title)}</h4>${body}</section>`;
    const downTitle = ok("stand_down") ? `Stand-down · ✓ done ${at("storm_stood_down")}`.trim() : failedIn("stand_down") ? "Stand-down · stopped" : "Stand-down · sending";
    const down = actions.some(action => stageOf(action) === "stand_down") ? section("stand_down", downTitle, `<div class="done-groups">${groups("stand_down")}</div>`) : "";
    return section("now", nowTitle, `<div class="done-groups">${groups("now")}</div>${adviceLine}`) +
      section("when_confirmed", laterTitle, `${status === "held" ? signals(storm, item) : ""}${status === "needs_attention" && storm.returned
        ? `<p class="stage-why">${escapeHtml(capFirst(String(storm.returned)))}</p>` : ""}<div class="done-groups">${groups("when_confirmed")}</div>`) + down;
  }
  // What a weather plan does ahead of the storm (owner: the head start), for the recommendation's card: in two stages, what
  // is done on approval; before, the standby crews' call times and the passengers told their new times.
  const AHEAD = ["STANDBY", "PAX-NOTICE"];
  function aheadOf(item, option) {
    if (item?.disruption?.type !== "weather" || !option) return "";
    const stages = stagesOf(option);
    if (stages) {
      const warn = stages.now.find(action => WARN.test(String(action.summary || "")));
      const lines = [warn ? ["Warn", [`${WARN.exec(warn.summary)[1]} flights · no new times yet`]] : null,
        callBys(option).length ? ["Standby", callBys(option).map(crewCallLine)] : null,
        dispatchAdvice(option).length ? ["Fuel and alternates", dispatchAdvice(option).flatMap(factLines)] : null].filter(Boolean);
      return lines.length ? `<p class="ic-sub">Now, on approval</p><dl class="ic-acts">${lines.map(([name, list]) => `<div><dt>${escapeHtml(name)}</dt><dd>${
        list.map(line => `<span>${escapeHtml(line)}</span>`).join("")}</dd></div>`).join("")}</dl>` : "";
    }
    const rows = rowsOf(option).filter(row => row.status === "in_plan" && AHEAD.includes(row.id) && factLines(row).length);
    return rows.length ? `<p class="ic-sub">Ahead of the weather</p>${actionList(rows)}` : "";
  }
  // The Decision step: each plan's checklist, every action that applies with its desk, status and facts.
  function checklist(option, number) {
    const rows = rowsOf(option);
    if (!rows.length) return "";
    const counts = [["in_plan", "in this plan"], ["advice", "advice"], ["refused", "refused"]]
      .map(([status, word]) => [rows.filter(row => row.status === status).length, word]).filter(([n]) => n).map(([n, word]) => `${n} ${word}`).join(", ");
    const facts = (row) => [...factLines(row), row.status !== "refused" && row.reason ? capFirst(String(row.reason)) : ""].filter(Boolean)
      .map(line => `<span>${escapeHtml(line)}</span>`).join("");
    return `<details class="pb-detail" data-plan-no="${escapeHtml(number)}" data-keep="pb-${escapeHtml(slug(option.option_id))}"><summary>${
      escapeHtml(`Plan ${number}: ${planName(option)}`)}${counts ? ` <span>${escapeHtml(counts)}</span>` : ""}</summary>
      <table class="pb-t"><thead><tr><th>Action</th><th class="pb-desk">Desk</th><th>Status</th><th>Facts</th></tr></thead><tbody>${rows.map(row =>
        `<tr data-action="${escapeHtml(row.id)}" data-status="${escapeHtml(row.status)}"><td class="pb-act"${Tips.attrs(escapeHtml,
          [row.name || shortName(row), AUTONOMY[row.autonomy] || ""].filter(Boolean).join("\n"), row.desk || "")}>${deskIcon(row.desk)}<b>${
          escapeHtml(shortName(row))}</b></td><td class="pb-desk">${escapeHtml(row.desk || "—")}</td><td><span class="pb-st ${escapeHtml(row.status)}">${
          escapeHtml(statusWords(row))}</span></td><td class="pb-facts">${facts(row) || '<span class="none">—</span>'}</td></tr>`).join("")}</tbody></table></details>`;
  }
  // The Rules screen: the catalogue, read only, with its version; what each action checks, where it goes and what it needs on
  // hover or focus.
  function catalogue(playbook) {
    const actions = Array.isArray(playbook?.actions) ? playbook.actions.filter(isObj) : [];
    if (!actions.length) return "";
    const version = String(playbook.version || "").replace(/^playbook-/, "");
    const rows = actions.map(action => {
      const id = `rl-pb-${slug(action.id)}`;
      const applies = (Array.isArray(action.applies_to) ? action.applies_to : []).map(type => APPLIES[type] || String(type).replaceAll("_", " "));
      return `<div class="pb-row" data-action="${escapeHtml(action.id)}" data-card="${escapeHtml(id)}" tabindex="0">${deskIcon(action.desk)}
        <div class="pb-name"><b>${escapeHtml(action.name)}</b> <small>${escapeHtml(action.desk)}</small></div>
        <span class="pb-applies">${applies.map(type => `<span>${escapeHtml(type)}</span>`).join("")}</span>
        <span class="pb-auto" data-tone="${escapeHtml(action.autonomy)}">${escapeHtml(AUTONOMY[action.autonomy] || "Not established")}</span></div>${
        infoCard(id, { title: String(action.name || ""), sub: String(action.desk || ""), rows: [["When", PHASE[action.phase] || "—"], ["Sent to", String(action.recipient || "—")],
          ["Autonomy", AUTONOMY[action.autonomy] || "—"]], listTitle: "Code checks", list: action.checks ? [String(action.checks)] : [],
          foot: action.needs ? `Needs: ${action.needs}` : "" })}`;
    }).join("");
    return `<section class="rl-playbook" aria-labelledby="rl-playbook-title"><div class="rl-title"><h2 id="rl-playbook-title">OCC playbook</h2><span>${
      escapeHtml(`${version ? `Version ${version} · ` : ""}${actions.length} actions · read only · the orchestrator chooses, code checks each action`)}</span></div>
      <div class="pb-cat"><div class="pb-cols" aria-hidden="true"><span></span><span>Action</span><span>Applies to</span><span>Autonomy</span></div>${rows}</div></section>`;
  }

  // Passengers stranded overnight and crews still over their flight-time limit after the plan's standby crews (ARROW's count,
  // plan.outcomes; never the Crew agent's duty rows), when the plan carries them.
  // A what-if being built for a question in the case chat (app.js renderWip): where its card will land, with its progress.
  function wipCard(entry, number) {
    const at = RecoveryView.lisbonTime(entry.asked_at);
    return `<article class="plan wip" data-wip="${escapeHtml(entry.id)}" aria-busy="true" aria-label="${escapeHtml(`What-if being built: ${entry.question || ""}`)}">
      <span class="ribbon">What-if</span>
      <header class="plan-head"><span class="rank">${escapeHtml(number)}</span><h4>${escapeHtml(entry.question || "")}</h4></header>
      <span class="busy-bar" aria-hidden="true"><i></i></span>
      <p class="wip-t" role="status">Building the plan <span data-since="${escapeHtml(entry.asked_at)}"></span></p>
      <p class="wip-by">${escapeHtml(`Asked in chat by ${entry.asked_by?.name || "an operator"}${at ? ` at ${at}` : ""}`)}</p>
    </article>`;
  }

  const stranded = (option) => [option?.outcomes?.stranded_overnight, option?.stranded_overnight, option?.cost?.overnight_passengers,
    option?.cost?.workings?.overnight?.passengers].find(isNum) ?? null;
  const crewsOver = (option) => (isNum(option?.outcomes?.crews_over_limit) ? option.outcomes.crews_over_limit : null);
  const CREWS_OVER_TIP = "Crew duties still over their flight-time limit after the plan's standby crews.";
  const recModel = (item) => RecoveryView.recommendation(item, env.originals);

  return Object.freeze({ cards, wipCard, summary, whyOthers, recommendation, decisionStep, plansStep, done, effects, catalogue });
})();
