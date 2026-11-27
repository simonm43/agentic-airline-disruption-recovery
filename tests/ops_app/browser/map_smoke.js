async (page) => {
  // Operations map business flows (operations map spec §3, §6, §7) against mocked /api routes.
  const base = await page.evaluate(() => new URLSearchParams(location.search).get("base")) || "http://127.0.0.1:8768";
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  // Owner: the same professional-screen check as the operator smoke (visible text and hover cards, Interface aside).
  const AMATEUR = [/\bDev\b/, /\bplayed\b/i, /ca-airrec-/, /Crew roster/, /Reservations/, /Airport slots/, /Names never/i, /[0-9a-f]{12,}/i,
    /\bact-\d/, /\b[0-9a-f]{4}-\d{2}-[a-z]/i, /id-airrec-/,
    // Owner (final review): a weather case reads as a forecast, never a storm.
    /\bstorm\b/i, /\bfixed\b/i];
  const professional = async (where) => {
    const { text, tips } = await page.evaluate(() => {
      const seen = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node.parentElement;
        if (el && node.textContent.trim() && !el.closest(".iface, script, style") && el.checkVisibility({ visibilityProperty: true })) seen.push(node.textContent);
      }
      const hover = [...document.querySelectorAll("[data-tip], [data-tip-title], [title]")]
        .map(e => [e.dataset.tip, e.dataset.tipTitle, e.getAttribute("title")].filter(Boolean).join(" "));
      return { text: seen.join(" "), tips: hover.join(" ") };
    });
    for (const pattern of AMATEUR) {
      const hit = (text.match(pattern) || tips.match(pattern) || [])[0];
      check(!hit, `${where}: "${hit}" should not show (${pattern})`);
    }
  };
  const errors = [], urls = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => urls.push(request.url()));
  let networkFailures = 1;

  const DAY = "2026-11-10";
  const A = "a".repeat(32), B = "b".repeat(32), C = "c".repeat(32), D = "d".repeat(32), E = "e".repeat(32);
  const at = (hhmm, next = false) => `${next ? "2026-11-11" : DAY}T${hhmm}:00`;
  // GET /api/network serves each flight's published timetable beside its current times.
  const leg = ([no, from, to, std, sta, reg, nextSta = false]) =>
    ({ flight_date: DAY, flight_no: no, origin: from, destination: to, std_local: at(std), sta_local: at(sta, nextSta), aircraft_reg: reg, status: "scheduled",
      published_std_local: at(std), published_sta_local: at(sta, nextSta) });
  // Today's rotations of the four demo aircraft (Dev schedule); AR805 and AR259 already carry their executed delays.
  const flights = [
    ["AR101", "LIS", "OPO", "10:30", "11:25", "CS-TQA"], ["AR102", "OPO", "LIS", "12:00", "12:55", "CS-TQA"],
    ["AR131", "LIS", "FAO", "14:10", "14:55", "CS-TQA"], ["AR132", "FAO", "LIS", "15:35", "16:20", "CS-TQA"],
    ["AR259", "LIS", "BEL", "10:50", "18:35", "CS-TUK"], ["AR260", "BEL", "LIS", "22:15", "06:00", "CS-TUK", true],
    ["AR305", "PDL", "OPO", "06:00", "08:30", "CS-TKW"], ["AR306", "OPO", "PDL", "09:05", "11:35", "CS-TKW"],
    ["AR689", "PDL", "FNC", "12:10", "14:00", "CS-TKW"], ["AR690", "FNC", "PDL", "14:35", "16:25", "CS-TKW"],
    ["AR941", "PDL", "OPO", "17:00", "19:30", "CS-TKW"], ["AR942", "OPO", "PDL", "20:05", "22:35", "CS-TKW"],
    ["AR1132", "OPO", "LIS", "06:00", "06:55", "CS-TKJ"], ["AR575", "LIS", "FNC", "09:00", "10:45", "CS-TKJ"],
    ["AR576", "FNC", "LIS", "11:20", "13:05", "CS-TKJ"], ["AR805", "LIS", "FNC", "14:40", "16:25", "CS-TKJ"],
    ["AR806", "FNC", "LIS", "17:00", "18:45", "CS-TKJ"], ["AR1131", "LIS", "OPO", "00:35", "01:30", "CS-TKJ", true],
    ["AR543", "LIS", "LHR", "09:00", "11:35", "CS-TJI"], ["AR553", "LIS", "CDG", "09:00", "11:30", "CS-TJX"],
    ["AR571", "LIS", "MAD", "09:00", "10:10", "CS-TKH"], ["AR310", "FNC", "LIS", "08:20", "10:05", "CS-TQB"]
  ].map(leg);
  flights.find(f => f.flight_no === "AR1131").std_local = at("00:35", true);
  // The executed delays moved these flights from their published times.
  const published = (no, std, sta, next = false) =>
    Object.assign(flights.find(f => f.flight_no === no), { published_std_local: at(std), published_sta_local: at(sta, next) });
  published("AR259", "10:30", "18:15"); published("AR805", "13:40", "15:25"); published("AR806", "16:00", "17:45"); published("AR1131", "23:35", "00:30", true);
  const network = {
    operating_date: DAY, as_of_local: `${DAY}T09:45:00`, timezone: "Europe/Lisbon", flights,
    aircraft: [["CS-TQA", "A320neo", 174], ["CS-TUK", "A330neo", 298], ["CS-TKW", "A321neo", 216], ["CS-TKJ", "A321neo", 216],
      ["CS-TJI", "A320neo", 174], ["CS-TJX", "A320neo", 174], ["CS-TKH", "A321neo", 216], ["CS-TQB", "A320neo", 174]]
      .map(([registration, fleet_type, seats]) => ({ registration, fleet_type, seats, base: "LIS", status: "serviceable" })),
    airports: [["LIS", -9.13, 38.77], ["OPO", -8.68, 41.24], ["FAO", -7.97, 37.01], ["PDL", -25.7, 37.74], ["FNC", -16.77, 32.7],
      ["BEL", -48.48, -1.38], ["LHR", -0.45, 51.47], ["CDG", 2.55, 49.01], ["MAD", -3.56, 40.47]]
      .map(([iata, lon, lat]) => ({ iata, position: [lon, lat] }))
  };
  const impact = (over) => ({ passengers: null, rebooked: null, review_passengers: 0, cost_eur: null, plans: 1, plan_title: null,
    route: null, authorised_by: null, actions_done: 0, actions_total: 0, ...over });
  const row = (trace, over) => ({ trace_id: trace, display_id: `DSR-20261110-${trace.slice(0, 6).toUpperCase()}`, status: "assessment_ready",
    created_at: `${DAY}T09:45:00Z`, source: "OCC", lane: { flights: [], unavailable: null }, ...over });
  const rows = [
    row(D, { headline: "AR259 · 20 min late", state: "handled", label: "Handled automatically", urgency: 5, status: "completed",
      disruption_type: "flight_delay", flight_no: "AR259", station: "LIS",
      first_departure: { flight_no: "AR259", std_local: at("10:30"), planned_local: at("10:50") },
      impact: impact({ passengers: 0, rebooked: 0, cost_eur: 900, route: "auto", authorised_by: "policy", actions_done: 2, actions_total: 2 }) }),
    row(B, { headline: "AR689 · 60 min late", state: "needs", label: "Needs manual action", urgency: 1, source: "System event",
      disruption_type: "flight_delay", flight_no: "AR689", station: "PDL",
      first_departure: { flight_no: "AR689", std_local: at("12:10"), planned_local: at("13:10") },
      impact: impact({ passengers: 0, rebooked: 0, cost_eur: 39840, route: "blocked" }) }),
    row(E, { headline: "Bird strike reported on arrival, checking which aircraft", state: "progress", label: "ARROW is assessing",
      urgency: 4, status: "assessing", disruption_type: "message", station: null, first_departure: null, impact: impact({ plans: 0 }) }),
    row(A, { headline: "CS-TQA · out of service 4 h", state: "needs", label: "Needs your decision", urgency: 1,
      disruption_type: "aircraft_defect", aircraft_reg: "CS-TQA", station: "LIS",
      first_departure: { flight_no: "AR101", std_local: at("10:30"), planned_local: at("14:00") },
      lane: { flights: [], unavailable: { from: at("09:45"), until: at("13:45") } },
      impact: impact({ passengers: 112, rebooked: 59, cost_eur: 206330, plans: 2, plan_title: "Wait for maintenance and delay the rotation", route: "occ" }) }),
    row(C, { headline: "AR805 · 60 min late", state: "needs", label: "Passenger Services review", urgency: 3, status: "completed", source: "System event",
      disruption_type: "flight_delay", flight_no: "AR805", station: "LIS",
      first_departure: { flight_no: "AR805", std_local: at("13:40"), planned_local: at("14:40") },
      impact: impact({ passengers: 26, rebooked: 0, review_passengers: 26, cost_eur: 19920, route: "auto", authorised_by: "policy", actions_done: 5, actions_total: 5 }) })
  ];
  // Map views (GET /api/cases/{trace}/map) as Task 1 serves them; CS-TQA's figures are the prototype's.
  const fl = (no, std, planned, status = "scheduled", reg = null, next = false) =>
    ({ flight_no: no, std_local: at(std), planned_local: planned ? at(planned, next) : null, status, aircraft_reg: reg });
  const plan = (over) => ({ recommended: false, executed: false, authorised_by: null, reason: null, passengers: null, rebooked: null,
    review: null, cost_eur: null, reasons: [], flights: [], execution: null, ...over });
  const mapOf = (trace, plans) => {
    const r = rows.find(x => x.trace_id === trace);
    return { trace_id: trace, display_id: r.display_id, headline: r.headline, state: r.state, label: r.label, urgency: r.urgency,
      disruption_type: r.disruption_type, source: r.source, reported_at: at("09:45"), created_at: r.created_at, station: r.station,
      unavailable: r.lane.unavailable, plans, ...views[trace] };
  };
  const views = {
    [A]: { report: "Hydraulic leak found on CS-TQA during the walk-around. Maintenance expects it out of service for about 4 hours.", aircraft_reg: "CS-TQA",
      created_at: `${DAY}T17:56:00Z` },
    [B]: { report: "Late inbound aircraft: AR689 will leave 60 minutes late.", aircraft_reg: "CS-TKW" },
    [C]: { report: "Ground handling is short-staffed; AR805 is expected to depart 60 minutes late.", aircraft_reg: "CS-TKJ" },
    [D]: { report: "Border-control queues (Entry/Exit System checks) are holding passengers for AR259: it will leave about 20 minutes late.", aircraft_reg: "CS-TUK" },
    [E]: { report: "Bird strike reported on arrival, checking which aircraft", aircraft_reg: null }
  };
  const maps = {
    [A]: mapOf(A, [
      plan({ option_id: "cancel-a", kind: "cancel", title: "Cancel AR101 and AR102", recommended: true, route: "occ",
        reason: "Cancellation minimizes total cost and unplaced passengers compared to the delay option.",
        passengers: 324, rebooked: 239, review: 85, cost_eur: 81000,
        reasons: ["Cost €81,000 · limit €20,000", "Rebooked 239 passengers · limit 10", "Cancellation: AR101 · AR102", "Passengers without rebooking: 85 passengers"],
        flights: [fl("AR101", "10:30", null, "cancelled", "CS-TQA"), fl("AR102", "12:00", null, "cancelled", "CS-TQA"), fl("AR999", "15:00", null, "cancelled", "CS-TQA")] }),
      plan({ option_id: "delay-a", kind: "delay", title: "Wait for maintenance and delay the rotation", route: "occ", passengers: 107, rebooked: 54, review: 53, cost_eur: 196370,
        reasons: ["Cost €196,370 · limit €20,000", "Long delays: AR101 +195 min · AR102 +195 min · AR131 +155 min · AR132 +150 min"],
        flights: [fl("AR101", "10:30", "13:45", "scheduled", "CS-TQA"), fl("AR102", "12:00", "15:15", "scheduled", "CS-TQA"),
          fl("AR131", "14:10", "16:45", "scheduled", "CS-TQA"), fl("AR132", "15:35", "18:05", "scheduled", "CS-TQA")] })
    ]),
    [B]: mapOf(B, [plan({ option_id: "delay-b", kind: "delay", title: "Delay the rotation", route: "blocked", passengers: 0, rebooked: 0, cost_eur: 39840,
      reasons: ["Cost €39,840 · limit €20,000", "Crew flight-time limits: D-PDL-002: 745 min · limit 720 min"],
      flights: [fl("AR689", "12:10", "13:10", "scheduled", "CS-TKW"), fl("AR690", "14:35", "15:35", "scheduled", "CS-TKW"),
        fl("AR941", "17:00", "18:00", "scheduled", "CS-TKW"), fl("AR942", "20:05", "21:05", "scheduled", "CS-TKW")] })]),
    // OCC approved the plan that was not recommended: the map must show what ran.
    [C]: mapOf(C, [
      plan({ option_id: "cancel-c", kind: "cancel", title: "Cancel AR805 and AR806", recommended: true, route: "occ", passengers: 180, rebooked: 120, review: 60,
        cost_eur: 54000, reasons: ["Cancellation: AR805 · AR806"], flights: [fl("AR805", "13:40", null, "cancelled", "CS-TKJ"), fl("AR806", "16:00", null, "cancelled", "CS-TKJ")] }),
      plan({ option_id: "delay-c", kind: "delay", title: "Delay the rotation", executed: true, authorised_by: "occ", route: "auto",
        execution: { status: "completed", actions_done: 6, actions_total: 6 },
        passengers: 26, rebooked: 0, review: 26, cost_eur: 19920, reasons: ["Passengers without rebooking: 26 passengers"],
        flights: [fl("AR805", "13:40", "14:40", "scheduled", "CS-TKJ"), fl("AR806", "16:00", "17:00", "scheduled", "CS-TKJ"),
          { ...fl("AR1131", "23:35", "00:35", "scheduled", "CS-TKJ", true) }] })]),
    [D]: mapOf(D, [plan({ option_id: "delay-d", kind: "delay", title: "Delay AR259", recommended: true, executed: true, authorised_by: "policy", route: "auto",
      execution: { status: "completed", actions_done: 2, actions_total: 2 },
      passengers: 0, rebooked: 0, review: 0, cost_eur: 900, flights: [fl("AR259", "10:30", "10:50", "scheduled", "CS-TUK")] })]),
    [E]: mapOf(E, [])
  };
  let mapFailures = { [C]: 1 };
  let stormDoc = null, stormFails = false;
  // AR805's delay as an older read of the case gives it once carried out (owner), without the case's own original times:
  // the map's day already carries the new times, so the plan's flights are compared with the published timetable, never with themselves.
  const ranLeg = (no, from, to, std, sta, next = false) => ({ flight_no: no, origin: from, destination: to, std_local: at(std, next), sta_local: at(sta, next),
    aircraft_reg: "CS-TKJ", status: "scheduled" });
  const ranDoc = { trace_id: C, display_id: rows.find(r => r.trace_id === C).display_id, status: "assessment_ready", version: 6, created_at: `${DAY}T09:45:00Z`,
    operating_date: DAY, source: { kind: "event", system: "Ground handling" }, stage: "Within every domain's limits",
    disruption: { type: "flight_delay", cause: "staff_shortage", flight_no: "AR805", delay_minutes: 60, scope: { flights: ["AR805"], aircraft_reg: "CS-TKJ" },
      message: "Ground handling is short-staffed; AR805 is expected to depart 60 minutes late.", reported_at: at("09:45") },
    options: [{ plan_id: "delay-c", option_id: "delay-c", kind: "delay", title: "Delay the rotation",
      flights: [ranLeg("AR805", "LIS", "FNC", "14:40", "16:25"), ranLeg("AR806", "FNC", "LIS", "17:00", "18:45"), ranLeg("AR1131", "LIS", "OPO", "00:35", "01:30", true)],
      cost: { total_eur: 19920 }, validation: { rebooking: { disrupted_passengers: 26, rebooked_passengers: 0, unplaced_passengers: 26 } },
      policy: { route: "auto", reasons: ["within every domain's limits; OCC is informed"] }, requires_duty_manager: false, recommended: true }],
    execution: { route: "auto", approved_by: "policy", status: "completed", option_id: "delay-c",
      actions: [{ label: "Ops control", kind: "retime_flights", summary: "Retime AR805, AR806, AR1131", status: "confirmed", message: "Retimed AR805 14:40; MVT sent" }],
      reviews: [{ kind: "passenger_services", status: "awaiting_expert", passengers: 26 }] },
    events: [{ event: "auto_execution_authorised", at: `${DAY}T09:50:00Z`, actor: "recovery-executor" }] };
  const caseDoc = { trace_id: A, display_id: rows.find(r => r.trace_id === A).display_id, status: "assessment_ready", version: 3,
    created_at: `${DAY}T09:45:00Z`, operating_date: DAY, source: { kind: "ui" }, stage: "Waiting for OCC.",
    disruption: { type: "aircraft_defect", scope: { aircraft_reg: "CS-TQA", flights: [] }, description: "Hydraulic leak", reported_at: at("09:45") },
    defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240, reported_at: at("09:45"), unavailable_until: at("13:45") }, options: [], events: [] };
  const booking = { flight: flights.find(f => f.flight_no === "AR131"), aircraft: { registration: "CS-TQA", fleet_type: "A320neo", status: "serviceable" },
    booking: { status: "available", flight_no: "AR131", passenger_count: 151, seat_capacity: 174, held_seat_count: 0, available_seats: 23,
      connecting_passenger_count: 12, connections: [{ flight_no: "AR205", passenger_count: 12 }] } };
  const bookings = {};
  // Task 3b: a point on a drawn route where the route itself is under the pointer, to click or hover it as a person would.
  const pathPoint = (selector) => page.evaluate((sel) => {
    const path = document.querySelector(sel);
    if (!path) return null;
    const total = path.getTotalLength(), m = path.getScreenCTM();
    for (const k of [0.5, 0.4, 0.6, 0.3, 0.7, 0.35, 0.65, 0.25, 0.75, 0.2, 0.8, 0.45, 0.55, 0.15, 0.85]) {
      const p = path.getPointAtLength(total * k);
      const x = m.a * p.x + m.c * p.y + m.e, y = m.b * p.x + m.d * p.y + m.f;
      if (x > 0 && y > 0 && x < innerWidth && y < innerHeight && document.elementFromPoint(x, y) === path) return { x, y };
    }
    return null;
  }, selector);
  const clickPath = async (selector) => {
    const point = await pathPoint(selector);
    check(point, `The route ${selector} should be clickable on screen`);
    await page.mouse.click(point.x, point.y);
    return point;
  };
  const rect = (locator) => locator.evaluate(el => { const b = el.getBoundingClientRect(); return { x0: b.left, y0: b.top, x1: b.right, y1: b.bottom }; });
  const fc = page.locator("#map-fc");
  // On a phone: the points of a drawn route outside the band of map between the case card's header and the map tools above the flight
  // sheet ("" when none).
  const routeHidden = (selector) => page.evaluate((sel) => {
    const path = document.querySelector(sel), card = document.querySelector("#map-card"), sheet = document.querySelector("#map-fc");
    const stage = document.querySelector("#map-stage").getBoundingClientRect();
    if (!path) return `no ${sel}`;
    const top = Math.max(0, stage.top, card.hidden ? 0 : card.getBoundingClientRect().bottom) - 2;
    const bottom = Math.min(innerHeight, stage.bottom, sheet.getBoundingClientRect().top, document.querySelector(".mp-tools").getBoundingClientRect().top) + 2;
    const total = path.getTotalLength(), m = path.getScreenCTM();
    const out = [0, 0.25, 0.5, 0.75, 1].map(k => path.getPointAtLength(total * k)).map(q => [m.a * q.x + m.c * q.y + m.e, m.b * q.x + m.d * q.y + m.f])
      .filter(([x, y]) => x < -2 || x > innerWidth + 2 || y < top || y > bottom);
    return out.length ? `${out.length} of 5 points outside ${Math.round(top)}–${Math.round(bottom)}: ${JSON.stringify(out.map(q => q.map(Math.round)))}` : "";
  }, selector);
  const peek = page.locator("#map-peek");

  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    const json = (status, value) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (url.pathname === "/api/session") {
      return json(200, { user: { oid: "00000000-0000-4000-8000-000000000010", name: "Duty Manager", role: "OCC Duty Manager" }, day: DAY,
        demo_time: "09:45", mode: "simulated", capabilities: { approve: true, review: false } });
    }
    if (url.pathname === "/api/cases") return json(200, { cases: rows });
    if (url.pathname === "/api/rules") return json(503, { detail: "not in this smoke" });
    if (url.pathname === "/api/network") {
      if (networkFailures > 0) { networkFailures -= 1; return json(503, { detail: "Schedule is waking up" }); }
      return json(200, network);
    }
    const mapMatch = url.pathname.match(/^\/api\/cases\/([0-9a-f]{32})\/map$/);
    if (mapMatch && maps[mapMatch[1]]) {
      if (mapFailures[mapMatch[1]] > 0) { mapFailures[mapMatch[1]] -= 1; return json(503, { detail: "waking up" }); }
      return json(200, maps[mapMatch[1]]);
    }
    if (url.pathname === `/api/cases/${A}`) return json(200, caseDoc);
    if (url.pathname === `/api/cases/${C}`) return json(200, ranDoc);
    if (stormDoc && url.pathname === `/api/cases/${stormDoc.trace_id}`) return stormFails ? json(503, { detail: "waking up" }) : json(200, stormDoc);
    if (url.pathname === "/api/flights/AR131") return json(200, booking);
    const bookingMatch = url.pathname.match(/^\/api\/flights\/([A-Z0-9]+)$/);
    if (bookingMatch && bookings[bookingMatch[1]]) return json(200, bookings[bookingMatch[1]]);
    return json(404, { detail: "not found" });
  });

  // 1. The Map tab: the list works even when the schedule cannot be read; Retry loads the map.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/src/ops_app/static/index.html`);
  await page.locator("#tower-rows tr").first().waitFor();
  check((await page.locator("#tab-map").innerText()).trim() === "Map", "The tab should be called Map");
  await page.locator("#tab-map").click();
  await page.locator("#map-status").filter({ hasText: "Schedule unavailable" }).waitFor();
  const listed = page.locator("#map-list [data-case]");
  check((await listed.count()) === 5, "The list should work without the schedule");
  // Final review: a disruption opened without the schedule must not call its flights missing.
  await page.locator(`#map-list [data-case="${A}"]`).click();
  await page.locator("#map-card").filter({ hasText: "Schedule unavailable — the plan's flights cannot be drawn." }).waitFor();
  check(!(await page.locator("#map-card").innerText()).includes("not in today's schedule"), "Without the schedule no flight is called missing");
  await page.locator("#map-retry").click();
  await page.locator('.mp-lbl[data-flight="AR101"]').waitFor();
  check((await page.locator("#map-card").innerText()).includes("AR999 not in today's schedule"), "After Retry the focused day is drawn");
  await page.keyboard.press("Escape");
  await page.locator(".mp-mk").first().waitFor();

  // 2. The same cases and order as the control tower; a case without a station has no marker.
  const order = await listed.evaluateAll(items => items.map(item => item.dataset.case));
  check(JSON.stringify(order) === JSON.stringify([A, B, C, E, D]), `Urgency order like the control tower: ${order}`);
  check((await page.locator(`#map-list [data-case="${E}"]`).innerText()).includes("No location"), "A case without a station says so");
  check((await page.locator(".mp-mk").count()) === 4, "One marker per case with a station");
  check((await page.locator(`.mp-mk[data-case="${A}"]`).getAttribute("data-tone")) === "needs", "CS-TQA needs OCC");
  const blocked = page.locator(`.mp-mk[data-case="${B}"]`);
  check((await blocked.getAttribute("data-tone")) === "manual" && (await blocked.getAttribute("data-kind")) === "delay",
    "AR689 is a delay that needs manual action: a red clock");
  check(!(await page.locator(`#map-list [data-case="${B}"]`).innerText()).includes("€"), "A blocked plan's figures are not its impact");

  // 3. The network now, then later in the day as planned.
  check(Number(await page.locator("#map-airborne").innerText()) > 0, "Aircraft in the air at 09:45");
  // Each aircraft in the air shows its flight number where there is room, and names its flight on hover.
  await page.waitForTimeout(900);  // the camera has flown back to Europe after Esc
  const meets = (a, b, gap = 0) => a.x0 < b.x1 + gap && b.x0 - gap < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
  const tags = await page.locator("#map-traffic .mp-ac-no").evaluateAll(els => els.map(el => {
    const b = el.getBoundingClientRect();
    return { text: el.textContent, x0: b.left, y0: b.top, x1: b.right, y1: b.bottom };
  }));
  const airborneNow = ["AR306", "AR575", "AR543", "AR553", "AR571", "AR310"];
  check(tags.length >= 4 && tags.every(tag => airborneNow.includes(tag.text)), `Flight numbers above the aircraft: ${JSON.stringify(tags.map(t => t.text))}`);
  check(!tags.some((a, i) => tags.some((b, j) => i < j && meets(a, b))), "Flight numbers must not overlap");
  // A flight number keeps clear of airport names ("OPO" and "AR571" read as one word) and of disruption markers.
  const names = await page.locator("#map-aps text").evaluateAll(els => els.map(el => { const b = el.getBoundingClientRect(); return { x0: b.left, y0: b.top, x1: b.right, y1: b.bottom }; }));
  const covered = tags.filter(a => names.some(b => meets(a, b, 4))).map(t => t.text);
  check(!covered.length, `Flight numbers must not cover airport names: ${covered}`);
  const discs = await page.locator("#map-markers .mp-disc, #map-markers .mp-tag").evaluateAll(els => els.map(el => { const b = el.getBoundingClientRect(); return { x0: b.left, y0: b.top, x1: b.right, y1: b.bottom }; }));
  const hidden = tags.filter(a => discs.some(b => meets(a, b))).map(t => t.text);
  check(!hidden.length, `Flight numbers must not hide behind disruption markers: ${hidden}`);
  // Hovering an aircraft previews its flight: where it comes from and goes to, its times and that it is in the air (Task 3b).
  await page.locator('#map-traffic .mp-ac[data-flight="AR571"]').hover();
  await peek.waitFor({ timeout: 3000 });
  const preview = (await peek.innerText()).replace(/\s+/g, " ");
  check(preview.includes("AR571 · Lisbon → Madrid") && preview.includes("09:00–10:10") && preview.includes("In the air"),
    `Hovering an aircraft previews its flight, route and times: ${preview}`);
  await page.mouse.move(2, 2);
  check(await peek.isHidden(), "The preview closes when the pointer leaves");
  const time = await page.locator("#map-time").textContent();
  check(time.includes("09:45") && time.includes("Now"), `Now is the demo clock: ${time}`);
  await page.locator("#map-slider").evaluate(slider => { slider.value = "870"; slider.dispatchEvent(new Event("input", { bubbles: true })); });
  const later = await page.locator("#map-time").textContent();
  check(later.includes("14:30") && later.includes("Planned") && !/predict/i.test(later), `Later is planned, not predicted: ${later}`);
  // Playing the day does not announce every five-minute step to a screen reader; paused, it does.
  await page.locator("#map-play").click();
  check((await page.locator("#map-time").getAttribute("aria-live")) === "off", "Play keeps the time quiet");
  await page.locator("#map-play").click();
  check((await page.locator("#map-time").getAttribute("aria-live")) === "polite", "Paused, the time is announced");
  await page.locator("#map-now").click();
  await page.screenshot({ path: ".playwright-cli\\map-1440-network.png" });
  await professional("Map");
  // Task 3b: a route line lists today's flights on it; a flight opens its card (published → now after an executed delay); no case opens.
  await clickPath('#map-routes .mp-route-hit[data-route="FNC-LIS"]');
  await fc.filter({ hasText: "Lisbon ⇄ Funchal" }).waitFor({ timeout: 3000 });
  await page.screenshot({ path: ".playwright-cli\\map-1440-route.png" });
  let fcText = await fc.innerText();
  check(["AR575", "AR576", "AR805", "AR806", "AR310"].every(no => fcText.includes(no)) && fcText.includes("5 flights today") && await page.locator("#map-card").isHidden()
    && !(await page.locator("#map-svg").getAttribute("class")).includes("dim"), `A route lists its flights without opening a case: ${fcText}`);
  check((await page.locator('#map-routes .mp-route.sel').getAttribute("data-route")) === "FNC-LIS", "The chosen route is highlighted");
  await fc.locator('[data-flight-open="AR805"]').click();
  await fc.filter({ hasText: "Later by 60 min" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("AR805") && fcText.includes("LIS") && fcText.includes("Lisbon") && fcText.includes("FNC") && fcText.includes("Funchal")
    && fcText.includes("13:40 → 14:40") && fcText.includes("15:25 → 16:25") && fcText.includes("CS-TKJ") && fcText.includes("A321neo"),
    `An executed delay reads published → now: ${fcText}`);
  await fc.locator("[data-fc-back]").click();
  await fc.filter({ hasText: "5 flights today" }).waitFor({ timeout: 3000 });
  await fc.locator('[data-flight-open="AR575"]').click();
  await fc.filter({ hasText: "AR575" }).filter({ hasText: "In the air now" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("09:00") && fcText.includes("10:45") && fcText.includes("On time"), `A flight in the air says so: ${fcText}`);
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await page.locator("#map-card").isHidden(), "Esc closes the flight card");
  // Keyboard: Enter on a disruption marker opens its case, as a click does.
  await page.locator(`.mp-mk[data-case="${A}"]`).focus();
  await page.keyboard.press("Enter");
  await page.locator("#map-card").filter({ hasText: "Needs your decision" }).waitFor({ timeout: 3000 });
  await page.keyboard.press("Escape");
  await page.locator(".mp-mk").first().waitFor();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  const wide = await page.evaluate(() => [...document.querySelectorAll("#map *")].filter(el => !el.closest("svg") && el.getBoundingClientRect().right > innerWidth + 1)
    .slice(0, 5).map(el => `${el.tagName.toLowerCase()}#${el.id}.${String(el.className.baseVal ?? el.className).split(" ")[0]}:${Math.round(el.getBoundingClientRect().right)}`));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `The map should not overflow at 390 px: ${wide.join(", ")}`);
  await page.screenshot({ path: ".playwright-cli\\map-390-network.png", fullPage: true });
  await professional("Map 390 px");
  await page.setViewportSize({ width: 1440, height: 900 });

  // 4. Focus on CS-TQA: the aircraft's day, the recommended plan, who decides and why.
  const card = page.locator("#map-card");
  const label = (no) => page.locator(`.mp-lbl[data-flight="${no}"]`);
  await page.locator(`#map-list [data-case="${A}"]`).click();
  await card.filter({ hasText: "Needs your decision" }).waitFor();
  await card.locator("[data-plan]").first().waitFor();
  check((await card.locator("[data-plan]").count()) === 2, "Two plans to compare");
  check((await card.locator('[data-plan][aria-pressed="true"]').innerText()).includes("Recommended"), "The recommended plan is shown first");
  check((await label("AR101").textContent()).includes("Cancelled"), "Cancelled legs say so on the map");
  let words = await card.innerText();
  // A Dev run: the map named plans differently from the case page and showed the demo clock as the report time.
  const planNames = await card.locator("[data-plan] > span").allTextContents();
  check(planNames.join("|") === "Cancel AR101 + AR102 + AR999|Delay the rotation", `Plans carry the case page's names: ${planNames}`);
  // Owner: the Recommended label sits on its line inside the plan button, the report reads without quote marks as on the
  // case page, and the list counts speak as the control tower does.
  check(await card.locator(".mp-plan .mp-rec").evaluate(el => {
    const [b, p] = [el.getBoundingClientRect(), el.closest(".mp-plan").getBoundingClientRect()];
    return b.height < 20 && b.left >= p.left && b.right <= p.right && b.bottom <= p.bottom && getComputedStyle(el).borderTopWidth === "0px";
  }), "The Recommended label should sit inside its plan button");
  check(!/[“”"]/.test(await card.locator(".mp-report").innerText()), "The report should not be wrapped in quote marks");
  const counts = (await page.locator("#map-counts").innerText()).replace(/\s+/g, " ");
  check(counts.includes("need OCC") && counts.includes("with Passenger Services") && !counts.includes("need people"), `The counts should speak as the control tower: ${counts}`);
  check(words.includes("Reported in ARROW · 17:56"), `The report time is when ARROW received it, as on the case page: ${words}`);
  check(words.includes("AR999 not in today's schedule"), `A plan flight outside today's schedule is listed, not drawn: ${words}`);
  check(words.includes("Proposal only — nothing has changed in the airline systems. 2 flights would change."), `Proposal line: ${words}`);
  check(words.includes("OCC decides") && words.includes("Cancellation: AR101 · AR102") && words.includes("€81,000"), `Who decides and why: ${words}`);
  // Owner: only AI-written text is marked, as AI. The orchestrator wrote the recommended plan's reason; who decides is code's.
  check((await card.locator(".mp-decide p .mk-ai").count()) === 1 && (await card.locator(".mp-decide > b .mk-ai").count()) === 0,
    "The orchestrator's reason should carry the AI mark, and who decides should not");
  check((await page.locator("#map-strip .mp-out").count()) === 1, "The rotation strip shows the out-of-service window");
  check((await page.locator("#map-strip .mp-row").allTextContents()).join("|") === "Schedule|Proposed plan", "The strip compares the schedule with the plan");
  check((await page.locator("#map-traffic .mp-ac-no").count()) === 0, "Other aircraft's flight numbers step back while one aircraft is in focus");
  await page.waitForTimeout(800);
  await page.screenshot({ path: ".playwright-cli\\map-1440-focus.png" });
  await professional("Map case card");
  // A Dev run: CS-TQA with two what-ifs from the case chat has four plans, and one row of four squeezed the Recommended label
  // out of its button. Four or more plans sit in rows of two, as on the case page.
  const twoPlans = maps[A];
  const blockedWhatIf = (n, minutes) => plan({ option_id: `what-if-${n}`, kind: "delay", title: `Delay the rotation, ${minutes} min more`, route: "blocked",
    cost_eur: 200000 + n, reasons: ["Lisbon capacity: the hour is full"], flights: twoPlans.plans[1].flights });
  maps[A] = { ...twoPlans, plans: [...twoPlans.plans, blockedWhatIf(3, 120), blockedWhatIf(4, 180)] };
  await page.locator(`#map-list [data-case="${A}"]`).click();
  await card.locator("[data-plan]").nth(3).waitFor();
  const tiles = await card.locator(".mp-plan").evaluateAll(els => els.map(el => {
    const box = el.getBoundingClientRect(), rec = el.querySelector(".mp-rec")?.getBoundingClientRect();
    return { top: Math.round(box.top), left: Math.round(box.left), inside: !rec || (rec.left >= box.left && rec.right <= box.right) };
  }));
  check(tiles.length === 4 && tiles.every(t => t.inside) && tiles[0].top === tiles[1].top && tiles[2].top > tiles[0].top
    && tiles[2].top === tiles[3].top && tiles[2].left === tiles[0].left, `Four plans sit in rows of two, each label inside its button: ${JSON.stringify(tiles)}`);
  maps[A] = twoPlans;
  await page.locator(`#map-list [data-case="${A}"]`).click();
  await card.locator("[data-plan]").nth(2).waitFor({ state: "detached" });
  // Keyboard: Space on a leg label opens that flight beside the case (Task 3b): where it comes from and goes to, its times,
  // its status, the aircraft and the plan that moves it; the case card stays open and neither card covers the other or the route.
  await label("AR102").focus();
  await page.keyboard.press(" ");
  await fc.filter({ hasText: "AR102" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("OPO") && fcText.includes("Porto") && fcText.includes("LIS") && fcText.includes("Lisbon") && fcText.includes("Cancelled")
    && fcText.includes("12:00") && fcText.includes("CS-TQA") && fcText.includes("A320neo") && fcText.includes("Plan 1 · Cancel AR101 + AR102 + AR999")
    && fcText.includes("Open case"), `A cancelled flight's card: ${fcText}`);
  check(await card.isVisible() && (await card.innerText()).includes("Needs your decision"), "The case card stays open beside the flight card");
  const [fcBox, cardBox, legBox] = [await rect(fc), await rect(card), await rect(page.locator('#map-rotation .mp-leg.sel[data-flight="AR102"]'))];
  check(!meets(fcBox, cardBox), `The flight card must not cover the case card: ${JSON.stringify([fcBox, cardBox])}`);
  check(!meets(fcBox, legBox), `The flight card must not cover the selected route: ${JSON.stringify([fcBox, legBox])}`);
  await page.waitForTimeout(400);
  await page.screenshot({ path: ".playwright-cli\\map-1440-case3-flight.png" });
  await professional("Map case flight card");
  // Between phone and desktop the two cards share the column over the list.
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.waitForTimeout(400);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && !meets(await rect(fc), await rect(card)) && await fc.isVisible(),
    "At 1024 px the flight card sits below the case card without overflow");
  const narrowEnds = await fc.locator(".mp-fc-time").evaluateAll(els => els.every(el => el.scrollWidth <= el.clientWidth + 1));
  check(narrowEnds, "At 1024 px the departure and arrival times fit their columns");
  await page.screenshot({ path: ".playwright-cli\\map-1024-case3-flight.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await card.isVisible(), "Esc closes the flight card first; the case stays open");

  // 5. The delay plan redraws the legs; later in the day the aircraft flies the delayed AR101.
  await card.locator('[data-plan="delay-a"]').click();
  const delayed = await label("AR101").textContent();
  check(delayed.includes("13:45") && delayed.includes("+195"), `Delayed legs show the new time and minutes: ${delayed}`);
  // Task 3b: clicking the delayed leg's label shows its departure and arrival was → now, how late, and the plan that moves it.
  await label("AR101").click();
  await fc.filter({ hasText: "Later by 195 min" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("Lisbon") && fcText.includes("Porto") && fcText.includes("10:30 → 13:45") && fcText.includes("11:25 → 14:40")
    && fcText.includes("Plan 2 · Delay the rotation") && fcText.includes("Proposal") && !fcText.includes("In the air"), `A delayed flight's card: ${fcText}`);
  // The case has no planned arrivals here: the arrival is timed from the timetable and says so on hover.
  check((await fc.locator(".mp-fc-end.to .mp-fc-est").getAttribute("title")) === "Estimated from the timetable" && (await fc.locator(".mp-fc-est").count()) === 1,
    "An arrival estimated from the timetable is marked");
  await page.locator("#map-slider").evaluate(slider => { slider.value = "870"; slider.dispatchEvent(new Event("input", { bubbles: true })); });
  check((await page.locator(".mp-me").textContent()).includes("AR101 in flight"), "At 14:30 CS-TQA flies the delayed AR101");
  check((await fc.innerText()).includes("In the air at 14:30"), "The card follows the map's time: in the air at 14:30");
  await page.screenshot({ path: ".playwright-cli\\map-1440-delay-1430.png" });
  await page.locator("#map-now").click();
  // A route line opens its flight too, and hovering it previews the flight; passengers come from the passenger system.
  const ar131 = '#map-rotation .mp-hit[data-flight="AR131"]';
  const over = await pathPoint(ar131);
  check(over, "AR131's route should be on screen");
  await page.mouse.move(over.x, over.y);
  await peek.waitFor({ timeout: 3000 });
  const legPeek = (await peek.innerText()).replace(/\s+/g, " ");
  check(legPeek.includes("AR131 · Lisbon → Faro") && legPeek.includes("16:45–17:30") && legPeek.includes("+155 min"), `Hovering a route previews its flight: ${legPeek}`);
  await page.screenshot({ path: ".playwright-cli\\map-1440-peek.png" });
  await page.mouse.click(over.x, over.y);
  await fc.filter({ hasText: "AR131" }).filter({ hasText: "12 connecting" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("Faro") && fcText.includes("14:10 → 16:45") && fcText.includes("14:55 → 17:30") && fcText.includes("Later by 155 min")
    && fcText.includes("151"), `Clicking a route opens its flight with passengers: ${fcText}`);
  await page.mouse.move(2, 2);
  // Open case from the flight card, and back to the map from the case header.
  await fc.getByRole("button", { name: /Open case/ }).click();
  await page.locator("#case-title").filter({ hasText: "CS-TQA" }).waitFor();
  await page.locator("#case-map").click();
  await card.filter({ hasText: "CS-TQA · out of service 4 h" }).waitFor();
  check(await fc.isHidden(), "Back on the map the case opens without the old flight card");

  // 6. Open the case, and back to the map from the case header.
  await card.getByText("Open case →").click();
  await page.locator("#case-title").filter({ hasText: "CS-TQA" }).waitFor();
  check(await page.locator("#workspace").isVisible(), "Open case shows the case");
  await page.locator("#case-map").click();
  await card.filter({ hasText: "CS-TQA · out of service 4 h" }).waitFor();
  check(await page.locator("#map").isVisible(), "Show on map returns to the map, focused");

  // 7. Show on map from the control tower: a blocked plan shows its rule, not its figures.
  await page.locator("#tab-tower").click();
  await page.locator(`#tower-rows tr[data-trace="${B}"] .tw-map`).click();
  await card.filter({ hasText: "Blocked" }).waitFor();
  words = await card.innerText();
  check(words.includes("Crew flight-time limits: D-PDL-002: 745 min") && !words.includes("lose flight or connection")
    && words.includes("Blocked — nothing has changed in the airline systems."), `Blocked: rule shown, figures hidden: ${words}`);

  // 8. A map view that cannot be read says so, with nothing left of the previous case; Retry reads it.
  await page.locator(`#map-list [data-case="${C}"]`).click();
  await card.filter({ hasText: "could not be read" }).waitFor();
  check((await page.locator(".mp-lbl").count()) === 0 && await page.locator("#map-rot").isHidden(),
    "The previous case's day is cleared while the next one loads");
  await card.getByRole("button", { name: "Retry" }).click();
  await card.filter({ hasText: "OCC approved" }).waitFor();
  check((await card.locator('[data-plan][aria-pressed="true"]').getAttribute("data-plan")) === "delay-c",
    "The plan carried out is shown first, not the recommended one");
  check((await card.innerText()).includes("Executed by ARROW — receipts are in the case."), "An executed plan says so");
  await card.locator('[data-plan="cancel-c"]').click();
  words = await card.innerText();
  check(words.includes("Not chosen") && !words.includes("Proposal only") && !words.includes("OCC decides"),
    `A plan not chosen is not a live proposal: ${words}`);
  // Owner ("why do I see these flights on time, i thought delay was propagated"): the case of a plan carried out compares
  // its flights with the published timetable, though the map's day already carries the new times.
  await card.getByText("Open case →").click();
  await page.locator("#case-title").filter({ hasText: "AR805" }).waitFor();
  // The plan's flights are in the Plans step (since the case summary).
  await page.locator('#steps [data-step="plans"]').click();
  const ran = page.locator("#step-plans .pl-plan").filter({ has: page.locator(".pl-tag.ran") }).locator(".plan-flights");
  check((await ran.locator("summary").innerText()).replace(/\s+/g, " ").trim() === "Flights 3 later · up to +1 h",
    `A plan carried out should count its flights as later, not on time: ${await ran.locator("summary").innerText()}`);
  await ran.locator("summary").click();
  const ranRows = await ran.locator("tbody tr").evaluateAll(list => list.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  check(ranRows.join(" | ") === "AR805 LIS → FNC 13:40 → 14:40 +60 min | AR806 FNC → LIS 16:00 → 17:00 +60 min | AR1131 LIS → OPO 23:35 → 00:35 +60 min",
    `Each flight should read from its published time: ${ranRows.join(" | ")}`);
  await page.locator("#case-map").click();
  await card.filter({ hasText: "AR805 · 60 min late" }).waitFor();

  // 9. After midnight: at 05:00 the next day CS-TUK flies AR260 home.
  await page.locator(`#map-list [data-case="${D}"]`).click();
  await card.filter({ hasText: "AR259 · 20 min late" }).waitFor();
  await page.locator("#map-slider").evaluate(slider => { slider.value = "1740"; slider.dispatchEvent(new Event("input", { bubbles: true })); });
  check((await page.locator(".mp-me").textContent()).includes("AR260 in flight"), "Positions after midnight follow the timetable");
  await page.locator("#map-now").click();
  // A plan stopped part-way is not "nothing has changed".
  maps[D] = { ...maps[D], plans: [{ ...maps[D].plans[0], executed: false, execution: { status: "needs_attention", actions_done: 1, actions_total: 2 } }] };
  await page.locator(`#map-list [data-case="${D}"]`).click();
  await card.filter({ hasText: "Stopped · 1 of 2 actions confirmed — see the case." }).waitFor();
  check(!(await card.innerText()).includes("nothing has changed"), "A partly carried out plan is not shown as unchanged");

  // 10. A report without a location: its card opens, the map stays where it is.
  await page.waitForTimeout(900);
  const camera = await page.evaluate(() => JSON.stringify(mapView.cam));
  await page.locator(`#map-list [data-case="${E}"]`).click();
  await card.filter({ hasText: "No plan yet" }).waitFor();
  await page.waitForTimeout(900);
  const after = await page.evaluate(() => JSON.stringify(mapView.cam));
  check(after === camera, `A case without a location does not move the map: ${camera} → ${after}`);
  // The open card follows the 10 s refresh: ARROW finishes reading the report and a plan appears.
  Object.assign(rows.find(r => r.trace_id === E), { state: "needs", label: "Needs your decision", urgency: 1, status: "assessment_ready" });
  maps[E] = { ...maps[E], state: "needs", label: "Needs your decision", plans: [plan({ option_id: "delay-e", title: "Delay AR131", recommended: true,
    route: "occ", passengers: 20, cost_eur: 25000, reasons: ["Cost limit for automatic plans: €25,000"], flights: [fl("AR131", "14:10", "15:10", "scheduled", "CS-TQA")] })] };
  await page.evaluate(() => loadCases());
  await card.filter({ hasText: "Delay AR131" }).waitFor();
  check(!(await card.innerText()).includes("No plan yet"), "The open card shows the plan once ARROW has one");

  // 11. Find a flight: its card shows where it flies, its times and its passengers and connections from the passenger system.
  await page.locator("#map-find").fill("ar131");
  await page.locator("#map-find").press("Enter");
  await fc.filter({ hasText: "12 connecting" }).waitFor();
  words = await fc.innerText();
  check(words.includes("AR131") && words.includes("Lisbon") && words.includes("Faro") && words.includes("151") && words.includes("14:10")
    && words.includes("On time"), `Flight card: ${words}`);
  check((await fc.locator("[data-pax]").getAttribute("title")).includes("AR205 · 12"), "The connections show on hover");
  // An aircraft without a plan shows its schedule once, not the schedule against itself.
  check((await page.locator("#map-strip .mp-row").allTextContents()).join("|") === "Schedule", "Without a plan the strip has one Schedule row");
  await page.waitForTimeout(800);
  await page.screenshot({ path: ".playwright-cli\\map-1440-aircraft.png" });
  await professional("Map flight card");
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await card.isVisible(), "Esc closes the flight card, then the aircraft");
  await page.keyboard.press("Escape");
  check(await card.isHidden(), "Esc closes the card");

  // 12. On a phone the card opens over the list and stays readable.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await page.locator(`#map-list [data-case="${A}"]`).click();
  await card.filter({ hasText: "Needs your decision" }).waitFor();
  await page.waitForTimeout(800);
  const wideFocus = await page.evaluate(() => [...document.querySelectorAll("#map *")].filter(el => !el.closest("svg") && el.getBoundingClientRect().right > innerWidth + 1)
    .slice(0, 5).map(el => `${el.tagName.toLowerCase()}#${el.id}.${String(el.className.baseVal ?? el.className).split(" ")[0]}:${Math.round(el.getBoundingClientRect().right)}`));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `The focused map should not overflow at 390 px: ${wideFocus.join(", ")}`);
  await page.screenshot({ path: ".playwright-cli\\map-390-focus.png", fullPage: true });
  await professional("Map 390 px case card");
  // Task 3b: on a phone the flight card is a bottom sheet below the case card, not over it.
  await label("AR101").focus();
  await page.keyboard.press("Enter");
  await fc.filter({ hasText: "AR101" }).filter({ hasText: "Porto" }).waitFor({ timeout: 3000 });
  await page.waitForTimeout(900);
  // The case card keeps its header and the list steps aside, so the chosen flight's route shows above the sheet.
  check((await card.innerText()).includes("CS-TQA · out of service 4 h") && await card.locator(".mp-card-body").isHidden() && await page.locator("#map-list").isHidden(),
    "With the sheet open on a phone the case card keeps only its header");
  const shownAt390 = await routeHidden('#map-rotation .mp-leg.sel[data-flight="AR101"]');
  check(!shownAt390, `At 390 px the chosen flight's route shows between the case card and the sheet: ${shownAt390}`);
  const sheet = await fc.evaluate(el => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, w: innerWidth, h: innerHeight }; });
  check(Math.abs(sheet.bottom - sheet.h) <= 1 && sheet.left <= 0.5 && Math.abs(sheet.right - sheet.w) <= 1 && sheet.top > sheet.h * 0.35,
    `At 390 px the flight card is a bottom sheet: ${JSON.stringify(sheet)}`);
  const caseBox = await rect(card);
  check(caseBox.y1 <= sheet.top + 1, `The bottom sheet must not cover the case card: ${JSON.stringify([caseBox, sheet])}`);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The flight sheet should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\map-390-case3-flight.png" });
  await professional("Map 390 px flight card");
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await card.locator(".mp-card-body").isVisible(), "Esc closes the sheet and the case card opens again");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1440, height: 900 });

  // An executed plan changes the schedule: the map reads it again (other changes do not).
  const reads = () => urls.filter(u => new URL(u).pathname === "/api/network").length;
  let before = reads();
  rows.find(r => r.trace_id === E).label = "ARROW is still assessing";
  await page.evaluate(() => loadCases());
  await page.waitForTimeout(300);
  check(reads() === before, "A case update without executed actions does not re-read the schedule");
  Object.assign(rows.find(r => r.trace_id === A).impact, { actions_done: 3, actions_total: 3 });
  await page.evaluate(() => loadCases());
  await page.waitForTimeout(300);
  check(reads() === before + 1, "An executed plan re-reads the schedule");

  // A Reset closes the open case: its card closes and it leaves the list (and the day is read again).
  before = reads();
  await page.locator(`#map-list [data-case="${D}"]`).click();
  await card.filter({ hasText: "AR259" }).waitFor();
  Object.assign(rows.find(r => r.trace_id === D), { state: "closed", label: "Closed", status: "closed" });
  await page.evaluate(() => loadCases());
  await card.waitFor({ state: "hidden", timeout: 4000 }).catch(() => {});
  check(await card.isHidden(), "The card of a case closed by Reset should close");
  check(!(await page.locator(`#map-list [data-case="${D}"]`).count()), "A closed case should leave the map list");
  await page.waitForTimeout(300);
  check(reads() === before + 1, "Reset reloads the day: the map reads the schedule again");

  // 12b. Case 5 (Task 4c; now a crosswind): gusts across Lisbon's single runway. One case for the whole forecast and no single
  // aircraft: the map draws the runway with the wind across it and the window on hover, and highlights the flights the plan moves or
  // cancels, without picking one aircraft; every aircraft stays in view.
  // The server's view still names the first affected flight's aircraft (trail.map_view); a storm must ignore it.
  const S = "8".repeat(32);
  const plus = (time, minutes) => { const t = Number(time.slice(0, 2)) * 60 + Number(time.slice(3)) + minutes; return `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };
  // The whole storm, as the operator smoke's Case 5 (Task 4b's offline run): flight, from, to, departs, arrives ("+" next day),
  // aircraft, minutes later in the cancel plan ("x" cancelled, "-" not in it), minutes later in the delay plan.
  const STORM = ("AR532 BER LIS 13:10 16:45 CS-TJC 30 32|AR604 AMS LIS 13:35 16:35 CS-TJQ - 25|AR608 MUC LIS 13:45 16:55 CS-TJV 32 35|" +
    "AR628 BRU LIS 14:00 16:45 CS-TQB 32 35|AR630 LGW LIS 14:00 16:35 CS-TQC 25 27|AR131 LIS FAO 14:10 14:55 CS-TQA x -|AR728 FNC LIS 14:50 16:35 CS-TQR 27 30|" +
    "AR656 ZRH LIS 14:55 17:45 CS-TRK 27 28|AR726 BCN LIS 14:55 16:45 CS-TKH 35 37|AR660 BLQ LIS 15:00 17:55 CS-TSB 32 33|AR686 LHR LIS 15:10 17:45 CS-TQU 28 30|" +
    "AR702 PDL LIS 15:10 17:35 CS-TRZ 25 25|AR700 GVA LIS 15:15 17:45 CS-TQK 30 31|AR132 FAO LIS 15:35 16:20 CS-TQA x -|AR840 OPO LIS 15:40 16:35 CS-TRH 30 32|" +
    "AR881 LIS FAO 15:40 16:25 CS-TRO x -|AR696 EDI LIS 15:50 18:55 CS-TQG 6 9|AR806 FNC LIS 16:00 17:45 CS-TKJ 31 33|AR802 BOD LIS 16:05 17:55 CS-TJJ 33 35|" +
    "AR828 RAK LIS 16:10 17:45 CS-TRF 33 34|AR784 CDG LIS 16:25 18:55 CS-TQI 8 11|AR852 AGP LIS 16:25 17:35 CS-TJR 26 26|AR864 OPO LIS 16:40 17:35 CS-TJM 19 19|" +
    "AR927 LIS FRA 16:40 19:40 CS-TJK 27 30|AR929 LIS SVQ 16:40 17:35 CS-TJN 30 32|AR931 LIS CDG 16:40 19:10 CS-TQH 32 35|AR937 LIS ORY 16:50 19:15 CS-TQJ 32 35|" +
    "AR939 LIS MAD 16:50 18:00 CS-TQM 35 37|AR882 FAO LIS 17:00 17:45 CS-TRO x 36|AR945 LIS OPO 17:10 18:05 CS-TJQ - 54|AR949 LIS AGP 17:10 18:20 CS-TQC 54 56|" +
    "AR951 LIS OPO 17:10 18:05 CS-TQR 56 57|AR953 LIS OPO 17:10 18:05 CS-TRH 50 50|AR959 LIS BCN 17:20 19:10 CS-TJC 59 67|AR961 LIS PDL 17:20 19:45 CS-TQB 65 71|" +
    "AR965 LIS MAD 17:30 18:40 CS-TJV 32 35|AR971 LIS LYS 17:40 20:00 CS-TQT 29 29|AR973 LIS AGP 17:40 18:50 CS-TRD 30 30|AR977 LIS BIO 17:50 19:20 CS-TKD 31 32|" +
    "AR979 LIS MAD 17:50 19:00 CS-TKE 32 34|AR981 LIS LYS 17:50 20:10 CS-TQX 34 35|AR1005 LIS PDL 18:10 20:35 CS-TRZ 25 25|AR930 SVQ LIS 18:10 19:05 CS-TJN 30 32|" +
    "AR997 LIS MAD 18:10 19:20 CS-TJM 25 25|AR999 LIS FAO 18:10 18:55 CS-TJR 26 26|AR1007 LIS MAN 18:20 21:05 CS-TQK 30 41|AR1009 LIS MAN 18:20 21:05 CS-TQU 28 30|" +
    "AR1011 LIS NCE 18:20 20:50 CS-TRF 40 43|AR1013 LIS BCN 18:20 20:10 CS-TRK 27 28|AR1015 LIS LGW 18:30 21:05 CS-TJJ 33 35|AR1017 LIS LHR 18:30 21:05 CS-TSB 32 33|" +
    "AR940 MAD LIS 18:35 19:45 CS-TQM 35 37|AR946 OPO LIS 18:40 19:35 CS-TJQ - 80|AR952 OPO LIS 18:40 19:35 CS-TQR 80 80|AR954 OPO LIS 18:40 19:35 CS-TRH 80 80|" +
    "AR1023 LIS SVQ 18:50 19:45 CS-TKB - 10|AR950 AGP LIS 18:55 20:05 CS-TQC 54 56|AR966 MAD LIS 19:15 20:25 CS-TJV 32 35|AR974 AGP LIS 19:25 20:35 CS-TRD 30 30|" +
    "AR1000 FAO LIS 19:30 20:15 CS-TJR 26 26|AR1063 LIS FAO 19:30 20:15 CS-TQG 6 9|AR1065 LIS MAD 19:30 20:40 CS-TQI 8 11|AR980 MAD LIS 19:35 20:45 CS-TKE 32 34|" +
    "AR932 CDG LIS 19:45 22:15 CS-TQH 28 31|AR960 BCN LIS 19:45 21:35 CS-TJC 56 64|AR938 ORY LIS 19:50 22:15 CS-TQJ 28 31|AR978 BIO LIS 19:55 21:25 CS-TKD 29 30|" +
    "AR998 MAD LIS 19:55 21:05 CS-TJM 25 25|AR928 FRA LIS 20:15 23:15 CS-TJK 22 25|AR1024 SVQ LIS 20:20 21:15 CS-TKB - 10|AR962 PDL LIS 20:20 22:45 CS-TQB 61 67|" +
    "AR972 LYS LIS 20:35 22:55 CS-TQT 29 29|AR1014 BCN LIS 20:45 22:35 CS-TRK 27 28|AR982 LYS LIS 20:45 23:05 CS-TQX 30 31|AR1064 FAO LIS 20:50 21:35 CS-TQG 6 9|" +
    "AR1006 PDL LIS 21:10 23:35 CS-TRZ 21 21|AR1066 MAD LIS 21:15 22:25 CS-TQI 8 11|AR1012 NCE LIS 21:25 23:55 CS-TRF 36 39|AR1008 MAN LIS 21:40 00:25+ CS-TQK 30 37|" +
    "AR1010 MAN LIS 21:40 00:25+ CS-TQU 28 30|AR1016 LGW LIS 21:40 00:15+ CS-TJJ 29 31|AR1018 LHR LIS 21:40 00:15+ CS-TSB 28 29|AR1097 LIS MXP 21:50 00:30+ CS-TJM 15 15|" +
    "AR1113 LIS PDL 22:05 00:30+ CS-TKD 22 23").split("|").map(entry => entry.split(" "));
  // The storm's day joins the schedule (the four demo aircraft's flights stay as they are) with the airports it flies to.
  const known = new Set(network.flights.map(f => f.flight_no));
  network.flights.push(...STORM.filter(([no]) => !known.has(no)).map(([no, from, to, std, sta, reg]) =>
    leg([no, from, to, std, sta.replace("+", ""), reg, sta.endsWith("+")])));
  network.airports.push(...[["BER", 13.5, 52.37], ["AMS", 4.76, 52.31], ["MUC", 11.79, 48.35], ["BRU", 4.48, 50.9], ["LGW", -0.19, 51.15], ["ZRH", 8.56, 47.46],
    ["BCN", 2.08, 41.3], ["BLQ", 11.29, 44.53], ["GVA", 6.11, 46.24], ["EDI", -3.37, 55.95], ["BOD", -0.72, 44.83], ["RAK", -8.04, 31.61], ["AGP", -4.5, 36.67],
    ["FRA", 8.57, 50.03], ["SVQ", -5.89, 37.42], ["ORY", 2.36, 48.73], ["LYS", 5.08, 45.73], ["BIO", -2.91, 43.3], ["MAN", -2.27, 53.35], ["NCE", 7.21, 43.66],
    ["MXP", 8.72, 45.63]].map(([iata, lon, lat]) => ({ iata, position: [lon, lat] })));
  const stormPlan = (kind) => STORM.filter(r => r[kind === "cancel" ? 6 : 7] !== "-").map(([no, , , std, , reg, cancel, delay]) => {
    const late = kind === "cancel" ? cancel : delay;
    return late === "x" ? fl(no, std, null, "cancelled", reg) : fl(no, std, plus(std, Number(late)), "scheduled", reg);
  });
  // The case's own plans carry each flight's new arrival and its minutes held in the air for a Lisbon arrival slot (weather.py): AR864
  // leaves 19 min late and holds 6 min more.
  const HELD = { AR864: 6 };
  const stormOption = (kind) => STORM.filter(r => r[kind === "cancel" ? 6 : 7] !== "-").map(([no, from, to, std, sta, reg, cancel, delay]) => {
    const late = kind === "cancel" ? cancel : delay, next = sta.endsWith("+"), arrives = sta.replace("+", "");
    if (late === "x") return { flight_no: no, origin: from, destination: to, std_local: at(std), sta_local: at(arrives, next), aircraft_reg: reg, status: "cancelled", held_airborne_minutes: 0 };
    const held = HELD[no] || 0, land = plus(arrives, Number(late) + held);
    return { flight_no: no, origin: from, destination: to, std_local: at(plus(std, Number(late))), sta_local: at(land, next || land < arrives),
      aircraft_reg: reg, status: "scheduled", held_airborne_minutes: held };
  });
  rows.push(row(S, { headline: "Strong crosswind at Lisbon · 16:00–18:00 · gusts 33 kt across runway 02/20 · 24 movements an hour (normally 40)", state: "needs", label: "Needs your decision", urgency: 1, source: "IPMA (weather)",
    disruption_type: "weather", station: "LIS", first_departure: { flight_no: "AR532", std_local: at("13:10"), planned_local: at("13:40") },
    impact: impact({ passengers: 655, rebooked: 41, cost_eur: 397477, plans: 2, plan_title: "Cancel the Faro round trips, then sequence the rest",
      plan_kind: "cancel", plan_cancelled: ["AR131", "AR132", "AR881", "AR882"], route: "occ" }) }));
  views[S] = { report: "TAF AMD LPPT 100920Z 1010/1112 19012KT 9999 SCT020 BKN040 TEMPO 1016/1018 27022G35KT 6000 SHRA BKN015", aircraft_reg: "CS-TQU" };
  maps[S] = mapOf(S, [
    plan({ option_id: "1c19c99447ae7da3", kind: "cancel", title: "Cancel the Faro round trips, then sequence the rest", recommended: true, route: "occ",
      reason: "The only plan the crew can fly", passengers: 655, rebooked: 41, review: 614, cost_eur: 397477,
      reasons: ["Cost €397,477 · limit €20,000", "Rebooked 41 passengers · limit 10", "Cancellation: AR131 · AR132 · AR881 · AR882", "Passengers without rebooking: 614 passengers"],
      flights: stormPlan("cancel") }),
    plan({ option_id: "2296eb1956be45f9", kind: "delay", title: "Accept the reduced Lisbon rate and fly the affected flights late", route: "blocked", passengers: 0, rebooked: 0,
      cost_eur: 58533, reasons: ["Crew flight-time limits: D-LIS-164: 681 min · limit 660 min"], flights: stormPlan("delay") })]);
  // The storm itself, as the case reads it (GET /api/cases/{trace}).
  stormDoc = { trace_id: S, display_id: maps[S].display_id, status: "assessment_ready", version: 4, created_at: `${DAY}T09:50:00Z`, operating_date: DAY,
    source: { kind: "event", system: "IPMA (weather)" }, stage: "OCC decides",
    disruption: { type: "weather", cause: "weather", airport: "LIS", hazard: "crosswind", window_start: "16:00", window_end: "18:00", hourly_rate: 24, normal_rate: 40,
      wind: "27022G35KT", crosswind_kt: 33, runway: "02/20", rate_basis: "the airline's OCC playbook rate for crosswind on Lisbon's single runway: the A320 family's demonstrated crosswind is 38 kt including gusts (35 kt for take-off with the A320neo's engines), so operations continue with care",
      title: "Strong crosswind at Lisbon · 16:00–18:00", scope: { flights: STORM.map(r => r[0]), aircraft_reg: null },
      message: views[S].report, reported_at: at("09:45") }, events: [],
    options: [["1c19c99447ae7da3", "cancel", "Cancel the Faro round trips, then sequence the rest"],
      ["2296eb1956be45f9", "delay", "Accept the reduced Lisbon rate and fly the affected flights late"]]
      .map(([option_id, kind, title]) => ({ option_id, plan_id: option_id, kind, title, flights: stormOption(kind) })) };
  await page.evaluate(() => loadCases());
  await page.evaluate(() => mapView.reload());
  const stormItem = page.locator(`#map-list [data-case="${S}"]`);
  await stormItem.waitFor();
  check((await page.locator(`.mp-mk[data-case="${S}"]`).getAttribute("data-kind")) === "weather" &&
    (await stormItem.locator(".mp-badge svg").innerHTML()) !== (await page.locator(`#map-list [data-case="${B}"] .mp-badge svg`).innerHTML()),
    "A storm should have its own marker icon");
  const listWindow = await stormItem.locator(".tw-keep").evaluate(el => ({ text: el.textContent, lines: el.getClientRects().length })).catch(() => null);
  check(listWindow?.text === "16:00–18:00" && listWindow.lines === 1, `The list should keep the storm's window on one line: ${JSON.stringify(listWindow)}`);
  await stormItem.click();
  await card.filter({ hasText: "Strong crosswind at Lisbon · 16:00–18:00" }).locator("[data-plan]").first().waitFor();
  const stormCard = await card.innerText();
  const stormNames = await card.locator("[data-plan] > span").allTextContents();
  check(stormNames.join("|") === "Cancel AR131 + AR132 + AR881 + AR882|Accept the reduced Lisbon rate" && !/CS-T/.test(await card.locator(".mp-card-head small").innerText()),
    `A storm's card names its plans and no aircraft: ${stormNames} / ${await card.locator(".mp-card-head small").innerText()}`);
  check(stormCard.includes("79 flights would change") && !stormCard.includes("not in today's schedule"), `The card should count the plan's flights: ${stormCard}`);
  const ring = page.locator("#map-rotation .mp-storm");
  check((await ring.count()) === 1 && (await page.locator(".mp-me").count()) === 0 && await page.locator("#map-rot").isHidden(),
    "A weather case should mark Lisbon without picking one aircraft or its rotation");
  // Owner ("strong crosswinds ... only one runway can be used"): Lisbon's runway 02/20 with the wind blowing across it from the
  // west, instead of a storm cell.
  const [runway] = await ring.locator(".mp-crosswind .mp-runway").evaluateAll(lines => lines.map(l => ["x1", "y1", "x2", "y2"].map(k => Number(l.getAttribute(k)))));
  const [wind] = await ring.locator(".mp-wind line").evaluateAll(lines => lines.map(l => ["x1", "y1", "x2", "y2"].map(k => Number(l.getAttribute(k)))));
  check(runway && (await ring.locator(".mp-wind polygon").count()) === 3 && (await ring.locator(".mp-storm-area").count()) === 0 &&
    runway[2] > runway[0] && runway[3] < runway[1] && Math.abs(runway[3] - runway[1]) > 2 * Math.abs(runway[2] - runway[0]) &&
    wind[2] > wind[0] && Math.abs(wind[3] - wind[1]) < Math.abs(wind[2] - wind[0]) / 2,
    `The crosswind should draw the runway (020°) and the wind across it from the west: ${JSON.stringify({ runway, wind })}`);
  // Owner ("when you click on the storm the planes should still somehow appear ... plane is a bit too small"): every aircraft
  // stays in view, those the plan moves in its colour, each about 14 px across.
  const shownAt = await page.locator("#map-slider").inputValue();
  await page.locator("#map-slider").evaluate(slider => { slider.value = "960"; slider.dispatchEvent(new Event("input", { bubbles: true })); });
  // Measured once the map has settled (a redraw may still be under way).
  await page.waitForFunction(() => [...document.querySelectorAll("#map-traffic .mp-ac path")].some(path => path.getBoundingClientRect().width > 0));
  await page.waitForTimeout(400);
  // Read and measured in one go: the map redraws its aircraft every frame while it moves.
  const planes = await page.evaluate(() => [...document.querySelectorAll("#map-traffic .mp-ac")].map(el => {
    const box = el.querySelector("path").getBoundingClientRect();
    return { plan: el.classList.contains("plan"), size: Math.max(box.width, box.height) };
  }));
  check(planes.length >= 10 && planes.some(p => p.plan) && planes.some(p => !p.plan) && planes.every(p => p.size >= 12),
    `The aircraft should stay in view in the weather case, the plan's in its colour, readable: ${JSON.stringify(planes.slice(0, 4))} of ${planes.length}`);
  await page.screenshot({ path: ".playwright-cli\\map-1440-weather-traffic.png" });
  await page.locator("#map-slider").evaluate((slider, value) => { slider.value = value; slider.dispatchEvent(new Event("input", { bubbles: true })); }, shownAt);
  await page.waitForTimeout(800);
  await ring.locator(".mp-storm-badge").hover();
  const ringTip = await page.locator("#tip").innerText();
  check(ringTip.includes("Strong crosswind 16:00–18:00") && ringTip.includes("Gusts 33 kt across runway 02/20") && ringTip.includes("Lisbon at 24 movements an hour (normally 40)"),
    `The mark should name the forecast, its gusts and the rate on hover: ${ringTip}`);
  await page.mouse.move(2, 2);
  check((await ring.locator(".mp-storm-tag").textContent()) === "16:00–18:00" &&
    (await card.locator(".mp-report span").innerText()) === "Weather · Lisbon · 16:00–18:00 · gusts 33 kt across runway 02/20 · 24 movements an hour (normally 40)" &&
    (await card.locator(".mp-report").getAttribute("title")).startsWith("TAF AMD LPPT"),
    "The ring should show the window and the card read the storm as the case page does, the TAF on hover");
  const legs = await page.locator("#map-rotation .mp-leg").evaluateAll(paths => paths.map(p => p.getAttribute("class")));
  check(legs.filter(c => c.includes("moved")).length >= 6 && legs.filter(c => c.includes("cancel")).length === 4 &&
    (await page.locator("#map-rotation .mp-lbl.cancel").count()) === 4 && (await page.locator("#map-rotation .mp-lbl:not(.cancel)").count()) === 0,
    `The plan's routes should be highlighted, each cancelled flight labelled: ${legs.length} legs`);
  // Cancelled flights' labels and the storm's mark keep clear of the airport names: LIS and MAD read in full.
  const clash = await page.evaluate(() => {
    const meets = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const names = [...document.querySelectorAll("#map-aps text")].map(el => [el.textContent, el.getBoundingClientRect()]);
    const marks = [...document.querySelectorAll("#map-rotation .mp-lbl, #map-rotation .mp-storm-badge, #map-rotation .mp-storm-tag")]
      .map(el => [el.dataset.flight || el.getAttribute("class"), el.getBoundingClientRect()]);
    return { names: names.map(([name]) => name), hits: marks.flatMap(([what, a]) => names.filter(([, b]) => meets(a, b)).map(([name]) => `${what} covers ${name}`)) };
  });
  check(clash.names.includes("LIS") && clash.names.includes("MAD") && !clash.hits.length, `Labels should keep clear of airport names: ${JSON.stringify(clash)}`);
  // Every route meets at Lisbon: its name is drawn again above them.
  check((await page.locator("#map-rotation > :last-child").evaluate(el => `${el.getAttribute("class")} ${el.textContent}`)) === "mp-storm-ap LIS",
    "Lisbon's name should read above the storm's routes");
  await page.screenshot({ path: ".playwright-cli\\map-1440-weather.png" });
  await professional("Map weather case");
  // Task 3b: a cancelled flight's label opens its card; a moved route lists the plan's flights on it; a held flight shows its hold.
  await page.locator('#map-rotation .mp-lbl[data-flight="AR881"]').click();
  await fc.filter({ hasText: "AR881" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("Cancelled") && fcText.includes("Lisbon") && fcText.includes("Faro") && fcText.includes("15:40")
    && fcText.includes("Plan 1 · Cancel AR131 + AR132 + AR881 + AR882") && fcText.includes("Open case"), `A storm's cancelled flight: ${fcText}`);
  await clickPath('#map-rotation .mp-spread-g[data-route="LIS-OPO"] .mp-hit');
  await fc.filter({ hasText: "Lisbon ⇄ Porto" }).waitFor({ timeout: 3000 });
  await page.screenshot({ path: ".playwright-cli\\map-1440-weather-route.png" });
  fcText = await fc.innerText();
  check(["AR840", "AR864", "AR951", "AR953", "AR952", "AR954"].every(no => fcText.includes(no)) && !fcText.includes("AR945") && fcText.includes("Plan 1"),
    `A storm's route lists the plan's flights on it: ${fcText}`);
  await fc.locator('[data-flight-open="AR864"]').click();
  await fc.filter({ hasText: "Held 6 min in the air" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes("Porto") && fcText.includes("Lisbon") && fcText.includes("16:40 → 16:59") && fcText.includes("17:35 → 18:00")
    && fcText.includes("Later by 19 min") && fcText.includes("CS-TJM") && fcText.includes("Plan 1 ·"), `A storm-held flight shows its hold: ${fcText}`);
  check((await fc.locator(".mp-fc-est").count()) === 0, "An arrival the case planned is not marked as estimated");
  check(!meets(await rect(fc), await rect(card)), "The flight card must not cover the storm's case card");
  await page.waitForTimeout(400);
  await page.screenshot({ path: ".playwright-cli\\map-1440-weather-flight.png" });
  await professional("Map storm flight card");
  // On a phone the storm's flight reads in the bottom sheet.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(900);
  const stormShown = await routeHidden("#map-rotation .mp-spread-g.sel .mp-spread");
  check(!stormShown, `At 390 px the storm flight's route shows between the case card and the sheet: ${stormShown}`);
  const stormSheet = await fc.evaluate(el => { const b = el.getBoundingClientRect(); return { bottom: b.bottom, top: b.top, h: innerHeight }; });
  check(Math.abs(stormSheet.bottom - stormSheet.h) <= 1 && (await rect(card)).y1 <= stormSheet.top + 1, `At 390 px the storm's flight is a bottom sheet: ${JSON.stringify(stormSheet)}`);
  await page.screenshot({ path: ".playwright-cli\\map-390-weather-flight.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(300);
  // When the case cannot be read, the arrival is timed from the timetable, marked so, and no hold is claimed.
  stormFails = true;
  rows.find(r => r.trace_id === S).refreshed = 1;
  await page.evaluate(() => loadCases());
  await fc.filter({ hasText: "17:35 → 17:54" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(!fcText.includes("Held") && fcText.includes("Later by 19 min") && (await fc.locator(".mp-fc-end.to .mp-fc-est").count()) === 1,
    `Without the case's plan the hold is not claimed and the arrival is marked estimated: ${fcText}`);
  stormFails = false;
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await card.isVisible(), "Esc closes the storm's flight card; the storm stays open");
  // Esc on a route card opened from the keyboard gives the focus back to its route.
  await page.locator('#map-rotation .mp-spread-g[data-route="LIS-OPO"]').focus();
  await page.keyboard.press("Enter");
  await fc.filter({ hasText: "Lisbon ⇄ Porto" }).waitFor({ timeout: 3000 });
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await page.evaluate(() => Boolean(document.activeElement?.matches('#map-rotation .mp-spread-g[data-route="LIS-OPO"]'))),
    "Esc on a storm route card gives the focus back to the route");
  await card.locator('[data-plan="2296eb1956be45f9"]').click();
  check((await card.innerText()).includes("Blocked") && (await page.locator("#map-rotation .mp-lbl.cancel").count()) === 0, "The delay plan cancels nothing");
  await page.keyboard.press("Escape");
  // Final review: a storm plan held until confirmed, or stood down, is neither carried out nor being carried out, and nothing it
  // would move is drawn as done.
  const stormPlans = maps[S].plans;
  const handledColour = await page.evaluate(() => MapDraw.TONES.handled);
  for (const [status, tag, line] of [["held", "Held until confirmed", "Held until confirmed"], ["stood_down", "Stood down", "Stood down · nothing changed"]]) {
    maps[S] = { ...maps[S], plans: [{ ...stormPlans[0], authorised_by: "occ", execution: { status, actions_done: status === "held" ? 2 : 4, actions_total: status === "held" ? 79 : 4 },
      executed: status === "stood_down" }, stormPlans[1]] };
    await stormItem.click();
    await card.filter({ hasText: line }).waitFor({ timeout: 5000 });
    const stateCard = await card.innerText();
    const rotation = await page.locator("#map-rotation").innerHTML();
    check((await card.locator('[data-plan="1c19c99447ae7da3"] small').innerText()).includes(tag) && !/Being carried out|Executed by ARROW|Now flying/.test(stateCard) &&
      !rotation.includes(handledColour), `A ${status} storm plan should read as such, nothing drawn as done: ${stateCard}`);
    await page.keyboard.press("Escape");
  }
  maps[S] = { ...maps[S], plans: stormPlans };

  // 12c. Task 3b: every value a flight card or preview inserts stays text: a city, an aircraft type and a connection carrying markup.
  const belem = network.airports.find(a => a.iata === "BEL"), tuk = network.aircraft.find(a => a.registration === "CS-TUK");
  belem.city = '<img src=x onerror="window.__xss=1">Belém';
  tuk.fleet_type = 'A330neo<svg onload="window.__xss=2">';
  bookings.AR259 = { flight: flights.find(f => f.flight_no === "AR259"), aircraft: null, booking: { status: "available", flight_no: "AR259", passenger_count: 3,
    seat_capacity: 298, held_seat_count: 0, available_seats: 295, connecting_passenger_count: 3,
    connections: [{ flight_no: '<img src=x onerror="window.__xss=3">', passenger_count: 3 }] } };
  await page.evaluate(() => mapView.reload());
  await page.locator("#map-find").fill("AR259");
  await page.locator("#map-find").press("Enter");
  await fc.filter({ hasText: "3 connecting" }).waitFor({ timeout: 3000 });
  fcText = await fc.innerText();
  check(fcText.includes('<img src=x onerror="window.__xss=1">Belém') && fcText.includes('A330neo<svg onload="window.__xss=2">'), `Inserted values read as text: ${fcText}`);
  await page.locator('#map-rotation .mp-lbl[data-flight="AR259"]').hover();
  await peek.waitFor({ timeout: 3000 });
  check((await peek.innerText()).includes('<img src=x onerror="window.__xss=1">Belém'), "The preview shows the city as text");
  const injected = await page.evaluate(() => ({ xss: window.__xss ?? null, nodes: document.querySelectorAll("#map img, #map svg[onload], #map-fc *[onerror]").length,
    title: document.querySelector("#map-fc [data-pax]")?.getAttribute("title") || "" }));
  check(injected.xss === null && injected.nodes === 0 && injected.title.includes('<img src=x onerror="window.__xss=3">'), `Nothing inserted runs as markup: ${JSON.stringify(injected)}`);
  await page.mouse.move(2, 2);
  delete belem.city; tuk.fleet_type = "A330neo"; delete bookings.AR259;
  await page.evaluate(() => mapView.reload());
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  check(await fc.isHidden() && await card.isHidden(), "Esc twice closes the flight and the aircraft");

  // 13. Back to the control tower.
  await page.locator("#map").getByText("← Control tower").click();
  check(await page.locator("#tower").isVisible(), "← Control tower should show the control tower");

  // 14. No ARROW sign-in yet: the sign-in service refuses the protected scripts (only the page, app.js,
  // styles.css and recovery_view.js are public). The page must offer the sign-in link, not hang.
  const pageErrorsBefore = errors.length;
  const protectedFile = /\/(land|map_draw|map_view|tower_view|trail_view|chat_view|rules_view)\.js$|\/(ui|tower|map)\.css$/;
  await page.route(protectedFile, route => route.fulfill({ status: 401, body: "" }));
  await page.route("**/api/session", route => route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"sign in"}' }));
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const signIn = page.locator("#notice a");
  await signIn.waitFor({ timeout: 8000 }).catch(() => {});
  check(await signIn.isVisible().catch(() => false), "Without an ARROW sign-in the page should show a sign-in link");
  check((await signIn.getAttribute("href").catch(() => "") || "").includes("/.auth/login/aad"), "The link should start the ARROW sign-in");
  errors.length = pageErrorsBefore;
  await page.unroute(protectedFile);
  await page.unroute("**/api/session");

  check(!urls.some(u => u.includes("atlas.microsoft.com")), "The map must not call Azure Maps");
  check(!errors.length, `Page errors: ${errors.join("; ")}`);
  return "Map: list without the schedule and Retry, control-tower order and tones, no marker without a station, blocked figures hidden, airborne count, now and planned time, quiet Play, Enter on a marker and Space on a leg label, 1440 and 390 px; focus with recommended plan (its label inside the button, four plans with what-ifs in rows of two, the orchestrator's reason marked AI, the report without quote marks, counts worded as the control tower), cancelled and delayed legs, absent flight listed, proposal line, out-of-service window, aircraft in flight at 14:30, Open case and Show on map round trip, a plan carried out read against the published timetable, tower Show on map to a blocked plan, map read error with Retry, after-midnight position, no-location card without moving, find a flight with booking, Esc, 390 px focus; a case closed by Reset closes its card; a crosswind forecast at Lisbon (its own icon, the runway with the wind across it and the window, its gusts and rate on hover, no single aircraft or rotation, every aircraft kept in view with the plan's in its colour and each about 14 px across, moved routes and cancelled flights highlighted, the card read as the case page); a flight card (Task 3b: route line, leg label and hover preview; cities, was → now times, status, aircraft, passengers, the plan that moves it and Open case; cancelled and storm-held flights; beside the case card at 1440 px, a bottom sheet at 390 px; Esc; inserted values escaped); back to the control tower, sign-in link instead of a hang without an ARROW session, no Azure Maps call";
}
