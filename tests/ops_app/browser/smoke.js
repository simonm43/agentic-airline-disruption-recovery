async (page) => {
  // A failure names the last check that passed, so a timeout says where it happened.
  let passed = "start";
  try {
  return await (async () => {
  // Operator app business flows (business UI and free-text spec §5, §8) against mocked /api routes; no live calls.
  const base = await page.evaluate(() => new URLSearchParams(location.search).get("base")) || "http://127.0.0.1:8768";
  const check = (condition, message) => { if (!condition) throw new Error(message); passed = message; };
  // Owner: a professional screen. No visible text or hover card says Dev or played, names a container app, uses the
  // old system names or "Names never", or shows a 12+ hex-digit id, a call or action reference (5a9d-02-…, act-2) or an
  // identity name (id-airrec-…), except inside an opened Interface section (.iface).
  const AMATEUR = [/\bDev\b/, /\bplayed\b/i, /ca-airrec-/, /Crew roster/, /Reservations/, /Airport slots/, /Names never/i, /[0-9a-f]{12,}/i,
    /\bact-\d/, /\b[0-9a-f]{4}-\d{2}-[a-z]/i, /id-airrec-/,
    // Owner: the storm's "Act now" is dropped, with the actions only it used.
    /\bAct now\b/i, /Wait and react/i, /hotel holds?\b/i, /slot swaps?\b/i, /earlier flights? (?:offered|to offer)/i, /\bCrew notice\b/i,
    // Owner (final review): no prompt version, model deployment, "fixed", MCP endpoint or storm on screen.
    /prompt v\d/i, /gpt-4/, /\bfixed\b/i, /\/mcp\b/, /\bstorm\b/i,
    // Owner ("code, not AI very unprofessional"): one discreet mark on AI-written text, no "Code" or "not AI" labels, no
    // label chains for an agent and no decorative diamonds.
    /code, not AI/i, /not by (?:the )?AI/i, /AI agent ·/, /AI assistant ·/, /◆/, /Agents read and recommend/];
  const professional = async (where) => {
    const { text, tips } = await page.evaluate(() => {
      const seen = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node.parentElement;
        if (!el || !node.textContent.trim() || el.closest(".iface, script, style")) continue;
        if (el.checkVisibility({ visibilityProperty: true })) seen.push(node.textContent);
      }
      const cards = [...document.querySelectorAll("template")].map(t => {
        const copy = t.content.cloneNode(true); copy.querySelectorAll(".iface").forEach(e => e.remove()); return copy.textContent;
      });
      const hover = [...document.querySelectorAll("[data-tip], [data-tip-title], [title]")]
        .map(e => [e.dataset.tip, e.dataset.tipTitle, e.getAttribute("title")].filter(Boolean).join(" "));
      return { text: seen.join(" "), tips: [...hover, ...cards].join(" ") };
    });
    for (const pattern of AMATEUR) {
      const hit = (text.match(pattern) || tips.match(pattern) || [])[0];
      check(!hit, `${where}: "${hit}" should not show (${pattern})`);
    }
  };
  // Owner ("still too much bloated text in one screen"): the words in view, counted as the approved prototype counted them
  // (the case summary prototype's common.js): every visible text in the viewport, a word being anything with a letter,
  // a digit or €. Since the steps became tabs (layout A) the Decision step's detail under the plan cards (the recommendation
  // in full, each plan by part) can follow them in view; skip leaves it out of the count.
  const visibleWords = (skip = "") => page.evaluate((skip) => {
    const words = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const el = node.parentElement;
      if (!el || !node.textContent.trim() || el.closest(`script, style, template, .visually-hidden${skip ? `, ${skip}` : ""}`)) continue;
      if (!el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const box = range.getBoundingClientRect();
      if (box.width === 0 || box.bottom <= 0 || box.top >= innerHeight || box.right <= 0 || box.left >= innerWidth) continue;
      words.push(...node.textContent.split(/\s+/).filter(word => /[\p{L}\p{N}€]/u.test(word)));
    }
    return { count: words.length, text: words.join(" ") };
  }, skip);
  // Nothing in the case summary spills out of it, and no two of its tiles, figures or trade-offs overlap.
  const bandOverlaps = () => page.evaluate(() => {
    const band = document.querySelector("#summary");
    if (!band || !band.checkVisibility()) return ["no summary"];
    const bad = [];
    const outer = band.getBoundingClientRect();
    const visible = (el) => el.checkVisibility() && !el.closest("template");
    for (const el of band.querySelectorAll("*")) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width && (r.right > outer.right + 1 || r.left < outer.left - 1)) bad.push(`${el.tagName}.${el.className} spills`);
    }
    for (const row of [band, ...band.querySelectorAll(".stats, .deltas, .t-todo, .why-h, .t-why, .dt, .stat")]) {
      const kids = [...row.children].filter(visible).map(kid => [kid, kid.getBoundingClientRect()]);
      kids.forEach(([ka, a], i) => kids.slice(i + 1).forEach(([kb, b]) => {
        if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) bad.push(`${ka.className || ka.tagName} over ${kb.className || kb.tagName}`);
      }));
    }
    return bad;
  });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const requests = [];
  let authStatus = 200, rulesStatus = 200, dayRows = null, rulesGate = null, casesStatus = 200, chatRefusal = null, networkDay = null;
  let traceTemplate = "https://portal.example/search/{trace}/view/{trace}";
  // The Foundry project link the session returns (GET /api/session foundry_portal_url); null when unset.
  const PORTAL = "https://ai.azure.com/nextgen/r/AAAAAAAAQACAAAAAAAAAEw,rg-airrec-dev-swc,,aif-airrec-chat-dev-4w7wuej5i2a5m,proj-airrec-chat-dev";
  let foundryPortal = PORTAL;

  const OCC = "00000000-0000-4000-8000-000000000010";
  const A = "a".repeat(32), B = "b".repeat(32), C = "c".repeat(32), D = "d".repeat(32), E = "e".repeat(32), G = "7".repeat(32);
  const H = "1".repeat(32), K = "2".repeat(32), N = "3".repeat(32), P = "5".repeat(32), Q = "6".repeat(32), W = "4".repeat(32);
  const now = Date.now();
  const iso = (ago) => new Date(now - ago * 1000).toISOString();
  const domains = (fdp, limit) => [
    { domain: "crew", constraint_status: "pass", duties: [{ duty_id: "D-LIS-101", status: "pass", modeled_fdp_minutes: fdp, modeled_fdp_limit_minutes: limit }] },
    { domain: "slots", constraint_status: "pass", capacity_fit: true },
    { domain: "booking", constraint_status: "pass" }
  ];
  const leg = (no, from, to, std, sta, status = "scheduled") =>
    ({ flight_no: no, origin: from, destination: to, std_local: `2026-11-10T${std}:00`, sta_local: `2026-11-10T${sta}:00`, aircraft_reg: "CS-TQA", status });
  // Case A mirrors a Dev run's case 3 figures; costs carry their workings (owner).
  const cancelPlan = {
    plan_id: "plan-cancel", option_id: "plan-cancel", aircraft_option_id: "cancel", kind: "cancel", title: "Cancel AR101 and AR102",
    flights: [leg("AR101", "LIS", "OPO", "10:00", "11:00", "cancelled"), leg("AR102", "OPO", "LIS", "11:40", "12:40", "cancelled")],
    domain_assessments: domains(410, 780), cost: { airline_eur: 26640, compensation_eur: 60450, care_eur: 5832, rebooking_eur: 7052,
      care: { meals_eur: 5832, hotel_eur: 0, taxi_eur: 0 }, overnight_passengers: 0, owed_compensation_passengers: 240, meals_passengers: 324,
      total_eur: 99974, low_eur: 99974, high_eur: 99974, basis: "indicative",
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [{ flight_no: "AR101", cancelled: true, aircraft: "narrow", eur: 13320 },
        { flight_no: "AR102", cancelled: true, aircraft: "narrow", eur: 13320 }] },
      compensation: [{ eur_each: 400, passengers: 30, why: "cancelled, rebooked · 1,500–3,500 km or within the EU" },
        { eur_each: 250, passengers: 150, why: "cancelled, rebooked · up to 1,500 km" },
        { eur_each: 200, passengers: 46, why: "cancelled, rebooked · 1,500–3,500 km or within the EU · halved: arrives within 3 h" },
        { eur_each: 125, passengers: 14, why: "cancelled, rebooked · up to 1,500 km · halved: arrives within 2 h" }],
      meals: [{ eur_each: 18, passengers: 324, why: "waits over 5 h, or cancelled or without an onward flight" }], overnight: null,
      rebooking: [{ eur_each: 150, passengers: 24, why: "rerouted on another airline · up to 1,500 km" },
        { eur_each: 13, passengers: 76, why: "rebooked on our own flights · 1,500–3,500 km or within the EU" },
        { eur_each: 11, passengers: 224, why: "rebooked on our own flights · up to 1,500 km" }] } },
    validation: { rebooking: { disrupted_passengers: 324, rebooked_passengers: 300, unplaced_passengers: 24 } },
    policy: { route: "occ", reasons: ["OCC decides", "cancellation", "24 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: false, recommendation_reason: null,
    // As the planner stores them: delay_messages is ARROW's fact from the plan's flights.
    levers: { aircraft_option_id: "cancel", reserve_option_ids: [], rebook: true, delay_messages: false, playbook_actions: ["THIN", "REBOOK"] },
    planned_actions: [{ label: "Ops control", summary: "Cancel AR101, AR102" }, { label: "Network manager", summary: "CNL AR101" }, { label: "Network manager", summary: "CNL AR102" }, { label: "Crew", summary: "Update crew duties" }, { label: "Passengers", summary: "Rebook 300 passengers" }]
  };
  // Cars keep 35 passengers on their connections (D108): from_flight_no, to_flight_no, passengers, minutes to connect.
  const transfers = [{ from_flight_no: "AR102", to_flight_no: "AR861", passengers: 12, minutes: 20, passport_control: true },
    { from_flight_no: "AR102", to_flight_no: "AR444", passengers: 9, minutes: 15, passport_control: false },
    { from_flight_no: "AR102", to_flight_no: "AR512", passengers: 6, minutes: 15, passport_control: false },
    { from_flight_no: "AR132", to_flight_no: "AR205", passengers: 5, minutes: 15, passport_control: false },
    { from_flight_no: "AR132", to_flight_no: "AR941", passengers: 3, minutes: 20, passport_control: true }];
  const delayPlan = {
    plan_id: "plan-delay", option_id: "plan-delay", aircraft_option_id: "delay", kind: "delay", title: "Fly the rotation late",
    flights: [leg("AR101", "LIS", "OPO", "13:45", "14:45"), leg("AR102", "OPO", "LIS", "15:25", "16:25"), leg("AR131", "LIS", "FAO", "17:05", "18:05"),
      leg("AR132", "FAO", "LIS", "18:40", "19:40")],
    domain_assessments: domains(440, 720), cost: { airline_eur: 13424, compensation_eur: 85050, care_eur: 12026, rebooking_eur: 6698,
      care: { meals_eur: 7514, hotel_eur: 3600, taxi_eur: 912 }, overnight_passengers: 36, owed_compensation_passengers: 331, meals_passengers: 481,
      total_eur: 117198, low_eur: 116286, high_eur: 118158, basis: "indicative",
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [{ flight_no: "AR101", delay_minutes: 195, eur: 3467 },
        { flight_no: "AR102", delay_minutes: 205, eur: 3645 }, { flight_no: "AR131", delay_minutes: 175, eur: 3112 }, { flight_no: "AR132", delay_minutes: 180, eur: 3200 }] },
      compensation: [{ eur_each: 400, passengers: 15, why: "arrives 3 h or more late · 1,500–3,500 km or within the EU" },
        { eur_each: 300, passengers: 1, why: "arrives 3 h or more late · over 3,500 km · halved: arrives within 4 h" },
        { eur_each: 250, passengers: 315, why: "arrives 3 h or more late · up to 1,500 km" }],
      meals: [{ eur_each: 7, passengers: 4, why: "waits past the care threshold, under 3 h" }, { eur_each: 10, passengers: 300, why: "waits 3–4 h" },
        { eur_each: 13, passengers: 100, why: "waits 4–5 h" }, { eur_each: 18, passengers: 77, why: "waits over 5 h, or cancelled or without an onward flight" }],
      overnight: { passengers: 36, rooms: 24, room_eur: 150, taxis: 48, taxi_eur: 19, meals_eur_each: 50 },
      rebooking: [{ eur_each: 168, passengers: 23, why: "rerouted on another airline · 1,500–3,500 km or within the EU" },
        { eur_each: 150, passengers: 16, why: "rerouted on another airline · up to 1,500 km" },
        { eur_each: 13, passengers: 8, why: "rebooked on our own flights · 1,500–3,500 km or within the EU" },
        { eur_each: 11, passengers: 30, why: "rebooked on our own flights · up to 1,500 km" }] } },
    validation: { rebooking: { disrupted_passengers: 77, rebooked_passengers: 38, unplaced_passengers: 39, transferred_passengers: 35, transfers } },
    policy: { route: "occ", reasons: ["OCC decides", "AR101 departs 225 min late: EU261 care threshold (2 h)", "AR102 departs 210 min late: EU261 care threshold (2 h)", "39 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: true, recommendation_reason: "Delaying the rotation protects more connections and disrupts fewer passengers than cancellation.",
    adjustment_minutes: 15,
    levers: { aircraft_option_id: "delay", reserve_option_ids: [], rebook: true, delay_messages: true, playbook_actions: ["RETIME", "REBOOK"] },
    planned_actions: [{ label: "Ops control", summary: "Retime AR101, AR102, AR131, AR132" }, { label: "Network manager", summary: "DLA AR101" }, { label: "Network manager", summary: "DLA AR102" },
      { label: "Network manager", summary: "DLA AR131" }, { label: "Network manager", summary: "DLA AR132" }, { label: "Airport CDM", summary: "TOBT AR101 LIS" },
      { label: "Airport CDM", summary: "TOBT AR131 LIS" }, { label: "Crew", summary: "Update crew duties" }, { label: "Passengers", summary: "Rebook 38 passengers" },
      { label: "Ground handling", summary: "Direct transfers for 35 passengers" }]
  };
  // The OCC playbook as GET /api/cases/{trace} gives each plan (playbook.checklist; the 11 actions of playbook-2026-11-22b): every
  // action that applies to the case, in catalogue order, with its desk, autonomy, status and facts (a line of labels and numbers,
  // or null); an action named that does not apply, refused.
  const PB = { RETIME: ["Retime flights, protect connections", "Aircraft control", "auto"],
    "GROUND-HOLD": ["Hold departures at origin, not in the air", "Aircraft control", "occ"], THIN: ["Cancel a round trip early, rebook", "Aircraft control", "occ"],
    "SWAP-AC": ["Swap aircraft", "Aircraft control", "occ"], STANDBY: ["Call standby crews", "Crew control", "auto"],
    REBOOK: ["Rebook on own or other airlines", "Hub and passengers", "auto"], CAR: ["Drive a short connection at Lisbon", "Hub and passengers", "auto"],
    "PAX-NOTICE": ["Tell passengers their new times", "Hub and passengers", "auto"],
    "FUEL-FLAG": ["Flag holding and extra-fuel risk for flights into the window", "Dispatch", "advice"],
    "ALT-FLAG": ["Screen alternates (Porto, Faro) against the same weather", "Dispatch", "advice"],
    "MX-ETR": ["Return-to-service estimate and deferral check", "Maintenance control", "advice"] };
  const pb = (rows) => rows.map(([id, status, facts = null, reason = null]) => ({ id, name: PB[id][0], desk: PB[id][1], autonomy: PB[id][2], status, reason, facts }));
  // Case 3's two plans: Maintenance's estimate is advice for both (the delay's says nothing yet); the cancel thins the round trip,
  // the delay retimes it. A fact that is not a line of text (the delay's cars) never shows its keys.
  const DEFECT_ETR = "CS-TQA back in service 14:30 (240 min estimate)";
  cancelPlan.playbook = pb([["RETIME", "not_needed"], ["THIN", "in_plan", "AR101, AR102 cancelled"], ["SWAP-AC", "not_needed"],
    ["STANDBY", "not_needed"], ["REBOOK", "in_plan", "38 passengers rebooked"], ["CAR", "not_needed"], ["MX-ETR", "advice", DEFECT_ETR]]);
  delayPlan.playbook = pb([["RETIME", "in_plan", "4 flights, up to +205 min"], ["THIN", "not_needed"], ["SWAP-AC", "not_needed"],
    ["STANDBY", "not_needed"], ["REBOOK", "in_plan"], ["CAR", "in_plan", { passengers: 35, connections: 5 }], ["MX-ETR", "advice"]]);
  // Case 4 of a Dev run, in shape: AR689 leaves 60 min late; delaying the rotation would take its crew over the
  // flight-time limit with no reserve, so ARROW offers two cancels: the first round trip, or the aircraft's last one.
  const narrowCancel = (no) => ({ flight_no: no, cancelled: true, aircraft: "narrow", eur: 13320 });
  const lateLine = (no, minutes) => ({ flight_no: no, delay_minutes: minutes, eur: Math.round(minutes * 1778) / 100 });
  const strandedMeals = (n) => ({ eur_each: 18, passengers: n, why: "waits over 5 h, or cancelled or without an onward flight" });
  const cancelPaid = (at400, at250, at125) => [{ eur_each: 400, passengers: at400, why: "cancelled, rebooked · 1,500–3,500 km or within the EU" },
    { eur_each: 250, passengers: at250, why: "cancelled, rebooked · up to 1,500 km" },
    { eur_each: 125, passengers: at125, why: "cancelled, rebooked · up to 1,500 km · halved: arrives within 2 h" }];
  const cancelLast = {
    plan_id: "plan-cancel-last", option_id: "cancel-last", aircraft_option_id: "cancel-last", kind: "cancel",
    title: "Fly the earlier flights late and cancel AR941 + AR942, the aircraft's last round trip",
    flights: [leg("AR689", "LIS", "FNC", "14:10", "16:00"), leg("AR690", "FNC", "LIS", "16:50", "18:40"),
      leg("AR941", "LIS", "MAD", "18:30", "20:45", "cancelled"), leg("AR942", "MAD", "LIS", "21:30", "21:50", "cancelled")],
    domain_assessments: domains(520, 780), cost: { airline_eur: 28774, compensation_eur: 41500, care_eur: 11064,
      care: { meals_eur: 5424, hotel_eur: 4500, taxi_eur: 1140 }, overnight_passengers: 48, owed_compensation_passengers: 168, total_eur: 81338, low_eur: 80438, high_eur: 82538, basis: "indicative",
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [lateLine("AR689", 60), lateLine("AR690", 60), narrowCancel("AR941"), narrowCancel("AR942")] },
        compensation: cancelPaid(20, 120, 28), meals: [strandedMeals(168)],
        overnight: { passengers: 48, rooms: 30, room_eur: 150, taxis: 60, taxi_eur: 19, meals_eur_each: 50 } } },
    validation: { rebooking: { disrupted_passengers: 168, rebooked_passengers: 150, unplaced_passengers: 18 } },
    policy: { route: "occ", reasons: ["OCC decides", "cancellation", "18 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: true,
    recommendation_reason: "Cancelling the last round trip ends the crew's day within their limit and disrupts fewer passengers than cancelling AR689 and AR690."
  };
  const cancelFirst = {
    plan_id: "plan-cancel-first", option_id: "cancel", aircraft_option_id: "cancel", kind: "cancel",
    title: "Cancel AR689 + AR690 and put the rest of the rotation back on time",
    flights: [leg("AR689", "LIS", "FNC", "13:10", "15:00", "cancelled"), leg("AR690", "FNC", "LIS", "15:50", "17:40", "cancelled"),
      leg("AR941", "LIS", "MAD", "18:30", "20:45"), leg("AR942", "MAD", "LIS", "21:30", "21:50")],
    domain_assessments: domains(300, 780), cost: { airline_eur: 26640, compensation_eur: 53250, care_eur: 3780,
      care: { meals_eur: 3780, hotel_eur: 0, taxi_eur: 0 }, overnight_passengers: 0, owed_compensation_passengers: 210, total_eur: 83670, low_eur: 83670, high_eur: 83670, basis: "indicative",
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [narrowCancel("AR689"), narrowCancel("AR690")] },
        compensation: cancelPaid(30, 150, 30), meals: [strandedMeals(210)], overnight: null } },
    validation: { rebooking: { disrupted_passengers: 210, rebooked_passengers: 196, unplaced_passengers: 14 } },
    policy: { route: "occ", reasons: ["OCC decides", "cancellation", "14 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: false, recommendation_reason: null
  };
  const delayOverLimit = {
    plan_id: "plan-delay-crew", option_id: "delay", aircraft_option_id: "delay", kind: "delay", title: "Fly the rotation late",
    flights: [leg("AR689", "LIS", "FNC", "14:10", "16:00"), leg("AR690", "FNC", "LIS", "16:50", "18:40"), leg("AR941", "LIS", "MAD", "19:30", "21:45"),
      leg("AR942", "MAD", "LIS", "22:30", "22:50")],
    domain_assessments: [{ domain: "crew", constraint_status: "fail", duties: [{ duty_id: "D-LIS-131", status: "fail", modeled_fdp_minutes: 805, modeled_fdp_limit_minutes: 780 }] },
      { domain: "slots", constraint_status: "pass", capacity_fit: true }, { domain: "booking", constraint_status: "pass" }],
    cost: { airline_eur: 5266, compensation_eur: 0, care_eur: 0, care: { meals_eur: 0, hotel_eur: 0, taxi_eur: 0 }, overnight_passengers: 0, owed_compensation_passengers: 0, total_eur: 5266,
      low_eur: 5266, high_eur: 5266, basis: "indicative",
      // D116: each late leg flies faster, so the delay shrinks down the rotation and the fuel is priced, with the CO2
      // allowances inside the EU ETS (not to or from Madeira until 2030), of how many minutes it could and why.
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [lateLine("AR689", 60), lateLine("AR690", 57), lateLine("AR941", 54), lateLine("AR942", 50)],
        fuel_eur_per_minute: { narrow: 85.4, wide: 158.6 }, co2_eur_per_minute: { narrow: 17.64, wide: 32.76 },
        fuel: [["AR689", 3, 0], ["AR690", 3, 0], ["AR941", 4, 70.56], ["AR942", 4, 70.56]].map(([no, minutes, co2]) => ({ flight_no: no, made_up_minutes: minutes,
          made_up_possible: minutes, reason: "worth more than its fuel at 30–60 min late", aircraft: "narrow", fuel_eur: Math.round(minutes * 8540) / 100, co2_eur: co2,
          eur: Math.round(minutes * 8540 + co2 * 100) / 100 })),
        // As in cost.py: the speed-up in one line, each flight's minutes named, and why no more.
        fuel_summary: "Fly faster: AR689 3 min, AR690 3 min, AR941 4 min, AR942 4 min (14 min) · fuel €1,196 + CO2 €141 = €1,337",
        fuel_basis: "3.5% of each flight's time in the air, where the minutes are worth their fuel. An A320 cruises at Mach 0.78 and may not exceed 0.82: about 5% faster, in cruise only." },
        compensation: [], meals: [], overnight: null } },
    validation: { rebooking: { disrupted_passengers: 41, rebooked_passengers: 41, unplaced_passengers: 0 } },
    policy: { route: "blocked", reasons: ["flight-time limit exceeded with no reserve crew"] },
    requires_duty_manager: false, recommended: false, recommendation_reason: null
  };
  // What ARROW did once OCC approved the delay, as the executor records it: kind, payload, expected and the system's message.
  const iso10 = (time) => `2026-11-10T${time}:00`;
  const retimes = [["AR101", "10:30", "13:45"], ["AR102", "12:00", "15:25"], ["AR131", "14:10", "17:05"], ["AR132", "15:40", "18:40"]];
  const executed = (transferStatus = "confirmed") => [
    { action_id: "x-01", system: "schedule", kind: "retime_flights", label: "Ops control", summary: "Retime AR101, AR102, AR131, AR132", status: "confirmed",
      message: "Retimed AR101 13:45, AR102 15:25, AR131 17:05, AR132 18:40; MVT sent",
      payload: { flights: retimes.map(([no, , now]) => ({ flight_no: no, std_local: iso10(now), sta_local: iso10(now) })) },
      expected: { flights: Object.fromEntries(retimes.map(([no, was]) => [no, { std_local: iso10(was), status: "scheduled" }])) } },
    ...retimes.map(([no, , now], i) => ({ action_id: `x-0${i + 2}`, system: "slots", kind: "network_manager.fpl_delay", label: "Network manager",
      summary: `DLA ${no}`, status: "confirmed", message: `DLA ${no} EOBT ${now}`, payload: { flight_no: no, eobt: iso10(now) } })),
    ...[["AR101", "13:45"], ["AR131", "17:05"]].map(([no, now], i) => ({ action_id: `x-0${i + 6}`, system: "slots", kind: "airport_cdm.tobt_update",
      label: "Airport CDM", summary: `TOBT ${no} LIS`, status: "confirmed", message: `TOBT ${no} ${now} at LIS`, payload: { flight_no: no, tobt: iso10(now), airport: "LIS" } })),
    { action_id: "x-08", system: "crew", kind: "update_duties", label: "Crew", summary: "Update crew duties", status: "confirmed",
      message: "Crew duties: D-LIS-101 release 20:10; D-LIS-117 release 21:05" },
    { action_id: "x-09", system: "booking", kind: "rebook_passengers", label: "Passengers", summary: "Rebook 38 passengers", status: "confirmed",
      message: "Rebooked 38 passengers in 21 bookings" },
    { action_id: "x-10", system: "booking", kind: "ground_handling.direct_transfer", label: "Ground handling", summary: "Direct transfers for 35 passengers",
      status: transferStatus, message: "Direct transfers requested: 35 passengers, 5 connections from AR102, AR132", payload: { transfers } }
  ];
  // Case 1 in a newer run (native dispatch spec §3): each action went to its recipient's own interface; the executor keeps every
  // call it sent with what it said, what was sent and the reply.
  const t21 = (time) => `2026-11-21T${time}:00`;
  const sentCall = (call_id, recipient, played_by, method, path, says, sent, message, at) =>
    ({ call_id, recipient, played_by, method, path, says, sent, status: "confirmed", message, at: `2026-11-21T${at}+00:00` });
  const DLA = "(DLA-AR259-LPPT1050-LPPR-DOF/261121)";
  const case1 = [
    { action_id: "5a9d-01-retime-flights", system: "schedule", kind: "retime_flights", label: "Ops control", summary: "Retime AR259",
      status: "confirmed", message: "AR259 departs 10:50, arrives 11:45, CS-TUK",
      payload: { flights: [{ flight_no: "AR259", std_local: t21("10:50"), sta_local: t21("11:45") }] },
      expected: { flights: { AR259: { std_local: t21("10:30"), sta_local: t21("11:25"), status: "scheduled" } } },
      calls: [sentCall("5a9d-01-retime-flights-ar259", "Operations control", "Schedule", "PATCH", "/api/flights/AR259", "AR259 departs 10:50 (was 10:30)",
        { std_local: t21("10:50"), sta_local: t21("11:45") }, "AR259 departs 10:50, arrives 11:45, CS-TUK", "09:47:01")] },
    { action_id: "5a9d-02-network-manager-fpl-delay", system: "slots", kind: "network_manager.fpl_delay", label: "Network manager", summary: "DLA AR259",
      status: "confirmed", message: "ACK", payload: { flight_no: "AR259", eobt: t21("10:50") },
      calls: [sentCall("5a9d-02-network-manager-fpl-delay", "EUROCONTROL Network Manager", "Slots", "POST", "/nm/messages",
        "Delay message AR259, off-block 10:50", { text: DLA }, "ACK", "09:47:02")] },
    { action_id: "5a9d-03-airport-cdm-tobt-update", system: "slots", kind: "airport_cdm.tobt_update", label: "Airport CDM", summary: "TOBT AR259 LIS",
      status: "confirmed", message: "accepted", payload: { flight_no: "AR259", tobt: t21("10:50"), airport: "LIS" },
      calls: [sentCall("5a9d-03-airport-cdm-tobt-update", "Lisbon airport (A-CDM)", "Slots", "POST", "/acdm/flights/AR259/tobt",
        "AR259 ready 10:50 at Lisbon", { airport: "LIS", tobt: t21("10:50") }, "accepted", "09:47:03")] },
    { action_id: "5a9d-04-update-duties", system: "crew", kind: "update_duties", label: "Crew", summary: "Update crew duties", status: "confirmed",
      message: "Crew duties: D-LIS-217 release 19:05",
      calls: [sentCall("5a9d-04-update-duties", "Crew system", "Crew", "POST", "/api/duty-changes", "Update crew duties for AR259",
        { flights: [{ flight_no: "AR259", std_local: t21("10:50"), sta_local: t21("11:45"), status: "scheduled", aircraft_reg: "CS-TUK" }],
          originals: [{ flight_no: "AR259", std_local: t21("10:30") }] }, "Crew duties: D-LIS-217 release 19:05", "09:47:04")] }
  ];
  // Final review: a retime is one update per flight, so it can stop part-way. After OCC approved the delay, operations
  // control moved AR101, then refused AR102 (the flight had changed since ARROW checked it); AR131 and AR132 were not sent and
  // nothing that depends on the retime started.
  const partlyRefused = [
    { action_id: "6b1e-01-retime-flights", system: "schedule", kind: "retime_flights", label: "Ops control", summary: "Retime AR101, AR102, AR131, AR132",
      status: "refused", message: "AR101 moved; AR102 refused: Source changed: flight AR102; AR131 and AR132 not sent",
      payload: { flights: retimes.map(([no, , now]) => ({ flight_no: no, std_local: iso10(now), sta_local: iso10(now) })) },
      expected: { flights: Object.fromEntries(retimes.map(([no, was]) => [no, { std_local: iso10(was), status: "scheduled" }])) },
      calls: [sentCall("6b1e-01-retime-flights-ar101", "Operations control", "Schedule", "PATCH", "/api/flights/AR101", "AR101 departs 13:45 (was 10:30)",
        { std_local: iso10("13:45"), sta_local: iso10("14:45") }, "AR101 departs 13:45, arrives 14:45, CS-TQA", "10:02:01"),
      { ...sentCall("6b1e-01-retime-flights-ar102", "Operations control", "Schedule", "PATCH", "/api/flights/AR102", "AR102 departs 15:25 (was 12:00)",
        { std_local: iso10("15:25"), sta_local: iso10("16:25") }, "Source changed: flight AR102", "10:02:02"), status: "refused" }] },
    ...retimes.map(([no, , now], i) => ({ action_id: `6b1e-0${i + 2}-network-manager-fpl-delay`, system: "slots", kind: "network_manager.fpl_delay",
      label: "Network manager", summary: `DLA ${no}`, status: "blocked", message: "Not sent: Ops control did not complete", payload: { flight_no: no, eobt: iso10(now) } })),
    { action_id: "6b1e-06-update-duties", system: "crew", kind: "update_duties", label: "Crew", summary: "Update crew duties", status: "blocked",
      message: "Not sent: Ops control did not complete" },
    { action_id: "6b1e-07-rebook-passengers", system: "booking", kind: "rebook_passengers", label: "Passengers", summary: "Rebook 38 passengers",
      status: "blocked", message: "Not sent: Ops control did not complete" },
    { action_id: "6b1e-08-ground-handling-direct-transfer", system: "booking", kind: "ground_handling.direct_transfer", label: "Ground handling",
      summary: "Direct transfers for 35 passengers", status: "blocked", message: "Not sent: Ops control did not complete", payload: { transfers } }
  ];
  // A what-if plan as ARROW's what_if tool stores it (tools.py): priced and routed by code, never recommended, after the
  // orchestrator's plans, with the question that asked for it. Its figures follow the prototype's Plan 3.
  const whatIfPlan = { ...delayPlan, plan_id: "plan-whatif-30", option_id: "plan-whatif-30", title: "Delay the rotation, 30 min more",
    flights: [leg("AR101", "LIS", "OPO", "14:15", "15:15"), leg("AR102", "OPO", "LIS", "15:55", "16:55"), leg("AR131", "LIS", "FAO", "17:35", "18:35"),
      leg("AR132", "FAO", "LIS", "19:10", "20:10")],
    cost: { airline_eur: 15558, compensation_eur: 86850, care_eur: 13892, rebooking_eur: 7938, care: { meals_eur: 8642, hotel_eur: 4200, taxi_eur: 1050 },
      overnight_passengers: 42, owed_compensation_passengers: 352, meals_passengers: 520, total_eur: 124238, low_eur: 123238, high_eur: 125338, basis: "indicative" },
    validation: { rebooking: { disrupted_passengers: 96, rebooked_passengers: 50, unplaced_passengers: 46 } },
    policy: { route: "occ", reasons: ["OCC decides", "AR101 departs 255 min late: EU261 care threshold (2 h)", "AR102 departs 235 min late: EU261 care threshold (2 h)",
      "46 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: false, recommendation_reason: null, adjustment_minutes: 45,
    levers: { aircraft_option_id: "delay", reserve_option_id: null, rebook: true, delay_messages: true, extra_delay_minutes: 30 },
    what_if: { question: "What if maintenance needs 30 more minutes?", asked_by: { oid: OCC, name: "Ana Costa" }, asked_at: "2026-11-10T11:40:00Z", entry_id: "w3" } };
  // The conversation as chat.py keeps it on the case: who asked and when, the answer, the calls behind it (each by its
  // endpoint, in words), what-if plans added. w1 was kept before the calls were worded: its arguments are JSON.
  const readCase = { kind: "tool", name: "read_case", title: "Read the case", access: "read", endpoint: "MCP read_case", detail: "DSR-20261110-444444" };
  const keptBefore = { kind: "tool", name: "read_case", title: "Read the case", access: "read", detail: '{"case_id": "DSR-20261110-444444"}' };
  const chatTurn = (id, at, name, question, extra = {}) => ({ id, asked_at: `2026-11-10T${at}Z`, asked_by: { oid: OCC, name }, question, status: "answered",
    answer: null, refused: false, answered_at: null, calls: [], what_if_plan_ids: [], response_id: `resp_${id}`, error: null, ...extra });
  const XSS = '<img src=x onerror="window.__chatXss=1"><b>Is AR131 still on time?</b>';
  const chatTurns = () => [
    chatTurn("w1", "10:12:00", "Rui Matos", "Why is the more expensive plan recommended?", { answered_at: "2026-11-10T10:12:03Z", calls: [keptBefore],
      answer: "Plan 2, Delay the rotation, disrupts **77** passengers; Plan 1, Cancel AR101 + AR102, disrupts **324**. ARROW's objectives put fewest passengers disrupted before lowest cost.\n\n" +
        "The delay costs **€17,224 more**: EU261 compensation +€24,600, passenger care +€6,194 (36 passengers in hotels), rebooking −€354, airline −€13,216." }),
    chatTurn("w2", "11:31:00", "Ana Costa", "Does the delay keep the crew legal on AR131–AR132?", { answered_at: "2026-11-10T11:31:14Z",
      calls: [readCase, { kind: "agent", name: "crew-recovery-agent", title: "Asked Crew", access: "read", endpoint: "A2A a2a-crew",
        detail: "Are the delay plan's duties within limits? → No duty breaks its limit." }],
      answer: "**Yes.** The Crew Recovery Agent checked the delayed duties just now: no duty goes over its flight-time limit, so Plan 2 needs no reserve crew for AR131 and AR132.\n\n" +
        "Code checks the same limit again before OCC can approve." }),
    chatTurn("w3", "11:40:00", "Ana Costa", "What if maintenance needs 30 more minutes?", { answered_at: "2026-11-10T11:40:19Z", what_if_plan_ids: ["plan-whatif-30"],
      calls: [readCase, { kind: "agent", name: "aircraft-recovery-agent", title: "Asked Aircraft", access: "read", endpoint: "A2A a2a-aircraft",
        detail: "Options if CS-TQA is free 30 min later? → Delay the rotation with 30 min more." },
        { kind: "tool", name: "what_if", title: "Asked ARROW for a what-if plan", access: "adds a plan", endpoint: "MCP what_if",
          detail: "DSR-20261110-444444 · aircraft_option_id delay · rebook yes · delay_messages yes · extra_delay_minutes 30 → Plan 3 · OCC decides" }],
      answer: "I asked ARROW to build it as Plan 3, a what-if. With 30 more minutes AR101 leaves 4 h 15 min late.\n\n" +
        "It disrupts **96** passengers (19 more than Plan 2) and costs **€124,238** (€7,040 more). Like Plan 2 it needs OCC." }),
    chatTurn("w4", "11:42:00", "Ana Costa", "Approve Plan 2 for me", { answered_at: "2026-11-10T11:42:01Z", refused: true,
      answer: "I can't approve, reject or carry out a plan. Approval stays with you: use **Approve this plan** on Plan 2, and ARROW asks you to confirm what it will change." }),
    chatTurn("w5", "11:45:00", "Ana Costa", XSS, { status: "failed", error: "No answer: the Case Assistant did not answer in time; ask again" }),
    chatTurn("w6", "11:50:00", "Ana Costa", "Which connections does Plan 2 still miss?", { status: "pending", response_id: null })
  ];
  const docs = {
    [A]: { trace_id: A, display_id: "DSR-20261110-AAAAAA", status: "assessment_ready", version: 7, created_at: iso(300), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC decides", summary: "CS-TQA grounded about 4 h; AR101 and AR102 at risk.",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA during the walk-around. Maintenance expects it out of service for about 4 hours.",
        description: "Hydraulic leak found on CS-TQA during the walk-around.", reading: "CS-TQA is out of service for 240 minutes.",
        reported_at: "2026-11-10T09:45:00", event_id: "EV-9C2D41",
        knock_on: { flights: [{ flight_no: "AR101", from: "LIS", to: "OPO", std_local: "2026-11-10T10:30:00", new_std_local: "2026-11-10T13:45:00", minutes_late: 195 },
          { flight_no: "AR102", from: "OPO", to: "LIS", std_local: "2026-11-10T12:00:00", new_std_local: "2026-11-10T15:25:00", minutes_late: 205 },
          { flight_no: "AR131", from: "LIS", to: "FAO", std_local: "2026-11-10T14:10:00", new_std_local: "2026-11-10T17:05:00", minutes_late: 175 },
          { flight_no: "AR132", from: "FAO", to: "LIS", std_local: "2026-11-10T15:40:00", new_std_local: "2026-11-10T18:40:00", minutes_late: 180 }],
          flights_affected: 4, max_minutes_late: 205, aircraft: ["CS-TQA"] } },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 }, options: [cancelPlan, delayPlan],
      // The same defect reported again: ARROW added it here instead of recording it twice.
      events: [{ event: "repeat_report", at: iso(240), actor: "arrow-intake", autonomy: "system", repeat: "DSR-20261110-FFFFFF",
        message: "Also reported by Maintenance (DSR-20261110-FFFFFF) · same disruption" }] },
    [B]: { trace_id: B, display_id: "DSR-20261110-BBBBBB", status: "assessment_ready", version: 9, created_at: iso(900), operating_date: "2026-11-10",
      source: { kind: "event", system: "Ground handling", event_id: "e1" }, stage: "Within every domain's limits",
      disruption: { type: "flight_delay", cause: "border_control", flight_no: "AR259", delay_minutes: 20, scope: { flights: ["AR259"], aircraft_reg: "CS-TUK" },
        message: "Border-control queues (Entry/Exit System checks) are holding passengers for AR259: it will leave about 20 minutes late.", reported_at: "2026-11-10T09:45:00" },
      options: [{ ...delayPlan, plan_id: "auto-1", option_id: "auto-1", flights: [leg("AR259", "LIS", "LHR", "11:30", "14:10")],
        cost: { total_eur: 900 }, validation: { rebooking: { disrupted_passengers: 0, rebooked_passengers: 0, unplaced_passengers: 0 } },
        policy: { route: "auto", reasons: ["within every domain's limits; OCC is informed"] }, requires_duty_manager: false,
        recommendation_reason: "Legal, protects every connection and costs least." }],
      execution: { route: "auto", approved_by: "policy", status: "completed", option_id: "auto-1",
        actions: [{ label: "Ops control", status: "confirmed", message: "Retimed AR259 11:30; MVT sent" },
          { label: "Network manager", status: "confirmed", message: "DLA AR259 EOBT 11:30" }] }, events: [] },
    [C]: { trace_id: C, display_id: "DSR-20261110-CCCCCC", status: "assessment_ready", version: 5, created_at: iso(600), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "Manual intervention",
      disruption: { type: "flight_delay", cause: "late_inbound", flight_no: "AR689", delay_minutes: 60, scope: { flights: ["AR689"], aircraft_reg: "CS-TKW" },
        message: "Late inbound aircraft: AR689 will leave 60 minutes late.", reported_at: "2026-11-10T09:45:00" },
      manual_intervention: { reason: "flight-time limit exceeded with no reserve crew: manual intervention",
        message: "Manual intervention: flight-time limit exceeded and no reserve crew. AR689 cannot operate as planned; crew control finds crew, or OCC cancels outside ARROW." },
      // Priced before workings were recorded: the parts and basis only.
      options: [{ ...delayPlan, plan_id: "blocked-1", option_id: "blocked-1", recommended: false, requires_duty_manager: false,
        cost: { ...delayPlan.cost, workings: undefined, rebooking_eur: undefined, owed_compensation_passengers: undefined, meals_passengers: undefined,
          total_eur: 110500, low_eur: 109588, high_eur: 111460 },
        policy: { route: "blocked", reasons: ["flight-time limit exceeded with no reserve crew: manual intervention"] } }], events: [] },
    // A Dev run: the orchestrator recommended the cancel, which disrupts more passengers; OCC approved the delay and the
    // ground handler has not yet confirmed the cars. Not listed in Today; opened by its link.
    [G]: { trace_id: G, display_id: "DSR-20261110-777777", status: "assessment_ready", version: 4, created_at: iso(1500), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC approved",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA. Maintenance expects it out of service for about 4 hours.", reported_at: "2026-11-10T09:45:00" },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 },
      options: [{ ...cancelPlan, recommended: true, recommendation_reason: "Cancelling costs less and leaves fewer overnight passengers than delaying the rotation." },
        { ...delayPlan, recommended: false, recommendation_reason: null }],
      execution: { route: "occ", approved_by: OCC, status: "pending_external", option_id: "plan-delay",
        actions: [...executed("pending"), { action_id: "x-11", system: "partner", kind: "partner.schedule_change", label: "Codeshare partner",
          summary: "Schedule change to the partner", status: "confirmed", message: "Schedule change sent to the partner" }],
        // Passengers without a seat today wait for Passenger Services from the start of the execution (review).
        reviews: [{ kind: "passenger_services", status: "awaiting_expert", passengers: 39 }] }, events: [] },
    // Owner: the recommendation on top, in its other shapes; opened by their links. H: the orchestrator recommends the
    // cheaper cancel, which disrupts 247 more passengers, and OCC has not decided. K: three plans, the delay blocked by the
    // crew's flight-time limit. N: the orchestrator recommended nothing.
    [H]: { trace_id: H, display_id: "DSR-20261110-111111", status: "assessment_ready", version: 3, created_at: iso(1600), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC decides",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA. Maintenance expects it out of service for about 4 hours.", reported_at: "2026-11-10T09:45:00" },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 },
      options: [{ ...cancelPlan, recommended: true, recommendation_reason: "Cancelling costs less and leaves fewer overnight passengers than delaying the rotation." },
        { ...delayPlan, recommended: false, recommendation_reason: null }], events: [] },
    [K]: { trace_id: K, display_id: "DSR-20261110-222222", status: "assessment_ready", version: 6, created_at: iso(1700), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC decides",
      disruption: { type: "flight_delay", cause: "late_inbound", flight_no: "AR689", delay_minutes: 60, scope: { flights: ["AR689"], aircraft_reg: "CS-TKW" },
        message: "Late inbound aircraft: AR689 will leave 60 minutes late.", reported_at: "2026-11-10T09:45:00" },
      options: [cancelLast, cancelFirst, delayOverLimit], events: [] },
    [N]: { trace_id: N, display_id: "DSR-20261110-333333", status: "assessment_ready", version: 2, created_at: iso(1800), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC decides",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA. Maintenance expects it out of service for about 4 hours.", reported_at: "2026-11-10T09:45:00" },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 },
      options: [{ ...cancelPlan, recommended: false }, { ...delayPlan, recommended: false, recommendation_reason: null,
        policy: { ...delayPlan.policy, reasons: [...delayPlan.policy.reasons, "standby crew report time not established", "standby crews changed since the plan: OCC decides", "standby crews left today not established"] } }], events: [] },
    [P]: { trace_id: P, display_id: "DSR-20261121-555555", status: "assessment_ready", version: 6, created_at: iso(2000), operating_date: "2026-11-21",
      source: { kind: "event", system: "Ground handling", event_id: "e5" }, stage: "Within every domain's limits",
      disruption: { type: "flight_delay", cause: "border_control", flight_no: "AR259", delay_minutes: 20, scope: { flights: ["AR259"], aircraft_reg: "CS-TUK" },
        message: "Border-control queues (Entry/Exit System checks) are holding passengers for AR259: it will leave about 20 minutes late.", reported_at: t21("09:45"), event_id: "EV-55D1C2" },
      options: [{ ...delayPlan, plan_id: "case1-delay", option_id: "case1-delay",
        flights: [{ flight_no: "AR259", origin: "LIS", destination: "LHR", std_local: t21("10:50"), sta_local: t21("13:30"), aircraft_reg: "CS-TUK", status: "scheduled" }],
        cost: { total_eur: 640 }, validation: { rebooking: { disrupted_passengers: 0, rebooked_passengers: 0, unplaced_passengers: 0 } },
        policy: { route: "auto", reasons: ["within every domain's limits; OCC is informed"] }, requires_duty_manager: false,
        recommendation_reason: "Legal, keeps every connection and costs least.",
        playbook: pb([["RETIME", "in_plan"], ["THIN", "not_needed"], ["SWAP-AC", "not_needed"], ["STANDBY", "not_needed"],
          ["REBOOK", "not_needed"], ["CAR", "not_needed"]]) }],
      execution: { route: "auto", approved_by: "policy", status: "completed", option_id: "case1-delay", actions: case1 },
      events: [{ event: "auto_execution_authorised", at: iso(1990), actor: "recovery-executor" },
        // Written by an earlier version: the older wording still reads as today's.
        { event: "repeat_report", at: iso(1500), actor: "arrow-intake", autonomy: "system", repeat: "DSR-20261121-5A9D00",
          message: "Reported again by Ground handling (DSR-20261121-5A9D00): the same disruption; nothing was recorded twice." }] },
    // Opened by its link: Case 3 approved by OCC and stopped part-way (partlyRefused).
    [Q]: { trace_id: Q, display_id: "DSR-20261110-666666", status: "assessment_ready", version: 8, created_at: iso(2100), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC approved",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA. Maintenance expects it out of service for about 4 hours.", reported_at: "2026-11-10T09:45:00" },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 },
      options: [{ ...cancelPlan, recommended: false }, { ...delayPlan, recommended: true }],
      execution: { route: "occ", approved_by: OCC, status: "needs_attention", option_id: "plan-delay", actions: partlyRefused },
      events: [{ event: "duty_manager_decision", at: iso(2060), option_id: "plan-delay", decision: "approve", case_version: 7 },
        { event: "execution_needs_attention", at: iso(2050), actor: "recovery-executor", message: "OCC attention: Ops control partly carried out " +
          `(${partlyRefused[0].message}). Dependent actions were not sent; what was done stays in place.` }] },
    // Case chat (case chat spec §5, case chat prototype): Case 3 waiting for OCC with six kept turns,
    // a what-if plan added in chat, a refusal, a question in HTML and an answer still pending; opened by its link.
    [W]: { trace_id: W, display_id: "DSR-20261110-444444", status: "assessment_ready", version: 9, created_at: iso(2200), operating_date: "2026-11-10",
      source: { kind: "ui" }, stage: "OCC decides",
      disruption: { type: "aircraft_defect", cause: "aircraft_defect", scope: { flights: ["AR101", "AR102"], aircraft_reg: "CS-TQA" },
        message: "Hydraulic leak found on CS-TQA during the walk-around. Maintenance expects it out of service for about 4 hours.", reported_at: "2026-11-10T09:45:00" },
      defect: { aircraft_reg: "CS-TQA", unavailable_minutes: 240 },
      options: [cancelPlan, delayPlan, whatIfPlan], chat: chatTurns(), events: [] }
  };
  // Control tower keys as GET /api/cases returns them (trail.case_card): urgency, first departure, impact, lane.
  const at = (time) => time && `2026-11-10T${time}:00`;
  const fl = (no, std, planned, status = "scheduled") => ({ flight_no: no, std_local: at(std), planned_local: at(planned), status });
  const impact = (passengers, rebooked, cost, plans, title, route, done = 0, total = 0, review = 0, by = null) =>
    ({ passengers, rebooked, review_passengers: review, cost_eur: cost, plans, plan_title: title, route, authorised_by: by, actions_done: done, actions_total: total });
  const card = (flights, impactOf, urgency, unavailable = null) => ({ urgency, impact: impactOf,
    first_departure: flights[0] ? { flight_no: flights[0].flight_no, std_local: flights[0].std_local, planned_local: flights[0].planned_local } : null,
    lane: { flights, unavailable } });
  const list = () => [
    { trace_id: A, display_id: "DSR-20261110-AAAAAA", created_at: docs[A].created_at, status: docs[A].status,
      headline: "CS-TQA · out of service 4 h", source: "OCC",
      ...(docs[A].execution
        ? docs[A].execution.reviews?.[0]?.status === "awaiting_expert"
          ? { state: "needs", label: "Passenger Services review", ...card([fl("AR101", "10:30", "13:45"), fl("AR102", "12:00", "15:25"), fl("AR131", "14:10", "17:05")],
            impact(77, 38, 117198, 2, "Fly the rotation late", "occ", 10, 10, 39, "occ"), 3, { from: at("09:45"), until: at("13:45") }) }
          : { state: "handled", label: "Approved and carried out", ...card([fl("AR101", "10:30", "13:45"), fl("AR102", "12:00", "15:25"), fl("AR131", "14:10", "17:05")],
            impact(77, 38, 117198, 2, "Fly the rotation late", "occ", 10, 10, 0, "occ"), 5, { from: at("09:45"), until: at("13:45") }) }
        : { state: "needs", label: "Needs your decision", ...card([fl("AR101", "10:30", "13:45"), fl("AR102", "12:00", "15:25"), fl("AR131", "14:10", "17:05")],
            impact(77, 38, 117198, 2, "Fly the rotation late", "occ"), 1, { from: at("09:45"), until: at("13:45") }) }) },
    { trace_id: C, display_id: "DSR-20261110-CCCCCC", created_at: docs[C].created_at, status: "assessment_ready",
      headline: "AR689 · 60 min late", state: "needs", label: "Needs manual action", source: "OCC",
      ...card([fl("AR689", "13:10", "14:10"), fl("AR690", "15:20", "16:20"), fl("AR941", "18:00", "19:00"), fl("AR942", "21:30", "22:30")],
        impact(77, 38, 110500, 1, "Fly the rotation late", "blocked"), 1) },
    { trace_id: E, display_id: "DSR-20261110-EEEEEE", created_at: iso(1200), status: "failed",
      headline: "AR412 · 40 min late", state: "stopped", label: "Stopped · nothing changed", source: "System event",
      ...card([fl("AR412", "14:40", null)], impact(null, null, null, 0, null, null), 2) },
    { trace_id: B, display_id: "DSR-20261110-BBBBBB", created_at: docs[B].created_at, status: "assessment_ready",
      headline: "AR259 · 20 min late", state: "handled", label: "Handled automatically", source: "Ground handling",
      ...card([fl("AR259", "11:10", "11:30"), fl("AR260", "22:10", "22:30")], impact(0, 0, 900, 1, "Delay", "auto", 2, 2, 0, "policy"), 5) },
    ...(docs[D] ? [{ trace_id: D, display_id: "DSR-20261110-DDDDDD", created_at: docs[D].created_at, status: "assessing",
      headline: docs[D].disruption.text.slice(0, 69), state: "progress", label: "ARROW is assessing", source: "OCC",
      ...card([], impact(null, null, null, 0, null, null), 4) }] : [])
  ];
  // Case 5 (Task 4b's offline run, in shape on this smoke's day; now a crosswind): a TAF forecasts gusts across Lisbon's runway 16:00–18:00 and
  // Schedule holds Lisbon to 24 movements an hour. One case for the whole storm, no single aircraft: cancelling the Faro round
  // trips moves 79 flights on 31 aircraft (OCC decides); accepting the reduced rate moves 81 on 32 and is blocked by a crew duty the
  // reserve does not cover. Each row: flight, from, to, departs, arrives ("+" next day), aircraft, minutes later in the cancel
  // plan ("x" cancelled, "-" not in it), minutes later in the delay plan.
  const S = "8".repeat(32);
  const TAF = "TAF AMD LPPT 100920Z 1010/1112 19012KT 9999 SCT020 BKN040 BECMG 1013/1015 23018G28KT 6000 RA BKN012 TEMPO 1016/1018 " +
    "27022G35KT 6000 SHRA BKN015 BECMG 1018/1020 30015KT 9999 NSW SCT020";
  const STORM = "AR532 BER LIS 13:10 16:45 CS-TJC 30 32|AR604 AMS LIS 13:35 16:35 CS-TJQ - 25|AR608 MUC LIS 13:45 16:55 CS-TJV 32 35|" +
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
    "AR1113 LIS PDL 22:05 00:30+ CS-TKD 22 23|AR870 OPO LIS 16:50 17:45 CS-TJX - 0";
  // Flights that make up time in the air: flight, minutes, how late it was (band), CO2 allowances (none outside the EU ETS).
  const STORM_FUEL = {
    cancel: "AR927 5 15-30 88.2|AR931 4 30-60 70.56|AR937 4 30-60 70.56|AR959 3 30-60 52.92|AR977 2 30-60 35.28|AR981 4 30-60 70.56|AR961 4 60-90 0|" +
      "AR1005 4 15-30 0|AR1011 4 30-60 70.56|AR1017 4 30-60 70.56|AR1015 4 30-60 70.56|AR978 2 15-30 35.28|AR928 5 15-30 88.2|AR960 3 30-60 52.92|AR962 4 60-90 0|" +
      "AR1006 4 15-30 0|AR1012 4 30-60 70.56|AR1016 4 15-30 70.56|AR1113 4 15-30 0",
    delay: "AR927 5 15-30 88.2|AR931 4 30-60 70.56|AR937 4 30-60 70.56|AR977 2 30-60 35.28|AR981 4 30-60 70.56|AR959 3 60-90 52.92|AR961 4 60-90 0|" +
      "AR1005 4 15-30 0|AR1007 4 30-60 70.56|AR1011 4 30-60 70.56|AR1017 4 30-60 70.56|AR1015 4 30-60 70.56|AR932 4 30-60 70.56|AR938 4 30-60 70.56|" +
      "AR978 2 15-30 35.28|AR928 5 15-30 88.2|AR960 3 60-90 52.92|AR982 4 30-60 70.56|AR962 4 60-90 0|AR1006 4 15-30 0|AR1012 4 30-60 70.56|" +
      "AR1016 4 30-60 70.56|AR1008 4 30-60 70.56|AR1113 4 15-30 0"
  };
  const clockAt = (time, next = false, add = 0) => {
    const t = Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5)) + add + (next ? 1440 : 0);
    const m = t % 1440;
    return `2026-11-${t >= 1440 ? "11" : "10"}T${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:00`;
  };
  const stormRows = STORM.split("|").map(entry => {
    const [no, from, to, std, sta, reg, cancel, delay] = entry.split(" ");
    return { no, from, to, std, sta: sta.replace("+", ""), next: sta.endsWith("+"), reg, plans: { cancel, delay } };
  });
  // Minutes held in the air before landing for a Lisbon arrival slot: AR864 also leaves late; AR870 (the delay plan only) keeps
  // its departure and only holds.
  const HELD = { AR864: 6, AR870: 6 };
  const stormOriginal = (r) => ({ flight_date: "2026-11-10", flight_no: r.no, origin: r.from, destination: r.to, std_local: clockAt(r.std),
    sta_local: clockAt(r.sta, r.next), aircraft_reg: r.reg, status: "scheduled" });
  const stormFlights = (kind) => stormRows.filter(r => r.plans[kind] !== "-").map(r => {
    const cancelled = r.plans[kind] === "x", late = cancelled ? 0 : Number(r.plans[kind]);
    return { ...stormOriginal(r), std_local: clockAt(r.std, false, late), sta_local: clockAt(r.sta, r.next, late), status: cancelled ? "cancelled" : "scheduled",
      held_airborne_minutes: HELD[r.no] || 0, made_up_minutes: 0, made_up_possible: 0, ets: true, intra_eu: true };
  }).sort((a, b) => a.std_local.localeCompare(b.std_local));
  const stormWorkings = (kind) => ({ airline: { rate_eur_per_minute: 17.78,
    flights: stormFlights(kind).filter(f => f.status === "cancelled" || Number(stormRows.find(r => r.no === f.flight_no).plans[kind]) > 0).map(f => (f.status === "cancelled" ? narrowCancel(f.flight_no)
      : lateLine(f.flight_no, Number(stormRows.find(r => r.no === f.flight_no).plans[kind])))),
    fuel_eur_per_minute: { narrow: 85.4, wide: 158.6 }, co2_eur_per_minute: { narrow: 17.64, wide: 32.76 },
    fuel: STORM_FUEL[kind].split("|").map(entry => {
      const [no, minutes, band, co2] = entry.split(" ");
      return { flight_no: no, made_up_minutes: Number(minutes), made_up_possible: Number(minutes), reason: `worth more than its fuel at ${band.replace("-", "–")} min late`,
        aircraft: "narrow", fuel_eur: Math.round(Number(minutes) * 8540) / 100, co2_eur: Number(co2), eur: Math.round(Number(minutes) * 8540 + Number(co2) * 100) / 100 };
    }),
    holding: stormFlights(kind).filter(f => f.held_airborne_minutes > 0).map(f => ({ flight_no: f.flight_no, held_minutes: f.held_airborne_minutes, aircraft: "narrow",
      fuel_eur: Math.round(f.held_airborne_minutes * 8540) / 100, co2_eur: Math.round(f.held_airborne_minutes * 1764) / 100,
      eur: Math.round(f.held_airborne_minutes * 10304) / 100, basis: "fuel at cruise burn (conservative)" })) },
    note: "Weather is an extraordinary circumstance: EU261 compensation is not owed. Care and rebooking are." });
  const stormReserve = { reserve_option_id: "R1", duty_id: "D-LIS-146", split_after_flight: "AR462", flights_for_reserve: ["AR827", "AR828"],
    crew: ["SC", "CC", "CC", "CC", "CPT", "FO"].map((rank, i) => ({ crew_id: `C0194${i}`, rank })), departure_local: "2026-11-10T14:00:00",
    report_local: "2026-11-10T13:00:00", release_local: "2026-11-10T18:48:00", fdp_minutes: 318, max_fdp_minutes: 780 };
  const stormDuty = (duty_id, status, fdp, limit) => ({ duty_id, status, sectors: 4, pilots: 2, modeled_fdp_minutes: fdp, modeled_fdp_limit_minutes: limit, connection: null });
  const QUOTA = [["AR1012", "00:27"], ["AR1010", "00:53"], ["AR1018", "00:43"], ["AR1016", "00:40"], ["AR1008", "00:55"]].map(([no, at]) => `LIS night quota: ${no} in-block ${at}`);
  const stormCancel = {
    plan_id: "1c19c99447ae7da3", option_id: "1c19c99447ae7da3", aircraft_option_id: "cancel", kind: "cancel", cause: "weather",
    title: "Cancel the Faro round trips, then sequence the rest", flights: stormFlights("cancel"),
    levers: { aircraft_option_id: "cancel", reserve_option_id: "R1", rebook: true, delay_messages: true }, crew_reserve: stormReserve,
    domain_assessments: [{ domain: "crew", constraint_status: "fail", duties: [stormDuty("D-LIS-005", "pass", 555, 780), stormDuty("D-LIS-146", "fail", 738, 720)] },
      { domain: "slots", constraint_status: "pass", capacity_fit: true, required_additional_delay_minutes: 0 }, { domain: "booking", constraint_status: "fail" }],
    // As in cost.py: a storm's plan owes no EU261 compensation (extraordinary circumstances); its workings say what would
    // otherwise have been owed, never counted in the total. Care and rebooking stay.
    cost: { airline_eur: 103174, compensation_eur: 0, care_eur: 11790, rebooking_eur: 95413, care: { meals_eur: 11790, hotel_eur: 0, taxi_eur: 0 },
      overnight_passengers: 0, owed_compensation_passengers: 0, meals_passengers: 655, total_eur: 210377, low_eur: 210377, high_eur: 210377, basis: "indicative",
      workings: { ...stormWorkings("cancel"), compensation: [],
        compensation_not_owed: { passengers: 653, eur: 187100, reason: "Extraordinary circumstances: weather. Care and rebooking are still owed." },
        meals: [strandedMeals(655)], overnight: null,
        rebooking: [{ eur_each: 168, passengers: 159, why: "rerouted on another airline · 1,500–3,500 km or within the EU" },
          { eur_each: 150, passengers: 455, why: "rerouted on another airline · up to 1,500 km" }, { eur_each: 11, passengers: 41, why: "rebooked on our own flights · up to 1,500 km" }] } },
    validation: { rebooking: { disrupted_passengers: 655, rebooked_passengers: 41, unplaced_passengers: 614, transferred_passengers: 101 },
      crew_plan: { duties_changed: 37, breaches_unresolved: 0, reserve_option_id: "R1" }, airport_requests: QUOTA },
    policy: { route: "occ", reasons: ["OCC decides", "cancellation", "weather: a forecast moves flights across the network", "estimated cost above €20,000 (€210,377)", "more than 10 passengers rebooked (41)", ...QUOTA,
      "614 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: true, recommendation_reason: "The only plan the crew can fly"
  };
  // What approving it would change, as GET /api/cases/{trace} adds it from the executor's action builder (app.planned,
  // actions.plan_actions; Case 5 offline run: 115 actions): night-quota requests, one retime and one cancel, a delay message per
  // departure moved more than 15 min, a ready time per Lisbon departure moved, cancel messages and slot releases, crew, passengers.
  const stormLater = stormCancel.flights.filter(f => f.status !== "cancelled");
  const stormGone = stormCancel.flights.filter(f => f.status === "cancelled").map(f => f.flight_no);
  const lateOf = (no) => Number(stormRows.find(r => r.no === no).plans.cancel);
  stormCancel.planned_actions = [
    ...QUOTA.map(summary => ({ system: "slots", label: "Slot coordinator", summary })),
    { system: "schedule", label: "Ops control", summary: `Retime ${stormLater.map(f => f.flight_no).join(", ")}` },
    { system: "schedule", label: "Ops control", summary: `Cancel ${stormGone.join(", ")}` },
    ...stormLater.filter(f => lateOf(f.flight_no) > 15).map(f => ({ system: "slots", label: "Network manager", summary: `DLA ${f.flight_no}` })),
    ...stormGone.map(no => ({ system: "slots", label: "Network manager", summary: `CNL ${no}` })),
    ...stormLater.filter(f => f.origin === "LIS").map(f => ({ system: "slots", label: "Airport CDM", summary: `TOBT ${f.flight_no} LIS` })),
    ...stormGone.map(no => ({ system: "slots", label: "Slot coordinator", summary: `Slot non-use ${no} LIS` })),
    { system: "crew", label: "Crew", summary: "Assign reserve crew R1" }, { system: "booking", label: "Passengers", summary: "Rebook 41 passengers" },
    { system: "booking", label: "Ground handling", summary: "Direct transfers for 101 passengers" }
  ];
  const stormDelay = {
    plan_id: "2296eb1956be45f9", option_id: "2296eb1956be45f9", aircraft_option_id: "delay", kind: "delay", cause: "weather",
    title: "Accept the reduced Lisbon rate and fly the affected flights late", flights: stormFlights("delay"),
    levers: { aircraft_option_id: "delay", reserve_option_id: "R1", rebook: true, delay_messages: true }, crew_reserve: { ...stormReserve, release_local: "2026-11-10T18:49:00", fdp_minutes: 319 },
    domain_assessments: [{ domain: "crew", constraint_status: "fail", duties: [stormDuty("D-LIS-146", "fail", 739, 720), stormDuty("D-LIS-164", "fail", 681, 660)] },
      { domain: "slots", constraint_status: "pass", capacity_fit: true, required_additional_delay_minutes: 0 }, { domain: "booking", constraint_status: "pass" }],
    cost: { airline_eur: 58533, compensation_eur: 0, care_eur: 0, rebooking_eur: 0, care: { meals_eur: 0, hotel_eur: 0, taxi_eur: 0 }, overnight_passengers: 0,
      owed_compensation_passengers: 0, meals_passengers: 0, total_eur: 58533, low_eur: 58533, high_eur: 58533, basis: "indicative",
      workings: { ...stormWorkings("delay"), compensation: [], meals: [], overnight: null, rebooking: [] } },
    validation: { rebooking: { disrupted_passengers: 0, rebooked_passengers: 0, unplaced_passengers: 0, transferred_passengers: 131 },
      crew_plan: { duties_changed: 39, breaches_unresolved: 1, reserve_option_id: "R1" } },
    policy: { route: "blocked", reasons: ["flight-time limit still exceeded after the reserve crew",
      "domains disagree: standby crew R3 cannot report in time, and a weather plan takes no added delay"] },
    requires_duty_manager: false, recommended: false, recommendation_reason: null,
    // plan.outcomes: crews over their limit counted after the plan's standby crews (one duty is left over here).
    outcomes: { stranded_overnight: 0, lose_flight_or_connection: 0, crews_over_limit: 1 }
  };
  stormCancel.outcomes = { stranded_overnight: 0, lose_flight_or_connection: 655, crews_over_limit: 0 };
  docs[S] = { trace_id: S, display_id: "DSR-20261110-F7348C", status: "assessment_ready", version: 4, created_at: iso(2300), operating_date: "2026-11-10",
    source: { kind: "event", system: "IPMA (weather)" }, stage: "OCC decides",
    disruption: { type: "weather", cause: "weather", airport: "LIS", icao: "LPPT", taf_window: "1016/1018", window_start: "16:00", window_end: "18:00",
      window_start_local: "2026-11-10T16:00:00", window_end_local: "2026-11-10T18:00:00", hazard: "crosswind", title: "Strong crosswind at Lisbon · 16:00–18:00",
      wind: "27022G35KT", crosswind_kt: 33, runway: "02/20", hourly_rate: 24, normal_rate: 40,
      rate_basis: "the airline's OCC playbook rate for crosswind on Lisbon's single runway: the A320 family's demonstrated crosswind is 38 kt including gusts (35 kt for take-off with the A320neo's engines), so operations continue with care",
      scope: { flights: stormRows.map(r => r.no), aircraft_reg: null },
      description: TAF, message: TAF, reading: "Gusts of 35 kt from the west across Lisbon's runway from 16:00 to 18:00 UTC.", reported_at: "2026-11-10T09:45:00" },
    affected_flights: stormRows.map(stormOriginal), options: [stormCancel, stormDelay], events: [] };
  // Its control tower row as GET /api/cases serves it (app.list_cases with trail.case_card).
  const stormRow = { trace_id: S, display_id: docs[S].display_id, created_at: docs[S].created_at, status: "assessment_ready", aircraft_reg: null,
    disruption_type: "weather", flight_no: null, headline: "Strong crosswind at Lisbon · 16:00–18:00 · gusts 33 kt across runway 02/20 · 24 movements an hour (normally 40)", state: "needs", label: "Needs your decision", source: "IPMA (weather)",
    station: "LIS", ...card([fl("AR532", "13:10", "13:40"), fl("AR608", "13:45", "14:17"), fl("AR630", "14:00", "14:25"), fl("AR628", "14:00", "14:32"),
      fl("AR131", "14:10", null, "cancelled"), fl("AR728", "14:50", "15:17")],
    { ...impact(655, 41, 210377, 2, stormCancel.title, "occ"), plan_kind: "cancel", plan_cancelled: ["AR131", "AR132", "AR881", "AR882"] }, 1) };
  // The storm as plans made of OCC playbook actions (owner: no "Act now"; this smoke's figures, not a run's): the two plans
  // ARROW built before the playbook, each naming PAX-NOTICE, FUEL-FLAG and ALT-FLAG. Accepting the reduced Lisbon rate retimes the
  // flights with two standby crews called at the forecast (the head start) and tells the passengers of the moved flights their new
  // times; cancelling the Faro round trips does the same with one standby crew. Both are OCC's (Weather plans); neither owes EU261
  // compensation (owner); each carries its outcomes (crews over their limit counted after the standby crews).
  const ST = "9".repeat(32);
  const xssFact = 'Faro <img src="x-fact" onerror="window.__xss=4"> TEMPO TSRA 17:00–19:00';
  const stormStandby = (id, duty, called, report, flights) => ({ reserve_option_id: id, standby_type: "home", called_local: `2026-11-10T${called}:00`,
    report_local: `2026-11-10T${report}:00`, duty_id: duty, flights_for_reserve: flights,
    crew: ["CPT", "FO", "SC", "CC", "CC", "CC"].map((rank, i) => ({ crew_id: `C07${id}${i}`, rank })), fdp_minutes: 300, max_fdp_minutes: 720 });
  const STORM_FUEL_ADVICE = ["FUEL-FLAG", "advice", "114 arrivals into Lisbon 14:30–21:00: carry holding fuel"];
  const delayLate = (no) => Number(stormRows.find(r => r.no === no).plans.delay);
  const delayMoved = stormFlights("delay").filter(f => delayLate(f.flight_no) > 0);
  const stormRate = { ...stormDelay, plan_id: "st-delay", option_id: "st-delay",
    levers: { aircraft_option_id: "delay", reserve_option_ids: ["R1", "R2"], rebook: true, delay_messages: true, playbook_actions: ["PAX-NOTICE", "FUEL-FLAG", "ALT-FLAG"] },
    crew_reserve: undefined, crew_reserves: [stormStandby("R1", "D-LIS-146", "09:45", "13:00", ["AR827", "AR828"]), stormStandby("R2", "D-LIS-164", "10:05", "13:20", ["AR851", "AR852"])],
    cost: { ...stormDelay.cost },
    outcomes: { stranded_overnight: 0, lose_flight_or_connection: 0, crews_over_limit: 0 },
    validation: { rebooking: { disrupted_passengers: 0, rebooked_passengers: 0, unplaced_passengers: 0, transferred_passengers: 131 },
      crew_plan: { duties_changed: 39, breaches_unresolved: 0, reserve_option_ids: ["R1", "R2"] } },
    policy: { route: "occ", reasons: ["OCC decides", "weather: a forecast moves flights across the network", "estimated cost above €20,000 (€58,533)"] },
    requires_duty_manager: true, recommended: true, recommendation_reason: "Nobody loses a flight; two standby crews called at the forecast cover the duties the delays push over.",
    playbook: pb([["RETIME", "in_plan", "81 flights, up to +80 min"], ["GROUND-HOLD", "in_plan", "18 arrivals into Lisbon wait at origin"], ["THIN", "not_needed"],
      ["STANDBY", "in_plan", "R1 called 09:45 · reports 13:00 · AR827, AR828; R2 called 10:05 · reports 13:20 · AR851, AR852"], ["REBOOK", "not_needed"],
      ["CAR", "in_plan", "131 passengers by car"], ["PAX-NOTICE", "in_plan", "85 flights · 9,412 passengers told their new times"], STORM_FUEL_ADVICE,
      ["ALT-FLAG", "advice", "Porto 277 km, Faro 221 km: no forecast hazard"]]),
    // As GET /api/cases/{trace} gives them (system, label, summary; no kind): the retime and its messages, then the notice, then crew.
    planned_actions: [{ system: "schedule", label: "Ops control", summary: `Retime ${delayMoved.map(f => f.flight_no).join(", ")}` },
      ...delayMoved.filter(f => delayLate(f.flight_no) > 15).map(f => ({ system: "slots", label: "Network manager", summary: `DLA ${f.flight_no}` })),
      { system: "booking", label: "Passengers", summary: "Tell passengers on 85 flights their new times" },
      { system: "crew", label: "Crew", summary: "Assign standby crews R1, R2" }, { system: "crew", label: "Crew", summary: "Update crew duties" },
      { system: "booking", label: "Ground handling", summary: "Direct transfers for 131 passengers" }] };
  const stormThin = { ...stormCancel, plan_id: "st-cancel", option_id: "st-cancel",
    levers: { ...stormCancel.levers, playbook_actions: ["PAX-NOTICE", "FUEL-FLAG", "ALT-FLAG", "MX-ETR"] },
    crew_reserve: undefined, crew_reserves: [stormStandby("R1", "D-LIS-146", "09:45", "13:00", ["AR827", "AR828"])],
    cost: { ...stormCancel.cost },
    outcomes: { stranded_overnight: 0, lose_flight_or_connection: 655, crews_over_limit: 0 },
    validation: { ...stormCancel.validation, crew_plan: { duties_changed: 37, breaches_unresolved: 0, reserve_option_ids: ["R1"] } },
    policy: { route: "occ", reasons: ["OCC decides", "cancellation", "weather: a forecast moves flights across the network", "estimated cost above €20,000 (€210,377)",
      "more than 10 passengers rebooked (41)", "614 passengers need Passenger Services review"] },
    requires_duty_manager: true, recommended: false, recommendation_reason: null,
    playbook: pb([["RETIME", "in_plan", "75 flights, up to +80 min"], ["GROUND-HOLD", "in_plan", "17 arrivals into Lisbon wait at origin"],
      ["THIN", "in_plan", "AR131, AR132, AR881, AR882 cancelled"], ["STANDBY", "in_plan", "R1 called 09:45 · reports 13:00 · AR827, AR828"],
      ["REBOOK", "in_plan", "41 passengers rebooked"], ["CAR", "in_plan", "101 passengers by car"], ["PAX-NOTICE", "in_plan", "79 flights · 8,650 passengers told their new times"],
      STORM_FUEL_ADVICE, ["ALT-FLAG", "advice", xssFact], ["MX-ETR", "refused", null, "does not apply to a weather forecast"]]),
    planned_actions: [{ system: "schedule", label: "Ops control", summary: `Retime ${stormLater.map(f => f.flight_no).join(", ")}` },
      { system: "schedule", label: "Ops control", summary: `Cancel ${stormGone.join(", ")}` },
      { system: "booking", label: "Passengers", summary: "Tell passengers on 79 flights their new times" },
      { system: "crew", label: "Crew", summary: "Assign standby crew R1" }, { system: "booking", label: "Passengers", summary: "Rebook 41 passengers" }] };
  docs[ST] = { ...docs[S], trace_id: ST, display_id: "DSR-20261110-99A001", version: 3, options: [stormRate, stormThin], events: [] };
  // Accepting the reduced rate, approved by OCC and carried out: each action by its kind, the passenger notice in plain words.
  const SX = "9a".repeat(16);
  const playbookDone = (label, kind, summary, message) => ({ label, kind, summary, status: "confirmed", message });
  docs[SX] = { ...docs[ST], trace_id: SX, display_id: "DSR-20261110-99A002", version: 5, stage: "OCC approved",
    execution: { route: "occ", approved_by: OCC, status: "completed", option_id: "st-delay", actions: [
      playbookDone("Ops control", "retime_flights", "Retime AR532, AR608", "Retimed AR532 13:42, AR608 14:17; MVT sent"),
      playbookDone("Network manager", "network_manager.fpl_delay", "DLA AR532", "DLA AR532 EOBT 13:42"),
      // Read by its kind and payload, whatever its summary says.
      { ...playbookDone("Passengers", "notify_passengers", "Passenger notice sent", "85 flights: 9,412 passengers told their new times"),
        payload: { flights: Array.from({ length: 85 }, (_, i) => `AR${500 + i}`) } },
      playbookDone("Crew", "assign_reserve_crew", "Assign standby crews R1, R2", "Standby crews R1, R2 assigned"),
      playbookDone("Crew", "update_duties", "Update crew duties", "Crew duties: D-LIS-146 release 18:30"),
      playbookDone("Ground handling", "ground_handling.direct_transfer", "Direct transfers for 131 passengers", "Cars booked for 131 passengers")] },
    events: [{ event: "duty_manager_decision", at: iso(2200), option_id: "st-delay", decision: "approve", case_version: 4 }] };
  // The storm in two stages (owner: "it does not make sense to already update eurocontrol ... better to already inform passengers
  // somehow"; case summary and held storm spec §2), in Task 2's shapes. A weather plan's actions carry a stage: now, carried out
  // when OCC approves (the passengers of the flights it moves or cancels warned of possible delays, its standby crews put on
  // alert, each to be called by its report less 60 min), and when confirmed, everything else, held until the storm is confirmed
  // and ARROW's re-check passes.
  const nowWarn = (flights) => ({ system: "booking", label: "Passengers", summary: `Warn passengers on ${flights} flights of possible delays`, stage: "now" });
  const nowAlert = (ids) => ({ system: "crew", label: "Crew", summary: `Put standby crew${ids.length === 1 ? "" : "s"} ${ids.join(", ")} on alert`, stage: "now" });
  const staged = (plan, warned, ids) => ({ ...plan, planned_actions: [nowWarn(warned), nowAlert(ids),
    ...plan.planned_actions.map(action => ({ ...action, stage: "when_confirmed" }))] });
  // Before OCC decides: both plans in two stages.
  const G0 = "d0".repeat(16);
  docs[G0] = { ...docs[ST], trace_id: G0, display_id: "DSR-20261110-D0D0D0", version: 3,
    options: [staged(stormRate, 85, ["R1", "R2"]), staged(stormThin, 83, ["R1"])], events: [] };
  // OCC approved the reduced rate: what the executor recorded (execution.storm and each action with its stage, kind and message).
  const warned85 = Array.from({ length: 85 }, (_, i) => `AR${500 + i}`);
  const STORM_METAR = "METAR LPPT 101420Z 27022G34KT 9999 -SHRA FEW015 BKN030 17/12 Q1009";
  const STORM_TAF_AMD = "TAF AMD LPPT 101015Z 1010/1112 19012KT 9999 SCT020 BKN040 TEMPO 1016/1018 27022G35KT 6000 SHRA BKN015";
  const STORM_TAF_DROPPED = "TAF AMD LPPT 101310Z 1013/1112 21015KT 9999 SCT025 BKN040 TEMPO 1015/1019 21020G30KT 6000 -RA BKN015 BECMG 1019/1021 30012KT NSW SCT030";
  const stormUpdate = (id, at, status, rate, description) => ({ event_id: id, at: `2026-11-10T${at}:00+00:00`, status, airport: "LIS", taf_window: "1016/1018",
    window: "16:00–18:00", window_start: "16:00", window_end: "18:00", hazard: "crosswind", rate_per_hour: rate, description });
  const heldStorm = (extra = {}) => ({ state: "awaiting", decide_by: "12:00",
    planned: { window: "16:00–18:00", window_start: "16:00", window_end: "18:00", rate_per_hour: 24, hazard: "crosswind" },
    confirm_by: ["METAR or amended TAF: strong crosswind 16:00–18:00", "EUROCONTROL regulation: Lisbon arrivals at 24 an hour or more"],
    stand_down_by: ["Amended TAF: no strong crosswind 16:00–18:00"], updates: [], held_at: "2026-11-10T09:52:00+00:00", ...extra });
  const stormAction = (n, stage, status, system, kind, label, summary, message, extra = {}) => ({ action_id: `5c0d-${String(n).padStart(2, "0")}-${kind.replace(/[._]/g, "-")}`,
    system, kind, label, summary, stage, status, message, ...extra });
  const stormNow = () => [
    stormAction(1, "now", "confirmed", "booking", "warn_passengers", "Passengers", "Warn passengers on 85 flights of possible delays",
      "Told 9,412 passengers on 85 flights of possible delays. Their new times follow once the forecast is confirmed.", { payload: { flights: warned85, kind: "possible_delay" } }),
    stormAction(2, "now", "confirmed", "crew", "alert_standby", "Crew", "Put standby crews R1, R2 on alert", "Standby crews R1, R2 on alert: call by 12:00, 12:20",
      { payload: { crews: [{ reserve_option_id: "R1", call_by_local: "2026-11-10T12:00:00", crew: [] }, { reserve_option_id: "R2", call_by_local: "2026-11-10T12:20:00", crew: [] }] } })];
  const delayMessages = delayMoved.filter(f => delayLate(f.flight_no) > 15);
  // The held part, in the order the executor sends it; sent: each with the airline system's answer, else as recorded while held.
  const stormHeldPart = (status, message = () => null) => [
    ["schedule", "retime_flights", "Ops control", `Retime ${delayMoved.map(f => f.flight_no).join(", ")}`, `Retimed ${delayMoved.length} flights; MVT sent`,
      { payload: { flights: delayMoved.map(f => ({ flight_no: f.flight_no, std_local: f.std_local, sta_local: f.sta_local })) },
        expected: { flights: Object.fromEntries(delayMoved.map(f => [f.flight_no, { std_local: stormOriginal(stormRows.find(r => r.no === f.flight_no)).std_local, status: "scheduled" }])) } }],
    ...delayMessages.map(f => ["slots", "network_manager.fpl_delay", "Network manager", `DLA ${f.flight_no}`, `DLA ${f.flight_no} EOBT ${f.std_local.slice(11, 16)}`,
      { payload: { flight_no: f.flight_no, eobt: f.std_local } }]),
    ["booking", "notify_passengers", "Passengers", "Tell passengers on 85 flights their new times", "Told 9,412 passengers on 85 flights their new times",
      { payload: { flights: warned85, kind: "new_times" } }],
    ["crew", "assign_reserve_crew", "Crew", "Assign standby crews R1, R2", "Standby crews R1, R2 assigned", {}],
    ["crew", "update_duties", "Crew", "Update crew duties", "Crew duties: D-LIS-146 release 18:30; D-LIS-164 release 19:10", {}],
    ["booking", "ground_handling.direct_transfer", "Ground handling", "Direct transfers for 131 passengers", "Cars booked for 131 passengers", {}]
  ].map(([system, kind, label, summary, sent, extra], i) => stormAction(i + 3, "when_confirmed", status, system, kind, label, summary,
    status === "confirmed" ? sent : message(kind), extra));
  const heldCount = stormHeldPart("not_started").length;
  // After the approval the case API gives no planned actions; the plans keep their playbook.
  const stormApproved = { ...docs[ST], options: [stormRate, stormThin].map(plan => ({ ...plan, planned_actions: undefined })), stage: "OCC approved" };
  const approvedEvents = () => [{ event: "duty_manager_decision", at: iso(2200), option_id: "st-delay", decision: "approve", case_version: 4 },
    { event: "storm_held", actor: "recovery-executor", at: "2026-11-10T09:52:00+00:00", message: "Now: done · when confirmed: held · decide by 12:00" }];
  const stormExecution = (status, storm, actions) => ({ route: "occ", approved_by: OCC, status, option_id: "st-delay", storm, actions });
  // Held: the now part done, the rest waits.
  const G1 = "d1".repeat(16);
  docs[G1] = { ...stormApproved, trace_id: G1, display_id: "DSR-20261110-D1D1D1", version: 6,
    execution: stormExecution("held", heldStorm(), [...stormNow(), ...stormHeldPart("not_started")]), events: approvedEvents() };
  // Confirmed by a METAR within the window at the planned rate: re-checked, the held part sent.
  const G2 = "d2".repeat(16);
  docs[G2] = { ...stormApproved, trace_id: G2, display_id: "DSR-20261110-D2D2D2", version: 9,
    execution: stormExecution("completed", heldStorm({ state: "confirmed", rechecked: true, updates: [stormUpdate("u-2", "13:30", "confirmed", 24, STORM_METAR)] }),
      [...stormNow(), ...stormHeldPart("confirmed")]),
    events: [...approvedEvents(), { event: "storm_confirmed", actor: "recovery-executor", at: "2026-11-10T13:30:00+00:00",
      message: "Strong crosswind confirmed: 16:00–18:00, rate 24 an hour. ARROW re-checks the plan, then sends the held actions." },
    { event: "storm_released", actor: "recovery-executor", at: "2026-11-10T13:31:00+00:00", message: `Sources rechecked: sending the ${heldCount} held actions.` }] };
  // Confirmed, but worse than planned: nothing more sent, back to OCC with the reason.
  const G3 = "d3".repeat(16);
  const WORSE = "the confirmed weather is worse than planned (rate 16 an hour, planned 24)";
  docs[G3] = { ...stormApproved, trace_id: G3, display_id: "DSR-20261110-D3D3D3", version: 8,
    execution: stormExecution("needs_attention", heldStorm({ state: "confirmed", returned: WORSE, updates: [stormUpdate("u-3", "13:30", "confirmed", 16, STORM_METAR)] }),
      [...stormNow(), ...stormHeldPart("not_started")]),
    events: [...approvedEvents(), { event: "storm_returned_to_occ", actor: "recovery-executor", at: "2026-11-10T13:30:00+00:00",
      message: `OCC decides: ${WORSE}. Nothing more was sent; the passengers stay warned and the standby crews on alert.` }] };
  // The forecast dropped: the warned passengers told no change is expected, the alerts released, nothing held sent.
  const G4 = "d4".repeat(16);
  docs[G4] = { ...stormApproved, trace_id: G4, display_id: "DSR-20261110-D4D4D4", version: 8,
    execution: stormExecution("stood_down", heldStorm({ state: "dropped", updates: [stormUpdate("u-4", "13:10", "dropped", null, STORM_TAF_DROPPED)] }),
      [...stormNow(), ...stormHeldPart("stood_down", () => "Not sent: the forecast was dropped and the plan stood down"),
        stormAction(heldCount + 3, "stand_down", "confirmed", "booking", "notify_no_change", "Passengers", "Tell passengers on 85 flights no change is expected",
          "Told 9,412 passengers on 85 flights that no change is expected.", { payload: { flights: warned85, kind: "no_change" } }),
        stormAction(heldCount + 4, "stand_down", "confirmed", "crew", "release_standby", "Crew", "Release standby crews R1, R2", "Released standby crews R1, R2")]),
    events: [...approvedEvents(), { event: "storm_dropped", actor: "recovery-executor", at: "2026-11-10T13:10:00+00:00",
      message: "Forecast dropped: no strong crosswind 16:00–18:00. ARROW releases the standby crews and tells the warned passengers no change is expected; nothing held is sent." },
    { event: "storm_stood_down", actor: "recovery-executor", at: "2026-11-10T13:11:00+00:00",
      message: "Stood down: standby crews R1, R2 released; passengers on 85 flights told no change is expected; nothing held was sent." }] };
  // The storm came after all, after the stand-down (executor: storm.confirmed_after_stand_down): OCC decides again.
  const G10 = "da".repeat(16);
  docs[G10] = { ...docs[G4], trace_id: G10, display_id: "DSR-20261110-DADADA", execution: { ...docs[G4].execution,
    storm: { ...docs[G4].execution.storm, confirmed_after_stand_down: true, updates: [...docs[G4].execution.storm.updates, stormUpdate("u-10", "14:20", "confirmed", 24, STORM_METAR)] } } };
  // An amended TAF confirming the storm before OCC decided: recorded on the case (its storm), nothing sent.
  const G5 = "d5".repeat(16);
  docs[G5] = { ...docs[G0], trace_id: G5, display_id: "DSR-20261110-D5D5D5", version: 4,
    storm: { state: "confirmed", updates: [stormUpdate("u-5", "10:20", "confirmed", 24, STORM_TAF_AMD)] },
    events: [{ event: "storm_confirmed", actor: "recovery-executor", at: "2026-11-10T10:20:00+00:00",
      message: "Strong crosswind confirmed: 16:00–18:00, rate 24 an hour · recorded; nothing is sent until OCC approves a plan." }] };
  // The forecast dropped before OCC decided (Task 2): recorded on the case; approving it then stands the plan down at once, with nothing
  // ever sent, so nothing to undo.
  const G8 = "d8".repeat(16);
  docs[G8] = { ...docs[G0], trace_id: G8, display_id: "DSR-20261110-D8D8D8", version: 4,
    storm: { state: "dropped", updates: [stormUpdate("u-8", "10:20", "dropped", null, STORM_TAF_DROPPED)] },
    events: [{ event: "storm_dropped", actor: "recovery-executor", at: "2026-11-10T10:20:00+00:00",
      message: "Forecast dropped: no strong crosswind 16:00–18:00 · recorded; nothing is sent until OCC approves a plan." }] };
  const G6 = "d6".repeat(16);
  const neverSent = (actions) => actions.map(action => ({ ...action, status: "stood_down", message: "Not sent: the forecast was dropped and the plan stood down" }));
  docs[G6] = { ...stormApproved, trace_id: G6, display_id: "DSR-20261110-D6D6D6", version: 6,
    storm: { state: "dropped", updates: [stormUpdate("u-6", "10:20", "dropped", null, STORM_TAF_DROPPED)] },
    execution: stormExecution("stood_down", heldStorm({ state: "dropped", held_at: undefined, updates: [stormUpdate("u-6", "10:20", "dropped", null, STORM_TAF_DROPPED)] }),
      neverSent([...stormNow(), ...stormHeldPart("not_started")])),
    events: [{ event: "duty_manager_decision", at: iso(2200), option_id: "st-delay", decision: "approve", case_version: 5 },
      { event: "storm_stood_down", actor: "recovery-executor", at: "2026-11-10T10:31:00+00:00",
        message: "Stood down: the forecast was dropped before anything was sent; nothing changes." }] };
  // A stand-down that stopped: the passenger system refused the no-change notice; the standby crews were released.
  const G7 = "d7".repeat(16);
  docs[G7] = { ...stormApproved, trace_id: G7, display_id: "DSR-20261110-D7D7D7", version: 8,
    execution: stormExecution("needs_attention", heldStorm({ state: "dropped", updates: [stormUpdate("u-7", "13:10", "dropped", null, STORM_TAF_DROPPED)] }),
      [...stormNow(), ...stormHeldPart("stood_down", () => "Not sent: the forecast was dropped and the plan stood down"),
        stormAction(heldCount + 3, "stand_down", "refused", "booking", "notify_no_change", "Passengers", "Tell passengers on 85 flights no change is expected",
          "Refused: the passenger system is read-only", { payload: { flights: warned85, kind: "no_change" } }),
        stormAction(heldCount + 4, "stand_down", "confirmed", "crew", "release_standby", "Crew", "Release standby crews R1, R2", "Released standby crews R1, R2")]),
    events: [...approvedEvents(), { event: "storm_dropped", actor: "recovery-executor", at: "2026-11-10T13:10:00+00:00",
      message: "Forecast dropped: no strong crosswind 16:00–18:00. ARROW releases the standby crews and tells the warned passengers no change is expected; nothing held is sent." }] };
  // Final review: the standby crews could not be released (refused), said as such.
  const G9 = "d9".repeat(16);
  docs[G9] = { ...docs[G7], trace_id: G9, display_id: "DSR-20261110-D9D9D9", execution: { ...docs[G7].execution,
    actions: docs[G7].execution.actions.map(action => (action.kind === "release_standby" ? { ...action, status: "refused", message: "Refused: R2 is already released" }
      : action.kind === "notify_no_change" ? { ...action, status: "confirmed", message: "Told 9,412 passengers on 85 flights that no change is expected." } : action)) } };
  // Case 2 of a Dev run, in shape (stored before Task 2 stopped building plans once one is automatic): AR444 leaves
  // 70 min late; the delay ran automatically within every limit and two cancellations were built but not needed.
  const R2 = "c2".repeat(16);
  const c2Delay = { ...delayPlan, plan_id: "c2-delay", option_id: "c2-delay", title: "Delay the rotation",
    flights: [leg("AR444", "HAM", "LIS", "12:10", "15:05"), leg("AR861", "LIS", "MAD", "16:04", "18:20"), leg("AR862", "MAD", "LIS", "19:02", "19:20")],
    domain_assessments: domains(575, 720),
    cost: { airline_eur: 3112, compensation_eur: 2750, care_eur: 198, rebooking_eur: 5016, care: { meals_eur: 198, hotel_eur: 0, taxi_eur: 0 }, overnight_passengers: 0,
      owed_compensation_passengers: 11, meals_passengers: 11, total_eur: 11076, low_eur: 11076, high_eur: 11076, basis: "indicative",
      workings: { airline: { rate_eur_per_minute: 17.78, flights: [lateLine("AR444", 70), lateLine("AR861", 60), lateLine("AR862", 45)] },
        compensation: [{ eur_each: 250, passengers: 11, why: "arrives 3 h or more late · up to 1,500 km" }], meals: [strandedMeals(11)], overnight: null,
        rebooking: [{ eur_each: 456, passengers: 11, why: "rerouted on another airline · up to 1,500 km" }] } },
    validation: { rebooking: { disrupted_passengers: 11, rebooked_passengers: 6, unplaced_passengers: 5, transferred_passengers: 19 } },
    policy: { route: "auto", reasons: ["within every domain's limits; OCC is informed"], limits: { version: 1, cost_cap_eur: 20000, rebook_limit: 10 } },
    requires_duty_manager: false, recommended: true,
    recommendation_reason: "Within every limit; a car keeps the 19 connecting passengers on their flights.", planned_actions: undefined,
    playbook: pb([["RETIME", "in_plan"], ["THIN", "not_needed"], ["SWAP-AC", "not_needed"], ["STANDBY", "not_needed"],
      ["REBOOK", "in_plan"], ["CAR", "in_plan"]]) };
  const c2Cancel = (id, a, b, cost) => ({ ...cancelPlan, plan_id: id, option_id: id, title: `Cancel ${a} and ${b}`,
    flights: [leg(a, "LIS", "MAD", "16:04", "18:20", "cancelled"), leg(b, "MAD", "LIS", "19:02", "19:20", "cancelled")], cost,
    playbook: pb([["RETIME", "not_needed"], ["THIN", "in_plan"], ["SWAP-AC", "not_needed"], ["STANDBY", "not_needed"],
      ["REBOOK", "in_plan"], ["CAR", "not_needed"]]),
    policy: { route: "occ", reasons: ["OCC decides", "cancellation"] }, requires_duty_manager: true, recommended: false, recommendation_reason: null });
  docs[R2] = { trace_id: R2, display_id: "DSR-20261110-C2C2C2", status: "assessment_ready", version: 5, created_at: iso(2400), operating_date: "2026-11-10",
    source: { kind: "event", system: "Ground handling" }, stage: "Within every domain's limits",
    disruption: { type: "flight_delay", cause: "staff_shortage", flight_no: "AR444", delay_minutes: 70, scope: { flights: ["AR444"], aircraft_reg: "CS-TNP" },
      message: "Ground handling in Hamburg is short-staffed; AR444 is expected to depart 70 minutes late.", reported_at: "2026-11-10T09:45:00" },
    options: [c2Delay, c2Cancel("c2-cancel-1", "AR861", "AR862", { ...cancelPlan.cost }),
      c2Cancel("c2-cancel-2", "AR1053", "AR1054", { ...cancelPlan.cost, airline_eur: 35303, total_eur: 108637, low_eur: 108637, high_eur: 108637 })],
    execution: { route: "auto", approved_by: "policy", status: "completed", option_id: "c2-delay",
      actions: [{ label: "Ops control", kind: "retime_flights", summary: "Retime AR444", status: "confirmed", message: "Retimed AR444 12:10; MVT sent" },
        { label: "Network manager", kind: "network_manager.fpl_delay", summary: "DLA AR444", status: "confirmed", message: "DLA AR444 EOBT 12:10" }],
      // A Dev run: 5 passengers without a same-day seat wait for Passenger Services.
      reviews: [{ kind: "passenger_services", status: "awaiting_expert", passengers: 5 }] },
    events: [{ event: "auto_execution_authorised", at: iso(2390), actor: "recovery-executor" }] };
  // Standby crews as Task 2 stores them: levers.reserve_option_ids and crew_reserves, each with home or airport standby,
  // the latest call and the report. Crew control calls a standby crew well before it reports.
  const SB = "5b".repeat(16);
  const standbyR1 = { reserve_option_id: "R1", base: "PDL", qualification: "A320F", standby_type: "home", called_local: "2026-11-10T15:54:00", report_local: "2026-11-10T16:54:00",
    earliest_departure_local: "2026-11-10T17:54:00", duty_id: "D-LIS-131", flights_for_reserve: ["AR941", "AR942"],
    crew: ["CPT", "FO", "SC", "CC", "CC", "CC"].map((rank, i) => ({ crew_id: `C0300${i}`, rank })), fdp_minutes: 290, max_fdp_minutes: 780 };
  const standbyR2 = { reserve_option_id: "R2", base: "LIS", qualification: "A320F", standby_type: "airport", called_local: "2026-11-10T14:05:00", report_local: "2026-11-10T14:05:00",
    earliest_departure_local: "2026-11-10T15:05:00", duty_id: "D-LIS-140", flights_for_reserve: ["AR690"],
    crew: ["CPT", "FO", "SC", "CC", "CC", "CC"].map((rank, i) => ({ crew_id: `C0310${i}`, rank })), fdp_minutes: 200, max_fdp_minutes: 720 };
  const standbyDuties = [{ domain: "crew", constraint_status: "fail", duties: [{ duty_id: "D-LIS-131", status: "fail", modeled_fdp_minutes: 805, modeled_fdp_limit_minutes: 780 },
    { duty_id: "D-LIS-140", status: "fail", modeled_fdp_minutes: 742, modeled_fdp_limit_minutes: 720 }] },
  { domain: "slots", constraint_status: "pass", capacity_fit: true }, { domain: "booking", constraint_status: "pass" }];
  const standbyPlan = { ...delayOverLimit, plan_id: "sb-delay", option_id: "sb-delay", title: "Fly the rotation late with two standby crews",
    levers: { aircraft_option_id: "delay", reserve_option_ids: ["R1", "R2"], rebook: true, delay_messages: true }, crew_reserves: [standbyR1, standbyR2],
    domain_assessments: standbyDuties,
    validation: { rebooking: { disrupted_passengers: 41, rebooked_passengers: 41, unplaced_passengers: 0 },
      // The complete standby crews left today, after the plan and without it, per base and qualification (Task 1, Task 2).
      crew_plan: { duties_changed: 6, breaches_unresolved: 0, reserve_option_ids: ["R1", "R2"],
        standby_left: [{ base: "FAO", qualification: "A320F", crews: 1 }, { base: "LIS", qualification: "A320F", crews: 1 }, { base: "PDL", qualification: "A320F", crews: 0 }] } },
    policy: { route: "occ", reasons: ["OCC decides", "uses the last standby crew at Ponta Delgada A320"] }, requires_duty_manager: true, recommended: true,
    recommendation_reason: "Two standby crews take the duties that would run over; every flight keeps flying.",
    // Case 4: the standby crews are the playbook's STANDBY, in the plan; the delay retimes and rebooks.
    playbook: pb([["RETIME", "in_plan"], ["THIN", "not_needed"], ["SWAP-AC", "not_needed"],
      ["STANDBY", "in_plan", "R1 called 15:54 · reports 16:54 · AR941, AR942; R2 called 14:05 · reports 14:05 · AR690"], ["REBOOK", "in_plan"],
      ["CAR", "not_needed"]]),
    planned_actions: [{ label: "Ops control", summary: "Retime AR689, AR690, AR941, AR942" }, { label: "Crew", summary: "Assign standby crews R1, R2" },
      { label: "Passengers", summary: "Rebook 41 passengers" }] };
  const standbyLate = { ...standbyPlan, plan_id: "sb-late", option_id: "sb-late", title: "Fly the rotation 30 min later with two standby crews",
    levers: { ...standbyPlan.levers, extra_delay_minutes: 30 },
    cost: { ...delayOverLimit.cost, airline_eur: 6900, total_eur: 6900, low_eur: 6900, high_eur: 6900 },
    validation: { ...standbyPlan.validation, crew_plan: { duties_changed: 6, breaches_unresolved: 2, reserve_option_ids: ["R1", "R2"] } },
    policy: { route: "blocked", reasons: ["flight-time limit still exceeded after the standby crews"] }, requires_duty_manager: false, recommended: false,
    recommendation_reason: null };
  // The agents' answers disagree when a standby crew cannot reach the flight it would take (planner.py, Task 2).
  const standbyR3 = { ...standbyR1, reserve_option_id: "R3", called_local: "2026-11-10T16:30:00", report_local: "2026-11-10T17:30:00", flights_for_reserve: ["AR775"] };
  const standbyConflict = { ...standbyPlan, plan_id: "sb-r3", option_id: "sb-r3", title: "Fly the rotation 15 min later with standby crew R3",
    levers: { ...standbyPlan.levers, reserve_option_ids: ["R3"], extra_delay_minutes: 15 }, crew_reserves: [standbyR3],
    cost: { ...delayOverLimit.cost, airline_eur: 6100, total_eur: 6100, low_eur: 6100, high_eur: 6100 },
    validation: { ...standbyPlan.validation, crew_plan: { duties_changed: 6, breaches_unresolved: 1, reserve_option_ids: ["R3"] } },
    policy: { route: "blocked", reasons: ["domains disagree: standby crew R3 cannot take AR775 in time, and this plan does not move it"] },
    requires_duty_manager: false, recommended: false, recommendation_reason: null };
  docs[SB] = { trace_id: SB, display_id: "DSR-20261110-5B5B5B", status: "assessment_ready", version: 3, created_at: iso(2500), operating_date: "2026-11-10",
    source: { kind: "ui" }, stage: "OCC decides",
    disruption: { type: "flight_delay", cause: "late_inbound", flight_no: "AR689", delay_minutes: 60, scope: { flights: ["AR689"], aircraft_reg: "CS-TKW" },
      message: "Late inbound aircraft: AR689 will leave 60 minutes late.", reported_at: "2026-11-10T09:45:00" },
    options: [standbyPlan, standbyLate, standbyConflict], events: [] };
  // Case 4 of a Dev run, in shape: the orchestrator recommends cancelling the last round trip; delaying would take a Ponta
  // Delgada crew 11 min over its duty limit, which a standby crew covers (owner: "people think why would you even consider cancelling").
  const C4 = "c4".repeat(16);
  const standbyOne = { ...standbyPlan, plan_id: "c4-delay", option_id: "c4-delay", title: "Fly the rotation late with a standby crew",
    levers: { ...standbyPlan.levers, reserve_option_ids: ["R1"] }, crew_reserves: [standbyR1],
    domain_assessments: [{ domain: "crew", constraint_status: "fail", duties: [{ duty_id: "D-LIS-131", status: "fail", modeled_fdp_minutes: 791, modeled_fdp_limit_minutes: 780 },
      { duty_id: "D-LIS-140", status: "pass", modeled_fdp_minutes: 600, modeled_fdp_limit_minutes: 720 }] }, ...standbyDuties.slice(1)],
    validation: { ...standbyPlan.validation, crew_plan: { ...standbyPlan.validation.crew_plan, reserve_option_ids: ["R1"] } },
    recommended: false, recommendation_reason: null };
  docs[C4] = { ...docs[SB], trace_id: C4, display_id: "DSR-20261110-C4C4C4", options: [cancelLast, standbyOne], events: [] };
  // Review: standby crews that cover only some of the duties the delay takes over their limit.
  const C5 = "c5".repeat(16);
  docs[C5] = { ...docs[SB], trace_id: C5, display_id: "DSR-20261110-C5C5C5", events: [],
    options: [cancelLast, { ...standbyPlan, recommended: false, recommendation_reason: null, requires_duty_manager: false,
      validation: { ...standbyPlan.validation, crew_plan: { ...standbyPlan.validation.crew_plan, breaches_unresolved: 1 } },
      policy: { route: "blocked", reasons: ["flight-time limit still exceeded after the standby crews"] } }] };
  // Review: OCC chooses between three plans; the verdict compares the least disruptive plan with the cheapest, as its card and
  // the Decision step do, not with the next least disruptive.
  const N3 = "3c".repeat(16);
  const delayMid = { ...delayPlan, plan_id: "plan-delay-mid", option_id: "plan-delay-mid", title: "Fly AR101 and AR102 late", recommended: false, recommendation_reason: null,
    cost: { ...delayPlan.cost, total_eur: 105000, low_eur: 105000, high_eur: 105000 },
    validation: { rebooking: { disrupted_passengers: 150, rebooked_passengers: 100, unplaced_passengers: 50 } } };
  docs[N3] = { ...docs[N], trace_id: N3, display_id: "DSR-20261110-3C3C3C", options: [{ ...cancelPlan, recommended: false }, delayMid,
    { ...delayPlan, recommended: false, recommendation_reason: null }], events: [] };
  // Review: a later report on Case 2's rotation, after Case 2's delay ran. ARROW read AR444, AR861 and AR862 at their new
  // times (the case's originals, as GET /api/cases/{trace} gives them); the map's day still publishes the timetable before Case 2.
  const R3 = "c3".repeat(16);
  const c3Leg = (no, from, to, std, sta) => ({ flight_no: no, origin: from, destination: to, std_local: `2026-11-10T${std}:00`, sta_local: `2026-11-10T${sta}:00`,
    aircraft_reg: "CS-TNP", status: "scheduled" });
  const c3Delay = { ...c2Delay, plan_id: "c3-delay", option_id: "c3-delay", title: "Delay the rotation",
    flights: [c3Leg("AR444", "HAM", "LIS", "12:40", "15:35"), c3Leg("AR861", "LIS", "MAD", "16:34", "18:50"), c3Leg("AR862", "MAD", "LIS", "19:32", "19:50")],
    cost: { ...c2Delay.cost, workings: { ...c2Delay.cost.workings, airline: { rate_eur_per_minute: 17.78,
      flights: [lateLine("AR444", 30), lateLine("AR861", 30), lateLine("AR862", 30)] } } },
    policy: { route: "occ", reasons: ["OCC decides", "estimated cost above €20,000 (€21,076)"] }, requires_duty_manager: true, recommended: true,
    recommendation_reason: "Keeps every connection with the cars already booked." };
  docs[R3] = { trace_id: R3, display_id: "DSR-20261110-C3C3C3", status: "assessment_ready", version: 2, created_at: iso(2450), operating_date: "2026-11-10",
    source: { kind: "event", system: "Ground handling" }, stage: "OCC decides",
    disruption: { type: "flight_delay", cause: "staff_shortage", flight_no: "AR444", delay_minutes: 30, scope: { flights: ["AR444"], aircraft_reg: "CS-TNP" },
      message: "Ground handling in Hamburg: AR444 is now expected another 30 minutes late.", reported_at: "2026-11-10T09:45:00" },
    originals: Object.fromEntries([c3Leg("AR444", "HAM", "LIS", "12:10", "15:05"), c3Leg("AR861", "LIS", "MAD", "16:04", "18:20"),
      c3Leg("AR862", "MAD", "LIS", "19:02", "19:20")].map(flight => [flight.flight_no, flight])),
    options: [c3Delay], events: [] };
  // GET /api/network for that day: each flight's current times (Case 2's delay ran) beside its published timetable.
  const c2Day = { operating_date: "2026-11-10", as_of_local: "2026-11-10T09:45:00", timezone: "Europe/Lisbon",
    flights: [["AR444", "HAM", "LIS", "12:10", "15:05", "11:00", "13:55"], ["AR861", "LIS", "MAD", "16:04", "18:20", "15:04", "17:20"],
      ["AR862", "MAD", "LIS", "19:02", "19:20", "18:17", "18:35"]].map(([no, from, to, std, sta, pstd, psta]) => ({ ...c3Leg(no, from, to, std, sta),
      flight_date: "2026-11-10", published_std_local: `2026-11-10T${pstd}:00`, published_sta_local: `2026-11-10T${psta}:00` })),
    aircraft: [{ registration: "CS-TNP", fleet_type: "A321neo", seats: 216, base: "LIS", status: "serviceable" }],
    airports: [["LIS", -9.13, 38.77], ["HAM", 9.99, 53.63], ["MAD", -3.56, 40.47]].map(([iata, lon, lat]) => ({ iata, position: [lon, lat] })) };
  // Review: a swap plan names each flight moved to another aircraft (its fact holds flight numbers, never its count).
  const SW = "5e".repeat(16);
  const swapPlan = { ...delayPlan, plan_id: "sw-swap", option_id: "sw-swap", aircraft_option_id: "swap", kind: "swap", title: "Swap AR965 and AR966 to CS-TNB",
    flights: [{ ...leg("AR965", "LIS", "MAD", "17:30", "18:40"), aircraft_reg: "CS-TNB" }, { ...leg("AR966", "MAD", "LIS", "19:15", "19:25"), aircraft_reg: "CS-TNB" }],
    cost: { total_eur: 4200 }, validation: { rebooking: { disrupted_passengers: 0, rebooked_passengers: 0, unplaced_passengers: 0 } },
    policy: { route: "occ", reasons: ["OCC decides", "aircraft change: CS-TNB"] }, requires_duty_manager: true, recommended: true,
    recommendation_reason: "CS-TNB is free and keeps every flight on time.", planned_actions: [{ label: "Ops control", summary: "Tail change AR965, AR966" }],
    playbook: pb([["RETIME", "not_needed"], ["THIN", "not_needed"], ["SWAP-AC", "in_plan", "AR965 → CS-TNB; AR966 → CS-TNB"], ["STANDBY", "not_needed"],
      ["REBOOK", "not_needed"], ["CAR", "not_needed"], ["MX-ETR", "advice", DEFECT_ETR]]) };
  docs[SW] = { ...docs[A], trace_id: SW, display_id: "DSR-20261110-5E5E5E", version: 2, options: [swapPlan], events: [] };
  // The passenger notice when Booking could not count the passengers: no number on its chip.
  const SN = "9b".repeat(16);
  docs[SN] = { ...docs[ST], trace_id: SN, display_id: "DSR-20261110-99A003", events: [],
    options: [{ ...stormRate, playbook: stormRate.playbook.map(row => (row.id === "PAX-NOTICE" ? { ...row, facts: "85 flights · passengers told not established" } : row)) }] };
  // Markup in what an agent or a system wrote (a plan's name, a duty id, a standby crew's id) is text, never HTML (review).
  const XS = "7c".repeat(16);
  const xssName = 'Hold <img src="x-plan" onerror="window.__xss=1"> & <b>wait</b>';
  const xssDuty = 'D-<img src="x-duty" onerror="window.__xss=2">';
  const xssCrew = 'R<svg onload="window.__xss=3">';
  const xssPlan = { ...standbyPlan, plan_id: "x-hold", option_id: "x-hold", kind: "hold", title: xssName,
    levers: { ...standbyPlan.levers, reserve_option_ids: [xssCrew] }, crew_reserves: [{ ...standbyR1, reserve_option_id: xssCrew, duty_id: xssDuty }],
    domain_assessments: [{ ...standbyDuties[0], duties: [{ duty_id: xssDuty, status: "fail", modeled_fdp_minutes: 805, modeled_fdp_limit_minutes: 780 }] },
      ...standbyDuties.slice(1)],
    validation: { ...standbyPlan.validation, crew_plan: { ...standbyPlan.validation.crew_plan, reserve_option_ids: [xssCrew] } },
    planned_actions: [{ label: "Crew", summary: `Assign standby crews ${xssCrew}` }] };
  docs[XS] = { ...docs[SB], trace_id: XS, display_id: "DSR-20261110-7C7C7C", options: [xssPlan], events: [] };
  // GET /api/rules as app.rules returns it: a change the signed-in duty manager proposed, its effect and policy.RULE_CATALOGUE.
  const release = { change_id: 1, cost_cap_eur: 20000, rebook_limit: 10, base_version: 1, reason: "ARROW release values",
    proposed_by: "arrow-release", proposed_at: "2026-11-01T00:00:00.000Z", status: "approved", version: 1,
    decided_by: "arrow-release", decided_at: "2026-11-01T00:00:00.000Z" };
  const waiting = { change_id: 7, cost_cap_eur: 15000, rebook_limit: 10, base_version: 1, reason: "Tighter cost control this winter",
    proposed_by: OCC, proposed_at: "2026-11-10T09:30:00.000Z", status: "proposed", version: null, decided_by: null, decided_at: null };
  const rule = (key, name, source, effect, group) => ({ key, name, source, effect, group });
  const rules = { in_force: { version: 1, cost_cap_eur: 20000, rebook_limit: 10, approved_by: "arrow-release", approved_at: release.decided_at },
    pending: { ...waiting, effect: [{ display_id: "DSR-20261110-GGGGGG", headline: "AR805 · 60 min late", plan: "plan-delay", now: "auto", proposed: "occ" }] },
    history: [waiting, release], can_change: true,
    catalogue: [
      rule("cost", "Cost limit for automatic plans", "ARROW rules, editable with four-eyes approval", "OCC decides", "limit"),
      rule("rebooking", "Rebooking limit", "ARROW rules, editable with four-eyes approval; Passenger agent L3", "OCC decides", "limit"),
      rule("aircraft", "Aircraft change or standby", "Aircraft agent L2 recommends; OCC decides", "OCC decides", "occ"),
      rule("cancellation", "Cancellation", "OCC keeps authority over major decisions", "OCC decides", "occ"),
      rule("airport", "Airport and slot requests", "Declared airport rules", "OCC decides", "occ"),
      rule("declined_rebooking", "Declining to rebook", "A passenger decision for OCC", "OCC decides", "occ"),
      rule("weather", "Weather plans", "A forecast moves flights across the network: a major decision for OCC", "OCC decides", "occ"),
      // Task 2's fixed rule (policy.RULE_CATALOGUE "Standby crews").
      rule("standby", "Standby crews", "Standby crews in one plan; a base's last complete standby crew", "OCC decides", "occ"),
      rule("crew", "Crew flight-time limits", "EASA flight-time limits; Crew agent L3", "Blocks plan", "safety"),
      rule("eu261", "Long delays", "EU261 Art. 6 care thresholds", "OCC decides", "safety"),
      rule("capacity", "Lisbon capacity", "Hourly capacity; Slot agent L3", "Blocks plan", "safety"),
      rule("answered", "Every check answered", "A missing or unknown answer is never a pass", "Blocks plan", "safety"),
      rule("passenger_services", "Passengers without rebooking", "Rebooking policy; Passenger Services expert", "Passenger Services decides", "expert")
    ],
    // GET /api/rules' playbook (playbook.catalogue_view): the 11 actions, read only, with the catalogue's version.
    playbook: { version: "playbook-2026-11-22b", actions: Object.entries(PB).map(([id, [name, desk, autonomy]]) => ({ id, name, desk, autonomy,
      applies_to: { "GROUND-HOLD": ["weather"], "SWAP-AC": ["aircraft_defect", "flight_delay"], "PAX-NOTICE": ["weather"], "FUEL-FLAG": ["weather"],
        "ALT-FLAG": ["weather"], "MX-ETR": ["aircraft_defect"] }[id] || ["aircraft_defect", "flight_delay", "weather"],
      phase: ["PAX-NOTICE", "FUEL-FLAG", "ALT-FLAG", "MX-ETR"].includes(id) ? "prepare" : "commit",
      checks: id === "PAX-NOTICE" ? "Flights yet to leave and not cancelled; each flight once a day" : `${name}: checked by ARROW's code`,
      recipient: { "Hub and passengers": "Passenger system", "Crew control": "Crew system", "Aircraft control": "Operations control",
        Dispatch: "Dispatch", "Maintenance control": "Maintenance control" }[desk],
      needs: id === "PAX-NOTICE" ? "a weather plan's retimed flights yet to leave" : "what the action needs" })) }
  };
  const agent = (key, name, level, label, status, asked, answered, calls) => ({ key, name, role: `${name} role`,
    autonomy: { level, label }, asked, answered, calls, status, prompt: key === "passenger" ? { available: false }
      : { available: true, version: "3", instructions: `You are the ${name} agent. Use only your tools.` } });
  // A call as the trail now gives it (trail._call_view): what it was for and its result in words (label, summary).
  const call = (label, system, at, operation = label, summary = "") => ({ at, system, operation, label, summary, detail: "Option: delay", ok: true, outcome: "ok" });
  // Entra agent IDs, models and Foundry names of the agents: fixture data only, never in app code. The trail names each model as
  // people know it (trail.model_name: GPT-4.1, GPT-4.1 mini).
  const FOUNDRY = {
    orchestrator: ["00000000-0000-4000-8000-000000000016", "GPT-4.1", "recovery-orchestrator-agent"],
    detection: ["00000000-0000-4000-8000-000000000015", "GPT-4.1", "disruption-detection-agent"],
    aircraft: ["00000000-0000-4000-8000-000000000011", "GPT-4.1 mini", "aircraft-recovery-agent"],
    crew: ["00000000-0000-4000-8000-000000000004", "GPT-4.1 mini", "crew-recovery-agent"],
    passenger: ["00000000-0000-4000-8000-000000000009", "GPT-4.1 mini", "passenger-reaccommodation-agent"],
    slot: ["00000000-0000-4000-8000-000000000012", "GPT-4.1 mini", "slot-negotiation-agent"]
  };
  const ARROW_ID = { kind: "arrow", name: "ARROW", id: "id-airrec-ops-dev" };
  // Read or write, MCP or REST, an agent's tool and the system's own route with the case's values (api); never a backend host.
  const traced = (c, endpoint, caller, request, response, how) => ({ ...c, endpoint, caller, request, response, ...how });
  const viaMcp = (tool, api = null, access = "read") => ({ access, transport: "mcp", tool, api });
  const viaRest = (access, api) => ({ access, transport: "rest", tool: null, api });
  // The agent itself in Foundry at the version the case ran (the trail's foundry name and version; the session's project link).
  const foundryOf = (key, version) => ({ name: FOUNDRY[key][2], version });
  const agentLink = (key, version) => `${PORTAL}/build/agents/${FOUNDRY[key][2]}/build?version=${version}`;
  const agentCaller = (key, name) => ({ kind: "agent", name, id: FOUNDRY[key][0] });
  // Agent answers as the trail returns them: the text, and the parsed object as facts (null for plain text).
  const answer = (agent, facts, text = JSON.stringify(facts)) => ({ kind: "answer", agent, title: `${agent[0].toUpperCase()}${agent.slice(1)} answered`, detail: text, facts, ok: true, raw: {} });
  const ask = (agent, question) => ({ kind: "ask", agent, title: `Asked ${agent[0].toUpperCase()}${agent.slice(1)}`, detail: question, ok: true, raw: {} });
  // The Detection agent's answer in newer runs: what it read and recorded, the event id and the aircraft. "constructor" is an
  // agent-supplied key that must read as a word, not as an object property.
  const detected = { readable: true, is_disruption: true, type: "aircraft_defect", event_id: "EV-9C2D41", affected_flight_ids: ["AR101", "AR102"],
    aircraft: ["CS-TQA"], constructor: "line_check", reason: "Hydraulic leak" };
  const crewFine = (minutes) => ({ recommendation: { reserve_option_id: null, reason: "No duty breaks its limit." }, facts: { constraint_status: "pass", breaches: [], duty_minutes: minutes } });
  // Asked in parallel: the cancel answer returns first; call_id ties each answer to its own question.
  const paired = (step, callId) => ({ ...step, call_id: callId });
  // policy.classify checks: input, limit and result per rule, as each plan recorded them with rules v1.
  const ruleCheck = (key, rule, input, limit, result, measure) => ({ key, rule, input, limit, result, ...(measure ? { measure } : {}) });
  const fixed = (crewInput, fdp, limit) => [
    ruleCheck("aircraft", "Aircraft change or standby", "same aircraft", "Always OCC", "pass"),
    ruleCheck("airport", "Airport and slot requests", "none", "Always OCC", "pass"),
    ruleCheck("crew", "Crew flight-time limits", crewInput, `${limit} min`, "pass", { value: fdp, limit }),
    ruleCheck("capacity", "Lisbon capacity", "fits at LIS", "Hourly allocation", "pass"),
    ruleCheck("answered", "Every check answered", "Crew · Passenger · Slot", "Unknown blocks", "pass")
  ];
  const planChecks = {
    "plan-cancel": [
      ruleCheck("cost", "Cost limit for automatic plans", "€99,974", "€20,000 (rules v1)", "occ", { value: 99974, limit: 20000 }),
      ruleCheck("rebooking", "Rebooking limit", "300 passengers", "10 passengers (rules v1)", "occ", { value: 300, limit: 10 }),
      ruleCheck("cancellation", "Cancellation", "AR101 · AR102", "Always OCC", "occ"),
      ruleCheck("eu261", "Long delays", "none", "120 / 180 / 240 min by distance", "pass"),
      ...fixed("D-LIS-101: 410 min", 410, 780),
      ruleCheck("passenger_services", "Passengers without rebooking", "24 passengers", "Passenger Services decides", "expert")
    ],
    "plan-delay": [
      ruleCheck("cost", "Cost limit for automatic plans", "€117,198", "€20,000 (rules v1)", "occ", { value: 117198, limit: 20000 }),
      ruleCheck("rebooking", "Rebooking limit", "38 passengers", "10 passengers (rules v1)", "occ", { value: 38, limit: 10 }),
      ruleCheck("cancellation", "Cancellation", "none", "Always OCC", "pass"),
      ruleCheck("eu261", "Long delays", "AR101 +225 min · AR102 +225 min · AR131 +175 min", "120 min (274 km) · 120 min (274 km) · 120 min (249 km)", "occ"),
      ...fixed("D-LIS-101: 440 min", 440, 720),
      ruleCheck("passenger_services", "Passengers without rebooking", "39 passengers", "Passenger Services decides", "expert")
    ]
  };
  // Case 5's checks as policy.classify records them since e064f36: a storm's plans carry the fixed "Weather plans" rule.
  const stormQuota = "LIS night quota: AR1012 in-block 00:27 · LIS night quota: AR1010 in-block 00:53 · LIS night quota: AR1018 in-block 00:43";
  const stormFixed = (crew, airport) => [
    ruleCheck("aircraft", "Aircraft change or standby", "same aircraft", "Always OCC", "pass"),
    ruleCheck("airport", "Airport and slot requests", airport, "Always OCC", "occ"),
    ruleCheck("weather", "Weather plans", "crosswind forecast", "Always OCC", "occ"), crew,
    ruleCheck("eu261", "Long delays", "none", "120 / 180 / 240 min by distance", "pass"),
    ruleCheck("capacity", "Lisbon capacity", "fits at LIS", "Hourly allocation", "pass"),
    ruleCheck("answered", "Every check answered", "Crew · Passenger · Slot", "Unknown blocks", "pass")];
  planChecks[stormCancel.plan_id] = [
    ruleCheck("cost", "Cost limit for automatic plans", "€210,377", "€20,000 (rules v1)", "occ", { value: 210377, limit: 20000 }),
    ruleCheck("rebooking", "Rebooking limit", "41 passengers", "10 passengers (rules v1)", "occ", { value: 41, limit: 10 }),
    ruleCheck("cancellation", "Cancellation", "AR131 · AR132 · AR881 · AR882", "Always OCC", "occ"),
    ...stormFixed(ruleCheck("crew", "Crew flight-time limits", "reserve covers the breach", "720 min", "pass"), stormQuota),
    ruleCheck("passenger_services", "Passengers without rebooking", "614 passengers", "Passenger Services decides", "expert")];
  planChecks[stormDelay.plan_id] = [
    ruleCheck("cost", "Cost limit for automatic plans", "€58,533", "€20,000 (rules v1)", "occ", { value: 58533, limit: 20000 }),
    ruleCheck("rebooking", "Rebooking limit", "0 passengers", "10 passengers (rules v1)", "pass", { value: 0, limit: 10 }),
    ruleCheck("cancellation", "Cancellation", "none", "Always OCC", "pass"),
    ...stormFixed(ruleCheck("crew", "Crew flight-time limits", "D-LIS-164: 681 min", "660 min", "blocked", { value: 681, limit: 660 }), stormQuota),
    ruleCheck("passenger_services", "Passengers without rebooking", "none", "Passenger Services decides", "pass")];
  const trail = (trace) => {
    const doc = docs[trace];
    const live = doc.status === "assessing";
    const rich = trace === A;
    // In newer runs the agents name the disruption by the operations event id its system issued (EV-…); older runs by the trace id.
    const event = doc.disruption.event_id || null;
    const ref = rich ? `event_id ${event}` : `trace_id ${trace}`;
    const by = (key, name, c, request, response, tool, api = null) =>
      (rich ? traced(c, `MCP ${tool}`, agentCaller(key, name), request, response, viaMcp(tool, api)) : { ...c, summary: "" });
    const stamp = new Date().toISOString();
    // ARROW's record of what was reported, apart from its checks (trail.reported_calls): under the Detection agent that asked for
    // it (Case A, recorded_via "detection"), on the report when ARROW recorded a typed event on receipt (Case 1), under the
    // orchestrator in older runs.
    const record = trace === P
      ? traced(call("Flight delay", "Operations control", iso(1998), "record_delay", "AR259 +20 min · Border-control queue · event EV-55D1C2"),
        "POST /api/delays", ARROW_ID, { flight_no: "AR259", delay_minutes: 20, cause: "border_control" },
        { flight_no: "AR259", delay_minutes: 20, event_id: "EV-55D1C2" }, viaRest("write", "POST /api/delays"))
      : rich ? traced(call("Aircraft defect", "Maintenance", iso(280), "record_defect", `CS-TQA out of service 4 h, until 13:45 · event ${event}`),
        "POST /api/defects", ARROW_ID, { aircraft_reg: "CS-TQA", unavailable_minutes: 240 }, { status: "recorded", event_id: event },
        viaRest("write", "POST /api/defects"))
      : doc.disruption.type === "weather" ? call("Weather forecast", "Operations control", iso(2290), "record_weather", "Strong crosswind at Lisbon · 16:00–18:00")
      : doc.disruption.type === "flight_delay" ? call("Flight delay", "Operations control", iso(280), "record_delay",
        `${doc.disruption.flight_no} +${doc.disruption.delay_minutes} min`)
      : call("Aircraft defect", "Maintenance", iso(280), "record_defect", "CS-TQA out of service 4 h");
    const reportedCalls = [record];
    const recordedVia = rich ? "detection" : doc.source.kind === "event" ? "intake" : "orchestrator";
    // Once the plan may run, the executor reads again every source the plan was checked against, then writes (trail.rechecks).
    const rechecks = rich && doc.execution ? [
      traced(call("Aircraft rotation", "Operations control", stamp, "get_aircraft_rotation", "CS-TQA · 3 flights today: AR101, AR102, AR131"),
        "GET /api/aircraft/{aircraft_reg}/rotation", ARROW_ID, { aircraft_reg: "CS-TQA" }, "3 flights today: AR101, AR102, AR131",
        viaRest("read", "GET /api/aircraft/CS-TQA/rotation")),
      traced(call("Recovery options", "Operations control", stamp, "get_recovery_options", "2: delay, cancel · AR101, AR102 affected"),
        "GET /api/events/{event_id}/recovery-options", ARROW_ID, { event_id: event }, { options: ["delay", "cancel"] },
        viaRest("read", `GET /api/events/${event}/recovery-options`))]
      : trace === P ? [traced(call("Aircraft rotation", "Operations control", iso(1985), "get_aircraft_rotation", "CS-TUK · 2 flights today: AR259, AR260"),
        "GET /api/aircraft/{aircraft_reg}/rotation", ARROW_ID, { aircraft_reg: "CS-TUK" }, "2 flights today: AR259, AR260",
        viaRest("read", "GET /api/aircraft/CS-TUK/rotation"))] : [];
    const arrowChecks = rich && doc.execution ? [traced(call("Approved action", "Operations control", stamp, "apply_action", "Retime flights · confirmed"),
      "POST /api/actions", ARROW_ID, { action_id: "act-1", kind: "retime_flights" }, { status: "confirmed" }, viaRest("write", "POST /api/actions"))] : [];
    // New runs: each option's effect on the rotation, the levers also checked and when the aircraft is back.
    const aircraftFacts = rich ? { recommendation: { option_id: "delay", reason: "Delay keeps every flight; cancellation is the last resort." },
      facts: { event_id: event, options: [
        { option_id: "delay", kind: "delay", title: "Wait for maintenance and delay the rotation", effect: "AR101–AR132 leave about 3 h late" },
        { option_id: "cancel", kind: "cancel", title: "Cancel through a safe aircraft-position boundary", effect: "AR101–AR102 cancelled; AR131 and AR132 on time" }],
        checked: [{ kind: "swap", available: false, why: "No other serviceable A320neo with 174 or more seats is free at LIS 10:30–19:40 (99 checked)" },
          { kind: "cancel", available: true, why: "Through LIS" }],
        aircraft_available_from: "2026-11-10T13:45:00" } }
      : { recommendation: { option_id: "delay", reason: "Delay keeps every flight; cancellation is the last resort." },
        facts: { options: [{ option_id: "delay", kind: "delay", title: "Wait for maintenance and delay the rotation" },
          { option_id: "cancel", kind: "cancel", title: "Cancel through a safe aircraft-position boundary" }] } };
    // The Passenger agent's EU261 exposure from ARROW (newer runs): the figures the plan priced from the same option and delay.
    const exposure = (option, extra, owed, compensation, care, rebooking, words, ago) => traced(call("EU261 exposure", "ARROW", iso(ago),
      "get_compensation_exposure", words), "MCP get_compensation_exposure", agentCaller("passenger", "Passenger"), { option_id: option, additional_delay_minutes: extra },
      { option_id: option, additional_delay_minutes: extra, passengers_owed_compensation: owed, compensation_eur: compensation, care_eur: care,
        rebooking_eur: rebooking, extraordinary_circumstances: false }, viaMcp("get_compensation_exposure"));
    // The orchestrator copies its brief's local date-times (owner review); people read the time.
    const reportAsked = `Read this report for case ${doc.display_id} and record the disruption: «${doc.disruption.message}» (reported by OCC at 2026-11-10T09:45:00)`;
    const aircraftAsked = `CS-TQA is out of service for 4 hours, until 2026-11-10T13:45:00. List the recovery options for ${ref}.`;
    const agents = [
        agent("detection", "Detection", "L4", "Autonomous: reads and records the disruption, changes no flight", live ? "working" : "done",
          rich ? reportAsked : `Confirm the disruption for ${ref}.`, JSON.stringify(detected),
          rich ? [traced(call("Aircraft defect", "ARROW", iso(281), "record_aircraft_defect", `CS-TQA out of service 4 h · event ${event}`),
            "MCP record_aircraft_defect", agentCaller("detection", "Detection"),
            { aircraft_reg: "CS-TQA", unavailable_minutes: 240, reading: "CS-TQA is out of service for 240 minutes." },
            { recorded: "aircraft defect · CS-TQA", event_id: event, flights_affected: 4, max_minutes_late: 205 }, viaMcp("record_aircraft_defect", null, "write"))]
            : [call("Disruption", "Operations control", iso(250), "get_disruption")]),
        agent("aircraft", "Aircraft", "L2", "Recommends only: an aircraft change needs a person", live ? "not asked" : "done",
          live ? "" : rich ? aircraftAsked : `List the recovery options for ${ref}.`,
          live ? "" : rich ? JSON.stringify(aircraftFacts) : `The recovery options for trace_id ${trace} are delay or cancel; no free aircraft to swap.`,
          live ? [] : [by("aircraft", "Aircraft", call("Recovery options", "Operations control", iso(240), "get_recovery_options", "2: delay, cancel · AR101, AR102 affected"),
            { event_id: event }, rich ? { options: ["delay", "cancel"], checked: aircraftFacts.facts.checked,
              aircraft: { aircraft_reg: "CS-TQA", type: "A320neo", seats: 174, available_from: "2026-11-10T13:45:00" } } : "delay · cancel",
            "get_recovery_options", `GET /api/events/${event}/recovery-options`)]),
        agent("crew", "Crew", "L3", "Acts within pre-approved rules", live ? "not asked" : "done",
          live ? "" : `Assess crew limits for ${ref}, option_id delay, and additional_delay_minutes 0.`,
          live ? "" : '{"recommendation": {"reason": "No duty breaks its limit."}, "facts": {"constraint_status": "pass", "breaches": []}}',
          live ? [] : [
            by("crew", "Crew", call("Crew impact", "Crew system", iso(230), "assess_crew", "Delay · no duty over its limit · D-LIS-101 440 of 720 min"),
              { event_id: event, option_id: "delay", additional_delay_minutes: 0 }, "No duty breach · D-LIS-101 440 of 720 min", "assess_crew_recovery",
              `GET /api/events/${event}/options/delay/impact`),
            by("crew", "Crew", call("Standby crews", "Crew system", iso(228), "get_reserve_options", "Delay · none needed"),
              { event_id: event, option_id: "delay", additional_delay_minutes: 0 }, null, "get_reserve_options", `GET /api/events/${event}/options/delay/standby-crews`)]),
        agent("passenger", "Passenger", "L3", "Acts within policy limits", live ? "not asked" : "done",
          live ? "" : rich ? `Assess passengers for ${ref}, option_id delay. case_id ${doc.display_id}.` : "Assess passengers.",
          // As in the final Dev run, the agent also put its booking count (44) in disrupted_passengers, beside the 112
          // passengers who lose their flight or connection (59 rebooked + 53 to Passenger Services).
          live ? "" : rich ? `${JSON.stringify({ recommendation: { rebook: true, reason: "53 passengers cannot be rebooked within 6 h." },
            facts: { misconnected_passengers: 112, disrupted_passengers: 44, rebooked_passengers: 59, unplaced_passengers: 53 } })}\n\n` +
            '{"recommendation": {"reason": "24 passengers cannot be rebooked after the cancellation."}}'
            : '{"recommendation": {"reason": "53 passengers cannot be rebooked within 6 h."}}\n\n{"recommendation": {"reason": "24 passengers cannot be rebooked after the cancellation."}}',
          // It reads its own system and ARROW's EU261 exposure, as in the final Dev run.
          live || !rich ? [] : [by("passenger", "Passenger", call("Passenger impact", "Passenger system", iso(227), "assess_booking",
            "640 passengers on the flights · 112 lose flight or connection"), { event_id: event, option_id: "delay", additional_delay_minutes: 0 },
            { affected_passengers: 640, misconnected_passengers: 112 }, "assess_passenger_recovery", `GET /api/events/${event}/options/delay/impact`),
            exposure("delay", 15, 331, 85050, 12026, 6698, "Delay +15 min · 331 owed compensation · €85,050 · care €12,026 · rebooking €6,698", 226),
            exposure("cancel", 0, 240, 60450, 5832, 7052, "Cancel · 240 owed compensation · €60,450 · care €5,832 · rebooking €7,052", 225)]),
        agent("slot", "Slot", "L3", "Acts within allocated slots", "not asked", "", "", [])
      ].map(a => (rich ? { ...a, identity: { client_id: FOUNDRY[a.key][0], name: FOUNDRY[a.key][2] }, model: FOUNDRY[a.key][1],
        foundry: foundryOf(a.key, a.prompt.available ? a.prompt.version : "2") } : a));
    // Newer runs (trail._call_rows): each call with what was sent in named fields, the reply and the recipient's own record
    // of it (the identity it validated, its backend); the record never repeats as a system call of its own.
    const callRow = (c) => ({ ...c, sent: c.sent.text ? c.sent : Object.fromEntries(Object.entries(c.sent).filter(([key]) => key !== "originals")),
      reply: c.message === "ACK" ? { status: "ACK" } : null, request_id: null,
      recorded: [{ at: c.at, system: c.recipient, caller: ARROW_ID, ok: true, outcome: "ok" }] });
    const actions = (doc.execution?.actions || []).map((action, i) => (rich ? { ...action, action_id: `act-${i + 1}`,
      request: { summary: action.message, flights: [{ flight_no: "AR101", std_local: "2026-11-10T13:45:00" }] },
      receipt: { status: action.status, at: "2026-11-10T10:02:00+00:00", reason: i === 1 ? "Accepted by the Network Manager" : null } }
      : action.calls ? { ...action, calls: action.calls.map(callRow) } : action));
    return {
      trace_id: trace, display_id: doc.display_id, status: doc.status,
      message: { text: doc.disruption.message || doc.disruption.text, source: "OCC", reported_at: "2026-11-10T09:45:00",
        reading: live ? "" : "aircraft defect · CS-TQA", reading_note: doc.disruption.reading },
      // A report typed when it arrived (Case 1, newer runs): the brief names the operations event and its type's own fields.
      orchestrator: { name: "Recovery orchestrator", brief: trace === P
        ? { case_id: doc.display_id, event_id: "EV-55D1C2", disruption: { type: "flight_delay", flight_no: "AR259", delay_minutes: 20, cause: "border_control",
          aircraft_reg: "CS-TUK" }, description: doc.disruption.message, reported_by: "Ground handling", reported_at: t21("09:45"),
          limits: { cost_cap_eur: 20000, rebook_limit: 10, max_plans: 4 } }
        : { message: doc.disruption.message || doc.disruption.text, reported_by: "OCC",
        limits: { cost_cap_eur: 20000, rebook_limit: 10, max_plans: 4 } }, steps_source: live ? "arrow" : "foundry",
        steps: live ? [] : [
          ...(rich ? [ask("detection", reportAsked), answer("detection", detected),
            { kind: "tool", tool: "get_playbook", title: "Read the playbook", detail: "", ok: true, raw: { arguments: { case_id: doc.display_id } } },
            { kind: "tool", tool: "record_case", title: "Wrote the case summary", detail: doc.summary, ok: true, raw: {} }]
            : [{ kind: "tool", tool: "record_disruption", title: "Read the message", detail: "CS-TQA is out of service for 240 minutes.", ok: true, raw: { arguments: { kind: "aircraft_defect" } } },
              ask("detection", `Confirm the disruption for ${ref}.`), answer("detection", detected)]),
          ask("aircraft", rich ? aircraftAsked : `List the recovery options for ${ref}.`),
          answer("aircraft", aircraftFacts),
          paired(ask("crew", `Assess crew limits for ${ref}, option_id delay, additional_delay_minutes 0.`), "call_crew_delay"),
          paired(ask("crew", `Assess crew limits for ${ref}, option_id cancel, additional_delay_minutes 0.`), "call_crew_cancel"),
          paired(answer("crew", crewFine(410)), "call_crew_cancel"), paired(answer("crew", crewFine(440)), "call_crew_delay"),
          ask("passenger", `Assess passengers for ${ref}, option_id delay, additional_delay_minutes 0.`),
          answer("passenger", { recommendation: { rebook: true, reason: "Rebook as many as policy allows." },
            facts: { misconnected_passengers: 112, disrupted_passengers: 44, rebooked_passengers: 59, unplaced_passengers: 53 } }),
          ask("passenger", `Assess passengers for ${ref}, option_id cancel, additional_delay_minutes 0.`),
          answer("passenger", null, "324 disrupted; 300 rebooked within policy; 24 for Passenger Services."),
          // The orchestrator built the recommended delay first and the cancel last, one option at a time, and asked the delay
          // without delay messages: ARROW's record says they are needed (planner: the lever records the plan's flights).
          { kind: "tool", tool: "build_plan", title: "Built a plan", detail: "delay", ok: true,
            raw: { arguments: { case_id: doc.display_id, levers: { ...delayPlan.levers, delay_messages: false } } } },
          { kind: "tool", tool: "build_plan", title: "Built a plan", detail: "cancel", ok: true,
            raw: { arguments: { case_id: doc.display_id, levers: cancelPlan.levers } } },
          { kind: "tool", tool: "submit_plan", title: "Recommended a plan", detail: delayPlan.recommendation_reason, ok: true, raw: {} }
        ], summary: doc.summary, model: "GPT-4.1", tokens: 6400, seconds: 41,
        prompt: { available: true, version: "5", instructions: "You are ARROW's Recovery Orchestrator." },
        ...(rich ? { identity: { client_id: FOUNDRY.orchestrator[0], name: "recovery-orchestrator-agent" }, foundry: foundryOf("orchestrator", "5") } : {}) },
      agents,
      arrow_checks: arrowChecks, rechecks, reported_calls: reportedCalls, recorded_via: recordedVia, calls_available: true,
      // The disruption as the Detection agent recorded it (trail._disruption_view): its type's own fields, the operations event id
      // and what happens if nothing changes.
      disruption: rich ? { type: "aircraft_defect", kind: "Aircraft defect", title: "CS-TQA out of service 4 h", fields: [["Aircraft", "CS-TQA"], ["Out of service", "4 h"]],
        event_id: event, knock_on: doc.disruption.knock_on, knock_on_words: "4 flights affected, up to +3 h 25 min if nothing changes" }
        : trace === P ? { type: "flight_delay", kind: "Flight delay", title: "AR259 +20 min", fields: [["Flight", "AR259"], ["Delay", "+20 min"], ["Cause", "Border-control queue"]],
          event_id: "EV-55D1C2", knock_on: { flights: [], flights_affected: 0, max_minutes_late: 0, aircraft: ["CS-TUK"] },
          knock_on_words: "No other flight affected if nothing changes" }
        : doc.disruption.type === "aircraft_defect" ? { type: "aircraft_defect", kind: "Aircraft defect", title: "CS-TQA out of service 4 h",
          fields: [["Aircraft", "CS-TQA"], ["Out of service", "4 h"]], event_id: null, knock_on: null, knock_on_words: "" } : null,
      // The system calls chat turns caused (trail.chat_calls): the agents the Case Assistant asked, ARROW building its what-if.
      chat_calls: trace === W ? [
        { ...traced(call("Crew impact", "Crew system", "2026-11-10T11:31:06Z", "assess_crew", "Delay · no duty over its limit"), "MCP assess_crew_recovery",
          agentCaller("crew", "Crew"), { event_id: "EV-4C4C4C", option_id: "delay", additional_delay_minutes: 0 }, "No duty breach",
          viaMcp("assess_crew_recovery", "GET /api/events/EV-4C4C4C/options/delay/impact")), entry_id: "w2" },
        { ...traced(call("Recovery options", "Operations control", "2026-11-10T11:40:05Z", "get_recovery_options", "2: delay, cancel · +30 min"), "MCP get_recovery_options",
          agentCaller("aircraft", "Aircraft"), { event_id: "EV-4C4C4C", additional_delay_minutes: 30 }, "delay · cancel",
          viaMcp("get_recovery_options", "GET /api/events/EV-4C4C4C/recovery-options?additional_delay_minutes=30")), entry_id: "w3" },
        { ...traced(call("Recovery options", "Operations control", "2026-11-10T11:40:11Z", "get_recovery_options", "2: delay, cancel · +30 min"),
          "GET /api/events/{event_id}/recovery-options", ARROW_ID, { event_id: "EV-4C4C4C", additional_delay_minutes: 30 }, "delay · cancel",
          viaRest("read", "GET /api/events/EV-4C4C4C/recovery-options?additional_delay_minutes=30")), entry_id: "w3" },
        { ...traced(call("Crew impact", "Crew system", "2026-11-10T11:40:12Z", "assess_crew", "Delay +30 min · no duty over its limit"),
          "GET /api/events/{event_id}/options/{option_id}/impact", ARROW_ID, { event_id: "EV-4C4C4C", option_id: "delay", additional_delay_minutes: 30 }, "No duty breach",
          viaRest("read", "GET /api/events/EV-4C4C4C/options/delay/impact?additional_delay_minutes=30")), entry_id: "w3" }
      ] : [],
      plans: live ? [] : doc.options.map(plan => ({ plan_id: plan.plan_id, title: plan.title, kind: plan.kind, route: plan.policy.route,
        recommended: plan.recommended, reason: plan.recommendation_reason, cost_eur: plan.cost.total_eur, adjustment_minutes: plan.adjustment_minutes ?? 0,
        levers: plan.levers ?? null,
        what_if: plan.what_if ? { question: plan.what_if.question, asked_by: plan.what_if.asked_by.name, asked_at: plan.what_if.asked_at, entry_id: plan.what_if.entry_id } : null,
        passengers_disrupted: plan.validation.rebooking.disrupted_passengers, flights: plan.flights.length,
        // As trail.py gives them: the four cost parts, None for a plan priced by an earlier version, and who is owed compensation.
        cost_parts: Number.isFinite(plan.cost.rebooking_eur) ? { airline: plan.cost.airline_eur, compensation: plan.cost.compensation_eur,
          care: plan.cost.care_eur, rebooking: plan.cost.rebooking_eur } : null, passengers_owed_compensation: plan.cost.owed_compensation_passengers ?? null,
        cancelled: plan.flights.filter(f => f.status === "cancelled").length, checks: planChecks[plan.plan_id] || null, rules_version: 1,
        rules: [
          { key: "crew", rule: "Crew flight-time limits", source: "EASA flight-time limits; Crew agent L3", result: "pass", detail: "Within limits" },
          { key: "aircraft", rule: "Aircraft change or standby", source: "Aircraft agent L2 recommends; OCC decides", result: "pass", detail: "Same aircraft" },
          { key: plan.kind === "cancel" ? "cancellation" : "eu261", rule: plan.kind === "cancel" ? "Cancellation" : "Long delays",
            source: plan.kind === "cancel" ? "OCC keeps authority over major decisions" : "EU261 Art. 6 care thresholds", result: "occ", detail: plan.policy.reasons[1] },
          { key: "passenger_services", rule: "Passengers without rebooking", source: "Rebooking policy; Passenger Services expert", result: "expert", detail: plan.policy.reasons.at(-1) }
        ] })),
      // As trail._outcome gives it: a storm's plan held or stood down by name, any other plan not completed (needs attention
      // included) "executing", with the execution's own status and the storm's weather (trail.storm_of).
      outcome: { state: live ? "assessing" : doc.manual_intervention ? "manual" : doc.execution?.status === "completed" ? "executed"
        : ["held", "stood_down"].includes(doc.execution?.status) ? doc.execution.status : doc.execution ? (doc.execution.status === "failed" ? "failed" : "executing")
        : doc.status === "assessment_ready" ? "occ" : doc.status,
        stage: doc.stage, approved_by: doc.execution?.approved_by, execution_status: doc.execution?.status ?? null, storm: doc.execution?.storm || doc.storm || null,
        actions },
      stats: { agents_asked: live ? 1 : 4, steps: live ? 0 : 16, lookups: agents.reduce((n, a) => n + a.calls.length, 0), arrow_checks: arrowChecks.length,
        rechecks: rechecks.length, reported_calls: reportedCalls.length,
        calls: actions.reduce((n, a) => n + (a.calls?.length || 0), 0),
        plans: live ? 0 : 2, rule_checks: live ? 0 : 8, actions: actions.length, seconds: live ? null : 41 }
    };
  };

  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const body = route.request().postData();
    requests.push({ method, path: url.pathname, body: body ? JSON.parse(body) : null, headers: route.request().headers() });
    const json = (status, value) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
    if (authStatus !== 200) return json(authStatus, { detail: "sign in" });
    if (url.pathname === "/api/session") {
      return json(200, { user: { oid: OCC, name: "Duty Manager", role: "OCC Duty Manager" }, day: "2026-11-10",
        demo_time: "09:45", mode: "simulated", capabilities: { approve: true, review: true },
        trace_url_template: traceTemplate, foundry_portal_url: foundryPortal });
    }
    if (url.pathname === "/api/cases" && method === "GET") {
      return casesStatus === 200 ? json(200, { cases: dayRows ?? list() }) : json(casesStatus, { detail: "unavailable" });
    }
    if (url.pathname === "/api/rules" && method === "GET") {
      if (rulesGate) await rulesGate;
      return rulesStatus === 200 ? json(200, rules) : json(rulesStatus, { detail: "Rules unavailable" });
    }
    if (url.pathname === "/api/rules/proposals" && method === "POST") {
      const proposal = { ...waiting, change_id: 8, ...JSON.parse(body), proposed_at: "2026-11-10T10:05:00.000Z" };
      rules.pending = { ...proposal, effect: [] };
      rules.history = [proposal, ...rules.history];
      return json(201, proposal);
    }
    const ruling = url.pathname.match(/^\/api\/rules\/proposals\/(\d+)\/(approve|reject|withdraw)$/);
    if (ruling && method === "POST") {
      // The approver's page is stale: the change was withdrawn in the meantime.
      if (ruling[2] === "approve") return json(409, { detail: "This change is no longer waiting for approval" });
      const decided = { ...rules.history.find(c => c.change_id === Number(ruling[1])), status: "withdrawn", decided_by: OCC,
        decided_at: "2026-11-10T09:58:00.000Z" };
      rules.history = rules.history.map(c => (c.change_id === decided.change_id ? decided : c));
      rules.pending = null;
      return json(200, decided);
    }
    if (url.pathname === "/api/cases" && method === "POST") {
      const report = JSON.parse(body);
      docs[D] = { trace_id: D, display_id: "DSR-20261110-DDDDDD", status: "assessing", version: 1, created_at: iso(2),
        operating_date: "2026-11-10", source: { kind: "ui" }, stage: "The orchestrator is reading the message.",
        disruption: { type: "message", text: report.text, scope: { flights: [], aircraft_reg: null }, reported_at: "2026-11-10T09:45:00" },
        options: [], events: [] };
      return json(202, { trace_id: D, display_id: "DSR-20261110-DDDDDD" });
    }
    const reviewed = url.pathname.match(/^\/api\/cases\/([0-9a-f]{32})\/reviews\/passenger_services$/);
    if (reviewed && method === "POST" && docs[reviewed[1]]?.execution) {
      const doc = docs[reviewed[1]];
      doc.version += 1;
      doc.execution.reviews = [{ ...doc.execution.reviews[0], status: "handled", decided_by: OCC }];
      doc.events.push({ event: "passenger_review_recorded", actor: OCC, at: "2026-11-10T10:05:00Z", case_version: doc.version });
      return json(200, { status: "recorded", trace_id: reviewed[1], version: doc.version });
    }
    const match = url.pathname.match(/^\/api\/cases\/([0-9a-f]{32})(\/trail|\/approval|\/chat)?$/);
    if (match && docs[match[1]]) {
      const trace = match[1];
      if (match[2] === "/trail") return json(200, trail(trace));
      // POST /api/cases/{trace}/chat (app.py): the question kept on the case, pending until the assistant answers.
      if (match[2] === "/chat" && method === "POST") {
        if (chatRefusal) { const refusal = chatRefusal; chatRefusal = null; return json(refusal.status, { detail: refusal.detail }); }
        const entry = chatTurn(`q${(docs[trace].chat || []).length + 1}`, "00:00:00", "Duty Manager", JSON.parse(body).question,
          { status: "pending", response_id: null, asked_at: new Date().toISOString() });
        docs[trace].chat = [...(docs[trace].chat || []), entry];
        return json(202, { entry_id: entry.id });
      }
      if (match[2] === "/approval") {
        const decision = JSON.parse(body);
        const doc = docs[trace];
        doc.version += 1;
        doc.events.push({ event: "duty_manager_decision", at: new Date().toISOString(), option_id: decision.option_id, decision: decision.decision, case_version: doc.version,
          ...(decision.comment ? { comment: decision.comment } : {}) });
        doc.execution = { route: "occ", approved_by: OCC, status: "completed", option_id: decision.option_id,
          actions: executed(), reviews: [{ kind: "passenger_services", status: "awaiting_expert", passengers: 39 }] };
        return json(200, { status: "recorded", trace_id: trace, option_id: decision.option_id, decision: decision.decision, version: doc.version });
      }
      return json(200, docs[trace]);
    }
    if (url.pathname === "/api/network") return networkDay ? json(200, networkDay) : json(503, { detail: "not in this smoke" });
    return json(404, { detail: "not found" });
  });

  // 1. ARROW opens on the control tower: every case of the day by urgency, nothing selected.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const towerRows = page.locator("#tower-rows tr");
  await towerRows.first().waitFor();
  check(await page.locator("#tower").isVisible() && !(await page.locator("#workspace").isVisible()), "The control tower should be the start screen");
  check((await page.locator("#tab-tower").getAttribute("aria-selected")) === "true", "Control tower should be the selected tab");
  check(!requests.some(r => /^\/api\/cases\/[0-9a-f]{32}/.test(r.path)), "No case should be opened by itself");
  check((await towerRows.count()) === 4, "Every case of the day should be listed");
  check((await towerRows.nth(0).textContent()).includes("CS-TQA · out of service 4 h"), "The decision OCC owes should be first");
  check((await towerRows.nth(2).textContent()).includes("AR412 · 40 min late") && (await towerRows.nth(3).textContent()).includes("AR259"), "Stopped before handled");
  check((await towerRows.nth(0).textContent()).includes("in 45 min"), "The first departure should count down from the demo clock");
  // Owner ("i dont think we need that infromation here"): the tower keeps the day's cases; the rules in force and the
  // agents' autonomy are on the Rules screen and each agent's card, and a rule change waiting shows in the header and the tab.
  check((await page.locator("#tower-rules, .tw-agents").count()) === 0 && (await page.locator("#tower .tw-chip").innerText()) === "Rule change waiting for a second approver",
    "The tower should hold no rules or agents panel, only the waiting rule change");
  const needOcc = await page.locator('#tower [data-kpi="occ"]').innerText();
  check(needOcc.includes("2") && needOcc.includes("1 decision · 1 manual action"), `Need OCC should count decisions and manual actions: ${needOcc}`);
  check((await page.locator('#tower [data-kpi="decision"]').innerText()).includes("€117,198"), "Awaiting your decision should sum the plans OCC must decide");
  check((await page.locator("#rules-count").textContent()) === "1", "Rules should show the pending change");
  const [kpisBox, tableBox] = [await page.locator("#tower .tw-kpis").boundingBox(), await page.locator("#tower .tw-table").boundingBox()];
  check(tableBox.x + tableBox.width >= kpisBox.x + kpisBox.width - 40, `The day's cases should take the tower's full width: ${JSON.stringify([kpisBox, tableBox])}`);
  check((await page.locator("#tower .tw-lane").count()) === 4 && (await page.locator("#tower .tw-band").count()) === 1, "One lane per case and the defect window");
  check((await towerRows.nth(3).locator(".tw-plan").innerText()).includes("Ran automatically · 2 of 2 actions"), "A plan policy carried out should say it ran automatically");
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-tower.png", fullPage: true });
  await professional("Control tower");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator("#tower").getByRole("button", { name: "Cost", exact: true }).click();
  check((await towerRows.nth(0).textContent()).includes("CS-TQA") && (await towerRows.nth(1).textContent()).includes("AR259"), "Cost should put the highest cost first and cases without a cost last");
  await towerRows.nth(0).click();

  // The row opens the case in Disruptions.
  await page.locator("#case-view").waitFor();
  check((await page.locator("#tab-workspace").getAttribute("aria-selected")) === "true" && !(await page.locator("#tower").isVisible()), "A row should open the case in Disruptions");
  await page.locator(".case-group.needs").waitFor();
  check((await page.locator(".case-group.needs .case-row").count()) === 2, "Needs you should hold the decision and the manual case");
  check((await page.locator(".case-group.handled").textContent()).includes("AR259 · +20 min"), "Handled should list the automatic AR259 case, as the prototype words it");
  await page.locator("#case-title").filter({ hasText: "CS-TQA out of service · 4 h" }).waitFor();
  const words = async (locator) => (await locator.textContent()).replace(/\s+/g, " ").trim();
  const drawer = page.locator("#drawer");
  const stepButton = (id) => page.locator(`#steps [data-step="${id}"]`);
  // The steps act as tabs (owner, layout A): the chosen step's content follows them full width in the page.
  const openStep = async (id) => {
    if ((await stepButton(id).getAttribute("aria-selected")) !== "true") await stepButton(id).click();
    await page.locator(`#step-${id}`).waitFor();
  };
  // The plan cards are the Decision step's first part.
  const showPlans = async () => openStep("decision");
  // Where the summary and the steps are, to check that choosing a step never moves them.
  const placeOf = (selector) => page.evaluate((s) => {
    const r = document.querySelector(s).getBoundingClientRect();
    const scroll = document.querySelector("#case").scrollTop;
    return [Math.round(r.left), Math.round(r.top + scroll), Math.round(r.width), Math.round(r.height)].join(",");
  }, selector);
  // Owner: one navigation level. The page tabs (Decision | How ARROW decided) and the sub-tabs (Graph | Conversation |
  // Rule checks) are gone; a stepper under the header tells the case, the current step highlighted, later steps muted.
  check((await page.locator("#tab-decision, #tab-trail, .case-tabs, #trail-views, [data-trail-view], [data-case-tab]").count()) === 0,
    "The case screen should have no page tabs or sub-tabs");
  const stepRow = async () => (await page.locator("#steps .step").evaluateAll(steps => steps.map(step =>
    `${step.querySelector(".lbl").textContent}:${["done", "current", "later"].find(state => step.classList.contains(state))}`))).join(" ");
  await stepButton("assessed").filter({ hasText: "4 agents" }).waitFor();
  check(await stepRow() === "Reported:done Assessed:done Plans:done Rules:done Decision:current Carried out:later", `The steps should lead to the decision: ${await stepRow()}`);
  check((await words(stepButton("decision"))) === "5 Decision Waiting for you" && (await stepButton("decision").getAttribute("aria-current")) === "step" &&
    (await words(stepButton("plans"))) === "✓ Plans 2 built" && (await words(stepButton("rules"))) === "✓ Rules OCC decides" &&
    (await words(stepButton("reported"))).includes("OCC") && (await words(stepButton("carried"))) === "6 Carried out Not yet",
    `Each step should say where the case stands: ${await words(page.locator("#steps ol"))}`);
  check((await stepButton("decision").getAttribute("aria-selected")) === "true" && await page.locator("#step-decision").isVisible() &&
    (await page.locator("#plans .plan").count()) === 2, "A case waiting for OCC should open on Decision, with its plan cards");
  // While the summary shows it replaces the status line; checks read the summary, never hidden text (review).
  check(await page.locator("#status-banner").isHidden() && await page.locator("#summary .t-todo").isVisible(), "The summary, not a hidden status line, should say what OCC must do");
  // Your action is announced from a node outside the band, which is redrawn (review); Today gives each row's details on hover
  // and focus, not only in a native title.
  check((await page.locator("#summary-live").textContent()) === "Your action Approve plan 2 Before 10:30 · AR101" &&
    (await page.locator("#summary-live").getAttribute("aria-live")) === "polite" && (await page.locator("#summary [aria-live]").count()) === 0,
    `What OCC must do should be announced from a node that persists: ${await page.locator("#summary-live").textContent()}`);
  const todayRow = page.locator(`.case-row[data-case="${C}"]`);
  check((await todayRow.getAttribute("title")) === null && (await todayRow.getAttribute("data-tip")).includes("Needs manual action") &&
    (await todayRow.getAttribute("data-tip")).includes("reported"), "Today's rows should give their details on hover and focus");
  // Owner ("would be better to really have a summary ... have to find somehow the information everywhere", "still too much
  // bloated text in one screen", "a summary of why we should choose plan X over plan Y", "I really like the agent graph"; case summary
  // prototype rev 2): a band under the header in labels and numbers (what happened, its impact, the one
  // thing OCC must do, why this plan and not the next), every detail on hover, focus or tap. It replaces the status line and the
  // recommendation line; the plan cards keep three numbers (lose flight, no seat today, cost) and what each plan does.
  const band = page.locator("#summary");
  const bandWords = async (selector) => words(band.locator(selector));
  const hoverText = async (locator) => {
    await page.mouse.move(2, 2);
    await page.locator("#tip-card").waitFor({ state: "hidden" });
    await locator.hover();
    await page.locator("#tip-card").waitFor({ timeout: 5000 }).catch(() => { throw new Error(`No card on hover: ${locator}`); });
    const text = (await page.locator("#tip-card").innerText()).replace(/\s+/g, " ").trim();
    await page.mouse.move(2, 2);
    await page.locator("#tip-card").waitFor({ state: "hidden" });
    return text;
  };
  const deltaRow = async () => (await band.locator(".dt").evaluateAll(tiles => tiles.map(tile =>
    `${tile.querySelector(".wm")?.textContent} ${tile.textContent.replace(/[✓!]/g, "").replace(/\s+/g, " ").trim()}`))).join(" | ");
  check(await band.isVisible() && (await bandWords(".t-what")) === "Disruption Aircraft defect" &&
    (await bandWords(".t-impact")) === "Impact 4 flights 481 passengers affected 0 crews need cover €117,198 plan 2" &&
    (await bandWords(".t-todo")) === "Your action Approve plan 2 Before 10:30 · AR101",
    `The summary should say what happened, its impact and what OCC must do: ${await words(band)}`);
  check((await bandWords(".t-why .tl")) === "Why plan 2 · Delay, not cancel" && (await bandWords(".verdict")) === "247 more keep their flight, €17,224 more" &&
    (await deltaRow()) === "✓ Keep flight +247 | ! Cost +€17,224 | ! Compensation +91 | ! Overnight +36" &&
    (await band.locator(".dt.good").count()) === 1 && (await band.locator(".dt.bad").count()) === 3,
    `Why plan 2 and not plan 1 should read as a verdict and signed trade-offs: ${await bandWords(".t-why")} | ${await deltaRow()}`);
  check(await page.locator("#status-banner").isHidden() && (await page.locator("#rec-line").count()) === 0,
    "The summary should replace the status line and the recommendation line");
  const whatTip = await hoverText(band.locator(".t-what .big"));
  check(whatTip.includes("Hydraulic leak found on CS-TQA during the walk-around") && whatTip.includes("CS-TQA · 4 hours"), `The disruption should open the report on hover: ${whatTip}`);
  const flightsTip = await hoverText(band.locator('.stat[data-stat="flights"]'));
  check(flightsTip.includes("AR101 · AR102 · AR131 · AR132"), `The flights should be named on hover: ${flightsTip}`);
  const bandPaxTip = await hoverText(band.locator('.stat[data-stat="passengers"]'));
  check(/Owed care 481/.test(bandPaxTip) && /Owed compensation 331/.test(bandPaxTip) && /Lose flight or connection 77/.test(bandPaxTip) && bandPaxTip.includes("Plan 2") &&
    bandPaxTip.includes("At least 481 passengers") && bandPaxTip.includes("nor are all passengers booked"),
    `The passengers should say how the plan affects them: ${bandPaxTip}`);
  check((await hoverText(band.locator('.stat[data-stat="cost"]'))).includes("How €117,198 adds up"), "The recommended plan's cost should open its workings");
  const keepTip = await hoverText(band.locator('.dt[data-delta="keep"]'));
  check(/Plan 2 · Delay the rotation 77/.test(keepTip) && /Plan 1 · Cancel AR101 \+ AR102 324/.test(keepTip), `A trade-off should give both plans' figures: ${keepTip}`);
  const verdictTip = await hoverText(band.locator(".verdict"));
  check(verdictTip.includes("Plan 2 · Delay the rotation against Plan 1") && /Orchestrator Delaying the rotation protects more connections/i.test(verdictTip) &&
    verdictTip.includes("Lose flight or connection 324 77 −247") && verdictTip.includes("Indicative cost €99,974 €117,198 +€17,224") &&
    verdictTip.includes("fewest passengers losing their flight or connection"), `The verdict should open the full comparison: ${verdictTip}`);
  check((await page.locator(".plan .numbers > div").evaluateAll(cells => cells.map(cell => cell.innerText.replace(/\s+/g, " ").trim()))).join(" | ") ===
    "324 lose flight | 24 no seat today | €99,974 cost | 77 lose flight | 39 no seat today | €117,198 cost" &&
    (await page.locator(".plan .chk, .plan .plan-diff, .plan .plan-flights").count()) === 0,
    `Each plan card should keep three numbers and what the plan does: ${await words(page.locator("#plans"))}`);
  await page.mouse.move(2, 2);
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  const caseWords = await visibleWords("#recommendation, #decision-more");
  check(caseWords.count <= 140, `Case 3 at 1440 px should show at most 140 words above the fold (prototype rev 2), the Decision step's detail aside: ${caseWords.count} · ${caseWords.text}`);
  check((await bandOverlaps()).length === 0, `Nothing in the summary should overlap at 1440 px: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-summary-case3.png" });
  await band.getByRole("button", { name: "4 agents · graph ›" }).click();
  await page.locator("#step-assessed").waitFor();
  const graphWidth = await page.evaluate(() => [document.querySelector("#trail-graph").getBoundingClientRect().width, document.querySelector("#summary").getBoundingClientRect().width]);
  check((await stepButton("assessed").getAttribute("aria-selected")) === "true" && Math.abs(graphWidth[0] - graphWidth[1]) < 2,
    `4 agents · graph should open the Assessed step's agent graph, as wide as the case: ${graphWidth}`);
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  const phoneWords = await visibleWords("#recommendation, #decision-more");
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await bandOverlaps()).length === 0,
    `The summary should fit 390 px without overlaps: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-summary-case3.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  check(phoneWords.count <= 115, `Case 3 at 390 px should show no more words than before the summary: ${phoneWords.count} · ${phoneWords.text}`);
  // Reported: the message as written, ARROW's reading and the report of the same disruption again. The steps act as tabs: choosing
  // one never moves the summary or the steps, and Today stays beside the case.
  const bandPlace = await placeOf("#summary"), stepsPlace = await placeOf("#steps");
  await stepButton("reported").click();
  await page.locator("#step-reported").waitFor();
  check((await stepButton("reported").getAttribute("aria-selected")) === "true" && (await drawer.getAttribute("aria-labelledby")) === "steptab-reported" &&
    await page.locator("#step-decision").isHidden() && await page.locator("#workspace > .rail").isVisible() &&
    (await placeOf("#summary")) === bandPlace && (await placeOf("#steps")) === stepsPlace,
    "Reported should show under the steps, with the summary, the steps and Today where they were");
  check((await page.locator("#case-message").textContent()).includes("Hydraulic leak found on CS-TQA"), "The original message should be shown");
  // Owner: no quote marks around sentences; the report keeps its bar and who reported it.
  check(await page.evaluate(() => ["::before", "::after"].every(at => ["none", "normal"].includes(getComputedStyle(document.querySelector("#case-message"), at).content))),
    "The report should not be wrapped in quote marks");
  const repeats = await page.locator("#case-repeats").innerText();
  check(repeats.includes("Also reported by Maintenance (DSR-20261110-FFFFFF) · same disruption") && !/recorded twice/.test(repeats), `A repeated report should be noted on the case: ${repeats}`);
  // Owner: the report as received only (who, when, the text); the Detection agent reads it, under Assessed.
  check((await page.locator("#case-reading, #case-recorded").count()) === 0 && !(await page.locator("#step-reported").innerText()).includes("read it as"),
    "Reported should show only the report as received, no reading");
  check((await words(page.locator("#case-facts"))).includes("Reference DSR-20261110-AAAAAA"), "Reported should give the case's reference");
  // Another step replaces it in place; the step is in the link, Esc leaves it as it is, and the arrow keys move along the steps.
  await stepButton("assessed").click();
  await page.locator("#step-assessed").waitFor();
  check(await page.locator("#step-reported").isHidden() && (await placeOf("#summary")) === bandPlace && (await placeOf("#steps")) === stepsPlace,
    "Assessed should replace Reported under the steps, nothing else moving");
  await stepButton("reported").click();
  await page.locator("#step-reported").waitFor();
  check(new URL(page.url()).searchParams.get("step") === "reported", `The open step should be in the link: ${page.url()}`);
  await stepButton("reported").focus();
  await page.keyboard.press("Escape");
  check(await page.locator("#step-reported").isVisible() && new URL(page.url()).searchParams.get("step") === "reported",
    "Esc should leave the chosen step as it is");
  await page.keyboard.press("ArrowRight");
  check((await page.evaluate(() => document.activeElement?.dataset?.step)) === "assessed", "The arrow keys should move along the steps");

  // 2. The plans, calm (owner: "too much info", "here also not readable text"; prototype rev 2: the summary above
  // says why this plan): per card three numbers (lose flight, no seat today, cost), the route and what the plan does, each with its
  // detail on hover or focus; its checks in the Decision step and its flights in the Plans step; Approve on the recommended plan,
  // Choose on the other.
  await showPlans();
  const plans = page.locator(".plan");
  check((await plans.count()) === 2, "Two plans should be shown");
  check((await plans.nth(0).locator("h4").textContent()) === "Cancel AR101 + AR102", "The cheaper plan should rank first");
  const recommended = plans.nth(1);
  check((await words(recommended.locator(".ribbon"))) === "Recommended" && (await words(recommended.locator(".rank"))) === "2" &&
    (await plans.nth(0).locator(".ribbon").count()) === 0, "The recommended card should carry the ribbon");
  const [headBox, bandBox, stepsBox, plansBox] = await Promise.all([page.locator(".case-head"), band, page.locator("#steps"), page.locator("#plans")].map(box => box.boundingBox()));
  check(headBox.y + headBox.height <= bandBox.y + 1 && bandBox.y + bandBox.height <= stepsBox.y && stepsBox.y + stepsBox.height <= plansBox.y,
    "The summary should sit under the header, above the steps and the plans");
  check(await page.evaluate(() => [...document.querySelectorAll(".plan")].every((card, i, all) => Math.abs(card.getBoundingClientRect().top - all[0].getBoundingClientRect().top) < 2)),
    "Two plans should sit side by side at 1440 px");
  // The workings open in a card beside the figure, on hover or keyboard focus (tips.js data-card). A card left open by the
  // resting cursor would cover the next figure, and any scroll closes cards: park the cursor, then let the scroll settle.
  const cardOf = async (locator) => {
    await page.mouse.move(2, 2);
    await page.locator("#tip-card").waitFor({ state: "hidden", timeout: 5000 }).catch(async () => {
      throw new Error(`A card stayed open (${(await page.locator("#tip-card").innerText()).slice(0, 80)}) before ${locator}`);
    });
    await locator.scrollIntoViewIfNeeded();
    await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
    await locator.hover();
    const card = page.locator("#tip-card");
    await card.waitFor({ timeout: 5000 }).catch(() => { throw new Error(`No workings card on hover: ${locator}`); });
    const text = (await card.innerText()).replace(/[ \t]*\n[ \t]*/g, "\n").replace(/[ \t]+/g, " ");
    await page.mouse.move(2, 2);
    return text;
  };
  const tipOf = async (locator) => { await locator.hover(); const text = await page.locator("#tip").innerText(); await page.mouse.move(2, 2); return text; };
  // A tap on a phone: the element in view first (a scroll closes cards), then the tap (a click: no touch in this browser).
  // A redraw (a dialog closing re-renders the plans) may replace the element between the scroll and the tap: try again.
  const tap = async (locator) => {
    for (let attempt = 0; ; attempt++) {
      try {
        await locator.scrollIntoViewIfNeeded();
        await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
        await locator.click();
        return;
      } catch (error) {
        if (attempt >= 2 || !/not attached/.test(error.message)) throw error;
      }
    }
  };
  const verdict = band.locator(".verdict [data-card]");
  // A line of words opens its card below it (or above), never beside: the card never covers the line it explains (review;
  // prototype compare-case3-hover-rec).
  const meets = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
  // How many of the line's parts the card covers; a card beside the line (not below or above it) counts as covering it.
  const coversLine = async (anchor, parts) => {
    await anchor.hover();
    await page.locator("#tip-card").waitFor();
    const [card, line] = [await page.locator("#tip-card").boundingBox(), await anchor.boundingBox()];
    const boxes = await Promise.all(parts.map(part => part.boundingBox()));
    const clear = card.y >= line.y + line.height - 1 || card.y + card.height <= line.y + 1;
    return boxes.filter(box => box && meets(card, box)).length + (clear ? 0 : 1);
  };
  check((await coversLine(verdict, [band.locator(".t-why .tl"), verdict])) === 0, "The verdict's card should open below the line, not over it");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rec-hover.png" });
  await page.mouse.move(2, 2);
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  const cellsFit = async () => page.locator(".plan .numbers > div").evaluateAll(cells => cells.every(cell =>
    cell.scrollWidth <= cell.clientWidth + 1 && [...cell.children].every(child => child.getBoundingClientRect().right <= cell.getBoundingClientRect().right + 1)));
  check(await cellsFit(), "Two plan cards should fit every number at 1440 px");
  const cardNumbers = async (plan) => plan.locator(".numbers > div").evaluateAll(cells => cells.map(cell => cell.innerText.replace(/\s+/g, " ").trim()));
  // What a plan does, in labels and numbers (owner): its chips under the numbers.
  const stripOf = async (plan) => (await plan.locator(".pb .pb-chip").evaluateAll(items => items.map(item => item.textContent.replace(/\s+/g, " ").trim()))).join(" | ");
  check((await cardNumbers(recommended)).join(" | ") === "77 lose flight | 39 no seat today | €117,198 cost" &&
    (await cardNumbers(plans.nth(0))).join(" | ") === "324 lose flight | 24 no seat today | €99,974 cost",
    `Each plan should show who loses their flight or connection, who has no seat today and its cost: ${(await cardNumbers(recommended)).join(" | ")}`);
  check((await words(recommended.locator(".route"))) === "OCC" && (await recommended.locator(".route").getAttribute("aria-label")) === "OCC decides" &&
    (await words(plans.nth(0).locator(".route"))) === "OCC", "Each card should show its route as a short chip");
  const routeTip = await cardOf(recommended.locator(".route"));
  check(routeTip.includes("Why OCC decides") && (routeTip.match(/owed care under EU261/g) || []).length === 1 &&
    routeTip.includes("AR101, AR102 leave 3 h 30 min to 3 h 45 min late, so passengers are owed care under EU261") && routeTip.includes("39 passengers go to Passenger Services"),
    `The route should say why on hover, the long delays in one reason: ${routeTip}`);
  const noSeatTip = await cardOf(recommended.locator('.nb[data-num="noseat"]'));
  check(noSeatTip.includes("No seat today · 39 passengers") && noSeatTip.includes("Passenger Services rebooks them"), `No seat today should explain itself: ${noSeatTip}`);
  const owedTip = await cardOf(band.locator('.dt[data-delta="owed"]'));
  check(owedTip.includes("Passengers owed EU261 compensation") && /Plan 2 · Delay the rotation\s+331/.test(owedTip) && /Plan 1 · Cancel AR101 \+ AR102\s+240/.test(owedTip),
    `Owed compensation should compare the two plans on hover: ${owedTip}`);
  const loseTip = await cardOf(recommended.locator('.nb[data-num="lose"]'));
  check(loseTip.includes("Lose flight or connection · 77 passengers") && /Kept by car, not counted\s+35/.test(loseTip) && loseTip.includes("rebooked on our flights or left for Passenger Services"),
    `Lose flight or connection should explain itself: ${loseTip}`);
  const totalTip = await cardOf(recommended.locator('.nb[data-num="cost"]'));
  check(totalTip.includes("How €117,198 adds up") && /EU261 compensation\s+€85,050/.test(totalTip) && /Rebooking\s+€6,698/.test(totalTip) && /Total\s+€117,198/.test(totalTip) &&
    totalTip.includes("315 passengers × €250 · arrives 3 h or more late · up to 1,500 km") &&
    totalTip.includes("23 passengers × €168 · rerouted on another airline · 1,500–3,500 km or within the EU") &&
    totalTip.includes("Overnight: 36 passengers, 24 rooms × €150, 48 taxis × €19, meals 36 × €50") && totalTip.includes("€116,286–€118,158 with hotel season"),
    `Hovering the total should show how it adds up: ${totalTip}`);
  // Keyboard focus opens the same card; Esc closes the card first.
  const cancelTotal = plans.nth(0).locator('.nb[data-num="cost"]');
  await cancelTotal.scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  await cancelTotal.focus();
  await page.locator("#tip-card").waitFor();
  check((await page.locator("#tip-card").innerText()).includes("How €99,974 adds up"), "Keyboard focus should open the workings too");
  await page.keyboard.press("Escape");
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  check((await recommended.getByRole("button", { name: "Approve plan 2" }).count()) === 1 && (await words(recommended.locator(".plan-actions"))) === "Approve" &&
    (await plans.nth(0).getByRole("button", { name: "Choose plan 1" }).count()) === 1, "Each card's button should say only its verb, the plan in its name");
  check(!/PNR|TOBT|FDP/.test(await page.locator("#case-view").innerText()), "The case should avoid internal codes");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case3.png", fullPage: true });
  await professional("Plans");
  await recommended.locator('.nb[data-num="cost"]').hover();
  await page.locator("#tip-card").waitFor();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-cost-tip.png" });
  await page.mouse.move(2, 2);
  await page.setViewportSize({ width: 1440, height: 900 });
  // Task 4c: plans of up to six flights keep their rotation picture, with a compact table, behind one Flights row, in the Plans step.
  await openStep("plans");
  const planFlights = (n) => page.locator(`#step-plans .pl-plan[data-plan-no="${n}"] .plan-flights`);
  check((await words(planFlights(2).locator("summary"))) === "Flights 4 later · up to +3 h 25 min" &&
    (await words(planFlights(1).locator("summary"))) === "Flights 2 cancelled", "The flights should read in a few words");
  await planFlights(2).locator("summary").click();
  const delayFlights = await planFlights(2).locator("tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  check(delayFlights.join(" | ") === "AR101 LIS → OPO 10:30 → 13:45 +195 min | AR102 OPO → LIS 12:00 → 15:25 +205 min | AR131 LIS → FAO 14:10 → 17:05 +175 min | AR132 FAO → LIS 15:40 → 18:40 +180 min" &&
    (await planFlights(2).locator(".mini-rotation svg").count()) === 1, `The flights should open to a compact table: ${delayFlights.join(" | ")}`);
  await planFlights(2).locator("summary").click();
  // The checks each plan passed (Crew · Lisbon · Passengers), with their detail on hover, in the Decision step.
  await openStep("decision");
  const whoPlan = (n) => page.locator(`#step-decision .who-plan[data-plan-no="${n}"]`);
  check((await whoPlan(2).locator(".chk").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim()).join(" | ") === "✓ Crew | ✓ Lisbon | → Passengers",
    `Each plan should show its checks as three chips in the Decision step: ${(await whoPlan(2).locator(".chk").allInnerTexts()).join(" | ")}`);
  const crewTip = await cardOf(whoPlan(2).locator('.chk[data-check="crew"]'));
  check(crewTip.includes("Crew flight-time limits") && crewTip.includes("Within limit · 440 of 720 min") && crewTip.includes("D-LIS-101") && crewTip.includes("440 of 720 min") &&
    crewTip.includes("FDP: flight duty period"), `The Crew chip should give the duty against its limit on hover: ${crewTip}`);
  // D108: passengers a car keeps on their connection are counted, not disrupted.
  const paxTip = await cardOf(whoPlan(2).locator('.chk[data-check="passengers"]'));
  check(/Lose flight or connection\s+77/.test(paxTip) && /Rebooked\s+38/.test(paxTip) && /To Passenger Services\s+39/.test(paxTip) && /Kept by car\s+35/.test(paxTip) &&
    /Overnight\s+36/.test(paxTip) && paxTip.includes("Passenger Services decides for those without a same-day seat"), `The Passengers chip should list who goes where: ${paxTip}`);
  await showPlans();
  // Decision: the recommendation in full (owner), each plan by part with each part's workings on hover, what the other
  // plan does differently, who decides each plan, the cost breakdowns.
  await openStep("decision");
  await page.locator("#step-decision").waitFor();
  const rec = page.locator("#recommendation");
  const recLines = async (box = rec) => box.locator(".rec-lines > li").evaluateAll(items => items.map(item => item.textContent.replace(/\s+/g, " ").trim()));
  check((await words(rec.locator("h3"))) === "Recommended: Plan 2, Delay the rotation" && (await words(rec.locator(".rec-chip"))) === "OCC decides",
    `The recommendation should name the plan as its card does: ${await words(rec.locator(".rec-head"))}`);
  const caseA = (await recLines()).join(" | ");
  // Owner: who a plan affects beside its cost (lose their flight or connection, owed compensation), and why the dearer
  // plan costs more in one line, from its workings; a list, not a "word · word" chain (owner).
  check(caseA === "247 fewer lose their flight or connection than Cancel AR101 + AR102, 91 more owed compensation, €17,224 more | " +
    "Why Delay the rotation costs more: 331 passengers arrive 3 h or more late, so €85,050 of EU261 compensation | " +
    "Cancelling AR101 and AR102 keeps AR131 and AR132 on time but leaves 324 passengers to rebook. The delay keeps everyone flying; 4 flights leave 2 h 55 min to 3 h 25 min late. | " +
    "Cost difference: EU261 compensation +€24,600, passenger care +€6,194 (36 passengers in hotels), rebooking −€354, airline −€13,216", `The recommendation should explain itself from the plans' figures: ${caseA}`);
  const recSaid = await words(rec.locator(".said"));
  check(recSaid === "Orchestrator AI Delaying the rotation protects more connections and disrupts fewer passengers than cancellation." && !/[“”"]/.test(recSaid),
    `The orchestrator's reason should be attributed, not quoted: ${recSaid}`);
  check((await rec.locator(".against").count()) === 0 && (await band.locator(".rec-flag").count()) === 0, "A choice that follows the objectives should not be flagged");
  const parts = page.locator("#step-decision .cmp-parts");
  check((await parts.locator("tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()))).join(" | ") ===
    "Lose flight or connection 324 77 | Owed compensation 240 331 | Indicative cost €99,974 €117,198 | Airline €26,640 €13,424 | EU261 compensation €60,450 €85,050 | " +
    "Passenger care €5,832 €12,026 | Rebooking €7,052 €6,698", `Each plan should read by part: ${await words(parts)}`);
  const airlineTip = await cardOf(parts.locator('[data-plan-no="2"][data-part="airline"]'));
  check(airlineTip.includes("Airline €13,424") && airlineTip.includes("4 flights, 755 min late in total × €17.78 a minute (EUROCONTROL) = €13,424") &&
    airlineTip.includes("AR132 +180 min · €3,200"), `The airline part should show its workings: ${airlineTip}`);
  const cancelTip = await cardOf(parts.locator('[data-plan-no="1"][data-part="airline"]'));
  check(cancelTip.includes("2 narrow-body cancellations × €13,320 (EUROCONTROL) = €26,640"), `A cancellation should be priced per aircraft: ${cancelTip}`);
  const compensationTip = await cardOf(parts.locator('[data-plan-no="1"][data-part="compensation"]'));
  check(compensationTip.includes("150 passengers × €250 · cancelled, rebooked · up to 1,500 km") && compensationTip.includes("EU261: the EU air passenger rights regulation"),
    `Compensation should show who is owed what, and spell out EU261: ${compensationTip}`);
  // Cancellation and rebooking cost research §5: rebooking on our own flights, and passengers we cannot place sent on another airline.
  const rebookingTip = await cardOf(parts.locator('[data-plan-no="1"][data-part="rebooking"]'));
  check(rebookingTip.includes("Rebooking €7,052") && rebookingTip.includes("24 passengers × €150 · rerouted on another airline · up to 1,500 km") &&
    rebookingTip.includes("224 passengers × €11 · rebooked on our own flights · up to 1,500 km") && rebookingTip.includes("a sourced assumption, low confidence") && rebookingTip.includes("low-medium confidence"),
    `Rebooking should show who is rebooked or rerouted, at what, and how sure the figures are: ${rebookingTip}`);
  // The one-line driver keeps the full workings on hover: both plans' compensation.
  const driverTip = (await cardOf(rec.locator(".rec-why .why"))).replace(/\s+/g, " ");
  check(driverTip.includes("EU261 compensation +€24,600") && driverTip.includes("315 passengers × €250 · arrives 3 h or more late") &&
    driverTip.includes("150 passengers × €250 · cancelled, rebooked"), `The driver should open both plans' workings: ${driverTip}`);
  // The cost difference opens both plans' workings for that part, on hover and on keyboard focus.
  const careDiff = await cardOf(rec.locator('.rec-cost [data-part="care"]'));
  check(careDiff.includes("Passenger care +€6,194") && careDiff.includes("Overnight: 36 passengers, 24 rooms × €150") && careDiff.includes("Meals: 324 × €18"),
    `The care difference should show both plans' care: ${careDiff}`);
  const recCompensation = rec.locator('.rec-cost [data-part="compensation"]');
  await page.mouse.move(2, 2);
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await recCompensation.scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  await recCompensation.focus();
  await page.locator("#tip-card").waitFor();
  const compensationDiff = (await page.locator("#tip-card").innerText()).replace(/\s+/g, " ");
  check(compensationDiff.includes("EU261 compensation +€24,600") && compensationDiff.includes("Plan 2 · Delay the rotation €85,050") &&
    compensationDiff.includes("315 passengers × €250 · arrives 3 h or more late") && compensationDiff.includes("Plan 1 · Cancel AR101 + AR102 €60,450") &&
    compensationDiff.includes("150 passengers × €250 · cancelled, rebooked") && compensationDiff.includes("EU261: the EU air passenger rights regulation"),
    `Keyboard focus on a cost difference should open both plans' workings: ${compensationDiff}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rec-cost-card.png" });
  await page.keyboard.press("Escape");
  check(await drawer.isVisible() && await page.locator("#tip-card").isHidden(), "Esc should close the card and leave the step");
  check((await words(page.locator('#step-decision .plan-why[data-plan-no="1"]'))) ===
    "Plan 1: Cancel AR101 + AR102 vs Plan 2 Keeps AR131 and AR132 on time, €17,224 cheaper, 247 more lose their flight or connection, 91 fewer owed compensation",
    `The other plan should say what it does differently: ${await words(page.locator("#step-decision .plan-whys"))}`);
  const who = await words(page.locator('#step-decision .who-plan[data-plan-no="2"] .who'));
  // A Dev run: the EU261 sentence was repeated once per late flight.
  check(who.startsWith("Needs OCC approval because") && (who.match(/owed care under EU261/g) || []).length === 1 && who.includes("AR101, AR102 leave 3 h 30 min to 3 h 45 min late"),
    `Who decides should be explained, long delays as one reason: ${who}`);
  // The plan cards above carry Approve and Choose; Who decides adds Reject for each plan, never a second Approve.
  check((await page.locator("#plans").getByRole("button", { name: "Approve plan 2" }).count()) === 1 &&
    (await page.locator("#plans").getByRole("button", { name: "Choose plan 1" }).count()) === 1 &&
    (await page.locator("#decision-more").getByRole("button", { name: /^(Approve|Choose) plan/ }).count()) === 0 &&
    (await page.locator("#decision-more").getByRole("button", { name: "Reject" }).count()) === 2, "Decision should offer Approve, Choose and Reject once each");
  // The same workings on a phone or keyboard: the cost breakdown.
  await page.locator('#step-decision .cost-detail[data-plan-no="2"] > summary').click();
  const breakdown = (await page.locator('#step-decision .cost-detail[data-plan-no="2"] > p').first().textContent()).trim();
  check(breakdown === "Airline €13,424 · Compensation €85,050 · Care €12,026 (481 with meals, 36 overnight) · Rebooking €6,698 · €116,286–€118,158 with hotel season · indicative, not a quote",
    `The cost breakdown should name its four parts: ${breakdown}`);
  check((await page.locator('#step-decision .cost-detail[data-plan-no="2"]').innerText()).includes("Meals: 300 × €10 · waits 3–4 h"), "The cost breakdown should list the workings");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-decision.png" });
  await professional("Decision");
  await page.setViewportSize({ width: 1440, height: 900 });
  // The Plans step: every plan in a row, its numbers and route (and its flights below); a row leads to its card.
  await openStep("plans");
  check((await page.locator("#step-plans .pl-row").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()))).join(" | ") ===
    "1 Cancel AR101 + AR102 324 lose 24 no seat €99,974 OCC decides | 2 Delay the rotation Recommended 77 lose 39 no seat €117,198 OCC decides",
    `The Plans step should list every plan: ${await words(page.locator("#step-plans .pl-rows"))}`);
  check((await page.locator("#plans-note").textContent()) === "Ranked by indicative cost; ARROW's orchestrator recommends one.", "The Plans step should say how the plans are ranked");
  await page.locator('#step-plans .pl-row[data-plan="1"]').click();
  check(await plans.nth(0).evaluate(el => el.classList.contains("flash")), "A row of the Plans step should mark its card");

  // 3. Approving previews each system change, then records the decision with the version.
  await recommended.getByRole("button", { name: "Approve plan 2" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const effects = await page.locator("#decision-effects").textContent();
  // Owner: the preview names each recipient as What ARROW did and the trail do.
  check(effects.includes("Operations control · retime AR101, AR102, AR131, AR132") && effects.includes("EUROCONTROL Network Manager · delay messages for AR101, AR102") &&
    effects.includes("Passenger system · rebook 38 passengers") && effects.includes("Crew system · update the crew duties") &&
    effects.includes("39 passengers outside the rebooking policy"), `The preview should list the planned actions in words: ${effects}`);
  check(effects.includes("Lisbon airport (A-CDM) · new ready times for AR101, AR131") && !effects.includes("at LIS"), `Ready times should name the airport once: ${effects}`);
  check(effects.includes("Ground handling · drive 35 passengers to their connecting flight"), `The direct transfers should be previewed: ${effects}`);
  await professional("Approval preview");
  // Owner: OCC can add a comment when it decides; it is sent with the decision and shown under Who decides.
  check(await page.locator("#decision-comment").inputValue() === "", "The comment box should open empty");
  await page.locator("#decision-comment").fill("  Crew control agrees with the delay  ");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-approval-comment.png" });
  await page.locator("#decision-confirm").click();
  await band.locator(".t-todo.action").waitFor();
  check(await page.locator("#status-banner").isHidden() && (await words(page.locator(".plan.chosen .ribbon"))) === "Approved by OCC" &&
    (await page.locator("#summary-live").textContent()) === "Action needed Review 39 passengers Passenger Services · no seat today",
    `The case should record OCC's approval on the plan it ran, and announce the action needed: ${await page.locator("#summary-live").textContent()}`);
  const approval = requests.find(r => r.path.endsWith("/approval"));
  check(approval.body.option_id === "plan-delay" && approval.body.decision === "approve" && approval.body.expected_version === 7 &&
    approval.body.idempotency_key.startsWith("decision:") && approval.headers["x-arrow-request"] === "operator", "The approval should carry the version and idempotency key");
  check(approval.body.comment === "Crew control agrees with the delay", `The approval should carry OCC's comment, trimmed: ${approval.body.comment}`);
  const occSaid = await page.locator('#decision-more .who-plan[data-plan-no="2"] .who-said').textContent();
  check(occSaid === "OCC comment“Crew control agrees with the delay”", `Who decides should show OCC's comment on the plan it approved: ${occSaid}`);
  await stepButton("carried").filter({ hasText: "39 wait for Passenger Services" }).waitFor();
  check(await stepRow() === "Reported:done Assessed:done Plans:done Rules:done Decision:done Carried out:current" &&
    (await words(stepButton("decision"))) === "✓ Decision Approved by OCC", `The steps should move on to Carried out: ${await stepRow()}`);
  // Owner ("the passenger service review is too much hidden"; prototype rev 2): the summary says an action is still needed,
  // in amber, and leads to Carried out, where Passenger Services marks them handled; the plan card keeps no faint note of it.
  const actionNeeded = band.locator(".t-todo.action");
  check(await actionNeeded.isVisible() && (await words(actionNeeded)) === "Action needed Review 39 passengers Passenger Services · no seat today" &&
    (await actionNeeded.locator(".amber-btn").evaluate(el => getComputedStyle(el).backgroundImage)).includes("rgb(245, 165, 36)") &&
    (await bandWords(".t-why .tl")) === "Why plan 2 · Delay, not cancel",
    `The summary should say in amber that Passenger Services still has passengers: ${await words(band)}`);
  // Once a plan ran, the main view shows it; the plan not chosen folds into one line.
  check((await plans.count()) === 1 && (await words(page.locator(".plan.chosen .ribbon"))) === "Approved by OCC" &&
    (await words(page.locator(".others-fold h3"))) === "Other options checked (1)" && (await words(page.locator(".others-fold .why"))) === "Not chosen: OCC approved Plan 2" &&
    (await words(page.locator(".plan.chosen .plan-actions"))) === "✓ Carried out · 10 of 10 confirmed Details ›",
    `The plan that ran should lead, the other folded: ${await words(page.locator("#plans"))}`);
  await page.locator(".plan.chosen").getByRole("button", { name: "Details ›" }).click();
  await page.locator("#done").filter({ hasText: "What ARROW did" }).waitFor();
  check((await page.locator("#done").textContent()).includes("39 passengers could not be rebooked"), "The expert review should be visible");
  // Owner: what ARROW did starts high level (one line, one row per outcome in words) and drills down.
  const done = page.locator("#done");
  check((await words(done.locator(".done-line"))) === "✓ All 10 changes confirmed by the airline systems", `One line should say how many changes the systems confirmed: ${await words(done.locator(".done-line"))}`);
  const outcomes = await done.locator(".done-group > summary").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check(outcomes.join(" | ") === "✓ Flights 4 flights now leave later +2 h 55 min to +3 h 25 min Confirmed | " +
    "✓ Air traffic control and airports 4 delay messages to EUROCONTROL · 2 departure times to Lisbon airport Confirmed | ✓ Crew 2 crew duties updated Confirmed | " +
    "✓ Passengers 38 passengers rebooked onto later flights · 35 passengers keep their connection with a car (5 connections) Confirmed",
    `Each outcome should read in plain words: ${outcomes.join(" | ")}`);
  check((await done.locator("details.done-group[open]").count()) === 0 && !(await done.locator(".done-detail li").first().isVisible()), "The actions should wait behind each outcome");
  const flightsDone = done.locator('details[data-group="retimed"]');
  await flightsDone.locator("summary").click();
  const retimedRows = await flightsDone.locator(".done-table tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  check(retimedRows.join(" | ") === "AR101 10:30 13:45 +3 h 15 min | AR102 12:00 15:25 +3 h 25 min | AR131 14:10 17:05 +2 h 55 min | AR132 15:40 18:40 +3 h",
    `Retimed flights should show was and now: ${retimedRows.join(" | ")}`);
  const passengersDone = done.locator('details[data-group="passengers"]');
  await passengersDone.locator("summary").click();
  check((await passengersDone.locator(".done-table").innerText()).includes("AR102\tAR861\t12\t20 min, passport control"), "The cars should list each connection kept");
  // The header keeps the End-to-end trace and Open in Foundry, whichever step is open.
  const traceLink = await page.locator("#case-trace").getAttribute("href");
  await page.waitForFunction(() => (document.querySelector("#case-foundry")?.getAttribute("href") || "").includes("version="));
  check(await page.locator("#case-trace").isVisible() && /^\/api\/cases\/[0-9a-f]{32}\/trace$/.test(traceLink || "") &&
    (await page.locator("#case-foundry").getAttribute("href")) === agentLink("orchestrator", "5") && await page.locator("#case-foundry").isVisible(),
    `The case should link its end-to-end trace and Foundry: ${traceLink}`);
  const shown = await page.locator("body").innerText();
  check(!/mocked|synthetic/i.test(shown), "The screen should not say mocked or synthetic");
  await done.locator('details[data-group="atc"] summary').click();
  await professional("What ARROW did");
  await done.locator("li").filter({ hasText: "EUROCONTROL Network Manager" }).first().hover();
  const tip = await page.locator("#tip").innerText();
  check(tip.includes("EUROCONTROL's Network Manager") && tip.includes("DLA: Delay message") && tip.includes("EOBT: Estimated off-block time"), `Hovering an action should explain it: ${tip}`);
  check(!(await done.innerText()).includes("Network manager"), "An older action should use the recipient's name as the newer calls do");
  await page.mouse.move(2, 2);
  // Passengers waiting for Passenger Services are their work, not an OCC decision (a Dev run).
  await showPlans();
  await page.locator(".case-group.expert .case-row").filter({ hasText: "Action needed · 39 passengers for Passenger Services" }).waitFor();
  // Owner (prototype rev 2): Today and the control tower group what still needs an action under Action needed.
  check((await page.locator(".case-group.expert h2").textContent()).startsWith("Action needed"), "The review should have its own group, Action needed");
  check((await page.locator(".case-group.expert .case-row .act-needed").evaluate(el => getComputedStyle(el).color)) === "rgb(245, 165, 36)",
    "Today should say in amber that an action is still needed");
  check((await page.locator(".case-group.needs .case-row").count()) === 1, "Needs you should keep only the manual case");
  // The control tower's row says so too, under its Action needed group.
  await page.locator("#tab-tower").click();
  const reviewRow = page.locator(`#tower-rows tr[data-trace="${A}"] .tw-state`);
  await reviewRow.filter({ hasText: "Action needed" }).waitFor();
  check((await words(reviewRow)) === "Action needed · 39 passengers for Passenger Services" && (await reviewRow.evaluate(el => el.classList.contains("act"))),
    `The tower row should say an action is still needed: ${await words(reviewRow)}`);
  const actionGroup = await words(page.locator('#tower [data-kpi="review"]'));
  check(actionGroup === "39 Action needed passengers for Passenger Services · AR101 39" && (await words(page.locator("#tower .tw-legend"))).includes("Action needed"),
    `The tower should group the passengers still waiting under Action needed: ${actionGroup}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-tower-action.png" });
  await page.locator("#tab-workspace").click();
  await page.locator(".others-fold").waitFor();
  await page.locator(".others-fold").getByRole("button", { name: "Show them" }).click();
  check((await plans.count()) === 2 && (await words(page.locator(".plan.other .ribbon"))) === "Not chosen", "Show them should unfold the other plan");
  await page.getByRole("button", { name: "Hide other options (1)" }).click();
  check((await plans.count()) === 1, "Hide should fold the other plan again");
  await openStep("carried");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-executed.png" });
  await page.setViewportSize({ width: 1440, height: 900 });

  // 4. Assessed (owner review: "the graph is not that nice anymore, cna we make it more readable, elss AI bloated text and
  // visible boxes etc, also when hovering over nice UX/UI?"; v2's purpose kept: who was asked, in what order, what each concluded,
  // what it read or wrote, how the plans were routed): every step a visible card with its name, one key result and badges (the
  // autonomy level, reads and writes with their counts, a dot per system called); the rest on its card on hover, focus or tap; a
  // click opens one panel below the graph; the conversation on demand.
  await openStep("assessed");
  await page.locator(".flow .node").first().waitFor();
  const step = (id) => page.locator(`.flow .node[data-node="${id}"]`);
  // Any scroll closes the cards (a resize settles the case's scroll after it): a card is opened once the scrolling has settled.
  const settled = () => page.evaluate(() => new Promise(done => {
    const stop = new AbortController();
    const quiet = () => { clearTimeout(quiet.timer); quiet.timer = setTimeout(() => { stop.abort(); done(); }, 200); };
    addEventListener("scroll", quiet, { capture: true, signal: stop.signal });
    quiet();
  }));
  // A step's card on hover: in view first, then the hover.
  const hoverCard = async (locator) => {
    await locator.scrollIntoViewIfNeeded();
    await settled();
    await locator.hover();
    await page.locator("#tip-card").waitFor();
  };
  const valueOf = async (id) => words(step(id).locator(".gn-val"));
  const badgesOf = (id) => step(id).evaluate((el) => [el.querySelector(".gn-chip.lvl")?.textContent || "-", el.querySelector(".gn-r")?.textContent || "-",
    el.querySelector(".gn-p")?.textContent || "-", el.querySelector(".gn-w")?.textContent || "-", el.querySelectorAll(".gn-dots i").length].join(" "));
  const boxes = await page.locator(".flow .node").evaluateAll(nodes => nodes.map(el => {
    const css = getComputedStyle(el);
    return `${parseFloat(css.borderTopWidth) >= 1 && css.borderTopStyle !== "none" && css.backgroundColor !== "rgba(0, 0, 0, 0)" ? "box" : "bare"}`;
  }));
  check(await page.locator(".flow svg.wires").isVisible() && boxes.length === 11 && boxes.every(box => box === "box"),
    `Every step should be a visible card, wired: ${boxes.join(" ")}`);
  // Layout A (the owner's choice): left to right in four columns, the report over the orchestrator, the agents, the plans,
  // ARROW's rules over the outcome; each fan of wires runs on one trunk, so no two wires cross.
  const graphColumns = await page.locator(".flow > .col").evaluateAll(cols => cols.map(col => ({
    heads: [...col.querySelectorAll(":scope > h4")].map(h4 => h4.textContent).join("+"),
    steps: [...new Set([...col.querySelectorAll(".node[data-node]")].map(node => node.dataset.node.split(":")[0]))].join(","),
    left: col.getBoundingClientRect().left, right: col.getBoundingClientRect().right })));
  const trunks = await page.evaluate(() => {
    const fans = {};
    for (const path of document.querySelectorAll("svg.wires path.wire")) {
      const x = (/ Q(-?[\d.]+),/.exec(path.getAttribute("d")) || [])[1];
      if (x) (fans[`${path.dataset.a.split(":")[0]}>${path.dataset.b.split(":")[0]}`] ||= new Set()).add(Math.round(Number(x)));
    }
    return Object.entries(fans).map(([fan, xs]) => `${fan} ${xs.size}`).sort().join(" | ");
  });
  const layout = graphColumns.map(col => `${col.heads}: ${col.steps}`).join(" | ");
  check(layout === "Report+Orchestrator: message,orchestrator | Agents: agent | Plans: plan | Rules+Outcome: rules,outcome" &&
    graphColumns.every((col, i) => !i || col.left >= graphColumns[i - 1].right) && trunks === "agent>plan 1 | orchestrator>agent 1 | plan>rules 1",
    `The graph should run left to right in four columns, each fan of wires on one trunk: ${layout} · ${trunks}`);
  const values = await page.locator(".flow .node .gn-val").evaluateAll(cells => cells.map(cell => cell.textContent.replace(/\s+/g, " ").trim()));
  check(values.join(" | ") === "Aircraft defect | Recommends plan 2 | CS-TQA out of service 4 h | 2 options | No duty breach | 112 lose flight | Not asked | " +
    "Cancel | Delay +15 min | 2 of 8 to OCC | Carried out", `Each step should show one key result: ${values.join(" | ")}`);
  // Labels and numbers, not sentences (owner): no "word · word" chain and at most six words a step.
  const graphText = await page.locator(".flow").innerText();
  check(!graphText.includes("·") && values.every(value => value.split(" ").length <= 6), `The graph should read as labels and numbers: ${graphText}`);
  const names = await page.locator('.flow .node[data-node^="agent:"] .gn-name').allInnerTexts();
  check(names.join("|") === "Disruption Detection|Aircraft Recovery|Crew Recovery|Passenger Re-accommodation|Slot Negotiation" &&
    (await words(step("orchestrator").locator(".gn-name"))) === "Recovery Orchestrator",
    `The agents should go by their scenario names, "Agent" on their cards: ${names.join("|")}`);
  // Who was asked and in what order; what each read, recorded or wrote, by count; the Detection agent's record of the report is
  // a recorded report, not a write (owner: "i thought agents only read").
  const askOrder = await page.locator('.flow .node[data-node^="agent:"]').evaluateAll(nodes => nodes.map(el => el.querySelector(".gn-no")?.textContent || "-").join(" "));
  check(askOrder === "1 2 3 4 -", `The agents asked should be numbered in the order asked: ${askOrder}`);
  const badges = { crew: await badgesOf("agent:crew"), passenger: await badgesOf("agent:passenger"), detection: await badgesOf("agent:detection"),
    aircraft: await badgesOf("agent:aircraft"), slot: await badgesOf("agent:slot") };
  check(badges.crew === "L3 2 - - 1" && badges.passenger === "L3 3 - - 2" && badges.detection === "L4 - 1 - 1" && badges.aircraft === "L2 1 - - 1" &&
    badges.slot === "L3 - - - 0" && (await step("agent:slot").getAttribute("class")).includes("gn-idle") && (await page.locator(".flow .gn-w").count()) === 1,
    `Each agent should show its autonomy level, its reads, recorded reports and writes and the systems it called: ${JSON.stringify(badges)}`);
  check((await page.locator(".flow .gn-chip.ai, .flow .gn-chip.code").count()) === 0 && (await step("orchestrator").locator(".gn-chip").count()) === 0 &&
    (await step("rules").locator(".gn-chip").count()) === 0 &&
    (await step("rules").locator(".gn-w").count()) === 0 && (await step("plan:plan-delay").locator(".gn-star").count()) === 1 &&
    (await words(step("plan:plan-delay").locator(".route"))) === "OCC", "No AI or Code chips on the graph; the recommended plan starred with its route");
  const summaryLine = await page.locator("#trail-stats").innerText();
  check(/4\s*agents/.test(summaryLine) && /8\s*system calls/.test(summaryLine) && /8\s*rule checks/.test(summaryLine) && /10\s*actions/.test(summaryLine) &&
    /41\s*s/.test(summaryLine) && (await page.locator("#trail-stats a").count()) === 0, `One summary line should count agents, calls, checks, actions and seconds: ${summaryLine}`);
  check(await page.locator("#case-trace").isVisible() && await page.locator("#case-foundry").isVisible(), "The trace and Foundry buttons should stay in the header beside the graph");
  check((await page.locator("#trail-graph .trail-steps").count()) === 0 && !(await page.locator("#trail-graph").innerText()).includes("Step by step"),
    "The graph should have no step list");
  check(!/mocked|synthetic|simulated/i.test(await page.locator("#step-assessed").innerText()), "Assessed should not say mocked, synthetic or simulated");
  await page.waitForFunction(() => document.querySelectorAll("svg.wires path.wire").length >= 8);
  await professional("How ARROW decided");
  // Agents answer at the reported delay; a plan may add minutes for capacity, so its figures differ: its card says why.
  const addedCard = await cardOf(step("plan:plan-delay"));
  check(addedCard.includes("Delay the rotation") && addedCard.includes("+15 min after the agents answered, so its figures can differ from theirs") &&
    addedCard.includes("an OCC duty manager decides") && !addedCard.includes("·"), `The plan's card should say why its figures differ from the agents': ${addedCard}`);
  // Hover, or keyboard focus, shows the agent's card: its full name, what its autonomy level means, model, identity, what it
  // concluded for each option, and each system call with read or write and its time; a way to Foundry. The Entra ID itself
  // waits in the panel's Interface section. The step's own wires light up.
  const agentCard = page.locator("#tip-card");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await hoverCard(step("agent:crew"));
  const cardText = await agentCard.innerText();
  check(!cardText.includes("00000000-0000-4000-8000-000000000004") && cardText.startsWith("Crew Recovery Agent") && /Identity\s*Own Entra agent identity/.test(cardText) &&
    /Model\s*GPT-4.1 mini/.test(cardText) && !/prompt v\d|gpt-4/.test(cardText) && cardText.includes("Acts within pre-approved rules; outside them OCC decides.") &&
    /Delay\s*No duty breach\s*Cancel\s*No duty breach/.test(cardText) && /Crew system\s*2 reads/.test(cardText) &&
    /Crew impact\s*READ\s*\d{2}:\d{2}:\d{2}/.test(cardText) && cardText.includes("Checks each option against crew duty limits") &&
    cardText.includes("finds standby crews") && cardText.includes("Open in Foundry") && (await agentCard.locator("dt .mk-ai").count()) === 1,
    `The card should give the agent's level, model, identity, conclusion per option and its calls: ${cardText}`);
  const lit = await page.locator("svg.wires path.wire.hot").evaluateAll(paths => paths.map(path => `${path.dataset.a}>${path.dataset.b}`));
  check(lit.length === 3 && lit.every(wire => wire.includes("agent:crew")), `Hovering a step should light its own wires: ${lit.join(" ")}`);
  await professional("Agent card");
  // Owner: "open in foundry link does not work": the link opens the agent itself, at the version the case ran.
  check((await agentCard.locator("a.tv-link").getAttribute("href")) === agentLink("crew", "3"), "Open in Foundry should open the agent itself at its version");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-trail-card.png" });
  await page.mouse.move(2, 2);
  await agentCard.waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator('.node[data-node="agent:detection"]').scrollIntoViewIfNeeded();
  await page.locator('.node[data-node="agent:detection"]').focus();
  await agentCard.waitFor();
  const detectionCard = await agentCard.innerText();
  check(detectionCard.includes("Reads the report, records the disruption in ARROW") && /ARROW\s*1 report recorded/.test(detectionCard) &&
    /Report recorded\s*REPORT/i.test(detectionCard) && !/\bwrite\b/i.test(detectionCard) &&
    /Event\s*EV-9C2D41/.test(detectionCard) && detectionCard.includes("4 flights affected, up to +3 h 25 min if nothing changes") &&
    detectionCard.includes("Autonomous: acts on its own"), `Keyboard focus should show the card too, with the record it made: ${detectionCard}`);
  check((await agentCard.locator("a.tv-link").innerText()).trim() === "Open in Foundry ↗" && (await agentCard.locator("a.tv-link").getAttribute("href")) === agentLink("detection", "3"),
    "Each agent's link should open that agent in Foundry");
  await page.keyboard.press("Escape");
  await agentCard.waitFor({ state: "hidden" });
  // Owner: a click opens one panel below the graph: the agent's input and output on the left, its tool calls on the right,
  // its instructions behind a toggle in the header.
  await page.locator('.node[data-node="agent:crew"]').click();
  const panel = page.locator("#trail-detail");
  let crew = await panel.innerText();
  check(/input/i.test(crew) && /output/i.test(crew) && /tool calls/i.test(crew) && !/question and full answer/i.test(crew),
    `The panel should hold Input, Output and Tool calls: ${crew.slice(0, 300)}`);
  check(crew.includes("Crew Recovery Agent") && crew.includes("No duty breach") && crew.includes("Recommends no standby crew") && crew.includes("No duty breaks its limit."),
    `The output should read in a few lines: ${crew.slice(0, 300)}`);
  check(crew.includes("GPT-4.1 mini") && crew.includes("Instructions") && !/prompt v\d/.test(crew) && !crew.includes("You are the Crew agent") && !/[0-9a-f]{32}/.test(crew),
    "The panel should name the model and offer the instructions, closed, never a prompt version or the trace id");
  check(crew.includes("Assess crew limits for event EV-9C2D41, the delay option.") && !crew.includes("event_id") && !crew.includes(", and."),
    `The input should be the question as asked, the disruption by its event id: ${crew.slice(0, 400)}`);
  const outputText = await panel.locator(".tv-part").filter({ has: page.locator("h5", { hasText: "Output" }) }).evaluate(part => {
    const copy = part.cloneNode(true); copy.querySelectorAll("details").forEach(d => d.remove()); return copy.textContent;
  });
  check(!/[{}]/.test(outputText), `The output should not show JSON: ${outputText}`);
  await panel.locator("summary", { hasText: "Raw answer" }).click();
  check((await panel.locator(".tv-raw pre").textContent()).includes('"constraint_status": "pass"'), "Raw answer should keep the agent's JSON");
  const toggle = panel.locator("[data-toggle]");
  await toggle.click();
  const prompt = panel.locator(".tv-prompt");
  check((await toggle.getAttribute("aria-expanded")) === "true" && (await prompt.innerText()).includes("You are the Crew agent. Use only your tools."), "The toggle should open the instructions");
  const [promptBox, partsBox] = [await prompt.boundingBox(), await panel.locator(".tv-parts").boundingBox()];
  check(promptBox.y < partsBox.y && promptBox.width > partsBox.width - 4, "The instructions should open full width under the header");
  await toggle.click();
  check(await prompt.isHidden(), "The toggle should close the instructions");
  const crewCalls = panel.locator("details.tv-call");
  check((await crewCalls.count()) === 2, "Both crew calls should be listed");
  // Owner: each call leads with what was done, in words, with its key detail, then the system, read or write, the result
  // and the time; the interface (tool, method, host, the path with the case's values, the caller's ID) only once the row is opened.
  const firstCall = (await crewCalls.first().locator("summary").innerText()).replace(/\s+/g, " ");
  check(firstCall.includes("Crew impact Delay, no duty over its limit") && firstCall.includes("Crew system") && /\bread\b/.test(firstCall) && !firstCall.includes("WRITE") &&
    firstCall.includes("ok") && !/MCP|assess_crew_recovery|ca-airrec|\/api\//.test(firstCall), `A call row should lead with what it was for and its result, the system, access: ${firstCall}`);
  const readTip = await tipOf(crewCalls.first().locator(".tv-acc-b"));
  check(readTip.includes("read roles") && readTip.includes("never changes"), `The read badge should say agents only read: ${readTip}`);
  check(await crewCalls.first().locator(".iface").isHidden(), "The interface should wait behind the row");
  await crewCalls.first().locator("summary").click();
  const opened = await crewCalls.first().innerText();
  const iface = (await crewCalls.first().locator(".iface").innerText()).replace(/\s+/g, " ");
  // Owner: "slash mcp at the end, really? dev hardcoded in the url??"; later, a REST route under an agent's tool call read as a
  // second call. The tool call and the caller's ID, never a host or a route.
  check(/^Interface/i.test(iface) && iface.includes("Tool call (MCP) to Crew system · assess_crew_recovery") &&
    !/Runs|\/api\/|ca-airrec|\/mcp\b|\{|defects/.test(iface) && iface.includes("00000000-0000-4000-8000-000000000004"),
    `The opened row's Interface should give the tool and the caller's ID, no route: ${iface}`);
  check(opened.includes("By Crew Recovery Agent") && opened.includes("EV-9C2D41") && opened.includes("No duty breach · D-LIS-101 440 of 720 min") &&
    /sent/i.test(opened) && /returned/i.test(opened) && !opened.includes("DSR-20261110-AAAAAA"),
    `An opened call should say who called, what was sent (the event, never the case) and what came back: ${opened}`);
  check((opened.match(/assess_crew_recovery/g) || []).length === 1 && !/ca-airrec|\/mcp\b/.test(opened), `The tool should appear once, no host: ${opened}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-trail-call.png" });
  const [sent, returned] = await crewCalls.first().locator(".tv-io2 > section").evaluateAll(parts => parts.map(part => part.getBoundingClientRect().toJSON()));
  check(Math.abs(sent.top - returned.top) < 2 && returned.left > sent.right, "Sent and Returned should sit side by side");
  check((await crewCalls.nth(1).locator("summary").innerText()).includes("Standby crews Delay, none needed"), "Each call should say what it was for and found");
  await crewCalls.nth(1).locator("summary").click();
  check((await crewCalls.nth(1).innerText()).includes("not recorded yet"), "A response not recorded should say so");
  // The Aircraft agent (answers in newer runs): each option with its effect, the levers it also checked, when the aircraft is back.
  await page.locator('.node[data-node="agent:aircraft"]').click();
  const aircraftPanel = await panel.innerText();
  check(aircraftPanel.includes("Cancel through a safe aircraft-position boundary") && aircraftPanel.includes("AR101–AR102 cancelled; AR131 and AR132 on time") &&
    aircraftPanel.includes("Agent's choice"), `Each option should say what it does to the rotation: ${aircraftPanel}`);
  const levers = await panel.locator(".tv-levers li").allInnerTexts();
  check(levers.length === 2 && levers[0].replace(/\s+/g, " ").startsWith("Aircraft swap not available No other serviceable A320neo") &&
    levers[1].replace(/\s+/g, " ") === "Cancellation available Through LIS", `Also checked should list each lever and why: ${levers.join(" | ")}`);
  check((await tipOf(panel.locator(".tv-lever-why").first())).includes("(99 checked)"), "A long reason should read in full on hover");
  check(aircraftPanel.includes("Aircraft available from 13:45") && !aircraftPanel.includes("case_id"), "The panel should say when the aircraft is back");
  check(aircraftPanel.includes("out of service for 4 hours, until 13:45. List the recovery options for event EV-9C2D41.") && !aircraftPanel.includes("2026-11-10T"),
    `A date-time at a sentence's end should read as the time: ${aircraftPanel.slice(0, 400)}`);
  const aircraftCall = panel.locator("details.tv-call").first();
  await aircraftCall.locator("summary").click();
  const aircraftReturned = await aircraftCall.locator(".tv-io2 > section").nth(1).innerText();
  check(aircraftReturned.includes("No other serviceable A320neo") && aircraftReturned.includes("A320neo"), `The Schedule tool's answer should list what it checked: ${aircraftReturned}`);
  await page.locator('.node[data-node="agent:passenger"]').click();
  const passengerPanel = await panel.innerText();
  check(passengerPanel.includes("Instructions not available") && passengerPanel.includes("53 passengers cannot be rebooked within 6 h. 24 passengers cannot be rebooked after the cancellation."),
    `Missing instructions should say so, and two answers read as words: ${passengerPanel.slice(0, 400)}`);
  check(passengerPanel.includes("Assess passengers for event EV-9C2D41, the delay option. Case DSR-20261110-AAAAAA."),
    `An id the question names after a full stop should start its sentence: ${passengerPanel.slice(0, 400)}`);
  // The Passenger agent reports the EU261 exposure ARROW's code computed, the same figures as the plan priced (owner): one
  // row per option, without a mark; the agent's own words carry the one AI mark (owner: no "Code" marks).
  const exposureLines = (await panel.locator(".tv-exposure > *").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim());
  check((await words(panel.locator(".tv-sub"))) === "EU261 exposure" && exposureLines.join(" | ") === "Delay +15 min | 331 owed compensation, €85,050, care €12,026, rebooking €6,698 | " +
    "Cancel | 240 owed compensation, €60,450, care €5,832, rebooking €7,052", `The Passenger agent should show the exposure per option: ${exposureLines.join(" | ")}`);
  check((await panel.locator(".mk-code").count()) === 0 && (await panel.locator(".tv-part .mk-ai").count()) === 1 &&
    (await tipOf(panel.locator(".tv-ai .mk-ai").first())).includes("Written by an AI agent"),
    "An agent's own words should carry one AI mark, computed figures none (EU AI Act transparency)");
  await page.locator('.node[data-node="rules"]').click();
  check((await panel.textContent()).includes("Aircraft change or standby"), "The rules panel should list the rules by name");
  // Owner review: a check that does not pass routes the plan; it is not a failed check. The plan by its card's name.
  const rulesDecision = (await panel.locator(".tv-part").filter({ has: page.locator("h5", { hasText: "Decision" }) }).innerText()).replace(/\s+/g, " ");
  check(rulesDecision.includes("Plan 2: Delay the rotation Recommended") && rulesDecision.includes("1 of 4 checks sends it to OCC, 1 to Passenger Services") &&
    !/checks pass|Fly the rotation late/.test(rulesDecision), `The rules panel should say what the checks did, the plan by its card's name: ${rulesDecision}`);
  check(/^Rules How (each|the) plan was routed Rules v1$/i.test((await panel.locator(".tv-head").innerText()).replace(/\s+/g, " ").trim()),
    `The rules panel should be headed plainly, with the rules' version: ${await panel.locator(".tv-head").innerText()}`);
  // ARROW's calls before OCC approved built the plans; from the approval on they are the executor's rechecks and writes. Recording
  // what was reported is intake, never a rule (owner: "why do I have a write action in the rules execution?").
  const outcomeCard = await cardOf(step("outcome"));
  check((await valueOf("rules")) === "2 of 8 to OCC" && (await step("rules").locator(".gn-calls").count()) === 0 && (await badgesOf("outcome")) === "- 2 - 1 1" &&
    /Operations control\s*1 write, 2 reads/.test(outcomeCard) && !outcomeCard.includes("Maintenance"),
    `ARROW's rules should hold no write, the executor's calls split at the approval: ${await valueOf("rules")} | ${await badgesOf("outcome")} | ${outcomeCard}`);
  check(/None for this step/.test(await panel.locator(".tv-part").filter({ has: page.locator("h5", { hasText: "System calls" }) }).innerText()),
    "The rules panel should list no system call before the approval");
  const messageCard = await cardOf(step("message"));
  check((await step("message").locator(".gn-calls").count()) === 0 && messageCard.includes("Hydraulic leak") && /Read by\s*Disruption Detection Agent/.test(messageCard),
    `The report should show only the report; the Detection agent recorded it: ${messageCard}`);
  await page.locator('.node[data-node="message"]').click();
  check((await panel.locator("details.tv-call").count()) === 0 && /as received/i.test(await panel.innerText()), "The report's panel should hold only the report as received");
  // Owner: the Detection agent reads the report and populates the disruption: its type's own fields, the event id, the knock-on.
  await page.locator('.node[data-node="agent:detection"]').click();
  const recorded = (await panel.locator(".tv-part").filter({ has: page.locator("h5", { hasText: "Output" }) }).innerText()).replace(/\s+/g, " ");
  check(recorded.includes("Aircraft defect: CS-TQA out of service 4 h") && recorded.includes("Aircraft CS-TQA") && recorded.includes("Out of service 4 h") &&
    recorded.includes("Event EV-9C2D41") && recorded.includes("Knock-on 4 flights affected, up to +3 h 25 min if nothing changes") &&
    recorded.includes("Recorded Defect in maintenance Flights changed None") && !/Flight\s|Delay\s|Cause\s/.test(recorded),
    `The Detection agent's output should be the disruption as recorded, only its type's fields, and that it changed no flight: ${recorded}`);
  check((await tipOf(panel.locator(".tv-recorded dd").filter({ hasText: "4 flights affected" }))).includes("AR102 OPO–LIS 12:00 → 15:25"), "The knock-on should list its flights on hover");
  const recordedTip = await tipOf(panel.locator(".tv-recorded dd").filter({ hasText: "Defect in maintenance" }));
  check(recordedTip.includes("marks the aircraft out of service as reported") && recordedTip.includes("It changes no flight"),
    `What was recorded should say what it changed, and that it changed no flight: ${recordedTip}`);
  // Owner ("i thought agents only read ... a bit uncontrolled write"): recording the report reads as a recorded report, not
  // a write; WRITE stays for the executor's changes.
  const recordCall = panel.locator("details.tv-call").first();
  const recordRow = (await recordCall.locator("summary").innerText()).replace(/\s+/g, " ");
  check((await panel.locator("details.tv-call").count()) === 1 && recordRow.includes("Report recorded Aircraft defect, CS-TQA out of service 4 h, event EV-9C2D41") &&
    recordRow.includes("Maintenance, via ARROW") && !recordRow.includes("WRITE") && (await recordCall.locator(".tv-acc-b").count()) === 0,
    `Recording the report should read as a recorded report, ARROW's record within it: ${recordRow}`);
  await recordCall.locator("summary").click();
  check((await words(recordCall.locator(".tv-note"))) === "Adds the reported defect to maintenance, which marks the aircraft out of service as reported. " +
    "It changes no flight: flights change only when ARROW carries out an approved plan.", `The opened record should say it changes no flight: ${await words(recordCall.locator(".tv-callbody"))}`);
  const recordIface = (await recordCall.locator(".iface").innerText()).replace(/\s+/g, " ");
  check(recordIface.includes("Tool call (MCP) to ARROW · record_aircraft_defect") && recordIface.includes("ARROW recorded it in Maintenance · POST /api/defects") &&
    recordIface.includes("00000000-0000-4000-8000-000000000015") && recordIface.includes("ARROW ID id-airrec-ops-dev"),
    `The record's detail should say how it went, both identities: ${recordIface}`);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-detection.png" });
  // Newer runs (owner: the airline's systems are written only when a plan runs): the same report recorded on ARROW's case only,
  // so no event id yet, and the plan's first action records the defect in Maintenance, which issues it.
  const newerRun = trail(A);
  newerRun.recorded_in = "arrow";
  newerRun.reported_calls = [];
  newerRun.disruption = { ...newerRun.disruption, event_id: null };
  for (const agent of newerRun.agents.filter((entry) => entry.key === "detection")) {
    agent.calls = agent.calls.map((c) => (c.operation === "record_aircraft_defect"
      ? { ...c, summary: "CS-TQA out of service 4 h", response: { recorded: "aircraft defect · CS-TQA", flights_affected: 4 } } : c));
  }
  const inArrow = await page.evaluate((data) => {
    const box = document.createElement("div");
    box.innerHTML = TrailView.detail(data, "agent:detection", {});
    return box.textContent.replace(/\s+/g, " ");
  }, newerRun);
  check(/Recorded\s*In ARROW's case\s*Flights changed\s*None/.test(inArrow) && !/Event\s*EV-/.test(inArrow) &&
    inArrow.includes("Recorded in ARROW Aircraft defect, CS-TQA out of service 4 h") && !inArrow.includes("via ARROW") &&
    inArrow.includes("Recorded on ARROW's case: no airline system changes. ARROW records the defect in maintenance, which marks the aircraft " +
      "out of service, as the first action of a plan it carries out."),
    `A newer run's report should read as recorded in ARROW, the systems written only when a plan runs: ${inArrow}`);
  const reported = await page.evaluate(() => RecoveryView.outcomeGroups({ status: "completed", actions: [{ kind: "record_disruption",
    system: "maintenance", label: "Maintenance", status: "confirmed", summary: "Record the CS-TQA defect: out of service 4 h",
    message: "Defect recorded as event EV-9C2D41: CS-TQA out of service until 13:45", calls: [{ status: "confirmed", event_id: "EV-9C2D41" }] }] })
    .map((group) => [group.title, group.headline.join(" · "), group.sub, group.status]));
  check(JSON.stringify(reported) === JSON.stringify([["Reported disruption", "Defect recorded in maintenance", "Event EV-9C2D41", "confirmed"]]),
    `What ARROW did should lead with the reported disruption its system recorded: ${JSON.stringify(reported)}`);
  await page.locator('.node[data-node="outcome"]').click();
  const outcomePanel = await panel.innerText();
  check(outcomePanel.includes("10 of 10 actions confirmed") && outcomePanel.includes("Re-checked 2 sources before writing") &&
    outcomePanel.includes("Approved action Retime flights, confirmed") && !outcomePanel.includes("/api/") && !outcomePanel.includes("Aircraft rotation"),
    `The outcome should list the actions, the executor's re-checks folded, its write in words: ${outcomePanel}`);
  // ARROW's own calls are REST routes on each system's backend; a write says so, its route and reference only once opened.
  const write = panel.locator("details.tv-call").filter({ hasText: "Approved action" });
  check(/write/i.test(await write.locator("summary .tv-acc-b").innerText()), "ARROW's write should be marked Write");
  await write.locator("summary").click();
  const writeIface = (await write.locator(".iface").innerText()).replace(/\s+/g, " ");
  check(writeIface.includes("POST /api/actions") && !writeIface.includes("ca-airrec") && writeIface.includes("Reference act-1") && writeIface.includes("id-airrec-ops-dev") &&
    !writeIface.includes("Same operation") && (await write.innerText()).includes("By ARROW") && !(await write.locator(".tv-io2").innerText()).includes("act-1"),
    `ARROW's call should give its REST route on the system's backend and its reference under Interface: ${writeIface}`);
  check((await write.locator(".tv-io2 > section").first().innerText()).replace(/\s+/g, " ").includes("Action retime flights"),
    "The executor's write should name the action it carried out");
  const dla = panel.locator("details.tv-act").filter({ hasText: "EUROCONTROL Network Manager" }).first();
  await dla.locator("summary").hover();
  check((await page.locator("#tip").innerText()).includes("DLA: Delay message"), "Hovering an action should explain it");
  await dla.locator("summary").click();
  const dlaText = await dla.innerText();
  check((await dla.locator(".iface").innerText()).includes("Reference act-2") && /request/i.test(dlaText) && dlaText.includes("DLA AR101 EOBT 13:45") && /receipt/i.test(dlaText) &&
    dlaText.includes("Accepted by the Network Manager") && dlaText.includes("10:02:00") && !/Action act-2/.test(dlaText),
    `An opened action should show its request and receipt, its reference under Interface: ${dlaText}`);
  const requestFields = await dla.locator(".tv-callbody .tv-kv dt, .tv-callbody .tv-mini th").allInnerTexts();
  check(requestFields.includes("Flight") && requestFields.includes("Departs") && !requestFields.some(name => /_| no$| local$/.test(name)),
    `The request's fields should read in words: ${requestFields.join(" | ")}`);
  await page.mouse.move(2, 2);
  await professional("Trail outcome");
  await page.locator('.node[data-node="orchestrator"]').click();
  // The orchestrator too: its input is ARROW's case brief, its output the plan it submitted and why, in its name, never in quotes.
  const orchestratorPanel = await panel.innerText();
  check(orchestratorPanel.includes("Hydraulic leak found on CS-TQA") && orchestratorPanel.includes("Limits: automatic plans up to €20,000, at most 10 rebookings, up to 4 plans") &&
    orchestratorPanel.includes("Recommends plan 2: Delay the rotation") && /tool calls/i.test(orchestratorPanel) && orchestratorPanel.includes("Instructions") &&
    !/prompt v\d/.test(orchestratorPanel) && orchestratorPanel.includes("Open in Foundry") && !/AI agent|coordinates the others/.test(orchestratorPanel),
    `The orchestrator's panel should hold its input, output and tool calls: ${orchestratorPanel.slice(0, 500)}`);
  check((await panel.locator(".tv-said").innerText()).replace(/\s+/g, " ") === `Orchestrator AI ${delayPlan.recommendation_reason}`,
    "The orchestrator's reason should be attributed and marked as written by AI");
  // Review: the recommended plan's own levers as ARROW recorded them, never the last plan built (here the cancel).
  const leversShown = (await panel.locator(".tv-stack .tv-kv").filter({ hasText: /^Levers/ }).allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim());
  check(leversShown.join(" | ") === "Levers Delay, no standby crew, rebook, delay messages",
    `The orchestrator should show the recommended plan's own levers: ${leversShown.join(" | ")}`);
  await panel.locator("details.tv-call").filter({ hasText: "Asked the Disruption Detection Agent" }).first().locator("summary").click();
  const orchestratorRows = (await panel.locator("details.tv-call > summary").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim());
  check(orchestratorRows[0].startsWith("Asked the Disruption Detection Agent") && orchestratorRows[1].startsWith("Read the playbook") && orchestratorRows[1].includes("ARROW") &&
    orchestratorRows.some(row => row.startsWith("Built a plan: delay")) && !orchestratorRows.some(row => /\bMCP\b|\bA2A\b|record_disruption|—|·/.test(row)),
    `The orchestrator should ask the Detection agent first, then read the playbook, in words, no placeholder for a time not recorded: ${orchestratorRows.join(" | ")}`);
  check((await panel.locator("details.tv-call[open] .iface").innerText()).includes("A2A"), "The protocol should wait in the opened row's Interface");
  // Field names follow their context: a tool's arguments by their own names; only an executor action's kind reads as its Action.
  const readPlaybook = panel.locator("details.tv-call").filter({ hasText: "Read the playbook" }).first();
  await readPlaybook.locator("summary").click();
  const readSent = (await readPlaybook.locator(".tv-io2 > section").first().innerText()).replace(/\s+/g, " ");
  check(readSent.includes("Case DSR-20261110-AAAAAA") && !readSent.includes("Action"), `A tool's arguments should not read as an action: ${readSent}`);
  await page.locator('.node[data-node^="plan:"]').last().click();
  check((await panel.locator(".tv-said").innerText()).startsWith("Orchestrator"), "A plan's reason should be attributed too");
  check(!/[“”]/.test(await page.locator("#step-assessed").innerText()), "Assessed should not wrap sentences in quote marks");
  await page.locator('.node[data-node="agent:crew"]').click();
  await panel.locator("details.tv-call").first().locator("summary").click();
  await page.locator("#drawer-body").evaluate(body => { body.scrollTop = 0; });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-assessed.png" });
  await hoverCard(step("agent:crew"));
  await page.screenshot({ path: ".playwright-cli\\operator-1440-assessed-card.png" });
  await page.mouse.move(2, 2);

  // The same decision as a conversation, on the same step below the graph: each question and its answer as facts, never JSON
  // (control tower and rules spec §5.3).
  const convoToggle = page.locator("#trail-convo > summary");
  check((await words(convoToggle)) === "Conversation 17" && !(await page.locator("#trail-convo").evaluate(el => el.open)), "The conversation should wait behind one row");
  await convoToggle.click();
  const convo = page.locator("#trail-alt .hd-convo");
  await convo.waitFor();
  check(await page.locator(".flow").isVisible(), "The conversation should open below the graph, on the same step");
  const affected = await page.evaluate(() => {
    const term = [...document.querySelectorAll("#trail-alt dt")].find(dt => dt.textContent === "Affected flights");
    return term ? [...term.nextElementSibling.querySelectorAll(".hd-chips span")].map(chip => chip.textContent) : [];
  });
  check(affected.join("|") === "AR101|AR102", `Affected flights should be chips: ${affected.join("|")}`);
  const said = await page.evaluate(() => {
    const copy = document.querySelector("#trail-alt").cloneNode(true);
    copy.querySelectorAll("details.hd-raw").forEach(raw => raw.remove());
    return copy.textContent;
  });
  check(!/[{}]/.test(said), "No JSON outside Raw answer");
  check(said.includes("Hydraulic leak") && said.includes("Aircraft defect") && !said.includes("aircraft_defect"), "Facts should read in words, the reason last");
  check(said.includes("ConstructorLine check") && !said.includes("native code"), "An agent key named like an object property should read as a word");
  check((await convo.locator("details.hd-raw pre").first().textContent()).includes('"affected_flight_ids"'), "Raw answer should keep the original text");
  const matrix = convo.locator(".hd-matrix");
  check((await matrix.locator("tbody tr").count()) === 2 && (await matrix.locator("thead").innerText()).toUpperCase().includes("CREW"), "Crew and passenger answers per option should form one table");
  check((await matrix.locator("tbody tr").nth(1).innerText()).includes("324 disrupted"), "A plain-text answer should be shown as text");
  const crewCells = await matrix.locator("tbody tr td:nth-child(2)").allInnerTexts();
  check(crewCells[0].includes("440 duty minutes") && crewCells[1].includes("410 duty minutes"), `Each crew answer should stay with its own option: ${crewCells.join(" | ")}`);
  // Review: one count of who loses their flight or connection; the agent's booking count in disrupted_passengers is left out.
  const paxCell = (await matrix.locator("tbody tr").first().locator("td:nth-child(3)").innerText()).replace(/\s+/g, " ").trim();
  check(paxCell.startsWith("112 lose flight or connection, 59 rebooked, 53 to review") && (paxCell.match(/lose flight or connection/g) || []).length === 1,
    `A Passenger answer should count who loses their flight or connection once: ${paxCell}`);
  check(said.includes("(reported by OCC at 09:45)") && said.includes("for 4 hours, until 13:45. List the recovery options"),
    "A date-time in a question should read as the time, in brackets or at a sentence's end");
  check(said.includes(delayPlan.recommendation_reason) && said.includes("Recommended: Delay the rotation") && !/[“”]/.test(said), "The recommendation and its reason should close the conversation, without quote marks");
  // The Aircraft agent's answer in newer runs: what each option does, and the levers it also checked, in words; the case number hidden.
  check(said.includes("AR101–AR102 cancelled; AR131 and AR132 on time") && said.includes("Aircraft swap · not available · No other serviceable A320neo") &&
    said.includes("Aircraft available from13:45") && !said.includes("CaseDSR"), `The Aircraft agent's options, levers and time should read as facts: ${said.slice(0, 600)}`);
  // The orchestrator's step list lives here, on demand; a step opens its node on the graph.
  const stepList = page.locator("#trail-alt details.tv-steps");
  await stepList.locator(":scope > summary").click();
  check((await stepList.locator(".trail-steps li").count()) === 17, "Every orchestrator step should be listed under the conversation");
  await convoToggle.scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-conversation.png" });
  await professional("Conversation");
  // In newer runs the Detection agent's turn opens the conversation: it read the report and recorded the disruption.
  check((await tipOf(convo.locator(".hd-who").first())) === "Disruption Detection Agent" && (await tipOf(convo.locator(".hd-who.orch").first())) === "Recovery Orchestrator Agent",
    "A short avatar should name its agent in full on hover");
  const detectionTurn = (await convo.locator(".hd-step").first().innerText()).replace(/\s+/g, " ");
  check(detectionTurn.includes("Recorded Aircraft defect: CS-TQA out of service 4 h") && detectionTurn.includes("Event EV-9C2D41") && detectionTurn.includes("AI"),
    `The Detection agent's turn should lead with what it recorded, marked as AI: ${detectionTurn}`);
  await stepList.locator("button", { hasText: "Asked Detection" }).click();
  await page.locator('.node[data-node="agent:detection"][aria-pressed="true"]').waitFor();
  check((await page.locator("#trail-detail").innerText()).includes("Disruption Detection Agent"), "A step should open its node on the graph");

  // Rules: each rule's input against its limit, per plan, recommended plan first.
  await page.locator('.node[data-node="rules"]').click();
  await page.locator("#trail-detail").getByRole("button", { name: "All rule checks ›" }).click();
  const costRow = page.locator('#trail-checks tr[data-check="cost"]');
  await costRow.waitFor();
  check((await stepButton("rules").getAttribute("aria-selected")) === "true" && await page.locator("#step-rules").isVisible(), "The rules link should open the Rules step");
  const cost = await costRow.innerText();
  check(cost.includes("€99,974") && cost.includes("€20,000 (rules v1)") && cost.includes("→ OCC"), `The cost row should show input, limit and result: ${cost}`);
  check((await costRow.locator(".hd-bar").count()) === 2, "A measured check should draw a bar");
  // Owner review: each plan by the name its card shows, not the system's option title.
  const heads = (await page.locator("#trail-checks .hd-checks thead th").allTextContents()).map(text => text.replace(/\s+/g, " ").trim());
  check(heads[1].startsWith("Delay the rotation Recommended") && heads[2].startsWith("Cancel AR101 + AR102") && !heads.join(" ").includes("Fly the rotation late"),
    `The recommended plan should come first, each plan by its card's name: ${heads.join(" | ")}`);
  const order = await page.locator("#trail-checks tr[data-check]").evaluateAll(rows => rows.map(row => row.dataset.check).join(","));
  check(order === "cost,rebooking,aircraft,cancellation,airport,crew,eu261,capacity,answered,passenger_services", `Rules should follow the catalogue: ${order}`);
  check((await page.locator('#trail-checks tr[data-check="eu261"]').innerText()).includes("Long delays"), "Rule names should be ARROW's");
  check((await words(page.locator("#trail-checks .hd-foot"))) === "Rules v1" && (await tipOf(page.locator("#trail-checks .hd-foot .gn-chip"))).includes("the agents cannot change them"),
    "The footer should name the rules' version, what it means on hover");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-checks.png" });
  await professional("Rule checks");
  await page.locator("#trail-checks").getByRole("button", { name: "ARROW rules on the graph ›" }).click();
  await page.locator('.node[data-node="rules"][aria-pressed="true"]').waitFor();
  await showPlans();

  // 5. A blocked plan never offers approval; the summary says what to do, the reason on hover.
  await page.locator(`[data-case="${C}"]`).click();
  await band.locator(".t-todo.bad").filter({ hasText: "Needs manual action" }).waitFor();
  check((await page.getByRole("button", { name: /^(Approve|Choose) plan/ }).count()) === 0, "A blocked plan must not be approvable");
  check((await words(page.locator(".plan .route"))) === "Blocked" && (await words(page.locator(".plan .plan-actions"))) === "Not possible" &&
    (await bandWords(".t-todo")) === "Your action Needs manual action No plan can run in ARROW" && (await bandWords(".verdict")) === "No plan can run" &&
    await page.locator("#status-banner").isHidden(),
    `A blocked plan should say so on its card and in the summary, without a recommendation: ${await words(band)}`);
  const manualTip = await hoverText(band.locator(".t-todo .big"));
  check(manualTip.includes("crew control finds crew, or OCC cancels outside ARROW"), `The manual action should say what to do on hover: ${manualTip}`);
  check((await words(stepButton("decision"))) === "5 Decision Needs manual action" && (await page.locator("#steps .step.current.bad").count()) === 1, "The decision step should say it needs manual action");
  // A plan priced before workings were recorded shows its parts, and its breakdown and basis on hover.
  await openStep("decision");
  const olderTip = await cardOf(page.locator('#step-decision .cmp-parts [data-part="airline"]'));
  check((await page.locator("#step-decision .cmp-parts tr.part").count()) === 3 && olderTip.includes("Airline €13,424 · Compensation €85,050 · Care €12,026 (36 overnight)") &&
    olderTip.includes("Basis: indicative") && !olderTip.includes("× €17.78"), `A plan without workings should show its parts and basis: ${olderTip}`);
  check((await page.locator(".plan-why").count()) === 0, "One blocked plan has nothing to compare");
  check(await page.locator("#recommendation").isHidden() && (await words(page.locator("#step-decision .who"))).startsWith("Blocked: crew would exceed their flight-time limit and no standby crew is available"),
    "Manual intervention keeps its own view, the reason in today's words");
  await openStep("carried");
  check((await words(page.locator("#done-empty"))) === "Nothing was sent to the airline systems." && await page.locator("#done").isHidden(), "Carried out should say nothing was sent");
  await showPlans();

  // 6. Reporting is one text box; the new case opens on the live graph.
  await page.locator("#report-open").click();
  await page.locator('[data-example^="Border-control queues (Entry/Exit System checks)"]').click();
  await page.locator("#report-send").click();
  await page.locator("#case-title").filter({ hasText: "New disruption report" }).waitFor();
  const report = requests.find(r => r.method === "POST" && r.path === "/api/cases");
  check(report.body.type === "message" && report.body.text.includes("AR259") && !("event_id" in report.body), `The report should be a message without an event id: ${JSON.stringify(report.body)}`);
  await page.locator('.node[data-node="agent:detection"]').waitFor();
  check((await stepButton("assessed").getAttribute("aria-selected")) === "true" && await drawer.isVisible() &&
    (await words(stepButton("assessed"))) === "2 Assessed Agents working…", "A new case should open Assessed, live");
  check(await page.locator("#status-banner").isVisible() && (await page.locator("#status-banner").textContent()).includes("ARROW is assessing"), "The status line should say ARROW is assessing");
  check((await page.locator('.node[data-node="agent:detection"]').getAttribute("class")).includes("working"), "A working agent should be marked");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-assessing.png" });
  await showPlans();
  check((await page.locator("#status-banner").getByRole("button", { name: "Watch it live" }).count()) === 1, "With the graph closed the status line should offer to watch it live");

  // 7. Phone: the control tower stacks; the list first, the case full screen with a way back, no sideways scrolling; a step's
  // content follows the steps in the page; a tap opens a figure's card.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#tab-tower").click();
  await towerRows.first().waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The control tower should not overflow at 390 px");
  check((await page.locator('#tower [data-kpi="review"]').innerText()).includes("39"), "Passengers for Passenger Services should count the review");
  const reviewRow390 = await page.locator(`#tower-rows tr[data-trace="${A}"] .tw-state`).evaluate(el => ({ text: el.textContent, ...el.getBoundingClientRect().toJSON() }));
  check(reviewRow390.text === "Action needed · 39 passengers for Passenger Services" && reviewRow390.left >= 0 && reviewRow390.right <= 391,
    `On a phone the tower row should say an action is needed, inside the screen: ${JSON.stringify(reviewRow390)}`);
  check((await towerRows.filter({ hasText: "CS-TQA" }).textContent()).includes("executed · +195 min"), "An executed case should show its planned departure");
  await page.screenshot({ path: ".playwright-cli\\operator-390-tower.png", fullPage: true });
  await page.locator("#tab-workspace").click();
  await page.locator("#back").click();
  await page.locator(".case-row").first().waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The list should not overflow at 390 px");
  await page.locator(`[data-case="${A}"]`).click();
  await page.locator("#back").waitFor();
  // A carried-out case opens on Carried out.
  await page.locator("#step-carried").waitFor();
  // Passengers wait for Passenger Services: the summary says so in amber, inside the screen, and opens Carried out under the steps.
  const action390 = await band.locator(".t-todo.action .amber-btn").evaluate(el => ({ text: el.textContent, visible: el.checkVisibility(), ...el.getBoundingClientRect().toJSON() }));
  check(action390.visible && action390.text === "Review 39 passengers" && action390.left >= 0 && action390.right <= 391 && (await bandOverlaps()).length === 0,
    `On a phone the summary should say an action is needed, inside the screen: ${JSON.stringify(action390)} ${(await bandOverlaps()).join("; ")}`);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The case should not overflow at 390 px");
  check(await page.locator("#steps .step.current .lbl").isVisible() && (await words(page.locator("#steps .step.current .lbl"))) === "Carried out",
    "On a phone the current step should be named under its dot");
  await page.screenshot({ path: ".playwright-cli\\operator-390-case.png", fullPage: true });
  await tap(band.getByRole("button", { name: "Review 39 passengers" }));
  await page.locator("#step-carried #review-handled").waitFor();
  const panel390 = await drawer.boundingBox();
  check((await stepButton("carried").getAttribute("aria-selected")) === "true" && panel390.x >= 0 && panel390.x + panel390.width <= 391 &&
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
    `At 390 px Carried out should follow the steps inside the screen: ${JSON.stringify(panel390)}`);
  await showPlans();
  await tap(page.locator('.plan.chosen .nb[data-num="cost"]'));
  await page.locator("#tip-card").waitFor();
  const tapBox = await page.locator("#tip-card").boundingBox();
  check((await page.locator("#tip-card").innerText()).includes("How €117,198 adds up") && tapBox.x >= 0 && tapBox.x + tapBox.width <= 391,
    `A tap should open the figure's card inside the screen: ${JSON.stringify(tapBox)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-tap-cost.png" });
  await page.waitForTimeout(400);
  await page.locator('.plan.chosen .nb[data-num="cost"]').click();
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await openStep("carried");
  await page.evaluate(() => document.querySelectorAll('#done details[data-group="retimed"], #done details[data-group="passengers"]').forEach(d => { d.open = true; }));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1),
    "What ARROW did should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-carried.png" });
  await openStep("assessed");
  await page.locator(".flow .node").first().waitFor();
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The graph should not overflow at 390 px");
  const stacked = await page.evaluate(() => {
    const graph = document.querySelector("#trail-graph");
    const cols = [...graph.querySelectorAll(".flow > .col")].map(col => col.getBoundingClientRect());
    return { wires: getComputedStyle(graph.querySelector("svg.wires")).display, down: cols.length === 4 && cols.every((r, i) => !i || r.top >= cols[i - 1].bottom),
      fits: graph.scrollWidth <= graph.clientWidth + 1 };
  });
  check(stacked.wires === "none" && stacked.down && stacked.fits, `At 390 px the graph should stack its columns, without wires or sideways scrolling: ${JSON.stringify(stacked)}`);
  await page.locator('.node[data-node="agent:crew"]').click();
  await page.locator("#trail-detail details.tv-call").first().locator("summary").click();
  check((await page.locator("#trail-detail").innerText()).includes("00000000-0000-4000-8000-000000000004"), "An opened call should show its caller at 390 px");
  const [sentSmall, returnedSmall] = await page.locator("#trail-detail details.tv-call").first().locator(".tv-io2 > section")
    .evaluateAll(parts => parts.map(part => part.getBoundingClientRect().toJSON()));
  check(returnedSmall.top >= sentSmall.bottom - 1, "On a phone Sent and Returned should stack");
  await page.locator("#trail-detail [data-toggle]").click();
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The detail panel should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-trail.png" });
  // A phone has no hover (owner review: the cards on hover, focus or tap). On a touch screen (emulated: no hover, a coarse
  // pointer, touch input) a tap on a step shows its card inside the screen, and it stays open; a second tap, or the card's
  // Details, opens the step's panel.
  const phone = await page.context().newCDPSession(page);
  await phone.send("Emulation.setEmulatedMedia", { features: [{ name: "hover", value: "none" }, { name: "pointer", value: "coarse" }] });
  await phone.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
  const touch = async (locator, scroll = true) => {
    if (scroll) { await locator.scrollIntoViewIfNeeded(); await settled(); }
    const box = await locator.boundingBox();
    const point = { x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2) };
    await phone.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [point] });
    await phone.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  };
  await page.mouse.move(2, 2);
  await touch(step("agent:passenger"));
  await page.locator("#tip-card").waitFor();
  await page.waitForTimeout(400);
  const tapCard = await page.locator("#tip-card").boundingBox();
  check(tapCard && (await page.locator("#tip-card").innerText()).startsWith("Passenger Re-accommodation Agent") && tapCard.x >= 0 && tapCard.x + tapCard.width <= 391 &&
    (await step("agent:passenger").getAttribute("aria-pressed")) === "false",
    `A tap should show the step's card inside the screen and keep it open, not open the step: ${JSON.stringify(tapCard)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-tap-agent.png" });
  await touch(step("agent:passenger"));
  await page.locator('.flow .node[data-node="agent:passenger"][aria-pressed="true"]').waitFor();
  check(await page.locator("#tip-card").isHidden() && (await page.locator("#trail-detail").innerText()).includes("Passenger Re-accommodation Agent"),
    "A second tap should open the step's panel");
  await touch(step("agent:crew"));
  const openStepButton = page.locator("#tip-card [data-card-open]");
  await openStepButton.waitFor();
  await page.waitForTimeout(400);
  await touch(openStepButton, false);
  await page.locator('.flow .node[data-node="agent:crew"][aria-pressed="true"]').waitFor();
  check(await page.locator("#tip-card").isHidden(), "The card's Details should open the step's panel");
  await phone.send("Emulation.setTouchEmulationEnabled", { enabled: false });
  await phone.send("Emulation.setEmulatedMedia", { features: [] });
  await phone.detach();
  await page.locator("#trail-convo > summary").click();
  await page.locator("#trail-alt .hd-convo").waitFor();
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The conversation should not overflow at 390 px");
  await openStep("rules");
  await page.locator('#trail-checks tr[data-check="cost"]').waitFor();
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The rule checks should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-checks.png" });
  await showPlans();

  // 8. A link with ?case= still opens that case directly in Disruptions, and &step= the step it names. Tracing is off here.
  await page.setViewportSize({ width: 1440, height: 900 });
  traceTemplate = null;
  await page.goto(`${base}/src/ops_app/static/index.html?case=${B}&step=carried`);
  await page.locator("#case-title").filter({ hasText: "AR259 leaves 20 min late" }).waitFor();
  check((await page.locator("#tab-workspace").getAttribute("aria-selected")) === "true" && !(await page.locator("#tower").isVisible()), "?case= should open the case, not the tower");
  await page.locator("#done .done-line").waitFor();
  check((await stepButton("carried").getAttribute("aria-selected")) === "true", "&step= should open the step it names");
  // Esc on another screen leaves the case alone (review): the step waits behind the Tower, Rules and Map screens.
  for (const tab of ["tower", "rules", "map"]) {
    await page.locator(`#tab-${tab}`).click();
    const before = page.url();
    await page.keyboard.press("Escape");
    check(page.url() === before, `Esc on the ${tab} screen should not change the link: ${before} → ${page.url()}`);
    await page.locator("#tab-workspace").click();
    check((await stepButton("carried").getAttribute("aria-selected")) === "true" && new URL(page.url()).searchParams.get("step") === "carried" &&
      await page.locator("#drawer").isVisible(), `Esc on the ${tab} screen should leave the open step open: ${page.url()}`);
  }
  // An older case: actions without kind, payload or expected times, and a plan priced only as a total; what exists is shown.
  check((await words(page.locator("#done .done-line"))) === "✓ All 2 changes confirmed by the airline systems", "An older case should count its confirmed changes");
  const olderOutcomes = await page.locator("#done .done-group > summary").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check(olderOutcomes.join(" | ") === "✓ Flights 1 flight retimed Confirmed | ✓ Air traffic control and airports 1 delay message to EUROCONTROL Confirmed",
    `Older actions should still group by outcome: ${olderOutcomes.join(" | ")}`);
  await page.locator('#done details[data-group="retimed"] summary').click();
  check((await page.locator('#done details[data-group="retimed"] .done-table tbody tr').innerText()).replace(/\s+/g, " ").trim() === "AR259 — 11:30 —",
    "An older retime should show its new time and no invented old one");
  check((await page.locator("#done details.call").count()) === 0 &&
    (await words(page.locator('#done details[data-group="retimed"] .done-detail li'))).includes("Operations control · Retimed AR259 11:30; MVT sent"),
    "An older action, carried out before calls were recorded, should still show as its action, its recipient named as today");
  await showPlans();
  check((await page.locator(".plan").count()) === 1 && (await page.locator(".others-fold").count()) === 0 && (await cardOf(page.locator('.plan .nb[data-num="cost"]'))).includes("Excludes care"),
    "A plan with only a total keeps its own cost wording");
  check((await bandWords(".t-why .tl")) === "Why plan 1 ran without OCC" && (await bandWords(".verdict")) === "Within every automatic limit · OCC informed" &&
    (await bandWords(".t-todo")) === "Your action ✓ Nothing to do Ran automatically · OCC informed" &&
    (await words(page.locator(".plan.chosen .ribbon"))) === "Ran automatically", `An automatic plan should read as carried out: ${await words(band)}`);
  await openStep("decision");
  check((await words(page.locator("#recommendation h3"))) === "Carried out automatically: Plan 1, Delay the rotation" &&
    (await recLines()).join(" | ") === "Within every limit" && (await page.locator("#recommendation .rec-chip").count()) === 0 &&
    (await words(page.locator("#recommendation .said"))) === "Orchestrator AI Legal, protects every connection and costs least." && (await page.locator("#step-decision .cmp-parts tr.part").count()) === 0,
    `A single plan carried out reads as such, within every limit, with the orchestrator's reason: ${await words(page.locator("#recommendation"))}`);
  // An older case: the trail kept no caller, request, response, identity or receipt; the screen says so plainly.
  await openStep("assessed");
  await page.locator(".flow .node").first().waitFor();
  check(await page.locator("#case-trace").isHidden() && await page.locator("#case-foundry").isVisible(), "Without tracing the header should offer Foundry but no trace");
  await page.locator('.node[data-node="agent:crew"]').click();
  const oldCall = page.locator("#trail-detail details.tv-call").first();
  check((await oldCall.locator("summary").innerText()).includes("Crew impact, delay") && (await oldCall.locator(".tv-acc-b").count()) === 0,
    "An older call should still say what it did, without an access it did not record");
  await oldCall.locator("summary").click();
  const oldText = await oldCall.innerText();
  check((oldText.match(/not recorded yet/g) || []).length === 3 && (await oldCall.locator(".iface").innerText()).includes("Tool call (MCP) to Crew system · assess_crew_recovery"),
    `An older call's caller, request and response should read 'not recorded yet', its tool under Interface: ${oldText}`);
  // A Dev run: a prose answer kept the raw trace id; older runs name the case by it.
  const olderAircraft = await cardOf(step("agent:aircraft"));
  check((await valueOf("agent:aircraft")) === "Answered" && olderAircraft.includes("for this case") && !/[0-9a-f]{32}/.test(olderAircraft),
    `A prose answer should wait on the agent's card, without ids: ${olderAircraft}`);
  await page.locator('.node[data-node="agent:aircraft"]').click();
  check((await page.locator("#trail-detail .tv-opts, #trail-detail .tv-levers").count()) === 0, "An older Aircraft answer shows no options it did not give");
  await hoverCard(step("agent:crew"));
  const oldCard = await page.locator("#tip-card").innerText();
  check(oldCard.includes("Model not recorded") && /Identity\s*not recorded yet/.test(oldCard), `An older agent card should say what is not recorded: ${oldCard}`);
  await page.mouse.move(2, 2);
  await page.locator('.node[data-node="outcome"]').click();
  const oldAction = page.locator("#trail-detail details.tv-act").first();
  await oldAction.locator("summary").click();
  check(((await oldAction.innerText()).match(/not recorded yet/g) || []).length === 2, "An older action's request and receipt should read 'not recorded yet'");
  traceTemplate = "https://portal.example/search/{trace}/view/{trace}";
  // A plan recorded before rule checks existed keeps the reason view, marked as such.
  await openStep("rules");
  await page.locator("#trail-checks .hd-checks").waitFor();
  check((await page.locator("#trail-checks").innerText()).includes("inputs not recorded"), "A plan without checks should say its inputs were not recorded");
  // Without the session's Foundry project link no link is offered: neither in the header nor on an agent (C4).
  foundryPortal = null;
  await page.goto(`${base}/src/ops_app/static/index.html?case=${A}&step=assessed`);
  await page.locator(".flow .node").first().waitFor();
  check(await page.locator("#case-foundry").isHidden() && (await page.locator('.tv-cards template').evaluateAll(cards =>
    cards.filter(card => card.content.querySelector("a.tv-link")).length)) === 0, "Without a Foundry project link no Open in Foundry should show");
  foundryPortal = PORTAL;

  // 8b. Owner (native dispatch spec §4): each action now goes to its recipient's own interface. What ARROW did and the trail
  // list one row per call, named by its recipient and what it says, then its state; opened, what was sent, the reply and the
  // Interface line (method, path and the call's reference). Hover cards explain the recipient and abbreviations, without ids.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${P}&step=carried`);
  const doneCalls = page.locator("#done");
  await doneCalls.locator("details.call").first().waitFor({ state: "attached" });
  check(await cellsFit(), "One plan card should fit every number at 1440 px");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-one-plan.png" });
  check((await words(doneCalls.locator(".done-line"))) === "✓ All 4 changes confirmed by the airline systems", "Case 1 should count its four confirmed changes");
  await page.evaluate(() => document.querySelectorAll("#done details.done-group").forEach(d => { d.open = true; }));
  const CALL_ROWS = ["Operations control · AR259 departs 10:50 (was 10:30)", "EUROCONTROL Network Manager · Delay message AR259, off-block 10:50 · ACK",
    "Lisbon airport (A-CDM) · AR259 ready 10:50 at Lisbon", "Crew system · Update crew duties for AR259"];
  const saysOf = (box) => box.locator(".call-says").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check((await saysOf(doneCalls)).join(" | ") === CALL_ROWS.join(" | "), `Each call should be named by its recipient and what it says: ${(await saysOf(doneCalls)).join(" | ")}`);
  const callSummaries = await doneCalls.locator("details.call > summary").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check((await doneCalls.locator(".call-how").count()) === 0 && callSummaries[0] === "✓ Operations control · AR259 departs 10:50 (was 10:30) Confirmed" &&
    !callSummaries.some(row => /PATCH|POST|\/api\//.test(row)), `A call row should read recipient · what it says · status: ${callSummaries.join(" | ")}`);
  await professional("What ARROW did · calls");
  check((await doneCalls.locator('details[data-group="retimed"] .done-table tbody tr').innerText()).replace(/\s+/g, " ").trim() === "AR259 10:30 10:50 +20 min",
    "The retimed flight should show was and now");
  const nmRow = doneCalls.locator("details.call").filter({ hasText: "EUROCONTROL Network Manager" });
  await nmRow.locator("summary").click();
  const [nmSent, nmReturned] = await nmRow.locator(".call-io > section").allInnerTexts();
  check(/^sent/i.test(nmSent) && nmSent.includes(DLA) && /^returned/i.test(nmReturned) && nmReturned.includes("ACK"),
    `An opened call should show the message sent and the reply: ${nmSent} | ${nmReturned}`);
  const nmIface = (await nmRow.locator(".iface").innerText()).replace(/\s+/g, " ").trim();
  check(nmIface === "Interface: POST /nm/messages · Reference 5a9d-02-network-manager-fpl-delay", `The opened call should end with its interface and reference: ${nmIface}`);
  check(!/act-\d|5a9d-|id-airrec-/.test(await nmRow.locator(".call-io").innerText()), "Sent and Returned should carry no references or identity names");
  const crewRow = doneCalls.locator("details.call").filter({ hasText: "Crew system" });
  await crewRow.locator("summary").click();
  const crewSent = await crewRow.locator(".call-io > section").first().innerText();
  check(crewSent.includes("AR259") && crewSent.includes("10:50") && !/original/i.test(crewSent), `Fields sent should read as words, without the original rows: ${crewSent}`);
  const doneText = await doneCalls.innerText();
  check(!/mocked|synthetic|played/i.test(doneText), `No call should say how it is played: ${doneText}`);
  const nmTip = await tipOf(nmRow.locator("summary"));
  check(!nmTip.includes("5a9d-0") && !/\bID\b/.test(nmTip) && nmTip.includes("EUROCONTROL's Network Manager") && nmTip.includes("DLA: Delay message") &&
    nmTip.includes("EOBT: Estimated off-block time"), `Hovering a call should explain it, without ids: ${nmTip}`);
  const cdmTip = await tipOf(doneCalls.locator("details.call").filter({ hasText: "A-CDM" }).locator("summary"));
  check(cdmTip.includes("A-CDM: Airport Collaborative Decision Making") && cdmTip.includes("TOBT: Target off-block time"), `A-CDM and TOBT should be spelled out: ${cdmTip}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-calls.png" });
  await openStep("reported");
  const olderRepeat = await words(page.locator("#case-repeats"));
  check(olderRepeat.endsWith("Also reported by Ground handling (DSR-20261121-5A9D00) · same disruption") && (await words(stepButton("reported"))).includes("2 reports"),
    `A repeat noted in the older wording should read as today's: ${olderRepeat}`);
  check((await page.locator("#step-reported details.tv-call").count()) === 0 && (await words(page.locator("#case-message"))).includes("Entry/Exit System checks"),
    "Reported should show only the report as received");
  await openStep("assessed");
  await page.locator(".flow .node").first().waitFor();
  const callsCard = await cardOf(step("outcome"));
  check((await badgesOf("outcome")) === "- 1 - 4 3" && /Operations control\s*1 write, 1 read/.test(callsCard) && /EUROCONTROL Network Manager\s*1 write/.test(callsCard),
    `The outcome should count each call once, the recheck with it: ${await badgesOf("outcome")} | ${callsCard}`);
  const typedReport = await cardOf(step("message"));
  check((await step("rules").locator(".gn-w").count()) === 0 && (await badgesOf("message")) === "- - 1 - 1" && /Read by\s*ARROW, when it arrived/.test(typedReport) &&
    /Operations control\s*1 report recorded/.test(typedReport) && (await valueOf("message")) === "Flight delay",
    `A typed event ARROW recorded on receipt should read under Report, never under ARROW rules: ${await badgesOf("message")} | ${typedReport}`);
  // Its brief names the operations event and its type's own fields, in words; the Detection agent confirmed what ARROW recorded.
  await page.locator('.node[data-node="orchestrator"]').click();
  const typedBrief = (await page.locator("#trail-detail .tv-part").filter({ has: page.locator("h5", { hasText: "Input" }) }).innerText()).replace(/\s+/g, " ");
  check(typedBrief.includes("Disruption Flight delay: AR259 +20 min") && typedBrief.includes("Cause Border-control queue") && typedBrief.includes("Event EV-55D1C2") &&
    typedBrief.includes("From Ground handling at 09:45") && !/allowed options|unknown/i.test(typedBrief), `A typed event's brief should read in words: ${typedBrief}`);
  await page.locator('.node[data-node="agent:detection"]').click();
  const confirmedEvent = (await page.locator("#trail-detail").innerText()).replace(/\s+/g, " ");
  check(confirmedEvent.includes("Flight delay: AR259 +20 min") && confirmedEvent.includes("Recorded Delay report in operations control, by ARROW on arrival") &&
    confirmedEvent.includes("Flights changed None") &&
    confirmedEvent.includes("Knock-on No other flight affected if nothing changes"), `The Detection agent should confirm a typed event ARROW recorded: ${confirmedEvent.slice(0, 400)}`);
  await page.locator('.node[data-node="outcome"]').click();
  const outcomeCalls = page.locator("#trail-detail");
  await outcomeCalls.locator("details.call").first().waitFor();
  check((await saysOf(outcomeCalls)).join(" | ") === CALL_ROWS.join(" | "), `System calls should name each call by its recipient: ${(await saysOf(outcomeCalls)).join(" | ")}`);
  await outcomeCalls.locator("details.tv-again > summary").click();
  check((await words(outcomeCalls.locator("details.tv-again > summary"))) === "Re-checked 1 source before writing" && (await outcomeCalls.locator("details.tv-call").count()) === 1 &&
    (await outcomeCalls.locator("details.tv-call > summary").innerText()).includes("Aircraft rotation CS-TUK, 2 flights today: AR259, AR260"),
    "The executor's re-check should fold above the writes and no write be listed twice");
  await professional("Trail calls");
  const trailNm = outcomeCalls.locator("details.call").filter({ hasText: "EUROCONTROL Network Manager" });
  const trailTip = await tipOf(trailNm.locator("summary"));
  check(!trailTip.includes("5a9d-0") && !trailTip.includes("id-airrec-ops-dev") && !trailTip.includes("ca-airrec") && trailTip.includes("EUROCONTROL's Network Manager"),
    `The hover card should explain the recipient without ids: ${trailTip}`);
  await trailNm.locator("summary").click();
  const [trailSent, trailReturned] = await trailNm.locator(".call-io > section").allInnerTexts();
  check(trailSent.includes(DLA) && trailReturned.includes("ACK"), `The trail's call should show the message and the reply: ${trailSent} | ${trailReturned}`);
  const trailIface = (await trailNm.locator(".iface").innerText()).replace(/\s+/g, " ");
  check(trailIface.includes("POST /nm/messages") && trailIface.includes("Reference 5a9d-02-network-manager-fpl-delay") && trailIface.includes("id-airrec-ops-dev") &&
    !trailIface.includes("ca-airrec"), `The opened call should give the call's route, its reference and the identity the recipient recorded, never a host: ${trailIface}`);
  await page.mouse.move(2, 2);
  await trailNm.locator("summary").hover();
  await page.locator("#tip").waitFor();
  await professional("Trail call hover card");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-call-tip.png" });
  await page.mouse.move(2, 2);
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1),
    "The calls in the trail should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-trail-calls.png" });
  await openStep("carried");
  await page.evaluate(() => document.querySelectorAll("#done details.done-group, #done details.call").forEach(d => { d.open = true; }));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1),
    "The calls in What ARROW did should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-calls.png" });
  await professional("390 px What ARROW did");
  await page.setViewportSize({ width: 1440, height: 900 });

  // 8c. Final review: a retime is one update per flight, so it can stop part-way. What ARROW did says which flight moved,
  // which was refused and which were not sent; each call reads as confirmed or refused, with the refusal's reason. The status
  // line and the line over the outcomes say the same; what the stop kept from running reads as not sent, by what it would have done.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${Q}&step=carried`);
  const partFlights = page.locator('#done details[data-group="retimed"]');
  await partFlights.waitFor();
  // The summary says what stopped the plan, and what to check before acting on hover (review: the status line is hidden).
  const partlyAction = await bandWords(".t-todo");
  const partlyTip = await hoverText(band.locator(".t-todo .big"));
  check(await page.locator("#status-banner").isHidden() && partlyAction === "Your action Needs attention Operations control partly carried out" &&
    partlyTip.includes("Needs attention · Operations control partly carried out") && partlyTip.includes("Dependent actions were not sent; what was done stays in place."),
    `The summary should say the plan was partly carried out, as the alert does, and what to check on hover: ${partlyAction} | ${partlyTip}`);
  check((await words(stepButton("carried"))) === "6 Carried out Stopped part-way" && (await page.locator("#steps .step.current.bad").count()) === 1,
    "Carried out should read as stopped part-way");
  const partlyLine = await words(page.locator("#done .done-line"));
  check(partlyLine === "! Stopped part-way · 1 flight moved · 1 refused · 7 not sent", `The line over the outcomes should agree with them: ${partlyLine}`);
  const partlyOutcomes = await page.locator("#done .done-group > summary").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check(partlyOutcomes.join(" | ") === "✕ Flights 1 of 4 flights moved AR102 refused · AR131 and AR132 not sent Partly done | " +
    "– Air traffic control and airports 4 delay messages to EUROCONTROL Not sent | – Crew Crew duties Not sent | " +
    "– Passengers Rebooking for 38 passengers · Cars for 35 passengers (5 connections) Not sent",
    `What a stop kept from running should read as not sent, by what it would have done: ${partlyOutcomes.join(" | ")}`);
  await page.locator('#done details[data-group="crew"] > summary').click();
  const unsentCrew = page.locator('#done details[data-group="crew"] .done-detail li');
  check((await words(unsentCrew.locator(".state"))) === "Not sent" && (await words(unsentCrew)).includes("Crew system · Not sent: Operations control did not complete"),
    `An action the stop kept from running should read as not sent: ${await words(unsentCrew)}`);
  await page.locator('#done details[data-group="crew"] > summary').click();
  const partlySummary = await words(partFlights.locator(":scope > summary"));
  check(partlySummary === "✕ Flights 1 of 4 flights moved AR102 refused · AR131 and AR132 not sent Partly done",
    `A retime stopped part-way should say which flight moved and which did not: ${partlySummary}`);
  await partFlights.locator(":scope > summary").click();
  const partlyHead = (await partFlights.locator(".done-table th").evaluateAll(cells => cells.map(cell => cell.textContent.trim()))).join(" | ");
  check(partlyHead === "Flight | Was | Planned | Result", `A retime stopped part-way should show each flight's result: ${partlyHead}`);
  const partlyRows = await partFlights.locator(".done-table tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  check(partlyRows.join(" | ") === "AR101 10:30 13:45 Moved | AR102 12:00 15:25 Refused | AR131 14:10 17:05 Not sent | AR132 15:40 18:40 Not sent",
    `The moved flight should read as moved and the refused one as refused: ${partlyRows.join(" | ")}`);
  const partlyCalls = await partFlights.locator("details.call > summary").evaluateAll(rows => rows.map(row =>
    `${row.querySelector(".call-says").textContent.replace(/\s+/g, " ").trim()} = ${row.querySelector(".state").textContent.trim()}`));
  check(partlyCalls.join(" | ") === "Operations control · AR101 departs 13:45 (was 10:30) = Confirmed | Operations control · AR102 departs 15:25 (was 12:00) = Refused",
    `Each flight's call should read as confirmed or refused: ${partlyCalls.join(" | ")}`);
  check((await words(partFlights.locator(".call-why"))) === "Source changed: flight AR102", "A refused call should give the recipient's reason");
  const stoppedPlan = await words(page.locator(".plan.chosen"));
  check(stoppedPlan.includes("Approved by OCC") && !/carried out/i.test(stoppedPlan), `A plan stopped part-way should not read as carried out: ${stoppedPlan}`);
  await partFlights.locator("details.call").nth(1).locator("summary").click();
  check((await partFlights.locator("details.call").nth(1).locator(".call-io > section").nth(1).innerText()).includes("Source changed: flight AR102"),
    "An opened refused call should show the reply");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-partly-refused.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1),
    "A retime stopped part-way should not overflow at 390 px");
  await page.setViewportSize({ width: 1440, height: 900 });

  // 9. Rules: four-eyes changes. The proposer cannot approve their own change but may withdraw it; a new proposal starts
  // from the values in force; another duty manager's stale approval is refused with the API's reason.
  await page.locator("#tab-tower").click();
  await page.locator("#tower").getByRole("button", { name: "Rule change waiting for a second approver" }).click();
  const rulesView = page.locator("#rules");
  const change = rulesView.locator(".rl-change");
  await change.waitFor();
  check((await page.locator("#tab-rules").getAttribute("aria-selected")) === "true" && !(await page.locator("#tower").isVisible()), "The waiting change should open the Rules screen");
  check((await page.locator("#rules-count").textContent()) === "1", "The Rules tab should count the waiting change");
  check(await change.getByRole("button", { name: "Approve" }).isDisabled(), "The proposer must not approve their own change");
  check(await change.getByText("another duty manager approves it").isVisible(), "The proposer should see who approves");
  const changeText = await change.innerText();
  check(changeText.includes("€20,000") && changeText.includes("€15,000") && changeText.includes("Duty Manager · 09:30") &&
    changeText.includes("Tighter cost control this winter") && changeText.includes("AR805"), `The change should show its values, proposer, reason and effect: ${changeText}`);
  const rulesText = await rulesView.innerText();
  for (const name of ["Cost limit for automatic plans", "Rebooking limit", "Aircraft change or standby", "Cancellation", "Airport and slot requests",
    "Declining to rebook", "Crew flight-time limits", "Long delays", "Lisbon capacity", "Every check answered"]) {
    check(rulesText.includes(name), `The Rules screen should list ${name}`);
  }
  check(!/[0-9a-f]{8}-[0-9a-f]{4}-/.test(rulesText) && !rulesText.includes("arrow-release"), "People should be named, not shown as object IDs");
  check((await rulesView.locator(".rl-row").filter({ hasText: "Long delays" }).locator(".rl-eu span").allTextContents()).join("|") ===
    "Late 2 h+ · ≤ 1,500 km|Late 3 h+ · ≤ 3,500 km or in the EU|Late 4 h+ · longer", "Long delays should show the three EU261 care thresholds");
  // Task 4: a storm's plans are always OCC's; the rule says when it triggers.
  const weatherRule = await rulesView.locator('.rl-row[data-rule="weather"]').innerText().catch(() => "");
  check(weatherRule.includes("Weather plans") && (await rulesView.locator('.rl-row[data-rule="weather"] .rl-when').textContent()) === "A weather forecast moves flights across the network" &&
    weatherRule.includes("OCC decides"), `The weather rule should say when it triggers: ${weatherRule}`);
  // Task 2: calling several standby crews, or a base's last, is OCC's; the rule says when it triggers.
  check((await rulesView.locator('.rl-row[data-rule="standby"] .rl-when').textContent()) === "More than two standby crews, or a base's last complete standby crew of a qualification" &&
    (await rulesView.locator('.rl-row[data-rule="standby"]').innerText()).includes("OCC decides"), "The standby crews rule should say when it triggers");
  // Fixed rules are checked by code on every plan; the pill is what happens when one triggers.
  const columns = await rulesView.locator(".rl-cols").first().innerText();
  check(/checked automatically/i.test(columns) && /then/i.test(columns), `The fixed rules should say they are checked automatically and what follows: ${columns}`);
  // Owner: "having 'fixed' in the description?": the lock says how the rule changes.
  check((await rulesView.locator(".rl-lock").first().getAttribute("title") || "") === "Not editable in ARROW: changed only by a reviewed release" &&
    (await rulesView.locator(".rl-title span").allTextContents()).join(" | ").includes("Reserved for OCC"), "The lock should say how the rule changes, never 'fixed'");
  check((await rulesView.locator(".rl-history tr").first().innerText()).includes("€15,000") && rulesText.includes("ARROW release values"), "History should list the newest change first");
  check(await rulesView.getByRole("button", { name: "Propose change" }).isDisabled(), "No new proposal while one is waiting");
  const rebookingFoot = await rulesView.locator('.rl-limit[data-rule="rebooking"] .rl-foot').innerText();
  check(rebookingFoot.includes("Highest today: CS-TQA 38 rebooked") && !rebookingFoot.includes("77"), `The rebooking limit should compare rebooked, not disrupted, passengers: ${rebookingFoot}`);
  // The OCC playbook (OCC playbook spec): read only, below the rules, with its version; each action's desk, where it applies and its
  // autonomy in one compact row, what code checks and where it goes on hover or focus.
  const playbookSection = rulesView.locator(".rl-playbook");
  check(await playbookSection.isVisible() && (await words(playbookSection.locator(".rl-title h2"))) === "OCC playbook" &&
    (await words(playbookSection.locator(".rl-title span"))).startsWith("Version 2026-11-22b · 11 actions · read only") && (await playbookSection.locator(".pb-row").count()) === 11 &&
    await page.evaluate(() => document.querySelector(".rl-playbook").getBoundingClientRect().top >=
      Math.max(...[...document.querySelectorAll(".rl-fixed")].map(rows => rows.getBoundingClientRect().bottom))),
    "The Rules screen should list the OCC playbook's 11 actions below the rules, with its version");
  const noticeRule = await playbookSection.locator('.pb-row[data-action="PAX-NOTICE"]').evaluate(el => el.innerText.replace(/\s+/g, " ").trim());
  check(noticeRule === "Tell passengers their new times Hub and passengers Weather Automatic within limits",
    `A playbook row should give its desk, where it applies and its autonomy: ${noticeRule}`);
  const etrRule = await playbookSection.locator('.pb-row[data-action="MX-ETR"]').evaluate(el => el.innerText.replace(/\s+/g, " ").trim());
  check(etrRule.endsWith("Maintenance control Defect Advice only") &&
    (await playbookSection.locator('.pb-row[data-action="GROUND-HOLD"]').innerText()).includes("OCC decides") &&
    (await playbookSection.locator('.pb-row[data-action="RETIME"] .pb-applies span').allTextContents()).join(" ") === "Defect Delay Weather", `Advice and OCC actions should say so: ${etrRule}`);
  const noticeRuleCard = await cardOf(playbookSection.locator('.pb-row[data-action="PAX-NOTICE"]'));
  check(noticeRuleCard.includes("Code checks") && noticeRuleCard.includes("Flights yet to leave and not cancelled; each flight once a day") &&
    /Sent to\s+Passenger system/.test(noticeRuleCard) && noticeRuleCard.includes("Prepares · reversible, hours ahead"), `A playbook row should open what code checks on hover: ${noticeRuleCard}`);
  check((await playbookSection.locator("button, input, select, textarea").count()) === 0, "The playbook should be read only");
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await playbookSection.scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rules-playbook.png" });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rules.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await change.getByRole("button", { name: "Withdraw" }).click();
  await change.waitFor({ state: "detached" });
  const withdraw = requests.find(r => r.method === "POST" && r.path === "/api/rules/proposals/7/withdraw");
  check(withdraw && withdraw.headers["x-arrow-request"] === "operator", "Withdraw should post the proposer's decision");
  check((await page.locator("#rules-count").textContent()) === "", "Nothing should be counted once the change is withdrawn");
  await rulesView.getByRole("button", { name: "Propose change" }).first().click();
  await page.locator("#rule-dialog[open]").waitFor();
  check((await page.locator("#rule-cost").inputValue()) === "20000" && (await page.locator("#rule-rebook").inputValue()) === "10", "The dialog should start from the values in force");
  await page.locator("#rule-cost").fill("15000");
  await page.locator("#rule-send").click();
  await page.locator("#rule-notice").filter({ hasText: "reason" }).waitFor();
  check(!requests.some(r => r.method === "POST" && r.path === "/api/rules/proposals"), "A proposal without a reason should not be sent");
  await page.locator("#rule-reason").fill("Tighter cost control this winter");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rule-dialog.png" });
  await page.locator("#rule-send").click();
  await page.locator("#rule-dialog[open]").waitFor({ state: "detached" });
  const proposal = requests.find(r => r.method === "POST" && r.path === "/api/rules/proposals");
  check(JSON.stringify(proposal.body) === JSON.stringify({ cost_cap_eur: 15000, rebook_limit: 10, reason: "Tighter cost control this winter" }) &&
    proposal.headers["x-arrow-request"] === "operator", `The proposal should carry both values and the reason: ${JSON.stringify(proposal.body)}`);
  await change.waitFor();
  check((await page.locator("#rules-count").textContent()) === "1" && (await change.innerText()).includes("€20,000") && (await change.innerText()).includes("€15,000"),
    "A new proposal should be counted and shown on the Rules screen");
  rules.pending.proposed_by = rules.history[0].proposed_by = "0f0f0f0f-0000-4000-8000-000000000000";
  await page.locator("#tab-tower").click();
  await page.locator("#tab-rules").click();
  await change.getByRole("button", { name: "Reject" }).waitFor();
  check(await change.getByRole("button", { name: "Approve" }).isEnabled() && (await change.getByRole("button", { name: "Withdraw" }).count()) === 0 &&
    (await change.innerText()).includes("Another duty manager"), "Another duty manager may approve or reject, not withdraw");
  await change.getByRole("button", { name: "Approve" }).click();
  await rulesView.getByText("This change is no longer waiting for approval").waitFor();
  check(requests.some(r => r.method === "POST" && r.path === "/api/rules/proposals/8/approve"), "Approve should post the decision");
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The Rules screen should not overflow at 390 px");
  // Each playbook row keeps its icon, name, autonomy and where it applies apart, inside the row (owner: "here also not readable text").
  const playbookClash = await page.evaluate(() => {
    const bad = [];
    for (const row of document.querySelectorAll(".rl-playbook .pb-row")) {
      const box = row.getBoundingClientRect();
      const kids = [...row.children].filter(kid => kid.checkVisibility()).map(kid => [kid, kid.getBoundingClientRect()]);
      kids.forEach(([kid, r], i) => {
        if (r.left < box.left - 1 || r.right > box.right + 1) bad.push(`${row.dataset.action}: ${kid.className.baseVal ?? kid.className} spills`);
        kids.slice(i + 1).forEach(([other, o]) => {
          if (r.left < o.right - 1 && o.left < r.right - 1 && r.top < o.bottom - 1 && o.top < r.bottom - 1) bad.push(`${row.dataset.action}: ${kid.className.baseVal ?? kid.className} over ${other.className}`);
        });
      });
    }
    return bad;
  });
  check((await page.locator(".rl-playbook .pb-row").count()) === 11 && playbookClash.length === 0, `At 390 px the playbook rows should not overlap: ${playbookClash.join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-rules.png", fullPage: true });
  await professional("390 px Rules");
  await page.setViewportSize({ width: 1440, height: 900 });

  // 10. An open case whose recommended plan cancels flights still shows them as scheduled until OCC decides; a plan
  // partly carried out is not shown as executed; an automatic plan OCC approved is credited to OCC. Rules still being
  // read do not read as unavailable, and a change whose effect cannot be computed shows no effect line.
  dayRows = [{ trace_id: "f".repeat(32), display_id: "DSR-20261110-FFFFFF", created_at: iso(60), status: "assessment_ready",
    headline: "CS-TQA · out of service 4 h", state: "needs", label: "Needs your decision", source: "OCC",
    ...card([fl("AR101", "10:30", null, "cancelled"), fl("AR102", "12:00", null, "cancelled"), fl("AR131", "14:10", "14:10")],
      impact(324, 300, 81000, 2, "Cancel AR101 and AR102", "occ"), 1, { from: at("09:45"), until: at("13:45") }) },
  { trace_id: "9".repeat(32), display_id: "DSR-20261110-999999", created_at: iso(120), status: "assessment_ready",
    headline: "CS-TUK · out of service 3 h", state: "needs", label: "Needs attention", source: "OCC",
    ...card([fl("AR205", "11:00", null, "cancelled"), fl("AR206", "12:40", null, "cancelled")],
      impact(150, 140, 30000, 1, "Cancel AR205 and AR206", "occ", 1, 3, 0, "occ"), 1) },
  { trace_id: "8".repeat(32), display_id: "DSR-20261110-888888", created_at: iso(180), status: "assessment_ready",
    headline: "AR300 · 20 min late", state: "handled", label: "Approved and carried out", source: "OCC",
    ...card([fl("AR300", "12:00", "12:20")], impact(0, 0, 900, 1, "Delay", "auto", 2, 2, 0, "occ"), 5) }];
  rules.pending.effect = null;
  let releaseRules;
  rulesGate = new Promise(resolve => { releaseRules = resolve; });
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const proposed = page.locator("#tower .tw-lane").first();
  await proposed.locator(".tw-dot.first").waitFor();
  check((await page.locator("#tower .tw-dot.cancelled").count()) === 0 && (await page.locator('#tower [title*="cancelled"]').count()) === 0 &&
    (await proposed.locator(".tw-dot.first").textContent()) === "AR101 10:30" && !(await page.locator("#tower-rows").textContent()).includes("cancelled") &&
    (await page.locator("#tower-rows tr").first().textContent()).includes("in 45 min"), "A proposed cancellation must not be drawn as done before OCC decides");
  const partly = await page.locator("#tower-rows tr").filter({ hasText: "CS-TUK" }).innerText();
  check(!partly.includes("executed") && partly.includes("AR205 11:00") && partly.includes("OCC approved · 1 of 3 actions"), `A plan partly carried out is not executed: ${partly}`);
  const approvedAuto = await page.locator("#tower-rows tr").filter({ hasText: "AR300" }).innerText();
  check(approvedAuto.includes("OCC approved · 2 of 2 actions") && !approvedAuto.includes("Ran automatically") && approvedAuto.includes("executed · +20 min"),
    `An automatic plan OCC approved is credited to OCC: ${approvedAuto}`);
  check((await page.locator('#tower [data-kpi="decision"]').innerText()).includes("€900 executed today"), "Executed today should count only plans fully carried out");
  check(!(await page.locator("#tower").textContent()).includes("Rules unavailable") && (await page.locator("#tower .tw-chip").count()) === 0,
    "Rules still being read should claim nothing on the tower");
  rulesGate = null;
  releaseRules();
  await page.locator("#tower .tw-chip").filter({ hasText: "Rule change waiting for a second approver" }).waitFor();
  await page.locator("#tab-rules").click();
  await page.locator("#rules .rl-change").waitFor();
  check((await page.locator("#rules .rl-effect").count()) === 0, "An effect that could not be computed should not be shown");

  // 11. An empty day counts zero; rules that cannot be read say so, and Retry reads them again.
  dayRows = []; rulesStatus = 503;
  await page.goto(`${base}/src/ops_app/static/index.html`);
  await page.locator("#tower").filter({ hasText: "No disruptions today" }).waitFor();
  check((await page.locator('#tower [data-kpi="occ"] b').textContent()) === "0" && (await page.locator('#tower [data-kpi="decision"] b').textContent()) === "€0", "An empty day should count zero");
  check((await page.locator("#tower .tw-chip").count()) === 0 && !(await page.locator("#tower").textContent()).includes("Rules"),
    "Unreadable rules should claim nothing on the tower; the Rules screen says so");
  await page.locator("#tab-rules").click();
  await page.locator("#rules").getByText("Rules could not be loaded").waitFor();
  rulesStatus = 200;
  await page.locator("#rules").getByRole("button", { name: "Retry" }).click();
  await page.locator("#rules").getByRole("heading", { name: "Autonomy limits" }).waitFor();
  dayRows = null; rulesStatus = 200;

  // 12. Passenger Services marks the waiting passengers handled: it asks first, then records who
  // handled them, and the case moves to Handled (a Dev run: one click, then no trace).
  // Passengers waiting for Passenger Services are the summary's amber action (prototype rev 2), which opens Carried out.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${A}`);
  await band.getByRole("button", { name: "Review 39 passengers" }).click();
  const handled = page.locator("#review-handled");
  await handled.waitFor({ timeout: 5000 }).catch(() => { throw new Error("The summary's action needed should open Carried out"); });
  check((await stepButton("carried").getAttribute("aria-selected")) === "true" && new URL(page.url()).searchParams.get("step") === "carried",
    "The summary's action needed should open Carried out");
  check((await handled.textContent()).trim() === "Mark 39 passengers handled", `The button should say what it records: ${await handled.textContent()}`);
  await handled.click();
  check(!requests.some(r => r.path.endsWith("/reviews/passenger_services")), "One click should not record the review");
  await page.locator("#review-confirm").click();
  await page.locator("#done").filter({ hasText: "39 passengers handled by Passenger Services" }).waitFor();
  check(await page.locator("#status-banner").isHidden() &&
    (await bandWords(".t-todo")) === "Your action ✓ Nothing to do Approved by OCC · carried out · 39 passengers handled",
    `The summary should say nothing is left to do and keep the passengers Passenger Services handled: ${await bandWords(".t-todo")}`);
  await stepButton("carried").filter({ hasText: "10 of 10 confirmed" }).waitFor();
  check((await page.locator("#steps .step.current.good").count()) === 1, "Once handled, Carried out should read as done");
  await showPlans();
  await page.locator(".case-group.handled .case-row").filter({ hasText: "CS-TQA" }).waitFor();

  // 12b. A Dev run: the orchestrator recommended the cancel, which disrupts 247 more passengers, and OCC approved the delay.
  // The plan OCC approved leads, the orchestrator's pick folded with its tag; the flag against ARROW's objectives stays on the
  // recommendation; what ARROW did says what is still waiting.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G}`);
  await showPlans();
  await page.locator(".plan.chosen").waitFor();
  check((await page.locator(".plan.chosen h4").textContent()) === "Delay the rotation" && (await words(page.locator(".plan.chosen .ribbon"))) === "Approved by OCC" &&
    (await words(page.locator(".plan.chosen .plan-actions"))) === "Approved by OCC · 10 of 11 confirmed Details ›" &&
    (await words(page.locator(".others-fold li"))) === "Plan 1: Cancel AR101 + AR102 €99,974 OCC decides Recommended" &&
    (await words(page.locator(".others-fold .why"))) === "Not chosen: OCC approved Plan 2", `The plan OCC approved should lead, the orchestrator's pick folded: ${await words(page.locator("#plans"))}`);
  check((await bandWords(".t-why .tl")) === "Why plan 2 · Delay, not cancel" && (await bandWords(".verdict")) === "247 more keep their flight, €17,224 more Against ARROW's objectives" &&
    (await bandWords(".t-todo")) === "Action needed Review 39 passengers Waiting for Ground handling",
    `The summary should say OCC approved, what it waits for, and flag the orchestrator's pick: ${await words(band)}`);
  check((await words(stepButton("carried"))) === "6 Carried out 10 of 11 confirmed" && (await page.locator("#steps .step.current.progress").count()) === 1,
    "Carried out should be in progress while a recipient has not confirmed");
  await openStep("decision");
  const overruledLines = await recLines();
  check((await words(rec.locator("h3"))) === "OCC approved Plan 2, Delay the rotation" && (await rec.locator(".rec-chip").count()) === 0 &&
    overruledLines[0] === "247 fewer lose their flight or connection than Cancel AR101 + AR102, 91 more owed compensation, €17,224 more" &&
    (await words(rec.locator(".against"))) === "The orchestrator's choice goes against ARROW's objectives on passengers who lose their flight or connection." &&
    (await words(rec.locator(".said"))) === "Orchestrator AI Recommended Plan 1: Cancel AR101 + AR102. Cancelling costs less and leaves fewer overnight passengers than delaying the rotation.",
    `The plan OCC approved should lead, with the orchestrator's other pick flagged against ARROW's objectives: ${await words(rec)}`);
  check((await words(page.locator('#step-decision .plan-why[data-plan-no="2"]'))) ===
    "Plan 2: Delay the rotation vs Plan 1 €17,224 more, 247 fewer lose their flight or connection, 91 more owed compensation, no cancellations",
    "The plan OCC chose should say what it does better");
  await openStep("carried");
  check((await words(page.locator("#done .done-line"))) === "… 10 of 11 changes confirmed · 1 waiting", `One line should say what is still waiting: ${await words(page.locator("#done .done-line"))}`);
  check((await words(page.locator('#done details[data-group="passengers"] > summary'))).endsWith("1 of 2 confirmed · waiting"), "An outcome should say what waits");
  const other = page.locator('#done details[data-group="other"]');
  await other.locator("summary").click();
  check((await words(other)).includes("1 other change") && (await words(other)).includes("Codeshare partner · Schedule change sent to the partner"),
    "An action ARROW does not know should still be listed");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-overruled.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The comparison should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-overruled.png" });
  await page.setViewportSize({ width: 1440, height: 900 });

  // 12c. Owner: the recommendation in its other shapes. The orchestrator's pick is cheaper but disrupts more passengers:
  // the line flags it. Three plans, one blocked: the two cancels read apart and the blocked plan says why in one line. No
  // recommendation: OCC chooses, the least disruptive plan against the cheapest.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${H}`);
  await band.locator(".rec-flag").waitFor();
  check((await bandWords(".t-why .tl")) === "Why plan 1 · Cancel, not delay" && (await bandWords(".verdict")) === "€17,224 less, but 247 more lose their flight Against ARROW's objectives" &&
    (await deltaRow()) === "! Keep flight −247 | ✓ Cost −€17,224 | ✓ Compensation −91 | ✓ Overnight −36",
    `A cheaper but more disruptive pick should read as such: ${await words(band)} | ${await deltaRow()}`);
  const flagTip = await tipOf(band.locator(".rec-flag"));
  check(flagTip.includes("The orchestrator's choice goes against ARROW's objectives on passengers who lose their flight or connection.") &&
    flagTip.includes("fewest passengers losing their flight or connection, lowest cost"), `The flag should spell out ARROW's objectives on hover: ${flagTip}`);
  await openStep("decision");
  const cheaper = (await recLines()).join(" | ");
  check((await words(rec.locator("h3"))) === "Recommended: Plan 1, Cancel AR101 + AR102" && cheaper === "247 more lose their flight or connection than Delay the rotation, 91 fewer owed compensation, €17,224 cheaper | " +
    "Why Delay the rotation costs more: 331 passengers arrive 3 h or more late, so €85,050 of EU261 compensation | " +
    "Cancelling AR101 and AR102 keeps AR131 and AR132 on time but leaves 324 passengers to rebook. The delay keeps everyone flying; 4 flights leave 2 h 55 min to 3 h 25 min late. | " +
    "Cost difference: airline +€13,216, rebooking +€354, passenger care −€6,194 (36 fewer passengers in hotels), EU261 compensation −€24,600",
    `A cheaper but more disruptive pick should read as such: ${cheaper}`);
  const dearerTip = (await cardOf(rec.locator(".rec-why .why"))).replace(/\s+/g, " ");
  check(dearerTip.startsWith("EU261 compensation +€24,600") && dearerTip.indexOf("Plan 2 · Delay the rotation €85,050") < dearerTip.indexOf("Plan 1 · Cancel AR101 + AR102 €60,450") &&
    dearerTip.includes("Plan 1 · Cancel AR101 + AR102 €60,450"), `The driver's card should read from the dearer plan's side: ${dearerTip}`);
  check((await words(rec.locator(".against"))) === "The orchestrator's choice goes against ARROW's objectives on passengers who lose their flight or connection." &&
    (await page.locator(".plan .against").count()) === 0, "The flag should sit in the recommendation, not on a card");
  await rec.locator(".against").hover();
  check((await page.locator("#tip").innerText()).includes("fewest passengers losing their flight or connection, lowest cost"), "The flag should spell out ARROW's objectives on hover");
  await page.mouse.move(2, 2);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rec-flag.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The flagged recommendation should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-rec-flag.png" });
  await page.setViewportSize({ width: 1440, height: 900 });

  // No text inside a plan card spills out of it or over its neighbour (owner: "here also not readable text").
  const overlaps = () => page.evaluate(() => {
    const bad = [];
    const visible = (el) => el.checkVisibility() && !el.closest("template");
    for (const card of document.querySelectorAll(".plan")) {
      const box = card.getBoundingClientRect();
      for (const el of card.querySelectorAll("*")) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width && (r.right > box.right + 1 || r.left < box.left - 1)) bad.push(`${card.dataset.planNo}: ${el.tagName}.${el.className} spills`);
      }
      for (const row of card.querySelectorAll(".chips, .numbers, .plan-head, .plan-diff, .plan-actions, .nb, .pb, .pb-chip")) {
        const kids = [...row.children].filter(visible).map(kid => [kid, kid.getBoundingClientRect()]);
        kids.forEach(([ka, a], i) => kids.slice(i + 1).forEach(([kb, b]) => {
          if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) bad.push(`${card.dataset.planNo}: ${ka.className || ka.tagName} over ${kb.className || kb.tagName}`);
        }));
      }
    }
    const cards = [...document.querySelectorAll(".plan")].map(card => card.getBoundingClientRect());
    cards.forEach((a, i) => cards.slice(i + 1).forEach(b => {
      if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) bad.push("cards overlap");
    }));
    return bad;
  });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${K}`);
  await band.locator(".verdict").waitFor();
  check((await page.locator(".plan h4").allTextContents()).join(" | ") === "Cancel AR941 + AR942 | Cancel AR689 + AR690 | Delay the rotation",
    "Two cancels should read apart by their flights");
  check((await bandWords(".t-why .tl")) === "Why plan 1, not plan 2" && (await bandWords(".verdict")) === "42 more keep their flight, €2,332 less" &&
    (await deltaRow()) === "✓ Keep flight +42 | ✓ Cost −€2,332 | ✓ Compensation −42 | ! Overnight +48",
    `Three plans: the summary weighs the recommendation against the next plan that can run: ${await words(band)} | ${await deltaRow()}`);
  check((await page.locator(".plan .numbers > div").count()) === 9 && await cellsFit() &&
    await page.evaluate(() => [...document.querySelectorAll(".plan")].every((card, i, all) => Math.abs(card.getBoundingClientRect().top - all[0].getBoundingClientRect().top) < 2)),
    "Three plan cards should sit side by side at 1440 px, every number inside its cell");
  check((await overlaps()).length === 0, `Nothing should overlap with three plans at 1440 px: ${(await overlaps()).join("; ")}`);
  const blockedRoute = await cardOf(page.locator(".plan").nth(2).locator(".route"));
  check((await words(page.locator(".plan").nth(2).locator(".route"))) === "Blocked" && blockedRoute.includes("Why it is blocked") &&
    blockedRoute.includes("Crew would exceed their flight-time limit and no standby crew is available"), `A blocked plan should say why on its chip: ${blockedRoute}`);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-three-plans.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await openStep("decision");
  const overTip = await cardOf(page.locator('#step-decision .who-plan[data-plan-no="3"] .chk[data-check="crew"]'));
  check(overTip.includes("Over the limit") && overTip.includes("D-LIS-131 · over") && overTip.includes("805 of 780 min") && overTip.includes("No standby crew covers the duty."),
    `The Crew chip should give the duty over its limit: ${overTip}`);
  const three = (await recLines()).join(" | ");
  check((await words(rec.locator("h3"))) === "Recommended: Plan 1, Cancel AR941 + AR942" && three === "42 fewer lose their flight or connection than Cancel AR689 + AR690, 42 fewer owed compensation, €2,332 cheaper | " +
    "Why Cancel AR689 + AR690 costs more: 42 more passengers arrive 2 h or more late after the cancellation, +€11,750 EU261 compensation | " +
    "Cancelling AR941 and AR942 flies AR689 and AR690 1 h late and leaves 168 passengers to rebook. Cancelling AR689 and AR690 keeps AR941 and AR942 on time but leaves 210 passengers to rebook. | " +
    "Cost difference: passenger care +€7,284 (48 passengers in hotels), airline +€2,134, EU261 compensation −€11,750 | " +
    "Not possible: Delay the rotation — crew over the flight-time limit, no standby crew", `Three plans, one blocked, should compare the two that can run: ${three}`);
  check((await rec.locator(".against").count()) === 0, "A pick with fewer passengers disrupted should not be flagged");
  await rec.locator(".rec-blocked").hover();
  check((await page.locator("#tip").innerText()).includes("crew would exceed their flight-time limit and no standby crew is available"), "The blocked line should explain itself on hover");
  await page.mouse.move(2, 2);
  const fuelTip = await cardOf(page.locator('#step-decision .cmp-parts [data-plan-no="3"][data-part="airline"]'));
  const tipLines = fuelTip.split(/\n+/);
  // Owner ("14 minutes faster only, that's weird"): the speed-up names each flight's minutes, and why no more.
  check(fuelTip.includes("4 flights, 221 min late in total × €17.78 a minute (EUROCONTROL) = €3,929") &&
    tipLines.includes("Fly faster: AR689 3 min, AR690 3 min, AR941 4 min, AR942 4 min (14 min) · fuel €1,196 + CO2 €141 = €1,337") &&
    tipLines.includes("3.5% of each flight's time in the air, where the minutes are worth their fuel. An A320 cruises at Mach 0.78 and may not exceed 0.82: about 5% faster, in cruise only.") &&
    tipLines.indexOf("AR689 makes up 3 of 3 min in the air · fuel €256 · worth more than its fuel at 30–60 min late") === tipLines.indexOf("AR689 +60 min · €1,067") + 1 &&
    tipLines.includes("AR941 makes up 4 of 4 min in the air · fuel €342 + CO2 €71 · worth more than its fuel at 30–60 min late") &&
    fuelTip.includes("CO2 allowances"), `Making up time in the air should name each flight's minutes, its fuel, CO2 and why no more: ${fuelTip}`);
  // A card's lines wrap inside it (no line runs past its edge), measured while it is open.
  const cardFits = async (locator) => {
    await page.mouse.move(2, 2);
    await page.locator("#tip-card").waitFor({ state: "hidden" });
    await locator.hover();
    await page.locator("#tip-card").waitFor();
    const fits = await page.locator("#tip-card").evaluate(card => {
      const box = card.getBoundingClientRect();
      return box.right <= innerWidth && box.width > 0 && [card, ...card.querySelectorAll("*")].every(el => el.scrollWidth <= el.clientWidth + 1 &&
        el.getBoundingClientRect().right <= box.right + 1);
    });
    await page.mouse.move(2, 2);
    return fits;
  };
  check(await cardFits(page.locator('#step-decision .cmp-parts [data-plan-no="3"][data-part="airline"]')), "The speed-up line and its basis should wrap inside the card");
  // The plan card's cost hover gives the same line and basis.
  await showPlans();
  const planFuel = await cardOf(page.locator(".plan").nth(2).locator('.nb[data-num="cost"]'));
  check(planFuel.includes("Fly faster: AR689 3 min, AR690 3 min, AR941 4 min, AR942 4 min (14 min)") && planFuel.includes("about 5% faster, in cruise only.") &&
    await cardFits(page.locator(".plan").nth(2).locator('.nb[data-num="cost"]')), `The cost hover should give the speed-up line and its basis, inside the card: ${planFuel}`);
  await openStep("decision");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rec-three.png" });
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  const narrowRec = await band.evaluate(box => {
    const outer = box.getBoundingClientRect();
    return { fits: [box, ...box.querySelectorAll("*")].filter(node => node.tagName !== "TEMPLATE").every(node => node.getBoundingClientRect().right <= innerWidth + 1),
      stacked: outer.bottom <= document.querySelector("#plans").getBoundingClientRect().top, width: outer.width };
  });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && narrowRec.fits && narrowRec.stacked && (await bandOverlaps()).length === 0,
    `The summary should stack inside a 390 px screen: ${JSON.stringify(narrowRec)} ${(await bandOverlaps()).join("; ")}`);
  check((await overlaps()).length === 0, `Nothing should overlap with three plans at 390 px: ${(await overlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-three-plans.png", fullPage: true });
  await tap(page.locator(".plan").nth(1).locator('.nb[data-num="lose"]'));
  await page.locator("#tip-card").waitFor();
  const paxBox = await page.locator("#tip-card").boundingBox();
  check(paxBox.x >= 0 && paxBox.x + paxBox.width <= 391 && (await page.locator("#tip-card").innerText()).includes("To Passenger Services"),
    `A tapped figure's card should open inside the screen, its values below or beside their labels: ${JSON.stringify(paxBox)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-tap-chip.png" });
  await page.waitForTimeout(400);
  await page.locator(".plan").nth(1).locator('.nb[data-num="lose"]').click();
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 1440, height: 900 });

  await page.goto(`${base}/src/ops_app/static/index.html?case=${N}`);
  await band.locator(".verdict").waitFor();
  check((await bandWords(".t-why .tl")) === "OCC chooses · 2 plans" && (await bandWords(".verdict")) === "Plan 2 is the least disruptive: 247 more keep their flight, €17,224 more" &&
    (await bandWords(".t-todo")) === "Your action Choose a plan OCC chooses · 2 plans",
    `Without a recommendation the summary should name the least disruptive plan against the cheapest: ${await words(band)}`);
  check((await page.locator(".plan .ribbon").count()) === 0 &&
    (await page.getByRole("button", { name: "Choose plan 1" }).count()) === 1 && (await page.getByRole("button", { name: "Choose plan 2" }).count()) === 1,
    "Without a recommendation no card compares itself to one, and each is a choice");
  await openStep("plans");
  check(await page.locator("#status-banner").isHidden() && !/ARROW recommends|Recommended/.test(await words(band)) && await page.locator("#plans-note").isVisible() &&
    (await page.locator("#plans-note").textContent()) === "Ranked by indicative cost.", "Without a recommendation nothing should claim one");
  await openStep("decision");
  const choose = await recLines();
  check((await words(rec.locator("h3"))) === "OCC chooses: 2 plans" && (await rec.locator(".rec-chip, .said, .against").count()) === 0 &&
    choose[0] === "Delay the rotation: 247 fewer lose their flight or connection than Cancel AR101 + AR102, 91 more owed compensation, €17,224 more" &&
    choose[1] === "Why Delay the rotation costs more: 331 passengers arrive 3 h or more late, so €85,050 of EU261 compensation" &&
    choose[3] === "Cost difference vs Cancel AR101 + AR102: EU261 compensation +€24,600, passenger care +€6,194 (36 passengers in hotels), rebooking −€354, airline −€13,216",
    `Without a recommendation OCC chooses, the least disruptive plan against the cheapest: ${choose.join(" | ")}`);
  check((await page.locator("#step-decision .plan-why").count()) === 0, "Without a recommendation no plan is compared to one");
  const standbyWho = await words(page.locator('#step-decision .who-plan[data-plan-no="2"] .who'));
  check(standbyWho.endsWith("a standby crew's report time is unknown; the standby crews changed since the plan was built; standby crews left today unknown") &&
    !/not established|: OCC decides/.test(standbyWho), `Unknown standby facts should read in plain words: ${standbyWho}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-rec-choose.png" });
  // Without a recommendation the orchestrator shows no plan's levers.
  await openStep("assessed");
  await page.locator('.node[data-node="orchestrator"]').click();
  const noPick = await page.locator("#trail-detail").innerText();
  check(noPick.includes("No plan recommended") && !noPick.includes("Levers"), `Without a recommendation no plan's levers should show: ${noPick.slice(0, 300)}`);
  await showPlans();
  await page.goto(`${base}/src/ops_app/static/index.html?case=${N3}`);
  await band.locator(".verdict").waitFor();
  check((await bandWords(".t-why .tl")) === "OCC chooses · 3 plans" && (await bandWords(".verdict")) === "Plan 3 is the least disruptive: 247 more keep their flight, €17,224 more" &&
    (await deltaRow()).startsWith("✓ Keep flight +247 | ! Cost +€17,224"),
    `With three plans the verdict should compare with the cheapest, as its card does: ${await bandWords(".t-why")} | ${await deltaRow()}`);
  check((await hoverText(band.locator(".verdict [data-card]"))).includes("against Plan 1"), "The verdict's card should compare the same plans");

  // 12f. Owner (Case 2): "I really dont get it why it even recommends the other options of cancelling". An automatic plan
  // that ran leads; the plans also built (older cases) fold into "Other options checked", until asked for.
  // The limits it ran within are the plan's own (policy.limits), whatever the rules in force today (review).
  const inForce = rules.in_force;
  rules.in_force = { ...inForce, version: 2, cost_cap_eur: 25000, rebook_limit: 12 };
  await page.goto(`${base}/src/ops_app/static/index.html?case=${R2}`);
  await page.locator("#case-title").filter({ hasText: "AR444 leaves 70 min late" }).waitFor();
  check((await stepButton("carried").getAttribute("aria-selected")) === "true" && await page.locator("#step-carried").isVisible(),
    "A carried-out case should open on Carried out");
  await showPlans();
  await page.locator(".others-fold").waitFor();
  check((await page.locator(".plan").count()) === 1 && (await words(page.locator(".plan.chosen .ribbon"))) === "Ran automatically" &&
    (await words(page.locator(".plan.chosen .plan-actions"))) === "✓ Carried out automatically · 2 of 2 confirmed Details ›",
    "An automatic case should show the plan that ran");
  // Owner ("passenger number (2) is a bit misleading"): the chips say what the plan does, in labels and numbers.
  check((await stripOf(page.locator(".plan.chosen"))) === "Retime 3 | Rebook 6 | Car 19", `Case 2's plan should say what it does: ${await stripOf(page.locator(".plan.chosen"))}`);
  const c2Rebook = await cardOf(page.locator('.plan.chosen .pb-chip[data-action="REBOOK"]'));
  check(c2Rebook.includes("6 passengers rebooked") && c2Rebook.includes("5 to Passenger Services"), `The Rebook chip should say who is rebooked: ${c2Rebook}`);
  check((await words(page.locator(".others-fold h3"))) === "Other options checked (2)" && (await words(page.locator(".others-fold .why"))) === "Not needed: the plan ran within every limit" &&
    (await page.locator(".others-fold li").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()))).join(" | ") ===
      "Plan 2: Cancel AR861 + AR862 €99,974 OCC decides | Plan 3: Cancel AR1053 + AR1054 €108,637 OCC decides",
    `The plans built but not needed should fold into one line each: ${await words(page.locator(".others-fold"))}`);
  // Owner ("the passenger service review is too much hidden"; prototype rev 2): the summary says why the plan ran without
  // OCC, in the automatic limits it stayed within, and that Passenger Services still has passengers, in amber, leading to them.
  await band.locator(".t-todo.action").waitFor();
  check((await bandWords(".t-todo")) === "Action needed Review 5 passengers Passenger Services · no seat today" &&
    (await bandWords(".t-why .tl")) === "Why plan 1 ran without OCC" && (await bandWords(".verdict")) === "Within every automatic limit · OCC informed" &&
    (await deltaRow()) === "✓ Cost €11,076 of €20,000 | ✓ Rebooked 6 of 10 | ✓ Cancellations 0 always OCC | ✓ Longest delay +70 min" &&
    (await band.locator(".t-why.ran").count()) === 1 && (await bandWords(".t-impact")) === "Impact 5 flights 11 passengers affected 0 crews need cover €11,076 plan 1",
    `An automatic case should say why it ran without OCC, and that Passenger Services still has passengers: ${await words(band)} | ${await deltaRow()}`);
  check((await band.locator(".t-todo .amber-btn").evaluate(el => getComputedStyle(el).backgroundImage)).includes("rgb(245, 165, 36)"),
    "The action needed should be an amber button in the summary");
  check((await hoverText(band.locator('.dt[data-delta="cost"]'))).includes("Cost limit for automatic plans €20,000 · rules v1"), "The automatic limit should open on hover, the plan's own");
  rules.in_force = inForce;
  await stepButton("assessed").filter({ hasText: "4 agents" }).waitFor();
  check(await stepRow() === "Reported:done Assessed:done Plans:done Rules:done Decision:done Carried out:current" &&
    (await words(stepButton("plans"))) === "✓ Plans 3 built · 1 ran" && (await words(stepButton("rules"))) === "✓ Rules Within every limit" &&
    (await words(stepButton("decision"))) === "✓ Decision Automatic" && (await words(stepButton("carried"))) === "6 Carried out 2 of 2 · 5 wait for Passenger Services" &&
    (await page.locator("#steps .step.current.warn").count()) === 1, `The steps should end at Carried out, waiting for Passenger Services: ${await words(page.locator("#steps ol"))}`);
  check((await overlaps()).length === 0 && (await bandOverlaps()).length === 0, `Nothing should overlap in an automatic case: ${[...await overlaps(), ...await bandOverlaps()].join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case2-auto.png" });
  await professional("Case 2 automatic");
  await band.getByRole("button", { name: "Review 5 passengers" }).click();
  await page.locator("#step-carried #review-handled").waitFor();
  await showPlans();
  await page.locator(".others-fold").getByRole("button", { name: "Show them" }).click();
  check((await page.locator(".plan .ribbon").allTextContents()).join(" | ") === "Ran automatically | Not needed | Not needed", "Show them should unfold the plans not needed");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case2-others.png" });
  await page.getByRole("button", { name: "Hide other options (2)" }).click();
  await openStep("plans");
  check((await page.locator("#step-plans .pl-tag").allTextContents()).join(" | ") === "Ran | Not needed | Not needed", "The Plans step should mark the plan that ran");
  await openStep("carried");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case2-carried.png" });
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await overlaps()).length === 0 && (await bandOverlaps()).length === 0,
    `The automatic case should fit 390 px: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-case2-auto.png" });
  await page.screenshot({ path: ".playwright-cli\\operator-390-case2-auto-full.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });

  // 12f2. Review: a plan's flights read against the case's own original times (GET /api/cases/{trace} originals, the times
  // its cost is priced against), never against an earlier case's delay that the map's day still publishes; once the plan ran, too.
  networkDay = c2Day;
  dayRows = [...list(), { trace_id: R3, display_id: docs[R3].display_id, created_at: docs[R3].created_at, status: "assessment_ready", headline: "AR444 · 30 min late",
    state: "needs", label: "Needs your decision", source: "Ground handling", disruption_type: "flight_delay", flight_no: "AR444",
    ...card([fl("AR444", "12:10", "12:40")], impact(19, 6, 21076, 1, "Delay the rotation", "occ"), 1) }];
  // The operator has the map's day open, then opens the case from the control tower.
  await page.goto(`${base}/src/ops_app/static/index.html`);
  await page.locator(`#tower-rows tr[data-trace="${R3}"]`).waitFor();
  await page.locator("#tab-map").click();
  await page.waitForFunction(() => Boolean(mapView.data));
  await page.locator("#tab-tower").click();
  await page.locator(`#tower-rows tr[data-trace="${R3}"] .tw-open`).click();
  const laterCase = page.locator(".plan").first();
  await laterCase.waitFor();
  // The plan's flights, in the Plans step.
  const laterFlights = page.locator('#step-plans .pl-plan[data-plan-no="1"] .plan-flights');
  const ownRows = async () => {
    if (!(await page.locator("#step-plans").isVisible())) await openStep("plans");
    if (!(await laterFlights.evaluate(d => d.open))) await laterFlights.locator("summary").click();
    return (await laterFlights.locator("tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()))).join(" | ");
  };
  const OWN_TIMES = "AR444 HAM → LIS 12:10 → 12:40 +30 min | AR861 LIS → MAD 16:04 → 16:34 +30 min | AR862 MAD → LIS 19:02 → 19:32 +30 min";
  // A redraw (the case read again after a decision) may land between reads: read until the rows settle.
  const settledRows = async (expected) => { for (let i = 0; i < 20; i++) { const rows = await ownRows(); if (rows === expected) return rows; await page.waitForTimeout(150); } return ownRows(); };
  check((await settledRows(OWN_TIMES)) === OWN_TIMES && (await words(laterFlights.locator("summary"))) === "Flights 3 later · up to +30 min",
    `A plan waiting for OCC should read against the case's own times, not the timetable before an earlier case: ${await ownRows()}`);
  await showPlans();
  await laterCase.getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  await page.locator("#decision-confirm").click();
  await page.locator(".plan.chosen").waitFor();
  check((await settledRows(OWN_TIMES)) === OWN_TIMES, `A plan carried out should still read against the case's own times: ${await ownRows()}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-own-times.png" });
  networkDay = null;
  dayRows = null;

  // 12g. Standby crews (EASA standby; Task 2's plan shape): a plan may call several; each reads as "call by, report"
  // (crew control calls well before the report), "standby crew" when one; the approval names them; a breach left after them
  // blocks the plan in today's words.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${SB}`);
  await page.locator("#case-title").filter({ hasText: "AR689 leaves 60 min late" }).waitFor();
  const standby = page.locator(".plan").nth(0);
  await standby.waitFor();
  // Each plan's checks are in the Decision step (since the summary), with their detail on hover.
  await openStep("decision");
  const whoCrew = (n) => page.locator(`#step-decision .who-plan[data-plan-no="${n}"] .chk[data-check="crew"]`);
  check((await words(whoCrew(1))) === "✓ Crew", "Two standby crews covering the duties should pass the crew check");
  const standbyTip = await cardOf(whoCrew(1));
  check(standbyTip.includes("Crew flight-time limits · Standby crews cover the duties") && standbyTip.includes("Standby crews R1, R2") &&
    standbyTip.includes("R1 · home standby · call by 15:54, report 16:54") && standbyTip.includes("R2 · airport standby · call by 14:05, report 14:05") &&
    standbyTip.includes("D-LIS-131 · would be over · covered") && standbyTip.includes("D-LIS-140 · would be over · covered") && !/· over\n/.test(standbyTip) &&
    standbyTip.includes("Standby crew: A crew on standby at home or at the airport, called out the same day (EASA standby).") && !/reserve/i.test(standbyTip) &&
    standbyTip.includes("Left today after this plan") && standbyTip.includes("Lisbon A320: 1 standby crew left") && standbyTip.includes("Ponta Delgada A320: 0 standby crews left") && !standbyTip.includes("Faro A320") &&
    !standbyTip.includes("Faro"),
    `The Crew chip should list the standby crews with when they are called and report: ${standbyTip}`);
  const stillCrew = await cardOf(whoCrew(2));
  check(stillCrew.includes("Over the limit") && stillCrew.includes("2 duties still over their limit after the standby crews") && stillCrew.includes("R2 · airport standby") &&
    stillCrew.includes("D-LIS-131 · over") && !stillCrew.includes("covered"), `The Crew chip should say the duties still over after the standby crews: ${stillCrew}`);
  const oneCrew = await cardOf(whoCrew(3));
  check(oneCrew.includes("1 duty still over its limit after the standby crew") && oneCrew.includes("Standby crew R3"),
    `One duty after one standby crew should read in the singular: ${oneCrew}`);
  await whoCrew(1).hover();
  await page.locator("#tip-card").waitFor();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-standby.png" });
  await page.mouse.move(2, 2);
  await showPlans();
  const standbyRoute = await cardOf(standby.locator(".route"));
  check(standbyRoute.includes("Why OCC decides") && standbyRoute.includes("The plan uses the last standby crew at Ponta Delgada A320"), `The standby rule should say why OCC decides: ${standbyRoute}`);
  const conflictRoute = await cardOf(page.locator(".plan").nth(2).locator(".route"));
  check(conflictRoute.includes("Why it is blocked") && conflictRoute.includes("Standby crew R3 cannot reach AR775 in time") && !/disagree/.test(conflictRoute),
    `What the agents disagree on should read in plain words: ${conflictRoute}`);
  const stillOver = page.locator(".plan").nth(1);
  const stillTip = await cardOf(stillOver.locator(".route"));
  check(stillTip.includes("Why it is blocked") && stillTip.includes("Crew would still exceed their flight-time limit after the standby crews"),
    `A breach left after the standby crews should block in today's words: ${stillTip}`);
  // A plan that cannot run is never weighed against the recommendation: the summary compares only plans that can run.
  check((await bandWords(".verdict")) === "The only plan the rules allow" && (await band.locator(".dt").count()) === 0,
    `Plans the rules block should not be traded off: ${await words(band)}`);
  await openStep("decision");
  check((await words(page.locator('#step-decision .who-plan[data-plan-no="2"] .who'))) === "Blocked: crew would still exceed their flight-time limit after the standby crews" &&
    (await recLines()).includes("Not possible: Delay the rotation, 30 min more — crew still over the flight-time limit after the standby crews") &&
    (await recLines()).includes("Not possible: Delay the rotation, 15 min more — standby crew R3 cannot reach AR775 in time") &&
    (await words(page.locator('#step-decision .who-plan[data-plan-no="3"] .who'))) === "Blocked: standby crew R3 cannot reach AR775 in time",
    `The Decision step should give the standby reason in today's words: ${(await recLines()).join(" | ")}`);
  await showPlans();
  await standby.getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const standbyEffects = (await page.locator("#decision-effects li").allInnerTexts()).map(line => line.replace(/\s+/g, " ").trim());
  check(standbyEffects.includes("Crew system · assign standby crews R1, R2"), `The approval preview should name the standby crews: ${standbyEffects.join(" | ")}`);
  const standbyEffectTip = await tipOf(page.locator("#decision-effects li").filter({ hasText: "standby crews" }));
  check(standbyEffectTip.includes("Standby crew: A crew on standby"), `The preview should explain a standby crew on hover: ${standbyEffectTip}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-standby-approve.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  await professional("Standby crews");

  // 12g2. Owner (Case 4: "quite confusing for the user why you recommend cancelling, but that's due to the crew limits"): when
  // the delay takes a crew over its duty limit, one short line above the plans says so, and what covers it. The summary counts the
  // crew the delay takes over its limit (it needs cover) in amber and weighs the orchestrator's plan against the next, in signed trade-offs.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${C4}`);
  await band.locator(".verdict").waitFor();
  check((await bandWords(".t-impact")) === "Impact 4 flights 168 passengers affected 1 crew needs cover €81,338 plan 1" && (await band.locator('.stat.warn[data-stat="crews"]').count()) === 1 &&
    (await bandWords(".t-todo")) === "Your action Approve plan 1 Before 13:10 · AR689" &&
    (await bandWords(".t-why .tl")) === "Why plan 1 · Cancel, not delay" && (await bandWords(".verdict")) === "127 more lose their flight, for €76,072 more Against ARROW's objectives" &&
    (await deltaRow()) === "! Keep flight −127 | ! Cost +€76,072 | ! Compensation +168 | ! Overnight +48" && (await band.locator(".rec-flag").count()) === 1,
    `Case 4's summary should count the crew over its limit and weigh the recommendation against the delay: ${await words(band)} | ${await deltaRow()}`);
  const crewOver = await hoverText(band.locator('.stat[data-stat="crews"]'));
  check(crewOver.includes("Ponta Delgada crew · 11 min over its duty limit"), `The crew the delay takes over its limit should be named on hover: ${crewOver}`);
  check((await bandOverlaps()).length === 0, `Nothing in Case 4's summary should overlap at 1440 px: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-summary-case4.png" });
  const whyOthers = page.locator("#rec-note");
  check(await whyOthers.isVisible() && (await words(whyOthers)) === "The delay puts a Ponta Delgada crew 11 min over its duty limit: a standby crew covers it, or a round trip is cancelled.",
    `One line should say why a cancellation is considered: ${await words(whyOthers)}`);
  const [recBox, noteBox, belowBox] = [await page.locator("#steps").boundingBox(), await whyOthers.boundingBox(), await page.locator("#plans").boundingBox()];
  check(noteBox.y >= recBox.y + recBox.height - 1 && noteBox.y + noteBox.height <= belowBox.y + 1 && noteBox.height <= 40,
    `The line should sit under the steps, above the plans, on one line: ${JSON.stringify([recBox, noteBox, belowBox])}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case4-why.png" });
  await professional("Case 4 why other plans");
  await page.setViewportSize({ width: 390, height: 844 });
  const note390 = await whyOthers.evaluate(el => ({ fits: el.scrollWidth <= el.clientWidth + 1, ...el.getBoundingClientRect().toJSON() }));
  check(note390.fits && note390.left >= 0 && note390.right <= 391 && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) &&
    (await bandOverlaps()).length === 0, `On a phone the line and the summary should fit inside the screen: ${JSON.stringify(note390)} ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-case4-why.png" });
  await page.screenshot({ path: ".playwright-cli\\operator-390-summary-case4.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // No standby crew covers it (Case 4 of a Dev run): the line says so; a case whose delay keeps every crew within its limit has none.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${K}`);
  await band.locator(".verdict").waitFor();
  check((await words(whyOthers)) === "The delay puts a crew 25 min over its duty limit: no standby crew covers it, so a round trip is cancelled.",
    `A delay no standby crew covers should say so: ${await words(whyOthers)}`);
  // Review: standby crews that cover only some of the duties say how many are still over.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${C5}`);
  await band.locator(".verdict").waitFor();
  check((await words(whyOthers)) === "The delay puts 2 crews up to 25 min over their duty limits: standby crews cover 1; 1 still over, so a round trip is cancelled.",
    `Standby crews covering only some duties should say how many are still over: ${await words(whyOthers)}`);
  await page.goto(`${base}/src/ops_app/static/index.html?case=${H}`);
  await band.locator(".verdict").waitFor();
  check(await whyOthers.isHidden(), "A delay that keeps every crew within its limit should add no line");

  // 12h. Markup in a plan's name, a duty id or a standby crew's id reads as text on the card, its hover cards, the steps and the
  // approval preview, and never runs.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${XS}`);
  const xssCard = page.locator(".plan").first();
  await xssCard.locator("h4").filter({ hasText: "Hold" }).waitFor();
  const injected = () => page.evaluate(() => {
    const planted = (root) => root.querySelectorAll('img[src^="x-"], svg[onload]').length + [...root.querySelectorAll("b")].filter(b => b.textContent === "wait").length;
    return { ran: window.__xss ?? null, nodes: planted(document),
      templates: [...document.querySelectorAll("template")].reduce((sum, template) => sum + planted(template.content), 0) };
  });
  check((await xssCard.locator("h4").textContent()) === xssName && (await cardOf(band.locator(".verdict [data-card]"))).includes(xssName),
    `A plan's name should read as text: ${await xssCard.locator("h4").textContent()}`);
  await openStep("decision");
  const xssCrewTip = await cardOf(page.locator('#step-decision .who-plan[data-plan-no="1"] .chk[data-check="crew"]'));
  check(xssCrewTip.includes(`${xssDuty} · would be over · covered`) && xssCrewTip.includes(xssCrew), `A duty id and a standby crew's id should read as text: ${xssCrewTip}`);
  for (const step of ["plans", "decision", "rules"]) {
    await openStep(step);
    if (step !== "rules") check((await page.locator("#drawer-body").innerText()).includes(xssName), `The ${step} step should name the plan as text`);
  }
  await showPlans();
  await xssCard.getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  check((await page.locator("#decision-title").textContent()).includes(xssName) &&
    (await page.locator("#decision-effects").innerText()).includes(xssCrew), `The approval preview should show the markup as text: ${await page.locator("#decision-dialog").innerText()}`);
  const leaked = await injected();
  check(leaked.ran === null && leaked.nodes === 0 && leaked.templates === 0, `Markup should never become HTML: ${JSON.stringify(leaked)}`);
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await overlaps()).length === 0, "The standby case should fit 390 px");
  const standbyChip = page.locator(".plan").first().locator('.pb-chip[data-action="STANDBY"]');
  await tap(standbyChip);
  await page.locator("#tip-card").waitFor();
  const standbyBox = await page.locator("#tip-card").boundingBox();
  check(standbyBox.x >= 0 && standbyBox.x + standbyBox.width <= 391 && (await page.locator("#tip-card").innerText()).includes("Standby crews"),
    `The standby card should open inside a 390 px screen: ${JSON.stringify(standbyBox)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-standby.png" });
  await page.waitForTimeout(400);
  await standbyChip.click();
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 1440, height: 900 });

  // 12d. Ask about this case (case chat spec §5; case chat prototype). The header button counts the
  // kept turns; the panel docks on the right; the Case Assistant explains itself on hover; each turn says who asked and when;
  // HTML in a question or answer is text; a refusal is amber; each answer lists its calls; plan numbers lead to their cards; a
  // what-if is a dashed card tagged What-if after ARROW's plans, never recommended; the case is read again while an answer is
  // pending, and no longer once none is; the server's refusals show plainly; on a phone the panel is a full-screen sheet.
  const caseReads = (trace) => requests.filter(r => r.method === "GET" && r.path === `/api/cases/${trace}`).length;
  const waitReads = async (trace, count) => { for (let i = 0; i < 24 && caseReads(trace) < count; i++) await page.waitForTimeout(500); return caseReads(trace) >= count; };
  docs[W].chat.at(-1).asked_at = new Date().toISOString();
  await page.goto(`${base}/src/ops_app/static/index.html?case=${W}`);
  const askButton = page.locator("#case-ask");
  const chatPanel = page.locator("#chat");
  const chatLog = page.locator("#chat-log");
  await page.locator("#case-title").filter({ hasText: "CS-TQA out of service · 4 h" }).waitFor();
  // Since the summary the header's tools are icons, each named on hover and in its accessible name.
  const askName = async () => `${await askButton.getAttribute("aria-label")} | ${await words(askButton)}`;
  check((await askName()) === "Ask about this case · 6 questions | 6" && await chatPanel.isHidden() && (await tipOf(askButton)).includes("Ask about this case"),
    `The header should offer the chat with its turn count: ${await askName()}`);
  // The chat docks on the right; the step shown under the case stays as it is, and choosing another step keeps the chat open.
  await openStep("reported");
  await askButton.click();
  await chatPanel.waitFor();
  check(await page.locator("#step-reported").isVisible() && (await stepButton("reported").getAttribute("aria-selected")) === "true",
    "Opening the chat should leave the step shown");
  await stepButton("assessed").click();
  await page.locator("#step-assessed").waitFor();
  check(await chatPanel.isVisible() && (await askButton.getAttribute("aria-expanded")) === "true", "Choosing a step should keep the chat open");
  await showPlans();
  await askButton.click();
  await chatPanel.waitFor({ state: "hidden" });
  await askButton.click();
  await chatPanel.waitFor();
  check((await askButton.getAttribute("aria-expanded")) === "true", "The button should say the panel is open");
  const [panelBox, paneBox] = [await chatPanel.boundingBox(), await page.locator("#case").boundingBox()];
  check(Math.abs(panelBox.x + panelBox.width - 1440) < 2 && panelBox.width >= 380 && panelBox.width <= 440 && paneBox.x + paneBox.width <= panelBox.x + 1 &&
    paneBox.width >= 900, `The panel should dock on the right beside the case: ${JSON.stringify([panelBox, paneBox])}`);
  check((await words(chatPanel.locator(".chat-kept"))) === "Kept on the case record", "The panel should say the conversation is kept");
  check(await chatLog.evaluate(log => log.scrollHeight - log.scrollTop - log.clientHeight < 2), "The panel should open on the latest turn");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat.png" });
  const assistantCard = await cardOf(page.locator("#chat-agent"));
  check((await words(page.locator("#chat-agent"))).startsWith("Case Assistant") && assistantCard.includes("read_case") && assistantCard.includes("what_if") &&
    /cannot/i.test(assistantCard) && /approve/i.test(assistantCard) && assistantCard.includes("GPT-4.1") && !/mini|gpt-/.test(assistantCard) && assistantCard.includes("case-assistant-agent") &&
    assistantCard.includes("Entra") && assistantCard.includes("An AI assistant") && assistantCard.includes("OCC decides") && assistantCard.includes("on the case record"),
    `The Case Assistant should give its role, tools, limits, identity and model on hover: ${assistantCard}`);
  // EU AI Act transparency (owner): the assistant is marked as AI, with the one discreet mark (owner: no label chains);
  // its card says it is an AI assistant and that OCC decides.
  check((await words(page.locator("#chat-agent"))) === "Case Assistant AI" && (await page.locator("#chat-agent .mk-ai").count()) === 1,
    "The Case Assistant should be marked as AI");
  await page.locator("#chat-agent").hover();
  await page.locator("#tip-card").waitFor();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat-agent.png" });
  await page.mouse.move(2, 2);
  const bys = (await chatLog.locator(".chat-by").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim());
  check(bys.slice(0, 4).join(" | ") === "Rui Matos 10:12 | Case Assistant AI 10:12 3 s | Ana Costa 11:31 | Case Assistant AI 11:31 14 s" &&
    bys.at(-1) === "Case Assistant AI", `Each question should say who asked and when, each answer when and how long, marked as AI: ${bys.join(" | ")}`);
  // Owner ("I dont see any progress bar or anything"): an answer on its way shows a moving bar and the seconds so far.
  const waitBox = chatLog.locator(".chat-wait");
  check((await waitBox.count()) === 1 && (await waitBox.locator(".busy-bar i").evaluate(el => getComputedStyle(el).animationName)) === "busy" &&
    /^Answering \d+ s$/.test(await words(waitBox)) && (await waitBox.getAttribute("role")) === "status",
    `A pending answer should show its progress: ${await words(waitBox)}`);
  const firstCount = await words(waitBox.locator("[data-since]"));
  await page.waitForTimeout(2100);
  check((await words(waitBox.locator("[data-since]"))) !== firstCount, `The seconds should count on while the answer is pending: ${firstCount}`);
  check((await chatLog.locator("img").count()) === 0 && (await chatLog.innerText()).includes(XSS) && (await page.evaluate(() => window.__chatXss)) === undefined,
    "HTML in a question should show as text");
  check((await chatLog.innerText()).includes("No answer: the Case Assistant did not answer in time; ask again"), "A failed turn should say why and to ask again");
  const refusal = chatLog.locator(".chat-a.refused");
  check((await refusal.count()) === 1 && (await refusal.innerText()).includes("I can't approve") && await refusal.evaluate(el => {
    const [r, g, b] = getComputedStyle(el).borderTopColor.match(/\d+/g).map(Number);
    return r > g && g > b;
  }), "A refusal should show in amber");
  // Calls lists the run's own calls, each agent asked followed by the system calls it made for that turn, as the decision trail
  // lists them apart from the assessment (read from the trail once the panel is open).
  await chatLog.locator('[data-entry="w2"] details.chat-how > summary').filter({ hasText: /Calls\s*3/ }).waitFor();
  const hows = (await chatLog.locator("details.chat-how > summary").allInnerTexts()).map(text => text.replace(/\s+/g, " ").trim());
  check(hows.join(" | ") === "Calls 1 | Calls 3 | Calls 4", `Each answer should count its calls: ${hows.join(" | ")}`);
  const w3 = chatLog.locator('[data-entry="w3"]');
  await w3.locator("details.chat-how > summary").click();
  const callRows = await w3.locator(".chat-call").evaluateAll(rows => rows.map(row => `${row.querySelector(".sys").textContent.trim()} = ${row.querySelector(".acc").textContent.trim()}`));
  check(callRows.join(" | ") === "ARROW = Read only | Aircraft Recovery Agent = Read only | Operations control = Read only | ARROW = Adds a plan",
    `Each call should name its system or agent and say read only or adds a plan: ${callRows.join(" | ")}`);
  const [readCall, askCall, schedCall, whatIfCall] = await w3.locator(".chat-call").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check(readCall === "ARROW Read the case Read only DSR-20261110-444444 Interface Tool call (MCP) to ARROW · read_case" &&
    askCall === "Aircraft Recovery Agent Live check Read only Options if CS-TQA is free 30 min later? → Delay the rotation with 30 min more. " +
      "Interface Agent to agent (A2A) question to Aircraft Recovery Agent",
    `Each call should say what it did and asked in words, its interface last: ${readCall} | ${askCall}`);
  check(schedCall === "Operations control Recovery options 2: delay, cancel, +30 min Read only By the Aircraft Recovery Agent " +
    "Interface Tool call (MCP) to Operations control · get_recovery_options",
    `The agent's system call should follow it, read only, without ARROW's own reads for the what-if: ${schedCall}`);
  check(whatIfCall.includes("DSR-20261110-444444") && whatIfCall.includes("+30 min") && whatIfCall.includes("→ Plan 3, OCC decides") &&
    whatIfCall.endsWith("Interface Tool call (MCP) to ARROW · what_if") &&
    !/[{}_]/.test(whatIfCall.replace("what_if", "")) && !whatIfCall.includes("What if maintenance"),
    `A call should say what it asked in words, without JSON, field names or the agent's wording of the question: ${whatIfCall}`);
  const w2 = chatLog.locator('[data-entry="w2"]');
  await w2.locator("details.chat-how > summary").click();
  const crewRows = await w2.locator(".chat-call").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()));
  check(crewRows[2] === "Crew system Crew impact Delay, no duty over its limit Read only By the Crew Recovery Agent " +
    "Interface Tool call (MCP) to Crew system · assess_crew_recovery",
    `The Crew agent's system call should read in words, its interface last: ${crewRows.join(" | ")}`);
  // A turn kept before the calls were worded still reads without JSON.
  const w1 = chatLog.locator('[data-entry="w1"]');
  await w1.locator("details.chat-how > summary").click();
  const keptCall = await words(w1.locator(".chat-call"));
  check(keptCall.includes("Read the case") && keptCall.includes("DSR-20261110-444444") && keptCall.endsWith("Interface Tool call (MCP) to ARROW · read_case") && !/[{}]/.test(keptCall),
    `An older turn's call should read in words: ${keptCall}`);
  await professional("Case chat");
  await w1.locator("details.chat-how > summary").click();
  await w2.locator("details.chat-how > summary").click();
  // Owner ("not that intuitive"): the what-if an answer added reads as its card does, three numbers and who decides, with a
  // link to its card.
  check((await words(w3.locator(".chat-whatif"))) === "3 Delay the rotation, 30 min more What-if Lose flight 96 Owed compensation 352 Cost €124,238 OCC decides Show plan 3 ›",
    `The answer should show the what-if it added: ${await words(w3.locator(".chat-whatif"))}`);
  await w3.scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat-calls.png" });
  const suggested = await chatPanel.locator("#chat-suggest button").allInnerTexts();
  check(suggested.join(" | ") === "What makes up the €117,198? | Why is Plan 2 recommended? | Does Plan 2 keep the crew within their limits?",
    `Suggested questions should come from the case, without a what-if already asked: ${suggested.join(" | ")}`);
  check(await chatPanel.locator("#chat-suggest button").first().isDisabled() && await page.locator("#chat-input").isDisabled(), "Nothing can be asked while an answer is pending");
  check((await words(chatPanel.locator("#chat-limits"))) === "What-if plans left: 2 of 3",
    `The footer should count the what-ifs left; what the assistant cannot do is on its card: ${await words(chatPanel.locator("#chat-limits"))}`);
  check(!/mocked|synthetic/i.test(await chatPanel.innerText()), "The panel should not say mocked or synthetic");
  // The what-if among the plans: after ARROW's plans, numbered on, dashed and tagged, never recommended; OCC approves it like any plan.
  const whatIfCard = page.locator(".plan.what-if");
  check((await page.locator(".plan").count()) === 3 && (await whatIfCard.count()) === 1 && (await words(whatIfCard.locator(".rank"))) === "3" &&
    (await words(whatIfCard.locator("h4"))) === "Delay the rotation, 30 min more" &&
    (await words(whatIfCard.locator(".ribbon"))) === "What-if" && !(await whatIfCard.innerText()).includes("Recommended") &&
    (await whatIfCard.evaluate(el => getComputedStyle(el).borderTopStyle)) === "dashed", "The what-if should be a dashed card tagged What-if, never Recommended");
  check((await words(whatIfCard.locator(".what-if-note"))) === "Asked in chat by Ana Costa at 11:40: What if maintenance needs 30 more minutes?" &&
    (await whatIfCard.getByRole("button", { name: "Choose plan 3" }).count()) === 1, "The what-if should say who asked it, when and what, and OCC may approve it");
  check((await cardNumbers(whatIfCard)).join(" | ") === "96 lose flight | 46 no seat today | €124,238 cost", `The what-if should read in its own figures: ${(await cardNumbers(whatIfCard)).join(" | ")}`);
  // A what-if is never weighed in the summary: it keeps the recommendation against ARROW's own next plan.
  check((await page.locator(".plan.recommended").count()) === 1 && (await words(page.locator(".plan.recommended .rank"))) === "2" &&
    (await bandWords(".t-why .tl")) === "Why plan 2 · Delay, not cancel" && (await bandWords(".verdict")) === "247 more keep their flight, €17,224 more" &&
    (await band.locator(".rec-flag").count()) === 0 && (await words(stepButton("plans"))) === "✓ Plans 2 built · 1 what-if",
    "A what-if should keep the plan numbers and the recommendation as they were");
  check((await page.locator("#plans-note").textContent()) === "ARROW's plans ranked by indicative cost; what-ifs follow in the order asked.",
    `The plans note should not call a what-if ranked by cost: ${await page.locator("#plans-note").textContent()}`);
  // Plan numbers in an answer lead to their cards, from any step.
  await openStep("assessed");
  check(await chatPanel.isVisible(), "Assessed should open beside the chat, which stays open");
  // How ARROW decided holds the assessment only (final review): the what-if asked in chat is a dashed node with no
  // agent wires, its minutes are not ARROW's added minutes, the orchestrator built two plans, and its panel says who asked
  // and lists the reads ARROW made to build it.
  const whatIfNode = page.locator('.node[data-node="plan:plan-whatif-30"]');
  await whatIfNode.waitFor();
  await page.waitForFunction(() => document.querySelectorAll("svg.wires path").length > 0);
  check((await words(whatIfNode.locator(".gn-name"))) === "Plan 3" && (await words(whatIfNode.locator(".gn-chip.whatif"))) === "What-if" &&
    (await whatIfNode.getAttribute("aria-label")).includes("a what-if asked in chat") && await whatIfNode.evaluate(el => el.classList.contains("gn-whatif") &&
    getComputedStyle(el).borderTopStyle === "dashed"), `The what-if should be a dashed node marked as asked in chat: ${await words(whatIfNode)}`);
  // Each wire names the steps it joins and ends on the step it leads to; the wires follow their steps when the chat docks beside
  // the case or leaves it (the case's width changes without a new render).
  const wireEnds = () => page.evaluate(() => {
    const flow = document.querySelector(".flow").getBoundingClientRect();
    const endOf = (d) => {
      let x = 0, y = 0;
      for (const [, cmd, args] of d.matchAll(/([MHVQ])([^MHVQ]*)/g)) {
        const n = args.trim().split(/[\s,]+/).map(Number);
        if (cmd === "M") [x, y] = n; else if (cmd === "H") x = n[0]; else if (cmd === "V") y = n[0]; else [x, y] = n.slice(2);
      }
      return [x, y];
    };
    return [...document.querySelectorAll("svg.wires path.wire")].map(path => {
      const box = document.querySelector(`.flow .node[data-node="${CSS.escape(path.dataset.b)}"]`).getBoundingClientRect();
      const [x, y] = endOf(path.getAttribute("d"));
      const on = x >= box.left - flow.left - 2 && x <= box.right - flow.left + 2 && y >= box.top - flow.top - 2 && y <= box.bottom - flow.top + 2;
      return [path.dataset.a, path.dataset.b, on];
    });
  });
  const offStep = async () => (await wireEnds()).filter(([, , on]) => !on).map(([a, b]) => `${a}>${b}`);
  const frames = () => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  const ends = await wireEnds();
  check(ends.length > 0 && !ends.some(([a, b]) => a.startsWith("agent:") && b === "plan:plan-whatif-30") &&
    ends.filter(([a, b]) => a === "plan:plan-whatif-30" && b === "rules").length === 1 && !(await offStep()).length,
    `No agent should be wired to the what-if; ARROW's rules check it too: ${JSON.stringify(ends)}`);
  await askButton.click();
  await chatPanel.waitFor({ state: "hidden" });
  await frames();
  const offWide = await offStep();
  await askButton.click();
  await chatPanel.waitFor();
  await frames();
  const offDocked = await offStep();
  check(!offWide.length && !offDocked.length, `The wires should follow their steps when the chat leaves and docks again: ${offWide.join(" ")} | ${offDocked.join(" ")}`);
  check((await valueOf("plan:plan-delay")) === "Delay +15 min" && (await whatIfNode.locator(".gn-add").count()) === 0,
    `The what-if's minutes should not count as ARROW's added minutes: ${await valueOf("plan:plan-delay")} | ${await words(whatIfNode)}`);
  await whatIfNode.click();
  const whatIfPanel = page.locator("#trail-detail");
  const whatIfDetail = (await whatIfPanel.innerText()).replace(/\s+/g, " ");
  check(/^Plan 3 Delay the rotation, 30 min more What-if/i.test(whatIfDetail) && whatIfDetail.includes("Asked In chat by Ana Costa at 11:40: What if maintenance needs 30 more minutes?") &&
    (await whatIfPanel.locator("details.tv-call").count()) === 2 && /How ARROW built it/i.test(whatIfDetail) &&
    whatIfDetail.includes("Passengers 96 lose flight or connection, 352 owed compensation") && whatIfDetail.includes("Flights 4") &&
    whatIfDetail.includes("Cost parts Airline €15,558, EU261 compensation €86,850, Passenger care €13,892, Rebooking €7,938"),
    `The what-if's panel should say who asked it and list ARROW's reads to build it: ${whatIfDetail.slice(0, 400)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-trail-whatif.png" });
  // An older run (the orchestrator read the report): the Detection agent shows what it concluded, never a record it did not make.
  check((await valueOf("agent:detection")) === "2 flights affected",
    "An older run's Detection agent should show its own conclusion");
  await page.locator("#trail-convo > summary").click();
  const conversationText = await page.locator("#step-assessed").innerText();
  check(conversationText.includes("built 2 plans and recommended one") && !conversationText.includes("built 3 plans"),
    `The conversation should count the orchestrator's plans only: ${conversationText.slice(0, 600)}`);
  await w3.locator(".chat-whatif").getByRole("button", { name: "Show plan 3 ›" }).click();
  check(await whatIfCard.evaluate(el => el.classList.contains("flash")), "Show plan 3 should mark the card");
  await page.waitForFunction(() => { const box = document.querySelector('.plan[data-plan-no="3"]')?.getBoundingClientRect(); return box && box.top >= 0 && box.bottom <= innerHeight; });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat-whatif.png" });
  await chatLog.locator('[data-entry="w1"] .chat-a button.plan-ref', { hasText: "Plan 1" }).click();
  await page.waitForFunction(() => { const box = document.querySelector('.plan[data-plan-no="1"]')?.getBoundingClientRect(); return box && box.top >= 0 && box.bottom <= innerHeight; });
  // Read again while pending; a pending answer older than 5 minutes reads as interrupted, and reading stops.
  check(await waitReads(W, caseReads(W) + 2), "The case should be read again while an answer is pending");
  docs[W].chat.at(-1).asked_at = new Date(Date.now() - 6 * 60000).toISOString();
  await chatLog.getByText("No answer (interrupted); ask again").waitFor();
  check(await page.locator("#chat-input").isEnabled(), "An interrupted answer should free the question box");
  const interrupted = caseReads(W);
  await page.waitForTimeout(4500);
  check(caseReads(W) === interrupted, "With no answer pending the case should not be read again");

  // Asking: a case waiting for OCC suggests a what-if and has three left; the server's refusal reads plainly and keeps the
  // question; a question posts with the operator header and shows as typed; the answer arrives by itself, then reading stops.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${H}`);
  await page.locator("#case-title").filter({ hasText: "CS-TQA out of service · 4 h" }).waitFor();
  check((await askName()) === "Ask about this case | ", `A case without questions should show no count: ${await askName()}`);
  await askButton.click();
  await chatPanel.waitFor();
  const offered = await chatPanel.locator("#chat-suggest button").allInnerTexts();
  check(offered.join(" | ") === "What makes up the €99,974? | Why is Plan 1 recommended? | Does Plan 1 keep the crew within their limits? | What if maintenance needs 30 more minutes?" &&
    (await words(chatPanel.locator("#chat-limits"))) === "What-if plans left: 3 of 3", `A case waiting for OCC should suggest a what-if: ${offered.join(" | ")}`);
  chatRefusal = { status: 409, detail: "This case already keeps 50 questions, the most it takes" };
  await page.locator("#chat-input").fill("Is AR131 at risk?");
  await chatPanel.getByRole("button", { name: "Ask", exact: true }).click();
  await page.locator("#chat-notice").filter({ hasText: "This case already keeps 50 questions, the most it takes" }).waitFor();
  check((await page.locator("#chat-input").inputValue()) === "Is AR131 at risk?" && await page.locator("#notice").isHidden(), "A refusal should show plainly in the panel and keep the question");
  // Another duty manager asked first: refused as busy, the panel reads the case again, shows their question pending and
  // keeps reading until their answer arrives (final review).
  docs[H].chat = [chatTurn("o1", "00:00:00", "Rui Matos", "Is the crew rested enough for AR131?",
    { status: "pending", response_id: null, asked_at: new Date().toISOString() })];
  chatRefusal = { status: 409, detail: "An answer is pending on this case; ask again once it arrives" };
  await chatPanel.getByRole("button", { name: "Ask", exact: true }).click();
  await page.locator("#chat-notice").filter({ hasText: "An answer is pending on this case; ask again once it arrives" }).waitFor();
  await chatLog.locator(".chat-by", { hasText: "Rui Matos" }).waitFor();
  check((await chatLog.innerText()).includes("Is the crew rested enough for AR131?") && (await chatLog.locator(".chat-wait").count()) === 1 &&
    await page.locator("#chat-input").isDisabled() && (await page.locator("#chat-input").inputValue()) === "Is AR131 at risk?",
    "A 409 should show the other manager's pending question and keep this one");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat-busy.png" });
  Object.assign(docs[H].chat[0], { status: "answered", answered_at: new Date(Date.parse(docs[H].chat[0].asked_at) + 5000).toISOString(),
    answer: "Yes: the crew of AR131 is within its duty limits on Plan 1." });
  await chatLog.getByText("Yes: the crew of AR131 is within its duty limits on Plan 1.").waitFor();
  await page.waitForFunction(() => !document.querySelector("#chat-input").disabled);
  const beforeAsk = caseReads(H);
  await page.locator("#chat-input").fill(XSS);
  await page.locator("#chat-input").press("Enter");
  await chatLog.locator(".chat-wait").waitFor();
  const asked = requests.filter(r => r.method === "POST" && r.path === `/api/cases/${H}/chat`).at(-1);
  check(asked.body.question === XSS && asked.headers["x-arrow-request"] === "operator", `The question should post as typed with the operator header: ${JSON.stringify(asked)}`);
  check((await page.locator("#chat-input").inputValue()) === "" && await page.locator("#chat-notice").isHidden() && (await askName()) === "Ask about this case · 2 questions | 2" &&
    (await chatLog.locator("img").count()) === 0 && (await chatLog.innerText()).includes(XSS), "The question should be kept and shown as typed");
  check(await waitReads(H, beforeAsk + 3), "The case should be read again while the answer is pending");
  const hEntry = docs[H].chat.at(-1);
  docs[H].options = [...docs[H].options, { ...whatIfPlan, plan_id: "plan-whatif-h", option_id: "plan-whatif-h", policy: { route: "occ", reasons: ["asked in chat: OCC decides"] },
    what_if: { question: XSS, asked_by: { oid: OCC, name: "Duty Manager" }, asked_at: hEntry.asked_at, entry_id: hEntry.id } }];
  docs[H].version += 1;
  Object.assign(hEntry, { status: "answered", answered_at: new Date(Date.parse(hEntry.asked_at) + 6000).toISOString(), what_if_plan_ids: ["plan-whatif-h"],
    calls: [readCase, { kind: "tool", name: "what_if", title: "Asked ARROW for a what-if plan", access: "adds a plan", endpoint: "MCP what_if",
      detail: "DSR-20261110-111111 · aircraft_option_id delay → Plan 3 · OCC decides" }],
    answer: "AR131 keeps its time on Plan 1. I asked ARROW for Plan 3, a what-if. <script>window.__chatXss=2</script>" });
  await chatLog.locator(".chat-by").filter({ hasText: "Case Assistant" }).filter({ hasText: "6 s" }).waitFor();
  // A what-if asked from this screen lands in view with a brief glow (owner: "not that intuitive").
  await page.locator("#plans .plan.what-if.landed").waitFor();
  check((await page.locator("#plans .plan.landed").count()) === 1 && (await stepButton("decision").getAttribute("aria-selected")) === "true",
    "A what-if asked here should land in view in the Decision step with a glow");
  check((await chatLog.innerText()).includes("<script>window.__chatXss=2</script>") && (await page.evaluate(() => window.__chatXss)) === undefined, "An answer should show as text too");
  const hCard = page.locator(".plan.what-if");
  await hCard.waitFor();
  const hRoute = await cardOf(hCard.locator(".route"));
  check(hRoute.includes("Why OCC decides") && hRoute.includes("A what-if never runs automatically") && (await words(hCard.locator(".what-if-note"))).endsWith(`: ${XSS}`) &&
    (await hCard.locator("img").count()) === 0, `A what-if the rules would run should wait for OCC, its question as text: ${hRoute}`);
  check((await words(chatPanel.locator("#chat-limits"))) === "What-if plans left: 2 of 3" && await page.locator("#chat-input").isEnabled(), "The answer should free the box and count the what-if");
  const answeredReads = caseReads(H);
  await page.waitForTimeout(4500);
  check(caseReads(H) === answeredReads, "Once answered the case should not be read again");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-chat-answer.png" });
  // Owner ("I dont see any progress bar or anything"): a what-if being built shows its progress in the chat and as a
  // placeholder where its card will land, the chat links to it, and its card comes into view with a glow when it lands.
  const wipAsk = "What if maintenance needs 45 more minutes?";
  await page.locator("#chat-input").fill(wipAsk);
  await page.locator("#chat-input").press("Enter");
  const wipChat = chatLog.locator(".chat-wait.what-if");
  await wipChat.waitFor();
  const wip = page.locator("#plans .plan.wip");
  await wip.waitFor();
  check(/^Building the what-if plan \d+ s Show in Decision ›$/.test(await words(wipChat)) && (await words(wip.locator(".rank"))) === "4" &&
    (await words(wip.locator("h4"))) === wipAsk && /^Building the plan \d+ s$/.test(await words(wip.locator(".wip-t"))) &&
    /^Asked in chat by Duty Manager at \d{2}:\d{2}$/.test(await words(wip.locator(".wip-by"))) && (await wip.locator(".busy-bar i").count()) === 1 &&
    (await wip.evaluate(el => getComputedStyle(el).borderTopStyle)) === "dashed",
    `A what-if being built should show in the chat and as a placeholder in Decision: ${await words(wipChat)} | ${await words(wip)}`);
  await page.waitForFunction(() => { const box = document.querySelector("#plans .plan.wip")?.getBoundingClientRect(); return box && box.top >= 0 && box.bottom <= innerHeight; });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-whatif-asked.png" });
  await openStep("assessed");
  await wipChat.getByRole("button", { name: "Show in Decision ›" }).click();
  await page.waitForFunction(() => { const box = document.querySelector("#plans .plan.wip")?.getBoundingClientRect(); return box && box.top >= 0 && box.bottom <= innerHeight; });
  check((await stepButton("decision").getAttribute("aria-selected")) === "true", "The chat's link should open the Decision step on the placeholder");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-whatif-building.png" });
  const wipEntry = docs[H].chat.at(-1);
  docs[H].options = [...docs[H].options, { ...whatIfPlan, plan_id: "plan-whatif-45", option_id: "plan-whatif-45", title: "Delay the rotation, 45 min more",
    levers: { ...whatIfPlan.levers, extra_delay_minutes: 45 }, policy: { route: "occ", reasons: ["asked in chat: OCC decides"] },
    what_if: { question: wipAsk, asked_by: { oid: OCC, name: "Duty Manager" }, asked_at: wipEntry.asked_at, entry_id: wipEntry.id } }];
  docs[H].version += 1;
  Object.assign(wipEntry, { status: "answered", answered_at: new Date(Date.parse(wipEntry.asked_at) + 9000).toISOString(), what_if_plan_ids: ["plan-whatif-45"],
    answer: "Plan 4 is the what-if with 45 more minutes. Like the others it needs OCC." });
  const landed = page.locator('#plans .plan.what-if[data-plan-no="4"]');
  await landed.waitFor();
  check(await landed.evaluate(el => el.classList.contains("landed")) && (await page.locator("#plans .plan.wip").count()) === 0,
    "The what-if should land with a glow in its placeholder's place");
  await page.waitForFunction(() => { const box = document.querySelector('#plans .plan[data-plan-no="4"]')?.getBoundingClientRect(); return box && box.top >= 0 && box.bottom <= innerHeight; });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-whatif-landed.png" });
  await chatLog.locator('[data-whatif="4"]').waitFor();
  check((await chatLog.locator(".chat-wait").count()) === 0 && (await words(chatLog.locator('[data-whatif="4"] button'))) === "Show plan 4 ›",
    "The answer should link to the what-if's card");

  // An executed case: no what-if is offered or counted.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${B}`);
  await page.locator("#case-title").filter({ hasText: "AR259 leaves 20 min late" }).waitFor();
  await askButton.click();
  await chatPanel.waitFor();
  const executedAsks = await chatPanel.locator("#chat-suggest button").allInnerTexts();
  check(executedAsks.join(" | ") === "What makes up the €900? | Does Plan 1 keep the crew within their limits?" && !(await words(chatPanel.locator("#chat-limits"))).includes("What-if") &&
    (await words(chatLog)) === "No questions on this case yet.", `An executed case should offer no what-if: ${executedAsks.join(" | ")}`);

  // A phone: a full-screen sheet without sideways scroll; a plan link closes it on the card.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${W}`);
  await page.locator("#case-title").filter({ hasText: "CS-TQA out of service · 4 h" }).waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "The case header should not overflow at 390 px");
  await askButton.click();
  await chatPanel.waitFor();
  const sheet = await chatPanel.boundingBox();
  check(Math.round(sheet.x) === 0 && Math.round(sheet.y) === 0 && Math.round(sheet.width) === 390 && Math.round(sheet.height) === 844 &&
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `At 390 px the panel should be a full-screen sheet without sideways scroll: ${JSON.stringify(sheet)}`);
  await chatLog.locator('[data-entry="w2"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-390-chat.png" });
  await chatLog.locator('[data-entry="w2"] .chat-src button', { hasText: "Plan 2" }).click();
  await chatPanel.waitFor({ state: "hidden" });
  await page.waitForFunction(() => { const box = document.querySelector('.plan[data-plan-no="2"]')?.getBoundingClientRect(); return box && box.top >= -2 && box.top < innerHeight / 2; });
  await page.screenshot({ path: ".playwright-cli\\operator-390-chat-plan.png" });
  await page.setViewportSize({ width: 1440, height: 900 });

  // 12e. Case 5 (Task 4c; now a crosswind): gusts across Lisbon's runway read from a TAF. The tower and the case header name the storm and its window;
  // the reading line gives the rate, the TAF as received and a plain reading on hover. Plans moving more than six flights read as
  // numbers, their flights behind one Flights row in a compact list; the storm's delay plan is never "Delay the rotation"; the
  // blocked reason says the reserve crew was not enough; the airline cost lists the minutes held in the air; compensation is not
  // owed for weather (owner); the chat offers no what-if.
  dayRows = [...list(), stormRow];
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const stormTower = page.locator(`#tower-rows tr[data-trace="${S}"]`);
  await stormTower.waitFor();
  const stormTowerText = (await stormTower.innerText()).replace(/\s+/g, " ");
  check(stormTowerText.includes("Strong crosswind at Lisbon · 16:00–18:00") && stormTowerText.includes("Cancel AR131 + AR132 + AR881 + AR882") &&
    !stormTowerText.includes("Delay the rotation") && !stormTowerText.includes("movements an hour"),
    `The control tower should name the forecast by its subject and window, and its plan: ${stormTowerText}`);
  // Its lane keeps the window in view: the hazard, then the place and window.
  const stormLane = await page.locator(".tw-lane").filter({ hasText: "Strong crosswind" }).locator(".tw-who").evaluate(el => ({
    text: el.innerText.replace(/\s+/g, " ").trim(), fits: [el, ...el.querySelectorAll("*")].every(node => node.scrollWidth <= node.clientWidth + 1) }));
  check(stormLane.text === "Strong crosswind Lisbon · 16:00–18:00" && stormLane.fits, `The storm's lane should show its window: ${JSON.stringify(stormLane)}`);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-tower-weather.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${S}`);
  await page.locator("#case-title").filter({ hasText: "Strong crosswind at Lisbon · 16:00–18:00" }).waitFor();
  // Today lists the storm with its whole window, kept on one line.
  const stormRail = await page.locator(`.case-row[data-case="${S}"] strong`).evaluate(el => ({ text: el.textContent,
    fits: el.scrollWidth <= el.clientWidth + 1, window: el.querySelector(".tw-keep")?.getClientRects().length ?? 0 }));
  check(stormRail.text === "Strong crosswind at Lisbon · 16:00–18:00" && stormRail.fits && stormRail.window === 1, `Today should show the storm's window: ${JSON.stringify(stormRail)}`);
  // Reported: the TAF as received, in monospace, TAF spelled out on hover; the Detection agent reads it (owner).
  await openStep("reported");
  const stormMessage = page.locator("#case-message");
  check((await stormMessage.innerText()).trim() === TAF && /mono|consolas/i.test(await stormMessage.evaluate(el => getComputedStyle(el).fontFamily)) &&
    !(await page.locator("#step-reported").innerText()).includes("read it as"), "The weather case's report should be the TAF as received, in monospace, unread");
  await stormMessage.hover();
  await page.locator("#tip").waitFor();
  const taf = await page.locator("#tip").innerText();
  check(taf.startsWith("TAF (Terminal Aerodrome Forecast) as received") && taf.includes("the airport's official weather forecast for the next 24–30 hours"),
    `TAF should be spelled out on hover: ${taf}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-weather-taf.png" });
  await page.mouse.move(2, 2);
  // The summary's disruption keeps the forecast as received with its plain reading: the rate's basis cites what it was set on.
  const stormWhat = await cardOf(page.locator(".t-what .big"));
  check(stormWhat.includes(TAF) && stormWhat.includes("Strong crosswind forecast between 16:00 and 18:00, gusts 33 kt across runway 02/20; " +
    "ARROW plans Lisbon at 24 movements an hour, the airline's OCC playbook rate for crosswind on Lisbon's single runway: the A320 family's demonstrated crosswind is 38 kt including gusts (35 kt for take-off with the A320neo's engines), so operations continue with care.") && !/assumption/i.test(stormWhat),
    `The disruption's card should show the forecast and a plain reading: ${stormWhat}`);
  await showPlans();
  const stormPlans = page.locator(".plan");
  check((await page.locator(".plan h4").allTextContents()).join(" | ") === "Cancel AR131 + AR132 + AR881 + AR882 | Accept the reduced Lisbon rate",
    `The storm's plans should be named by what they do: ${(await page.locator(".plan h4").allTextContents()).join(" | ")}`);
  const stormCells = [(await cardNumbers(stormPlans.nth(0))).join(" | "), (await cardNumbers(stormPlans.nth(1))).join(" | ")];
  check(stormCells[0] === "655 lose flight | 614 no seat today | €210,377 cost" && stormCells[1] === "0 lose flight | 0 no seat today | €58,533 cost",
    `A plan moving many flights should read as numbers: ${stormCells.join(" || ")}`);
  check(await cellsFit(), "The storm's plan cards should fit every number at 1440 px");
  // Owner: weather owes no EU261 compensation; the cost says so, and what would otherwise be owed, on hover.
  const notOwed = await cardOf(stormPlans.nth(0).locator('.nb[data-num="cost"]'));
  check(notOwed.includes("Not owed · weather is an extraordinary circumstance") && notOwed.includes("653 passengers · €187,100 if it were owed") &&
    notOwed.includes("Care and rebooking still owed") && !/If weather/.test(notOwed), `The cost should say compensation is not owed for weather: ${notOwed}`);
  // The full list sits behind each plan's Flights row in the Plans step: compact, scrollable, by departure.
  await openStep("plans");
  const stormFlightsOf = (n) => page.locator(`#step-plans .pl-plan[data-plan-no="${n}"] details.plan-flights`);
  check((await words(stormFlightsOf(1).locator("summary"))) === "Flights 4 cancelled · 75 later · up to +1 h 20 min" &&
    (await words(stormFlightsOf(2).locator("summary"))) === "Flights 81 later · up to +1 h 20 min · 1 on time", "A plan moving many flights should count them");
  await stormFlightsOf(2).locator("summary").click();
  const delayRows = await stormFlightsOf(2).locator("tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  const departs = await stormFlightsOf(2).locator("tbody tr").evaluateAll(rows => rows.map(row => row.dataset.departs));
  const scroller = await stormFlightsOf(2).locator(".flight-scroll").evaluate(el => ({ scrolls: el.scrollHeight > el.clientHeight + 20, height: el.clientHeight }));
  check(delayRows.length === 82 &&
    delayRows[0] === "AR532 BER → LIS 13:10 → 13:42 +32 min" && departs.every((value, i) => !i || departs[i - 1] <= value) && scroller.scrolls && scroller.height <= 320 &&
    (await stormFlightsOf(2).locator("svg").count()) === 0, `The Flights row should list every flight by departure: ${delayRows.slice(0, 3).join(" | ")} (${JSON.stringify(scroller)})`);
  // A flight held in the air lands late even when it leaves on time; one that also leaves late says both.
  check(delayRows.includes("AR870 OPO → LIS 16:50 Lands 6 min late · held in the air") && delayRows.includes("AR864 OPO → LIS 16:40 → 16:59 +19 min · held 6 min in the air") &&
    !delayRows.some(row => row.endsWith("On time")), `A flight held in the air should say it lands late: ${delayRows.filter(row => /AR8(64|70)/.test(row)).join(" | ")}`);
  await stormFlightsOf(1).locator("summary").click();
  const cancelRows = await stormFlightsOf(1).locator("tbody tr").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()));
  check(cancelRows.length === 79 && cancelRows.includes("AR131 LIS → FAO 14:10 Cancelled") && cancelRows.includes("AR882 FAO → LIS 17:00 Cancelled"),
    `A cancelled flight should say so in the list: ${cancelRows.slice(0, 7).join(" | ")}`);
  await showPlans();
  const stormDelayCard = stormPlans.nth(1);
  // The route says why on hover: more than one cancelled flight is counted; a crew duty the standby crew does not cover reads
  // in its own words, not "no standby crew".
  const cancelRoute = await cardOf(stormPlans.nth(0).locator(".route"));
  check(cancelRoute.includes("Why OCC decides") && cancelRoute.includes("4 flights are cancelled") && cancelRoute.includes("A weather forecast moves flights across the network") &&
    cancelRoute.includes("5 arrivals fall in Lisbon's night quota (00:27–00:55)") && !/a flight is cancelled|weather:/.test(cancelRoute), `Cancelled flights should be counted: ${cancelRoute}`);
  const delayRoute = await cardOf(stormDelayCard.locator(".route"));
  check(delayRoute.includes("Why it is blocked") && delayRoute.includes("Crew would still exceed their flight-time limit after the standby crew") && !delayRoute.includes("no standby crew is available") &&
    delayRoute.includes("Standby crew R3 cannot report in time; a weather plan cannot wait for it"),
    `The blocked plan should say the standby crew was not enough: ${delayRoute}`);
  // A plan stored before Task 2 carries one reserve crew: it reads as one standby crew (its checks in the Decision step).
  await openStep("decision");
  const oneStandby = await cardOf(page.locator('#step-decision .who-plan[data-plan-no="1"] .chk[data-check="crew"]'));
  check(oneStandby.includes("Standby crew covers the duty") && oneStandby.includes("Standby crew R1") && oneStandby.includes("R1 · standby · report 13:00") && !/reserve/i.test(oneStandby),
    `An older plan with one reserve crew should still read: ${oneStandby}`);  await openStep("decision");
  const stormWho = await words(page.locator('#step-decision .who-plan[data-plan-no="2"] .who'));
  const cancelWho = await words(page.locator('#step-decision .who-plan[data-plan-no="1"] .who'));
  check(cancelWho.startsWith("Needs OCC approval because 4 flights are cancelled; a weather forecast moves flights across the network;") &&
    !/a flight is cancelled|weather:/.test(await page.locator("#step-decision").innerText()), `Cancelled flights should be counted: ${cancelWho}`);
  check(stormWho === "Blocked: crew would still exceed their flight-time limit after the standby crew; standby crew R3 cannot report in time; a weather plan cannot wait for it",
    `The blocked plan should say the standby crew was not enough: ${stormWho}`);
  const stormRec = (await recLines()).join(" | ");
  check((await words(rec.locator("h3"))) === "Recommended: Plan 1, Cancel AR131 + AR132 + AR881 + AR882" &&
    stormRec.includes("Not possible: Accept the reduced Lisbon rate — crew still over the flight-time limit after the standby crew") &&
    stormRec.includes("5 arrivals fall in Lisbon's night quota (00:27–00:55)") && !/LIS night quota/.test(stormRec), `The recommendation should read the storm's plans: ${stormRec}`);
  const stormBlockedTip = await tipOf(rec.locator(".rec-blocked"));
  check(stormBlockedTip.includes("crew would still exceed their flight-time limit after the standby crew") && !stormBlockedTip.includes("no standby crew is available"),
    `The blocked line should explain itself on hover: ${stormBlockedTip}`);
  await showPlans();
  // Approving the cancellation previews the storm's moves as a count, not 75 flight numbers, and does not leave them out.
  await stormPlans.nth(0).getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  // The executor's own planned actions (115): a group of more than six flights is counted, the night-quota requests read as one.
  const STORM_EFFECTS = ["Slot coordinator · request night-quota slots for 5 Lisbon arrivals (00:27–00:55)",
    "Operations control · retime 75 flights, up to 80 min later", "Operations control · cancel AR131, AR132, AR881, AR882",
    "EUROCONTROL Network Manager · delay messages for 68 departures", "EUROCONTROL Network Manager · cancellation messages for AR131, AR132, AR881, AR882",
    "Lisbon airport (A-CDM) · new ready times for 29 Lisbon departures", "Slot coordinator · release the slots of AR131 at LIS, AR132 at LIS, AR881 at LIS, AR882 at LIS",
    "Crew system · assign standby crew R1", "Passenger system · rebook 41 passengers", "Ground handling · drive 101 passengers to their connecting flight",
    "Passenger Services · 614 passengers outside the rebooking policy go to an expert"].join(" | ");
  const stormEffects = (await page.locator("#decision-effects li").allInnerTexts()).map(line => line.replace(/\s+/g, " ").trim()).join(" | ");
  check(stormEffects === STORM_EFFECTS, `The approval preview should count the storm's moves: ${stormEffects}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-weather-approve.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  // Minutes held in the air before landing are priced like the minutes made up; the weather note sits under compensation.
  await openStep("decision");
  const stormParts = page.locator("#step-decision .cmp-parts");
  const stormAirline = await cardOf(stormParts.locator('[data-plan-no="1"][data-part="airline"]'));
  check(stormAirline.includes("1 flight holds 6 min in the air before landing: fuel €512 + CO2 €106 = €618") &&
    stormAirline.includes("AR864 holds 6 min in the air before landing · fuel €512 + CO2 €106 · fuel at cruise burn (conservative)") &&
    stormAirline.includes("75 flights, 2,374 min late in total × €17.78 a minute (EUROCONTROL) = €42,210") &&
    stormAirline.includes("4 narrow-body cancellations × €13,320 (EUROCONTROL) = €53,280") && stormAirline.split(/\n+/).length <= 10,
    `The airline cost should list the holding like the made-up minutes, as totals for a storm: ${stormAirline}`);
  const stormCompensation = await cardOf(stormParts.locator('[data-plan-no="1"][data-part="compensation"]'));
  check(stormCompensation.includes("Not owed · weather is an extraordinary circumstance") && stormCompensation.includes("653 passengers · €187,100 if it were owed") &&
    stormCompensation.includes("Care and rebooking still owed") && !stormCompensation.includes("× €250"), `Compensation should say weather owes none: ${stormCompensation}`);
  const noCompensation = await cardOf(stormParts.locator('[data-plan-no="2"][data-part="compensation"]'));
  check(!noCompensation.includes("Weather may excuse") && !noCompensation.includes("if it were owed"), `A plan that would owe nothing has nothing to excuse: ${noCompensation}`);
  // Crews over their limit as ARROW counts them after the standby crews (plan.outcomes), only when a plan leaves one over.
  const crewsRow = await stormParts.locator('tr[data-row="crews"]').evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()).join(" / "));
  check(crewsRow === "Crews over limit 0 1" && (await stormParts.locator('tr[data-row="overnight"]').count()) === 0,
    `Plans by part should count the crews still over their limit after the standby crews: ${crewsRow}`);
  check(!(await page.locator("body").innerText()).includes("Delay the rotation") &&
    !(await page.evaluate(() => [...document.querySelectorAll("template")].some(t => t.content.textContent.includes("Delay the rotation")))),
    "The storm's delay plan should never be called Delay the rotation");
  await professional("Weather case");
  await showPlans();
  // The approval dialog's close redraws the steps; both lists stay open. The delay plan's list shows the flights held in the air.
  await openStep("plans");
  check(await stormFlightsOf(1).evaluate(d => d.open) && await stormFlightsOf(2).evaluate(d => d.open), "A redraw should keep the flight lists the operator opened");
  await stormFlightsOf(2).locator(".flight-scroll").evaluate(box => { box.scrollTop = box.querySelector("tr[data-departs$='T16:40:00']")?.offsetTop - 40 || 0; });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-weather.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const n of [1, 2]) await stormFlightsOf(n).locator("summary").click();
  await showPlans();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-case5.png" });
  // The Case Assistant: no what-if (a storm plan takes no added delay); why the delay is blocked; what the cancellation costs.
  await askButton.click();
  await chatPanel.waitFor();
  const stormAsks = await chatPanel.locator("#chat-suggest button").allInnerTexts();
  check(stormAsks.includes("Why is the delay plan blocked?") && stormAsks.includes("What makes up the cancellation's cost?") && !stormAsks.some(ask => /^What if/.test(ask)),
    `A storm should offer its own questions and no what-if: ${stormAsks.join(" | ")}`);
  await page.locator("#chat-close").click();
  // Rules: the storm's plans carry the fixed weather rule, in the catalogue's order.
  await openStep("rules");
  await page.locator('#trail-checks tr[data-check="cost"]').waitFor();
  const stormOrder = await page.locator("#trail-checks tr[data-check]").evaluateAll(rows => rows.map(row => row.dataset.check).join(","));
  const weatherCheck = (await page.locator('#trail-checks tr[data-check="weather"]').innerText().catch(() => "")).replace(/\s+/g, " ");
  check(stormOrder === "cost,rebooking,aircraft,cancellation,airport,weather,crew,eu261,capacity,answered,passenger_services" &&
    weatherCheck.startsWith("Weather plans Always OCC") && weatherCheck.includes("Crosswind forecast") && (weatherCheck.match(/→ OCC/g) || []).length === 2,
    `The storm's rule checks should show the weather rule as OCC's: ${stormOrder} / ${weatherCheck}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-weather-checks.png" });
  await showPlans();
  // A phone: the same title, numbers, list and wording; the TAF on tap (focus); the holding line in the cost's card and breakdown.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${S}&step=reported`);
  await page.locator("#case-title").filter({ hasText: "Strong crosswind at Lisbon · 16:00–18:00" }).waitFor();
  await page.locator("#case-message.forecast").waitFor();
  await page.locator("#case-message").focus();
  const phoneTaf = await page.locator("#tip").innerText();
  const phoneTip = await page.locator("#tip").boundingBox();
  check(phoneTaf.startsWith("TAF (Terminal Aerodrome Forecast) as received") && phoneTip.x >= 0 && phoneTip.x + phoneTip.width <= 390,
    `On a phone TAF should be spelled out on focus, inside the screen: ${phoneTaf} ${JSON.stringify(phoneTip)}`);
  await page.locator("#case-message").blur();
  await page.screenshot({ path: ".playwright-cli\\operator-390-weather-reported.png" });
  await showPlans();
  check((await words(stormPlans.nth(1).locator(".route"))) === "Blocked", "At 390 px the storm's cards should read the same");
  await openStep("plans");
  check((await words(stormFlightsOf(2).locator("summary"))) === "Flights 81 later · up to +1 h 20 min · 1 on time", "At 390 px the storm's flights should read the same");
  if (!(await stormFlightsOf(2).evaluate(d => d.open))) await stormFlightsOf(2).locator("summary").click();
  check((await stormFlightsOf(2).locator("tbody tr").count()) === 82, "At 390 px the Flights row should list every flight");
  const heldList = await stormFlightsOf(2).locator(".flight-scroll").evaluate(box => {
    box.scrollTop = box.querySelector("tr[data-departs$='T16:40:00']")?.offsetTop - 40 || 0;
    return { wide: box.scrollWidth - box.clientWidth };
  });
  await stormFlightsOf(2).locator(".flight-scroll").scrollIntoViewIfNeeded();
  check(heldList.wide <= 1, `At 390 px the list should not scroll sideways: ${JSON.stringify(heldList)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-weather-held.png" });
  await showPlans();
  await tap(stormPlans.nth(0).locator('.nb[data-num="cost"]'));
  await page.locator("#tip-card").waitFor();
  const phoneCostCard = await page.locator("#tip-card").innerText();
  const phoneCostBox = await page.locator("#tip-card").boundingBox();
  check(phoneCostCard.includes("1 flight holds 6 min in the air before landing") && phoneCostCard.includes("Not owed · weather is an extraordinary circumstance") &&
    phoneCostBox.x >= 0 && phoneCostBox.x + phoneCostBox.width <= 391, `On a phone a tap on the cost should open its workings inside the screen: ${phoneCostCard}`);
  await page.waitForTimeout(400);
  await stormPlans.nth(0).locator('.nb[data-num="cost"]').click();
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await overlaps()).length === 0, "The storm case should not overflow at 390 px");
  await professional("390 px weather case");
  await page.screenshot({ path: ".playwright-cli\\operator-390-weather.png", fullPage: true });
  await openStep("decision");
  await page.locator('#step-decision .cost-detail[data-plan-no="1"] > summary').click();
  const phoneCost = await page.locator('#step-decision .cost-detail[data-plan-no="1"]').innerText();
  check(phoneCost.includes("AR864 holds 6 min in the air before landing") && phoneCost.includes("Not owed · weather is an extraordinary circumstance"), `The cost breakdown should hold the same lines: ${phoneCost}`);
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The Decision step should not overflow at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-weather-decision.png" });
  await showPlans();
  await stormPlans.nth(0).getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const phoneEffects = (await page.locator("#decision-effects li").allInnerTexts()).map(line => line.replace(/\s+/g, " ").trim()).join(" | ");
  const phoneDialog = await page.locator("#decision-dialog").boundingBox();
  check(phoneEffects === STORM_EFFECTS && phoneDialog.x >= 0 && phoneDialog.x + phoneDialog.width <= 390 &&
    await page.locator("#decision-effects").evaluate(list => [...list.querySelectorAll("li")].every(li => li.scrollWidth <= li.clientWidth + 1)),
    `At 390 px the approval preview should read the same and fit: ${phoneEffects} ${JSON.stringify(phoneDialog)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-weather-approve.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  dayRows = null;
  await page.setViewportSize({ width: 1440, height: 900 });

  // 12f. The OCC playbook (OCC playbook spec; owner: "passenger number (2) is a bit misleading"): each plan card says what the
  // plan does in one compact line of labels and numbers (Retime 81 · Hold 18 · Standby 2 · Car 131 · Notify 9,412), with Advice beside
  // them only when the advice says something; each action's facts open on hover, focus or tap (the standby crews called at the
  // forecast, the passengers told their new times). Refusals and actions not used stay in the Decision step's checklist. The storm's
  // plans keep their own names; the recommendation's card says what is done ahead of the storm; a storm owes no EU261 compensation
  // (owner); crews over their limit are ARROW's count after the standby crews, never the Crew agent's duty rows; the approval
  // and Carried out read the passenger notice in plain words. A plan stored before the playbook says what it does from its own
  // figures; a fact is text.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${ST}`);
  await page.locator("#case-title").filter({ hasText: "Strong crosswind at Lisbon" }).waitFor();
  const stormPb = page.locator(".plan");
  await stormPb.nth(1).locator(".pb").waitFor();
  const [rateCard, thinCard] = [stormPb.nth(0), stormPb.nth(1)];
  const shownEverywhere = async () => page.evaluate(() => [document.body.innerText, ...[...document.querySelectorAll("template")].map(t => t.content.textContent),
    ...[...document.querySelectorAll("[data-tip]")].map(el => el.dataset.tip)].join(" "));
  check((await page.locator(".plan h4").allTextContents()).join(" | ") === "Accept the reduced Lisbon rate | Cancel AR131 + AR132 + AR881 + AR882" &&
    (await page.locator(".plan .plan-sub, .plan[data-stance], .rec-stance, .stance-t").count()) === 0 && !/Act now|Wait and react/i.test(await shownEverywhere()),
    `The storm's plans should keep their own names, with no stances: ${(await page.locator(".plan h4").allTextContents()).join(" | ")}`);
  check(await stripOf(rateCard) === "Retime 81 | Hold 18 | Standby 2 | Car 131 | Notify 9,412 | Advice" &&
    await stripOf(thinCard) === "Retime 75 | Hold 17 | Cancel 4 | Standby 1 | Rebook 41 | Car 101 | Notify 8,650 | Advice",
    `Each plan should say what it does in labels and numbers: ${await stripOf(rateCard)} / ${await stripOf(thinCard)}`);
  // The storm opens with the same summary as any case for now (its now and when-confirmed stages come later): the forecast, its
  // impact, Approve plan 1 by the first standby crew's call time, and why accepting the reduced rate rather than cancelling.
  check((await bandWords(".t-what")) === "Disruption Strong crosswind forecast" && (await bandWords(".t-todo")) === "Your action Approve plan 1 Standby call 09:45" &&
    /^Impact \d+ flights 9,412 passengers affected 2 crews need cover €58,533 plan 1$/.test(await bandWords(".t-impact")) &&
    (await bandWords(".t-why .tl")) === "Why plan 1 · Delay, not cancel" && (await bandWords(".verdict")) === "Nobody loses a flight, for €151,844 less" &&
    (await deltaRow()) === "✓ Keep flight +655 | ✓ Cost −€151,844 | ✓ No seat today −614 | ! Standby crews +1",
    `The storm should open with a summary like any case: ${await words(band)} | ${await deltaRow()}`);
  const bandTaf = await hoverText(band.locator(".t-what .big"));
  check(bandTaf.includes("TAF (Terminal Aerodrome Forecast) as received") && bandTaf.includes("TEMPO 1016/1018"), `The forecast should open the TAF as received: ${bandTaf}`);
  check((await bandOverlaps()).length === 0, `Nothing in the storm's summary should overlap at 1440 px: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-summary-storm.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await bandOverlaps()).length === 0,
    `The storm's summary should fit 390 px: ${(await bandOverlaps()).join("; ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-summary-storm.png" });
  await page.setViewportSize({ width: 1440, height: 900 });
  check((await rateCard.locator('.pb[role="list"] > .pb-chip[role="listitem"]').count()) === 6 && !/Aircraft \d|Crew \d|Passengers \d/.test(await stripOf(thinCard)) &&
    (await page.locator(".plan .pb-chip").filter({ hasText: /refused|not needed|not in this plan|not used/i }).count()) === 0,
    "The chips should list what the plan does, not desk counts; refusals and actions not used stay in the Decision step");
  check((await rateCard.locator(".pb").evaluate(el => el.getBoundingClientRect().height)) <= 26, "The chips should take one line");
  const crewDesk = await cardOf(rateCard.locator('.pb-chip[data-action="STANDBY"]'));
  check(crewDesk.includes("Crew control") && crewDesk.includes("Standby crews") &&
    crewDesk.includes("R1 called 09:45 · reports 13:00 · AR827, AR828\nR2 called 10:05 · reports 13:20 · AR851, AR852"),
    `The Standby chip should show when the standby crews are called: ${crewDesk}`);
  const noticeChip = await cardOf(rateCard.locator('.pb-chip[data-action="PAX-NOTICE"]'));
  check(noticeChip.includes("Passenger notice") && noticeChip.includes("85 flights · 9,412 passengers told their new times"), `The Notify chip should show the notice: ${noticeChip}`);
  const carChip = await cardOf(rateCard.locator('.pb-chip[data-action="CAR"]'));
  check(carChip.includes("Car transfers") && carChip.includes("131 passengers by car"), `The Car chip should show the cars: ${carChip}`);
  const adviceDesk = await cardOf(rateCard.locator('.pb-chip[data-action="advice"]'));
  check(adviceDesk.includes("114 arrivals into Lisbon 14:30–21:00: carry holding fuel") && adviceDesk.includes("Porto 277 km, Faro 221 km: no forecast hazard") &&
    adviceDesk.includes("Advice never changes the plan"), `The Advice chip should give dispatch's advice: ${adviceDesk}`);
  const thinAdvice = await cardOf(thinCard.locator('.pb-chip[data-action="advice"]'));
  check(thinAdvice.includes(xssFact) && !thinAdvice.includes("does not apply"), `A fact with markup should read as text; a refusal stays off the card: ${thinAdvice}`);
  // Keyboard: a chip opens its card on focus, like the checks.
  await page.mouse.move(2, 2);
  await thinCard.locator('.pb-chip[data-action="THIN"]').focus();
  await page.locator("#tip-card").waitFor();
  const focusedDesk = await page.locator("#tip-card").innerText();
  check(focusedDesk.includes("AR131, AR132, AR881, AR882 cancelled") && focusedDesk.includes("Cancel early"), `Focus on a chip should open its action: ${focusedDesk}`);
  await page.evaluate(() => document.activeElement.blur());
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  // The recommendation: what is done ahead of the storm on hover, no crew over its limit; weather owes no compensation.
  const stormLine = await bandWords(".t-why");
  check(stormLine.startsWith("Why plan 1 · Delay, not cancel Nobody loses a flight, for €151,844 less"), `The recommendation should read as any other: ${stormLine}`);
  const stormRecCard = await cardOf(band.locator(".verdict [data-card]"));
  check(stormRecCard.includes("Ahead of the weather") && stormRecCard.includes("R1 called 09:45 · reports 13:00 · AR827, AR828") &&
    stormRecCard.includes("85 flights · 9,412 passengers told their new times") && stormRecCard.includes("Lose flight or connection 655 0 −655") &&
    !/Car transfers|Retime|Hold at origin/.test(stormRecCard), `The recommendation's card should say what is done ahead of the storm: ${stormRecCard}`);
  const thinCost = await cardOf(thinCard.locator('.nb[data-num="cost"]'));
  check(!/If weather|excuse/i.test(thinCost) && /Total\s+€210,377/.test(thinCost) && thinCost.includes("Not owed · weather is an extraordinary circumstance"),
    `The cost should count no compensation for weather, and say so: ${thinCost}`);
  check(!/crews? over (?:the |their )?limit/i.test(await shownEverywhere()), "No crew is over its limit after the standby crews, whatever the Crew agent's duty rows say");
  check(await cellsFit() && (await overlaps()).length === 0, `Nothing should overlap on the storm's cards at 1440 px: ${(await overlaps()).join("; ")}`);
  await professional("Playbook storm plans");
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-storm.png", fullPage: true });
  await rateCard.locator('.pb-chip[data-action="STANDBY"]').hover();
  await page.locator("#tip-card").waitFor();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-chip.png" });
  await page.mouse.move(2, 2);
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await band.locator(".verdict [data-card]").hover();
  await page.locator("#tip-card").waitFor();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-compare.png" });
  await page.mouse.move(2, 2);
  await page.setViewportSize({ width: 1440, height: 900 });
  // The Decision step: no total if weather excused compensation (owner: none is owed), each plan's checklist.
  await openStep("decision");
  check((await words(rec.locator("h3"))) === "Recommended: Plan 1, Accept the reduced Lisbon rate", `The Decision step should name the plan by its own name: ${await words(rec.locator("h3"))}`);
  const shownText = async (locator) => locator.evaluate(el => el.innerText.replace(/\s+/g, " ").trim());
  check((await page.locator('#step-decision .cmp-parts tr[data-row="excused"]').count()) === 0 && !/If weather|excuse/i.test(await page.locator("#step-decision").innerText()) &&
    (await shownText(page.locator('#step-decision .cmp-parts tr[data-row="owed"]'))) === "Owed compensation 0 0" &&
    (await page.locator('#step-decision .cmp-parts tr[data-row="crews"], #step-decision .cmp-parts tr[data-row="overnight"]').count()) === 0,
    "Plans by part should owe no compensation for weather, and show no crews over limit or stranded rows when there are none");
  const checklists = page.locator("#step-decision .pb-detail");
  check((await checklists.count()) === 2 && (await words(checklists.nth(0).locator("summary"))) === "Plan 1: Accept the reduced Lisbon rate 5 in this plan, 2 advice" &&
    (await words(checklists.nth(1).locator("summary"))) === "Plan 2: Cancel AR131 + AR132 + AR881 + AR882 7 in this plan, 2 advice, 1 refused",
    `Each plan should have its checklist: ${await words(checklists.nth(0).locator("summary"))} / ${await words(checklists.nth(1).locator("summary"))}`);
  for (const index of [0, 1]) await checklists.nth(index).locator("summary").click();
  const checkRow = async (plan, id) => shownText(checklists.nth(plan).locator(`tr[data-action="${id}"]`));
  check((await checklists.nth(0).locator("thead th").allTextContents()).join(" | ") === "Action | Desk | Status | Facts" &&
    (await checkRow(0, "STANDBY")) === "Standby crews Crew control In this plan R1 called 09:45 · reports 13:00 · AR827, AR828 R2 called 10:05 · reports 13:20 · AR851, AR852" &&
    (await checkRow(0, "PAX-NOTICE")) === "Passenger notice Hub and passengers In this plan 85 flights · 9,412 passengers told their new times" &&
    (await checkRow(0, "THIN")) === "Cancel early Aircraft control Not in this plan —" &&
    (await checkRow(1, "MX-ETR")) === "Maintenance estimate Maintenance control Refused: does not apply to a weather forecast —" &&
    (await checkRow(1, "FUEL-FLAG")) === "Fuel Dispatch Advice 114 arrivals into Lisbon 14:30–21:00: carry holding fuel" &&
    (await checklists.nth(0).locator("tbody tr").count()) === 9 && (await checklists.nth(1).locator("tbody tr").count()) === 10,
    `The checklist should give each action's desk, status in plain words and facts: ${await checkRow(0, "STANDBY")} / ${await checkRow(1, "MX-ETR")}`);
  // Keyboard: an action's full name and autonomy open on focus (the desk column hides on a phone).
  await checklists.nth(0).locator('tr[data-action="STANDBY"] .pb-act').focus();
  await page.locator("#tip").waitFor();
  check((await page.locator("#tip").innerText()).includes("Call standby crews") && (await page.locator("#tip").innerText()).includes("Automatic within limits"),
    "An action in the checklist should name itself in full on focus");
  await page.evaluate(() => document.activeElement.blur());
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1), "The checklist should fit the step at 1440 px");
  await checklists.nth(0).scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-checklist.png" });
  await professional("Playbook storm");
  await showPlans();
  // Approving the recommended plan previews the notice in plain words, after the retime and before the crew.
  await rateCard.getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const rateEffects = (await page.locator("#decision-effects li").allInnerTexts()).map(line => line.replace(/\s+/g, " ").trim());
  const effectAt = (line) => rateEffects.indexOf(line);
  check(effectAt("Operations control · retime 81 flights, up to 80 min later") === 0 && effectAt("Passenger system · tell passengers on 85 flights their new times") > 0 &&
    effectAt("Crew system · assign standby crews R1, R2") > effectAt("Passenger system · tell passengers on 85 flights their new times") &&
    (await page.locator("#decision-title").textContent()) === 'Approve "Accept the reduced Lisbon rate"?' && !rateEffects.some(line => line.includes("ask:")),
    `The approval preview should read the notice in plain words: ${rateEffects.join(" | ")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-approve.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  const planted = await page.evaluate(() => ({ ran: window.__xss ?? null, nodes: document.querySelectorAll('img[src^="x-"]').length,
    templates: [...document.querySelectorAll("template")].reduce((sum, template) => sum + template.content.querySelectorAll('img[src^="x-"]').length, 0) }));
  check(planted.ran === null && planted.nodes === 0 && planted.templates === 0, `A playbook fact should never become markup: ${JSON.stringify(planted)}`);
  // A phone: the same chips and checklist fit 390 px; a tap opens a desk's card inside the screen.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${ST}`);
  await rateCard.locator(".pb").waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && (await overlaps()).length === 0,
    `The storm's playbook should fit 390 px: ${(await overlaps()).join("; ")}`);
  await professional("390 px playbook storm");
  await page.screenshot({ path: ".playwright-cli\\operator-390-playbook-storm.png", fullPage: true });
  await tap(rateCard.locator('.pb-chip[data-action="PAX-NOTICE"]'));
  await page.locator("#tip-card").waitFor();
  const phoneChip = await page.locator("#tip-card").boundingBox();
  check((await page.locator("#tip-card").innerText()).includes("85 flights · 9,412 passengers told their new times") && phoneChip.x >= 0 && phoneChip.x + phoneChip.width <= 391,
    `A tap on a chip should open its card inside the screen: ${JSON.stringify(phoneChip)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-390-playbook-chip.png" });
  await page.waitForTimeout(400);
  await rateCard.locator('.pb-chip[data-action="PAX-NOTICE"]').click();
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await openStep("decision");
  await page.locator('#step-decision .pb-detail[data-plan-no="1"] > summary').click();
  check(await page.locator("#drawer-body").evaluate(body => body.scrollWidth <= body.clientWidth + 1) &&
    (await shownText(page.locator('#step-decision .pb-detail[data-plan-no="1"] tr[data-action="STANDBY"]'))).startsWith("Standby crews In this plan R1 called 09:45"),
    "At 390 px the checklist should fit, its desk behind the action's icon");
  await page.locator('#step-decision .pb-detail[data-plan-no="1"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: ".playwright-cli\\operator-390-playbook-checklist.png" });
  await showPlans();
  await page.setViewportSize({ width: 1440, height: 900 });
  // Carried out: the notice by its kind, in plain words, with the passengers' other outcomes.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${SX}&step=carried`);
  await page.locator('#done details[data-group="passengers"]').waitFor();
  const doneLine = async (group) => words(page.locator(`#done details[data-group="${group}"] .what b`));
  check((await doneLine("passengers")) === "Passengers on 85 flights told their new times · 131 passengers keep their connection with a car" &&
    (await doneLine("crew")) === "Standby crews R1, R2 assigned · 1 crew duty updated" && (await page.locator('#done details[data-group="other"]').count()) === 0,
    `What ARROW did should read the passenger notice in plain words: ${await doneLine("passengers")} / ${await doneLine("crew")}`);
  await professional("Carried out storm");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-carried.png" });
  await showPlans();
  // Case 3: Maintenance's estimate is advice; on the plan whose estimate says nothing yet, no Advice chip. A fact that is not a
  // line of text never shows its keys.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${H}`);
  const case3 = page.locator(".plan");
  await case3.nth(1).locator(".pb").waitFor();
  check(await stripOf(case3.nth(0)) === "Cancel 2 | Rebook 300 | Advice" && await stripOf(case3.nth(1)) === "Retime 4 | Rebook 38 | Car 35",
    `Case 3's plans should say what each does: ${await stripOf(case3.nth(0))} / ${await stripOf(case3.nth(1))}`);
  const etr = await cardOf(case3.nth(0).locator('.pb-chip[data-action="advice"]'));
  check(etr.includes("Maintenance estimate") && etr.includes(DEFECT_ETR) && etr.includes("Advice never changes the plan"), `Maintenance's estimate should read as advice with its facts: ${etr}`);
  const carDesk = await cardOf(case3.nth(1).locator('.pb-chip[data-action="CAR"]'));
  check(carDesk.includes("Car transfers") && carDesk.includes("35 passengers by car") && !/Passengers: 35|Connections: 5/.test(carDesk), `A fact should only ever be a line of text: ${carDesk}`);
  check((await overlaps()).length === 0 && await cellsFit(), `Nothing should overlap on Case 3's cards: ${(await overlaps()).join("; ")}`);
  await page.locator("#tip-card").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-playbook-case3.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await openStep("decision");
  for (const plan of ["1", "2"]) await page.locator(`#step-decision .pb-detail[data-plan-no="${plan}"] > summary`).click();
  check((await shownText(page.locator('#step-decision .pb-detail[data-plan-no="1"] tr[data-action="MX-ETR"]'))) === `Maintenance estimate Maintenance control Advice ${DEFECT_ETR}` &&
    (await shownText(page.locator('#step-decision .pb-detail[data-plan-no="2"] tr[data-action="MX-ETR"]'))) === "Maintenance estimate Maintenance control Advice —" &&
    (await shownText(page.locator('#step-decision .pb-detail[data-plan-no="1"] tr[data-action="RETIME"]'))) === "Retime Aircraft control Not in this plan —" &&
    (await page.locator("#step-decision .cmp-parts tr[data-row=overnight], #step-decision .cmp-parts tr[data-row=excused], #step-decision .cmp-parts tr[data-row=crews]").count()) === 0,
    "Case 3's checklist should keep every advice row, and no storm figures");
  await showPlans();
  await page.goto(`${base}/src/ops_app/static/index.html?case=${SB}`);
  await page.locator(".plan").first().locator(".pb").waitFor();
  check((await stripOf(page.locator(".plan").first())) === "Retime 4 | Standby 2 | Rebook 41", `Case 4's plan should call standby crews: ${await stripOf(page.locator(".plan").first())}`);
  const standbyDesk = await cardOf(page.locator(".plan").first().locator('.pb-chip[data-action="STANDBY"]'));
  check(standbyDesk.includes("Standby crews") && standbyDesk.includes("R1 called 15:54 · reports 16:54 · AR941, AR942"), `The Standby chip should give the standby crews: ${standbyDesk}`);
  // A plan stored before the playbook (Case 4 of a Dev run) says what it does from its own figures.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${K}`);
  await band.locator(".verdict").waitFor();
  check((await page.locator(".plan").count()) === 3 && [await stripOf(page.locator(".plan").nth(0)), await stripOf(page.locator(".plan").nth(1)),
    await stripOf(page.locator(".plan").nth(2))].join(" / ") === "Retime 2 | Cancel 2 | Rebook 150 / Cancel 2 | Rebook 196 / Retime 4 | Rebook 41",
    `A plan without a playbook should say what it does from its own figures: ${await stripOf(page.locator(".plan").nth(0))}`);
  const rebookChip = await cardOf(page.locator(".plan").nth(1).locator('.pb-chip[data-action="REBOOK"]'));
  check(rebookChip.includes("196 passengers rebooked") && rebookChip.includes("14 to Passenger Services"), `The Rebook chip should say who is rebooked: ${rebookChip}`);
  // Review: each chip's number is its own kind's. A swap counts the flights moved to another aircraft, never a flight
  // number in its fact; a passenger notice whose passengers Booking could not count shows no number.
  await page.goto(`${base}/src/ops_app/static/index.html?case=${SW}`);
  await page.locator(".plan .pb").first().waitFor();
  check((await stripOf(page.locator(".plan").first())) === "Swap 2 | Advice", `A swap should count its flights: ${await stripOf(page.locator(".plan").first())}`);
  const swapChip = await cardOf(page.locator('.plan .pb-chip[data-action="SWAP-AC"]'));
  check(swapChip.includes("Swap aircraft") && swapChip.includes("AR965 → CS-TNB") && swapChip.includes("AR966 → CS-TNB"), `The Swap chip should name each flight: ${swapChip}`);
  await page.goto(`${base}/src/ops_app/static/index.html?case=${SN}`);
  await page.locator(".plan .pb").first().waitFor();
  check((await stripOf(page.locator(".plan").first())) === "Retime 81 | Hold 18 | Standby 2 | Car 131 | Notify | Advice",
    `A notice without a passenger count should show no number: ${await stripOf(page.locator(".plan").first())}`);
  const noticeUnknown = await cardOf(page.locator('.plan .pb-chip[data-action="PAX-NOTICE"]'));
  check(noticeUnknown.includes("85 flights · passengers told not established"), `The Notify chip should say the count is not established: ${noticeUnknown}`);

  // 12g. The storm in two stages (owner: "for the last case it does not make sense to already update eurocontrol ... better to
  // already inform passengers somehow and have some alternative plans"; "wouldnt it make sense to keep it consistent with what we
  // have?"): the same journey and one approval. Each storm plan says what is done Now (passengers warned of possible delays, standby
  // crews on alert, fuel and alternates advice) and what waits When confirmed; OCC approves once, by the first standby call. Once
  // approved the Now part is done and the rest held, with what would confirm the storm and what would stand it down; then the held
  // actions as sent, the case back with OCC and why, or stood down with the no-change notice. A confirmation before OCC decided is
  // shown and nothing sent. The tower and Today show the held storm with its decide-by time; the trail groups the actions by stage.
  // TAF, METAR and EUROCONTROL explained on hover; 1440 and 390 px, nothing overlapping.
  const inner = async (locator) => locator.evaluate(el => el.innerText.replace(/\s+/g, " ").trim());
  const stageChips = async (scope, stage) => (await scope.locator(`.pb-stage[data-stage="${stage}"] .pb-chip`).evaluateAll(items =>
    items.map(item => item.textContent.replace(/\s+/g, " ").trim()))).join(" | ");
  const stageRows = async (card) => (await card.locator(".stage-row").evaluateAll(rows => rows.map(row => row.innerText.replace(/\s+/g, " ").trim()))).join(" | ");
  const groupRows = async (scope) => (await scope.locator(".done-group > summary").evaluateAll(rows => rows.map(row => row.textContent.replace(/\s+/g, " ").trim()))).join(" | ");
  const tipText = async (locator) => {
    await page.mouse.move(2, 2);
    await locator.scrollIntoViewIfNeeded();
    await locator.hover();
    await page.locator("#tip").waitFor();
    const text = await page.locator("#tip").innerText();
    await page.mouse.move(2, 2);
    return text.replace(/\s+/g, " ");
  };
  const stageOverlaps = () => page.evaluate(() => {
    const bad = [];
    const visible = (el) => el.checkVisibility() && !el.closest("template");
    for (const card of document.querySelectorAll(".stage-card, .stage-plan")) {
      const box = card.getBoundingClientRect();
      for (const el of card.querySelectorAll("*")) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width && (r.right > box.right + 1 || r.left < box.left - 1)) bad.push(`${el.tagName}.${el.className} spills`);
      }
      for (const row of [card, ...card.querySelectorAll(".stage-row, .chips-in, .pb, .pb-chip, .sig")]) {
        const kids = [...row.children].filter(visible).map(kid => [kid, kid.getBoundingClientRect()]);
        kids.forEach(([ka, a], i) => kids.slice(i + 1).forEach(([kb, b]) => {
          if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) bad.push(`${ka.className || ka.tagName} over ${kb.className || kb.tagName}`);
        }));
      }
    }
    const cards = [...document.querySelectorAll(".stage-card")].map(card => card.getBoundingClientRect());
    cards.forEach((a, i) => cards.slice(i + 1).forEach(b => {
      if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) bad.push("stage cards overlap");
    }));
    return bad;
  });
  const fitsScreen = async (where) => {
    const bad = [...await stageOverlaps(), ...await overlaps(), ...(await bandOverlaps()).filter(entry => entry !== "no summary")];
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1) && !bad.length, `${where}: nothing should overflow or overlap: ${bad.join("; ")}`);
  };
  const wordsAtMost = async (limit, where) => {
    await page.mouse.move(2, 2);
    const seen = await visibleWords("#recommendation, #decision-more");
    check(seen.count <= limit, `${where} should show at most ${limit} words above the fold (prototype rev 2), the Decision step's detail aside: ${seen.count} · ${seen.text}`);
    return seen.count;
  };
  const stormWords = {};
  // Today as on the demo day, as the prototype counted it: Cases 1–5 with this storm, without the free-text report an earlier step
  // left assessing.
  const stormDay = (trace, state, label, extra = {}) => [...list().filter(row => row.trace_id !== D),
    { ...stormRow, trace_id: trace, display_id: docs[trace].display_id, state, label, ...extra }];
  // Before OCC decides: each plan in two stages, its numbers and the approval by the first standby call.
  await page.setViewportSize({ width: 1440, height: 900 });
  dayRows = stormDay(G0, "needs", "Needs your decision");
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G0}`);
  await page.locator('.plan .pb-stage[data-stage="when_confirmed"]').first().waitFor();
  const [rateStaged, thinStaged] = [page.locator(".plan").nth(0), page.locator(".plan").nth(1)];
  const stagedLine = async () => [await stageChips(rateStaged, "now"), await stageChips(rateStaged, "when_confirmed"), await stageChips(thinStaged, "now"),
    await stageChips(thinStaged, "when_confirmed")].join(" / ");
  check((await words(rateStaged.locator('.pb-stage[data-stage="now"] .pb-pt'))) === "Now" &&
    (await words(rateStaged.locator('.pb-stage[data-stage="when_confirmed"] .pb-pt'))) === "When confirmed" &&
    (await stagedLine()) === "Warn 85 | Standby 2 | Fuel / Retime 81 | Hold 18 | EUROCONTROL | Car 131 | New times 9,412 / " +
      "Warn 83 | Standby 1 | Fuel / Retime 75 | Hold 17 | Cancel 4 | Rebook 41 | Car 101 | New times 8,650" &&
    (await page.locator(".plan .pb-chip.advice").count()) === 0 && (await page.locator(".plan .numbers").count()) === 2,
    `Each storm plan should say what is done now and what waits until the storm is confirmed: ${await stagedLine()}`);
  const warnCard = await cardOf(rateStaged.locator('.pb-chip[data-action="WARN"]'));
  check(warnCard.includes("Warn passengers of possible delays") && warnCard.includes("85 flights · no new times yet"), `Warn should say who is warned and that no times change yet: ${warnCard}`);
  const alertCard = await cardOf(rateStaged.locator('.pb-chip[data-action="ALERT"]'));
  check(alertCard.includes("Standby crews on alert") && alertCard.includes("R1 · call by 12:00 · reports 13:00 · AR827, AR828") &&
    alertCard.includes("R2 · call by 12:20 · reports 13:20 · AR851, AR852"), `The Standby chip should give each crew's call-by time: ${alertCard}`);
  const fuelCard = await cardOf(rateStaged.locator('.pb-chip[data-action="FUEL"]'));
  check(fuelCard.includes("114 arrivals into Lisbon 14:30–21:00: carry holding fuel") && fuelCard.includes("Porto 277 km, Faro 221 km: no forecast hazard") &&
    fuelCard.includes("Advice never changes the plan"), `Fuel should give dispatch's fuel and alternates advice: ${fuelCard}`);
  const networkCard = await cardOf(rateStaged.locator('.pb-chip[data-action="EUROCONTROL"]'));
  check(networkCard.includes("EUROCONTROL Network Manager") && networkCard.includes(`${delayMessages.length} delay messages`) && /European air traffic/.test(networkCard),
    `EUROCONTROL should say what it is and what it is sent: ${networkCard}`);
  check((await tipText(rateStaged.locator('.pb-stage[data-stage="now"] .pb-pt'))).includes("Carried out when OCC approves") &&
    (await tipText(rateStaged.locator('.pb-stage[data-stage="when_confirmed"] .pb-pt'))).includes("Held until the forecast is confirmed and ARROW's re-check passes"),
    "Now and When confirmed should explain themselves on hover");
  check((await bandWords(".t-todo")) === "Your action Approve plan 1 By 12:00 · standby call", `OCC should approve once, by the first standby call: ${await bandWords(".t-todo")}`);
  // The crosswind's own name is two words where the thunderstorm's was one: one more word in view wherever the hazard is named
  // (Today, the title, the summary; held, also what confirms and what stands down).
  stormWords.decide = await wordsAtMost(183, "The storm before deciding at 1440 px");
  await fitsScreen("The storm before deciding at 1440 px");
  await professional("Storm in two stages");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-decide.png" });
  // The Decision step: the plan OCC approves once, in two stages.
  await openStep("decision");
  const once = page.locator("#step-decision .stage-plan");
  check((await words(once.locator("h3"))) === "Plan 1 · approve once" && (await stageChips(once, "now")) === "Warn 85 | Standby 2 | Fuel" &&
    (await stageChips(once, "when_confirmed")) === "Retime 81 | Hold 18 | EUROCONTROL | Car 131 | New times 9,412" &&
    (await words(once.locator(".stage-rule"))) === "One approval · re-check before sending · standby called at call-by",
    `The Decision step should show the plan approved once in two stages: ${await inner(once)}`);
  const onceTip = await tipText(once.locator(".stage-rule"));
  check(onceTip.includes("only if nothing got worse") && onceTip.includes("call-by time"), `The rule should explain itself: ${onceTip}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-decision.png" });
  await showPlans();
  // The approval previews the Now part, then what waits until the storm is confirmed.
  await rateStaged.getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const stagedEffects = (await page.locator("#decision-effects li").allTextContents()).map(line => line.replace(/\s+/g, " ").trim());
  check(stagedEffects.slice(0, 5).join(" | ") === "Now · on approval | Passenger system · warn passengers on 85 flights of possible delays | " +
    "Crew system · put standby crews R1, R2 on alert | When confirmed · after ARROW's re-check | Operations control · retime 81 flights, up to 80 min later" &&
    stagedEffects.includes("Passenger system · tell passengers on 85 flights their new times") && stagedEffects.includes("Crew system · assign standby crews R1, R2") &&
    (await words(page.locator("#decision-preview"))).includes("the rest waits until the forecast is confirmed"),
    `The approval should preview the two stages: ${stagedEffects.join(" | ")} · ${await words(page.locator("#decision-preview"))}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-approve.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 390, height: 844 });
  await fitsScreen("The storm before deciding at 390 px");
  stormWords.decidePhone = (await visibleWords("#recommendation, #decision-more")).count;
  await page.screenshot({ path: ".playwright-cli\\operator-390-storm-decide.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // A confirmation that arrived before OCC decided: shown, nothing sent.
  dayRows = stormDay(G5, "needs", "Needs your decision");
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G5}`);
  await band.locator(".t-what .sub").waitFor();
  check((await bandWords(".t-what")) === "Disruption Strong crosswind confirmed Amended TAF · 10:20" &&
    (await bandWords(".t-todo")) === "Your action Approve plan 1 Forecast confirmed · nothing sent yet" && (await stageChips(page.locator(".plan").first(), "now")) === "Warn 85 | Standby 2 | Fuel",
    `A confirmation before the decision should show, with nothing sent: ${await bandWords(".t-what")} | ${await bandWords(".t-todo")}`);
  const earlyTip = await hoverText(band.locator(".t-what .big"));
  check(earlyTip.includes("Amended TAF (Terminal Aerodrome Forecast) as received") && earlyTip.includes(STORM_TAF_AMD) &&
    earlyTip.includes("Strong crosswind 16:00–18:00 · rate 24 an hour") && earlyTip.includes("Nothing is sent until OCC approves a plan"), `The update should open as received: ${earlyTip}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-early.png" });
  // Held: the tower and Today show it with its decide-by time.
  const heldRow = { ...stormRow, trace_id: G1, display_id: docs[G1].display_id, state: "progress", label: "Held · decide by 12:00", urgency: 4,
    storm: { state: "awaiting", decide_by: "12:00" }, impact: { ...impact(0, 0, 58533, 2, stormRate.title, "occ", 2, heldCount + 2, 0, "occ"), plan_kind: "delay", plan_cancelled: [],
      execution_status: "held" } };
  // Final review: a stood-down storm (every action it sent confirmed, the held ones never sent and not counted) is not executed:
  // nothing moved, no cost executed today.
  const stoodRow = { ...stormRow, trace_id: G4, display_id: docs[G4].display_id, state: "handled", label: "Stood down", urgency: 5,
    storm: { state: "dropped", decide_by: "12:00" }, impact: { ...impact(0, 0, 58533, 2, stormRate.title, "occ", 4, heldCount + 4, 0, "occ"), plan_kind: "delay", plan_cancelled: [],
      execution_status: "stood_down", actions_stood_down: heldCount } };
  // The storm came after all, after the stand-down (trail.case_state "needs"): OCC decides again, in the tower's needs group.
  const afterRow = { ...stoodRow, trace_id: G10, display_id: docs[G10].display_id, state: "needs", label: "Forecast confirmed after stand-down", urgency: 1 };
  dayRows = [...list().filter(row => row.trace_id !== D), heldRow, stoodRow, afterRow];
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const heldTower = page.locator(`#tower-rows tr[data-trace="${G1}"]`);
  await heldTower.waitFor();
  check((await words(heldTower.locator(".tw-state"))) === "Held · decide by 12:00" && (await words(heldTower.locator(".tw-plan"))).endsWith("OCC approved · Held · decide by 12:00") &&
    !(await words(heldTower.locator(".tw-dep"))).includes("executed"), `The tower should show the held storm with its decide-by time: ${await words(heldTower)}`);
  const stoodTower = page.locator(`#tower-rows tr[data-trace="${G4}"]`);
  check((await words(stoodTower.locator(".tw-plan"))).endsWith("OCC approved · Stood down") && !(await words(stoodTower.locator(".tw-dep"))).includes("executed") &&
    (await words(page.locator('#tower [data-kpi="decision"] small'))) === "€118,098 executed today · indicative",
    `A stood-down storm should show nothing executed: ${await words(stoodTower)} | ${await words(page.locator('#tower [data-kpi="decision"] small'))}`);
  const afterTower = page.locator(`#tower-rows tr[data-trace="${G10}"]`);
  check((await afterTower.getAttribute("data-tone")) === "needs" && (await words(afterTower.locator(".tw-state"))) === "Forecast confirmed after stand-down" &&
    (await words(page.locator('#tower [data-kpi="occ"] small'))) === "1 decision · 1 manual action",
    `A storm confirmed after its stand-down should need OCC's decision: ${await words(afterTower)} | ${await words(page.locator('#tower [data-kpi="occ"] small'))}`);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({ path: ".playwright-cli\\operator-1440-tower-storm-held.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  dayRows = [...list().filter(row => row.trace_id !== D), heldRow];
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G1}`);
  const nowCard = page.locator('.stage-card[data-stage="now"]'), laterCard = page.locator('.stage-card[data-stage="when_confirmed"]');
  await showPlans();
  await laterCard.waitFor();
  check((await words(page.locator(`.case-row[data-case="${G1}"] .tag`))) === "Held · 12:00", "Today should show the held storm with its decide-by time");
  check((await bandWords(".t-todo")) === "Approved · plan 1 Held · decide by 12:00 Now done · rest held" && (await bandWords(".t-what")) === "Disruption Strong crosswind forecast" &&
    (await page.locator(".plan").count()) === 0, `Once approved, the summary should say the rest is held and by when: ${await bandWords(".t-todo")}`);
  check((await words(nowCard.locator(".stage-t"))) === "Now · ✓ done 09:52" &&
    (await stageRows(nowCard)) === "Passengers warned 9,412 on 85 flights | Standby on alert R1 12:00 R2 12:20 | Fuel and alternates 114 arrivals · Porto, Faro",
    `Now should be done: passengers warned, standby crews on alert with their call-by times, fuel and alternates advice: ${await stageRows(nowCard)}`);
  check((await words(laterCard.locator(".stage-t"))) === "When confirmed · held" && (await words(laterCard.locator(".stage-big"))) === "Decide by 12:00 first standby call" &&
    (await stageChips(laterCard, "when_confirmed")) === "Retime 81 | Hold 18 | EUROCONTROL | Car 131 | New times 9,412" &&
    (await inner(laterCard.locator(".sig"))) === "Confirms METAR or amended TAF with strong crosswind · EUROCONTROL regulation for Lisbon arrivals " +
      "Stands down Amended TAF without strong crosswind" &&
    (await words(laterCard.locator(".stage-rule"))) === "Re-check before sending · standby called at call-by",
    `When confirmed should be held, with what confirms it and what stands it down: ${await inner(laterCard)}`);
  check((await tipText(laterCard.locator(".term", { hasText: "METAR" }).first())).includes("routine airport weather report") &&
    (await tipText(laterCard.locator(".term", { hasText: "TAF" }).first())).includes("Terminal Aerodrome Forecast") &&
    (await tipText(laterCard.locator(".term", { hasText: "EUROCONTROL" }).first())).includes("Network Manager") &&
    /call by 12:00 · reports 13:00 · AR827, AR828/.test(await tipText(nowCard.locator(".sbx").first())),
    "METAR, TAF, EUROCONTROL and each standby crew should explain themselves on hover");
  // The executor's exact conditions (window and rate) on focus.
  const exactly = async (locator) => {
    await locator.focus();
    await page.locator("#tip").waitFor();
    const text = (await page.locator("#tip").innerText()).replace(/\s+/g, " ");
    await page.evaluate(() => document.activeElement.blur());
    return text;
  };
  const confirmsTip = await exactly(laterCard.locator(".sig dd").first());
  const standsDownTip = await exactly(laterCard.locator(".sig dd").nth(1));
  check(confirmsTip.includes("METAR or amended TAF: strong crosswind 16:00–18:00") && confirmsTip.includes("EUROCONTROL regulation: Lisbon arrivals at 24 an hour or more") &&
    standsDownTip.includes("Amended TAF: no strong crosswind 16:00–18:00"), `What confirms and what stands down should give the exact conditions: ${confirmsTip} | ${standsDownTip}`);
  check((await words(stepButton("carried"))) === "6 Carried out Rest held", `Carried out should say the rest is held: ${await words(stepButton("carried"))}`);
  stormWords.held = await wordsAtMost(181, "The storm held at 1440 px");
  await fitsScreen("The storm held at 1440 px");
  await professional("Storm held");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-held.png" });
  // Carried out: the Now part done, the held part with what confirms and stands it down, each action as it waits.
  await openStep("carried");
  const doneNow = page.locator('#done .stage-sec[data-stage="now"]'), doneLater = page.locator('#done .stage-sec[data-stage="when_confirmed"]');
  check((await words(page.locator("#done .done-line"))) === `… Now: 2 of 2 confirmed · ${heldCount} held until the forecast is confirmed` &&
    (await words(doneNow.locator("h4"))) === "Now · ✓ done 09:52" &&
    (await groupRows(doneNow)) === "✓ Crew Standby crews R1, R2 on alert Confirmed | ✓ Passengers Passengers on 85 flights warned of possible delays Confirmed" &&
    (await words(doneNow.locator(".stage-advice"))) === "Fuel and alternates advice to dispatch · 114 arrivals · Porto, Faro" &&
    (await words(doneLater.locator("h4"))) === "When confirmed · held · decide by 12:00" &&
    (await inner(doneLater.locator(".sig"))).startsWith("Confirms METAR or amended TAF") &&
    (await groupRows(doneLater)).startsWith("… Flights 81 flights to retime") &&
    (await doneLater.locator(".done-group > summary .state").allTextContents()).every(state => state.trim() === "Held"),
    `Carried out should split the actions by stage: ${await words(page.locator("#done .done-line"))} | ${await groupRows(doneNow)} | ${await groupRows(doneLater)}`);
  await professional("Storm held, carried out");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-held-carried.png" });
  // The trail's outcome groups the actions by stage.
  await openStep("assessed");
  const heldNode = page.locator('.node[data-node="outcome"]');
  await heldNode.waitFor();
  check((await valueOf("message")) === "Forecast" && (await step("message").getAttribute("aria-label")).startsWith("Forecast from"),
    "A weather case was reported as a forecast, not a message");
  const heldCard = await cardOf(heldNode);
  check((await valueOf("outcome")) === "Now done, rest held" && heldCard.includes(`Now 2 of 2, ${heldCount} held`),
    `The trail's outcome should say the rest is held: ${await valueOf("outcome")} | ${heldCard}`);
  await heldNode.click();
  const stageHeads = (await page.locator("#trail-detail .tv-stage").allTextContents()).map(line => line.replace(/\s+/g, " ").trim());
  check(stageHeads.join(" | ") === `Now · 2 of 2 confirmed | When confirmed · ${heldCount} held` && (await page.locator("#trail-detail").innerText()).includes("Decide by 12:00"),
    `The trail should group the actions by stage: ${stageHeads.join(" | ")}`);
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  await fitsScreen("The storm held at 390 px");
  stormWords.heldPhone = (await visibleWords("#recommendation, #decision-more")).count;
  await page.screenshot({ path: ".playwright-cli\\operator-390-storm-held.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // Confirmed within the window at the planned rate: re-checked, the held actions sent.
  dayRows = stormDay(G2, "handled", "Approved and carried out", { storm: { state: "confirmed", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G2}`);
  await showPlans();
  await laterCard.waitFor();
  check((await bandWords(".t-what")) === "Disruption Strong crosswind confirmed METAR · 13:30" &&
    (await bandWords(".t-todo")) === "Your action ✓ Forecast confirmed · carried out Sent 13:31" &&
    (await words(nowCard.locator(".stage-t"))) === "Now · ✓ done 09:52" && (await words(laterCard.locator(".stage-t"))) === "When confirmed · ✓ sent 13:31" &&
    (await words(laterCard.locator(".stage-big"))) === "Re-checked: nothing worse" &&
    (await stageChips(laterCard, "when_confirmed")) === "Retime 81 | Hold 18 | EUROCONTROL | Car 131 | New times 9,412" && (await laterCard.locator(".pb-stage.sent").count()) === 1,
    `A confirmed storm should show the held part sent after the re-check: ${await bandWords(".t-todo")} | ${await inner(laterCard)}`);
  const metarTip = await hoverText(band.locator(".t-what .big"));
  check(metarTip.includes("METAR (routine airport weather report) as received") && metarTip.includes(STORM_METAR) && metarTip.includes("Strong crosswind 16:00–18:00 · rate 24 an hour"),
    `The confirmation should open as received: ${metarTip}`);
  const recheckTip = await tipText(laterCard.locator(".stage-big"));
  check(recheckTip.includes("inside the planned window") && recheckTip.includes("rate not lower than planned") && recheckTip.includes("unchanged"), `The re-check should say what it checked: ${recheckTip}`);
  stormWords.confirmed = await wordsAtMost(146, "The storm confirmed at 1440 px");
  await fitsScreen("The storm confirmed at 1440 px");
  await professional("Storm confirmed");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-confirmed.png" });
  await openStep("carried");
  check((await words(page.locator("#done .done-line"))) === `✓ All ${heldCount + 2} changes confirmed by the airline systems` &&
    (await words(doneLater.locator("h4"))) === "When confirmed · ✓ sent 13:31" &&
    (await groupRows(doneLater)).includes("✓ Passengers Passengers on 85 flights told their new times · 131 passengers keep their connection with a car Confirmed") &&
    (await groupRows(doneLater)).includes("✓ Crew Standby crews R1, R2 assigned · 2 crew duties updated Confirmed"),
    `Carried out should show the held actions as sent: ${await groupRows(doneLater)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-confirmed-carried.png" });
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  await fitsScreen("The storm confirmed at 390 px");
  stormWords.confirmedPhone = (await visibleWords("#recommendation, #decision-more")).count;
  await page.screenshot({ path: ".playwright-cli\\operator-390-storm-confirmed.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // Confirmed but worse than planned: nothing more sent, back to OCC with the reason.
  dayRows = stormDay(G3, "needs", "Needs attention", { storm: { state: "confirmed", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G3}`);
  await showPlans();
  await laterCard.waitFor();
  check((await bandWords(".t-todo")) === "Your action Back to you The confirmed weather is worse than planned (rate 16 an hour, planned 24)" &&
    (await words(laterCard.locator(".stage-t"))) === "When confirmed · not sent" && (await words(laterCard.locator(".stage-big"))) === "Back to you" &&
    (await words(laterCard.locator(".stage-why"))) === "The confirmed weather is worse than planned (rate 16 an hour, planned 24)" &&
    (await words(laterCard.locator(".stage-sub"))) === "Nothing more was sent · passengers stay warned · standby crews on alert" &&
    (await words(stepButton("carried"))) === "6 Carried out Back to you",
    `A worse storm should come back to OCC with the reason: ${await bandWords(".t-todo")} | ${await inner(laterCard)}`);
  await fitsScreen("The storm back with OCC at 1440 px");
  await professional("Storm back with OCC");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-returned.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await fitsScreen("The storm back with OCC at 390 px");
  await page.screenshot({ path: ".playwright-cli\\operator-390-storm-returned.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // Stood down: the warned passengers told no change is expected, the alerts released, nothing held sent.
  dayRows = stormDay(G4, "handled", "Stood down", { storm: { state: "dropped", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G4}`);
  await showPlans();
  await laterCard.waitFor();
  check((await bandWords(".t-what")) === "Disruption Forecast dropped Amended TAF · 13:10" && (await bandWords(".t-todo")) === "Your action ✓ Stood down Nothing held was sent" &&
    (await words(nowCard.locator(".stage-t"))) === "Now · undone 13:11" &&
    (await stageRows(nowCard)) === "Passengers told no change expected · 85 flights | Standby released R1, R2" &&
    (await words(laterCard.locator(".stage-t"))) === "When confirmed · never sent" &&
    (await stageChips(laterCard, "when_confirmed")) === "Retime 81 | Hold 18 | EUROCONTROL | Car 131 | New times 9,412" &&
    (await words(laterCard.locator(".stage-sub"))) === "Nothing to undo at EUROCONTROL or the airport" && (await words(stepButton("carried"))) === "✓ Carried out Stood down",
    `A dropped forecast should stand the storm down: ${await bandWords(".t-todo")} | ${await stageRows(nowCard)} | ${await inner(laterCard)}`);
  const downTip = await hoverText(band.locator(".t-todo .big"));
  check(downTip.includes("The forecast dropped: standby crews R1, R2 released") && downTip.includes("passengers on 85 flights told no change is expected") && downTip.includes("nothing held was sent"),
    `The stand-down should say what it actually sent: ${downTip}`);
  stormWords.stoodDown = await wordsAtMost(145, "The storm stood down at 1440 px");
  await fitsScreen("The storm stood down at 1440 px");
  await professional("Storm stood down");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-stood-down.png" });
  await openStep("carried");
  const doneDown = page.locator('#done .stage-sec[data-stage="stand_down"]');
  check((await words(page.locator("#done .done-line"))) === `✓ Stood down · 4 sent · ${heldCount} never sent` &&
    (await words(doneDown.locator("h4"))) === "Stand-down · ✓ done 13:11" &&
    (await groupRows(doneDown)) === "✓ Crew Standby crews R1, R2 released Confirmed | ✓ Passengers Passengers on 85 flights told no change is expected Confirmed" &&
    (await words(doneLater.locator("h4"))) === "When confirmed · never sent" &&
    (await doneLater.locator(".done-group > summary .state").allTextContents()).every(state => state.trim() === "Not sent"),
    `Carried out should show the stand-down and nothing held sent: ${await words(page.locator("#done .done-line"))} | ${await groupRows(doneDown)} | ${await groupRows(doneLater)}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-stood-down-carried.png" });
  await openStep("assessed");
  await page.locator('.node[data-node="outcome"]').waitFor();
  check((await page.locator('.node[data-node="outcome"]').textContent()).includes("Stood down"), "The trail's outcome should say the storm stood down");
  await page.locator('.node[data-node="outcome"]').click();
  check((await page.locator("#trail-detail .tv-stage").allTextContents()).map(line => line.replace(/\s+/g, " ").trim()).join(" | ") ===
    `Now · 2 of 2 confirmed | When confirmed · ${heldCount} never sent | Stand-down · 2 of 2 confirmed`, "The trail should show the stand-down as its own stage");
  await showPlans();
  await page.setViewportSize({ width: 390, height: 844 });
  await fitsScreen("The storm stood down at 390 px");
  stormWords.stoodDownPhone = (await visibleWords("#recommendation, #decision-more")).count;
  await page.screenshot({ path: ".playwright-cli\\operator-390-storm-stood-down.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  // Review: a stand-down reads from what was actually sent. The forecast dropped before OCC approved: nothing was ever sent,
  // so there is nothing to undo.
  dayRows = stormDay(G6, "handled", "Stood down", { storm: { state: "dropped", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G6}`);
  await showPlans();
  await laterCard.waitFor();
  check((await words(nowCard.locator(".stage-t"))) === "Now · nothing sent" && (await nowCard.locator(".stage-row").count()) === 0 &&
    (await words(nowCard.locator(".stage-sub"))) === "The forecast dropped before anything was sent" &&
    (await words(laterCard.locator(".stage-t"))) === "When confirmed · never sent" && (await bandWords(".t-todo")) === "Your action ✓ Stood down Nothing was sent",
    `A forecast dropped before anything was sent should say nothing was sent: ${await inner(nowCard)} | ${await bandWords(".t-todo")}`);
  check((await hoverText(band.locator(".t-todo .big"))).includes("before anything was sent"), "The stand-down should say nothing had been sent");
  await fitsScreen("A storm stood down before anything was sent");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-nothing-sent.png" });
  await openStep("carried");
  check((await words(page.locator("#done .done-line"))) === `✓ Stood down · 0 sent · ${heldCount + 2} never sent` && (await words(doneNow.locator("h4"))) === "Now · nothing sent" &&
    (await words(doneLater.locator("h4"))) === "When confirmed · never sent" && (await page.locator('#done .stage-sec[data-stage="stand_down"]').count()) === 0,
    `Carried out should say nothing was sent: ${await words(page.locator("#done .done-line"))} | ${await words(doneNow.locator("h4"))}`);
  await showPlans();
  // A stand-down that stopped: the no-change notice refused, the standby crews released; the held part never sent.
  dayRows = stormDay(G7, "needs", "Needs attention", { storm: { state: "dropped", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G7}`);
  await showPlans();
  await laterCard.waitFor();
  check((await words(nowCard.locator(".stage-t"))) === "Now · stand-down stopped" &&
    (await stageRows(nowCard)) === "Passengers not told refused · no change expected · 85 flights | Standby released R1, R2" &&
    (await words(laterCard.locator(".stage-t"))) === "When confirmed · never sent" && (await bandWords(".t-todo")) === "Your action Needs attention Passenger system refused",
    `A stopped stand-down should say what was undone and what not: ${await stageRows(nowCard)} | ${await inner(laterCard)} | ${await bandWords(".t-todo")}`);
  await fitsScreen("A stopped stand-down");
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-stand-down-stopped.png" });
  await openStep("carried");
  check((await words(doneDown.locator("h4"))) === "Stand-down · stopped" && (await words(doneLater.locator("h4"))) === "When confirmed · never sent" &&
    (await words(doneNow.locator("h4"))) === "Now · ✓ done 09:52", `Carried out should show the stand-down stopped: ${await words(doneDown.locator("h4"))}`);
  await showPlans();
  // The storm confirmed after its stand-down: the summary asks OCC to decide again; Today lists it under Needs you.
  dayRows = stormDay(G10, "needs", "Forecast confirmed after stand-down", { storm: { state: "dropped", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G10}`);
  await showPlans();
  await laterCard.waitFor();
  check((await bandWords(".t-todo")) === "Your action Forecast confirmed after stand-down · decide again" && (await bandWords(".t-what")) === "Disruption Strong crosswind confirmed METAR · 14:20" &&
    (await page.locator(`.case-group.needs .case-row[data-case="${G10}"]`).count()) === 1,
    `A storm confirmed after its stand-down should ask OCC to decide again: ${await bandWords(".t-todo")} | ${await bandWords(".t-what")}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-after-stand-down.png" });
  // Standby crews not released read as such, in Carried out and on the Now card.
  dayRows = stormDay(G9, "needs", "Needs attention", { storm: { state: "dropped", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G9}`);
  await showPlans();
  await laterCard.waitFor();
  check((await stageRows(nowCard)) === "Passengers told no change expected · 85 flights | Standby not released refused · R1, R2" &&
    (await words(nowCard.locator(".stage-t"))) === "Now · stand-down stopped", `Standby crews not released should say so: ${await stageRows(nowCard)}`);
  await openStep("carried");
  check((await groupRows(doneDown)).startsWith("✕ Crew Standby crews R1, R2 not released (refused) Refused"), `Carried out should say the crews were not released: ${await groupRows(doneDown)}`);
  await showPlans();
  // The trail reads a storm back with OCC (trail._outcome: "executing") by its execution: back to OCC, the held actions not sent.
  dayRows = stormDay(G3, "needs", "Needs attention", { storm: { state: "confirmed", decide_by: "12:00" } });
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G3}`);
  await openStep("assessed");
  const backNode = page.locator('.node[data-node="outcome"]');
  await backNode.waitFor();
  const backCard = await cardOf(backNode);
  check((await valueOf("outcome")) === "Back to OCC" && backCard.includes(`2 of ${heldCount + 2} confirmed, ${heldCount} not sent`) &&
    !backCard.includes("Being carried out"), `The trail should read the storm back with OCC: ${await valueOf("outcome")} | ${backCard}`);
  await backNode.click();
  check((await page.locator("#trail-detail .tv-stage").allTextContents()).map(line => line.replace(/\s+/g, " ").trim()).join(" | ") ===
    `Now · 2 of 2 confirmed | When confirmed · ${heldCount} not sent` && (await page.locator("#trail-detail .tv-act .mark.occ").count()) === 0 &&
    (await page.locator("#trail-detail .tv-act .mark.unknown").count()) === heldCount, "Held actions never sent should read as not sent, not waiting");
  await showPlans();
  // A plan that stopped part-way (not a storm): what was never sent reads as not sent ("–"), never as failed.
  dayRows = null;
  await page.goto(`${base}/src/ops_app/static/index.html?case=${Q}`);
  await openStep("assessed");
  await page.locator('.node[data-node="outcome"]').waitFor();
  check((await page.locator('.node[data-node="outcome"]').textContent()).includes("Stopped"), "The trail should say the plan stopped");
  await page.locator('.node[data-node="outcome"]').click();
  const unsentMarks = await page.locator("#trail-detail .tv-act summary").filter({ hasText: "Not sent" }).locator(".mark").allTextContents();
  check(unsentMarks.length === 7 && unsentMarks.every(mark => mark === "–"), `Actions never sent should read as not sent: ${unsentMarks.join(" ")}`);
  await showPlans();
  // Approving after a recorded update previews what will happen: a confirmed storm sends both parts after the re-check; a dropped
  // forecast stands the plan down at once and nothing is sent.
  dayRows = stormDay(G5, "needs", "Needs your decision");
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G5}`);
  await page.locator(".plan").first().getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  const earlyEffects = (await page.locator("#decision-effects li").allTextContents()).map(line => line.replace(/\s+/g, " ").trim());
  check((await words(page.locator("#decision-preview"))) === "The forecast is already confirmed: ARROW re-checks the plan, then sends both parts:" &&
    (await page.locator("#decision-effects li.eff-stage").count()) === 0 && earlyEffects.includes("Passenger system · warn passengers on 85 flights of possible delays") &&
    earlyEffects.includes("Operations control · retime 81 flights, up to 80 min later"), `A storm already confirmed should preview both parts going out: ${earlyEffects.join(" | ")}`);
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  // A confirmation worse than the plan (rate 16 an hour, planned 24): approving returns the case to OCC and sends nothing.
  dayRows = stormDay(G5, "needs", "Needs your decision");
  docs[G5] = { ...docs[G5], storm: { state: "confirmed", worse: "the confirmed weather is worse than planned (rate 16 an hour, planned 24)",
    updates: [stormUpdate("u-5", "10:20", "confirmed", 16, STORM_TAF_AMD)] } };
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G5}`);
  await page.locator(".plan").first().getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  check((await words(page.locator("#decision-preview"))) === "The confirmed weather is worse than planned (rate 16 an hour, planned 24): ARROW will return the case to you; nothing is sent." &&
    (await page.locator("#decision-effects li").count()) === 0, `A worse confirmation should preview a return to OCC: ${await words(page.locator("#decision-preview"))}`);
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  dayRows = stormDay(G8, "needs", "Needs your decision");
  await page.goto(`${base}/src/ops_app/static/index.html?case=${G8}`);
  await page.locator(".plan").first().getByRole("button", { name: "Approve plan 1" }).click();
  await page.locator("#decision-dialog[open]").waitFor();
  check((await words(page.locator("#decision-preview"))) === "The forecast was dropped: approving stands the plan down at once; nothing is sent." &&
    (await page.locator("#decision-effects li").count()) === 0, `A dropped forecast should preview a stand-down with nothing sent: ${await words(page.locator("#decision-preview"))}`);
  await page.screenshot({ path: ".playwright-cli\\operator-1440-storm-approve-dropped.png" });
  await page.locator("#decision-cancel").click();
  await page.locator("#decision-dialog").waitFor({ state: "hidden" });
  dayRows = null;

  // 13. Signed out: the sign-in link returns to the same case.
  authStatus = 401;
  await page.goto(`${base}/src/ops_app/static/index.html?case=${A}`);
  const link = page.locator("#notice a");
  await link.waitFor();
  check(decodeURIComponent(await link.getAttribute("href")).includes(`?case=${A}`), "Sign-in should return to the linked case");

  // 14. Today keeps itself current: a failed list read says so and the next good read clears it; a session that
  // expires later offers sign-in instead of a silently frozen list (review). Fake time drives the 10 s refresh.
  authStatus = 200; casesStatus = 503;
  await page.clock.install();
  await page.goto(`${base}/src/ops_app/static/index.html`);
  const notice = page.locator("#notice");
  await notice.filter({ hasText: "waking up" }).waitFor();
  const refreshUntil = async (done, message) => {
    for (let i = 0; i < 40 && !(await done()); i++) await page.clock.runFor(1000);
    check(await done(), message);
  };
  casesStatus = 200;
  await refreshUntil(async () => await notice.isHidden(), "A good list refresh should clear the list's error");
  await towerRows.first().waitFor();
  casesStatus = 401;
  await refreshUntil(async () => (await page.locator("#notice a").count()) > 0, "An expired session during a refresh should offer sign-in");
  casesStatus = 200;

  check(!errors.length, `Page errors: ${errors.join("; ")}`);
  return "Operator workflow: control tower first (urgency order, counts, cost sort, row opens the case, no rules or agents panel), Today grouped; the case as a journey (owner, 22 Nov): no page tabs or sub-tabs, six steps under the header (Reported, Assessed, Plans, Rules, Decision, Carried out; the current one highlighted, later ones muted, arrow keys, the step in the link), acting as tabs (layout A, 23 Nov): the chosen step's content full width under them in the page, Decision by default while OCC decides and Carried out after, the summary, the steps and Today never moving, Esc leaving the step, the case chat docked beside it; a summary under the header (prototype rev 2, 23 Nov: the disruption, its impact in flights, passengers, crews needing cover and cost, your action, why this plan and not the next in a verdict and signed trade-offs, the agents' graph; at most 140 words in view for Case 3 at 1440 px, as the prototype; Cases 2, 3, 4 and the storm at 1440 and 390 px, nothing overlapping), its comparison on the verdict's hover; calm plan cards side by side (lose flight, no seat today, cost, a short route chip and what the plan does; the checks in the Decision step and the flights in the Plans step), every detail on hover, focus or tap (a line's card opens below it, never over it; Esc closes the card first, and does nothing to the case from another screen), nothing overlapping at 1440 or 390 px with three plans; Approve on the recommended plan, Choose on the others, Reject in the Decision step; an automatic case shows the plan that ran and folds the others into Other options checked, and the summary says why it ran without OCC; a case waiting for Passenger Services shows Action needed in amber in the summary, which opens Carried out; plans the rules block never traded off; the Decision step with the recommendation in full (against each plan, why the dearer costs more, trade-off, cost difference by part with both plans' workings, the flag against ARROW's objectives, a blocked plan in one line, OCC approved, carried out automatically, no recommendation), each plan by part with each part's workings, what each other plan does differently, who decides and each cost breakdown; approval preview and receipt; Carried out with What ARROW did (one line, outcomes, each call by its recipient with what was sent, the reply and its interface, a retime stopped part-way, Passenger Services marking passengers handled after a confirm); Assessed with the graph (layout A, 24 Nov: four columns left to right, every step a visible card with one key result and badges for its level, reads, writes and systems, no word · word chains, one trunk per fan of wires that follow their steps when the chat docks, stacked at 390 px; a step's card on hover, focus or a real touch tap: identity, model by name, conclusion per option, each call read or write, Open in Foundry at the agent's version, none without the project link), the panel with input, output, tool calls and their interface, and the conversation with its steps; calls in plain words (what each was for and its result; the tool call (MCP) and the system's own route only in the opened detail, never a host; WRITE on every write the executor made; the executor's re-checks folded above its writes); one discreet AI mark on AI-written text (EU AI Act transparency), no Code marks or label chains; the Detection agent's record (its type's own fields, the event id, the knock-on; a recorded report that changes no flight, never WRITE, ARROW's record in its detail); the Passenger agent's EU261 exposure; Rules with each rule's input and limit (older plans marked); the End-to-end trace and Open in Foundry in the header; standby crews (several per plan, call by and report times, duties they cover and those still over, one for older plans, the approval naming them, the blocked reasons and the Rules screen's rule in today's words); case chat (turn count, docked panel, Case Assistant card marked AI, kept turns, progress while an answer is pending, a what-if being built shown in the chat and as a placeholder in Decision that the chat links to, its card landing in view with a glow, HTML as text, refusals, calls in words, plan links to their cards, suggested questions, what-ifs as dashed cards never recommended, reading while an answer is pending, a full-screen sheet at 390 px); a crosswind forecast read from a TAF (window, gusts across the runway, rate and its sourced basis, the TAF as the report in monospace with TAF spelled out on hover, plans of many flights, the holding line, no EU261 compensation for weather with what would be owed on hover, 1440 and 390 px); the OCC playbook (what each plan does in labels and numbers in one line, advice only when it says something, each action's facts on hover, focus or tap, the standby crews' call times and the passenger notice, the Decision step's checklist with refusals, the storm's two plans by their own names with what is done ahead of the storm and no compensation for weather, crews over limit after the standby crews, the passenger notice in the approval and Carried out, the catalogue's 11 actions on Rules, a fact as text, an older plan from its own figures, 1440 and 390 px); Passenger Services still to act in amber in the summary, and under Action needed in Today and the tower (1440 and 390 px); each chip's number by its own kind (a swap's flights, no number for passengers not counted); a plan's flights against the case's own original times, waiting or carried out, whatever the map's day publishes; why other plans when a delay takes a crew over its duty limit, one line, with the duties standby crews cover and those still over (1440 and 390 px); Reported holding only the report as received, a typed event's record on the graph's report, never under ARROW rules; the speed-up per flight with its basis, inside its card; rules four-eyes; proposed cancellation not shown as done; empty day; unreadable rules with retry; sign-in keeps the case; a failed list refresh reported and cleared; markup in a plan's name, a duty id or a standby id as text; one vocabulary and no Dev, played, container-app, hash-like, storm, prompt-version, deployment, 'fixed' or /mcp wording; no live calls";
  })();
  } catch (error) { throw new Error(`${error.message}\nLast check passed: ${passed}`); }
}
