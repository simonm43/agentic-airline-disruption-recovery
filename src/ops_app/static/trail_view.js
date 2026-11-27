"use strict";
// How ARROW decided (business UI and free-text spec §5.4; owner-approved prototype v2), now the case journey's Assessed and
// Rules steps: one summary line, the graph (a card per step, its details on hover, focus or tap: layout A, the
// owner's choice), one detail panel below it (Decision · System calls), the conversation with the step-by-step
// list, and the rule checks.
// Pure rendering from GET /api/cases/{trace}/trail; app.js owns loading and selection.

const TrailView = (() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Agents name their own keys ("constructor" included): look up only a table's own entries.
  const own = (table, key) => (Object.hasOwn(table, key) ? table[key] : undefined);
  const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const clock = (value) => {
    const time = Date.parse(value || "");
    return Number.isFinite(time) ? new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit",
      second: "2-digit", timeZone: "Europe/Lisbon" }).format(new Date(time)) : "";
  };
  const FACTS = {
    is_disruption: "Disruption confirmed", cause: "Cause", aircraft_reg: "Aircraft",
    affected_flight_ids: "Flights affected", option_id: "Option", constraint_status: "Check",
    capacity_fit: "Fits airport capacity", breaches: "Duty breaches", reserve_option_id: "Standby crew", reserve_option_ids: "Standby crews",
    reserve_options: "Standby options", misconnected_passengers: "Lose flight or connection",
    disrupted_passengers: "Lose flight or connection", rebooked_passengers: "Rebooked within policy",
    unplaced_passengers: "To Passenger Services", rebook: "Rebook", delay_messages: "Delay messages",
    additional_delay_minutes: "Extra delay (min)", slot_conflicts: "Slot conflicts", options: "Options",
    option_ids: "Options", kind: "Kind"
  };
  const ROUTE = {
    auto: "Runs automatically", occ: "OCC decides", blocked: "Blocked"
  };
  const OUTCOME = {
    executed: "Carried out", executing: "Being carried out", occ: "Waiting for OCC",
    manual: "Needs manual action", assessing: "Assessing", failed: "Stopped", no_disruption: "No disruption",
    closed: "Closed", held: "Now done · rest held", stood_down: "Stood down"
  };
  // A storm plan's actions by stage (case summary and held storm spec §2): now, when confirmed, and the stand-down's.
  const STAGES = [["now", "Now"], ["when_confirmed", "When confirmed"], ["stand_down", "Stand-down"]];
  const stageOf = (action) => (action?.stage === "when_confirmed" || action?.stage === "stand_down" ? action.stage : "now");
  const isStaged = (actions) => actions.some(action => stageOf(action) !== "now");
  // The execution's own status decides (trail._outcome reads a plan that stopped, and a storm back with OCC, as "executing"): once it
  // has ended, what was never sent will not be.
  const ENDED = new Set(["completed", "needs_attention", "stood_down", "failed"]);
  const NEVER_SENT = new Set(["not_started", "blocked", "stood_down"]);
  const ended = (outcome) => (outcome?.execution_status ? ENDED.has(outcome.execution_status) : ["executed", "failed", "stood_down"].includes(outcome?.state));
  function outcomeLabel(outcome) {
    if (outcome?.execution_status === "needs_attention") return outcome.storm?.returned ? "Back to OCC" : "Stopped";
    return OUTCOME[outcome?.state] || outcome?.state || "";
  }
  function stageLine(actions, label, outcome) {
    const confirmed = actions.filter(action => action.status === "confirmed").length;
    if (outcome?.state === "held" && actions.every(action => action.status === "not_started")) return `${label} · ${actions.length} held`;
    if (actions.every(action => action.status === "stood_down")) return `${label} · ${actions.length} never sent`;
    if (ended(outcome) && actions.every(action => NEVER_SENT.has(action.status))) return `${label} · ${actions.length} not sent`;
    return `${label} · ${confirmed} of ${actions.length} confirmed`;
  }
  // The outcome node's line: actions confirmed, or a storm's stages in short.
  function outcomeWords(outcome, actions) {
    if (!actions.length) return outcome.state === "occ" ? "Your decision" : "No action";
    const confirmed = actions.filter(a => a.status === "confirmed").length;
    if (outcome.state === "held") {
      const now = actions.filter(action => stageOf(action) === "now");
      return `Now ${now.filter(a => a.status === "confirmed").length} of ${now.length} · ${actions.filter(a => stageOf(a) === "when_confirmed" && a.status === "not_started").length} held`;
    }
    if (outcome.state === "stood_down") return `${confirmed} sent · ${actions.filter(a => a.status === "stood_down").length} never sent`;
    const unsent = actions.filter(a => NEVER_SENT.has(a.status)).length;
    if (ended(outcome) && unsent) return `${confirmed} of ${actions.length} confirmed · ${unsent} not sent`;
    return `${confirmed} of ${actions.length} actions confirmed`;
  }
  const MARK = { pass: "✓", occ: "!", expert: "→", blocked: "✕", ok: "✓", fail: "✕" };

  function value(v) {
    if (v === true) return "yes";
    if (v === false) return "no";
    if (v === null || v === undefined || v === "") return "none";
    if (Array.isArray(v)) return v.length ? v.map(x => typeof x === "object" ? (x.option_id || x.reserve_option_id || x.kind || "item") : x).join(", ") : "none";
    if (typeof v === "object") return Object.keys(v).length ? "see full answer" : "none";
    return String(v).replaceAll("_", " ");
  }

  // An agent answers in JSON; the panel shows its reason and the facts in words.
  // An agent asked about several plans answers once per plan; the trail joins the answers with a
  // blank line (trail.py). Each JSON answer is read, never shown raw (a Dev run).
  function answers(text) {
    const raw = String(text || "").trim();
    try { return [JSON.parse(raw)]; } catch { /* several answers, or prose */ }
    const pieces = raw.split(/\n\s*\n(?=\s*\{)/);
    if (pieces.length < 2) return null;
    try { return pieces.map(piece => JSON.parse(piece.trim())); } catch { return null; }
  }

  function plainAnswer(text) {
    const all = (answers(text) || []).filter(one => one && typeof one === "object" && !Array.isArray(one));
    if (!all.length) return { summary: humanize(text), facts: [] };
    const data = all[0];
    const reason = (one) => {
      const r = one.recommendation && typeof one.recommendation === "object" ? one.recommendation : {};
      return r.reason || one.reason || one.summary || "";
    };
    const rec = data.recommendation && typeof data.recommendation === "object" ? data.recommendation : {};
    const summary = humanize([...new Set(all.map(reason).filter(Boolean))].join(" "));
    const pool = { ...(data.facts && typeof data.facts === "object" ? data.facts : {}), ...data, ...rec };
    const facts = Object.entries(pool).filter(([key, v]) => own(FACTS, key) && typeof v !== "object" || (own(FACTS, key) && Array.isArray(v)))
      .map(([key, v]) => [FACTS[key], value(v)]);
    return { summary, facts: [...new Map(facts).entries()] };
  }

  function short(label) { return String(label || "").split(":")[0]; }
  const BADGE = { detection: "Autonomous", aircraft: "Recommends", crew: "Within rules", passenger: "Within policy", slot: "Within slots" };

  // The orchestrator's questions carry ids for the agents; people read "this case" (an older trace id), the case number (case_id,
  // in newer runs), the event and "the delay option". A date-time it copied from its brief reads as the time (owner review:
  // "at 2026-11-24T09:45:00"): only one without an offset, the brief's local time on the case's operating day.
  const LOCAL_TIME = /\b\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?![\d:]|\.\d|Z|[+-]\d{2})/g;
  // A word put at the start of a sentence reads with a capital ("… the delay option. Case DSR-…").
  const opening = (whole, offset) => /(?:^|[.!?]\s+)$/.test(whole.slice(0, offset));
  const lead = (word, whole, offset) => (opening(whole, offset) ? cap(word) : word);
  // Whole identifiers only: reserve_option_id and option_ids stay as written (review).
  function humanize(text) {
    return String(text || "")
      .replace(/(for )?trace_id[:= ]*[0-9a-f]{32}/gi, (m, f, offset, whole) => (f ? `${f}this case` : `${lead("this", whole, offset)} case`))
      .replace(/\bcase_id\b[:= ]*["']?([\w-]+)["']?/gi, (m, id, offset, whole) => `${lead("case", whole, offset)} ${id}`)
      .replace(/\bevent_id\b[:= ]*["']?([\w-]+)["']?/gi, (m, id, offset, whole) => `${lead("event", whole, offset)} ${id}`)
      .replace(/\boption_id\b[:= ]*["']?([\w-]+)["']?/gi, (m, id, offset, whole) => `${lead("the", whole, offset)} ${id} option`)
      // No extra delay says nothing: it goes, with its own sentence when it is one ("… EU261 exposure. additional_delay_minutes 0.").
      .replace(/([.!?])\s+\badditional_delay_minutes\b[:= ]*0\b\.?/gi, "$1")
      .replace(/,?\s*(?:\band\s+)?\badditional_delay_minutes\b[:= ]*0\b/gi, "")
      .replace(/\badditional_delay_minutes\b[:= ]*(\d+)/gi, "+$1 min")
      .replace(LOCAL_TIME, (m, hours, minutes) => `${hours}:${minutes}`);
  }

  function stepOrder(trail) {
    const order = new Map();
    let n = 0;
    for (const step of trail.orchestrator?.steps || []) {
      if (step.kind === "ask" && step.agent && !order.has(step.agent)) order.set(step.agent, ++n);
    }
    return order;
  }
  // The plans the assessment built; a what-if asked in the case chat is not part of how ARROW decided (trail.py).
  const assessed = (trail) => (trail.plans || []).filter((plan) => !isObject(plan.what_if));

  // ---------- The graph, its summary line and its detail panel (prototype v2, approved by the owner) ----------
  const HEX = /\b[0-9a-f]{32}\b/g;
  // People read the case number (DSR-…), never the 32-character trace id.
  const ids = (trail, text) => String(text ?? "").replace(HEX, trail.display_id || "this case");
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  // What a plan's checks did (owner review): a check that does not pass routes the plan, it is not a failure. "11 checks
  // pass"; "3 of 11 checks send it to OCC · 1 to Passenger Services"; "1 of 12 checks blocks it".
  function checkWords(rules) {
    const results = (Array.isArray(rules) ? rules : []).filter(isObject).map((rule) => rule.result);
    const total = results.length;
    if (!total) return "";
    const count = (kind) => results.filter((result) => result === kind).length;
    const occ = count("occ"), blocked = count("blocked"), expert = count("expert");
    const unknown = total - count("pass") - occ - blocked - expert;
    const verb = (n, one, many) => (n === 1 ? one : many);
    if (!occ && !blocked && !expert && !unknown) return `${plural(total, "check")} ${verb(total, "passes", "pass")}`;
    const of = `of ${plural(total, "check")}`;
    const passing = total - expert - unknown;
    const first = occ ? `${occ} ${of} ${verb(occ, "sends", "send")} it to OCC`
      : blocked ? `${blocked} ${of} ${verb(blocked, "blocks", "block")} it` : `${passing} ${of} ${verb(passing, "passes", "pass")}`;
    return [first, occ && blocked ? `${blocked} ${verb(blocked, "blocks", "block")}` : "", expert ? `${expert} to Passenger Services` : "",
      unknown ? `${unknown} not established` : ""].filter(Boolean).join(" · ");
  }
  // A plan by the name its card shows (owner review: "Cancel AR101 + AR102", not the system's option title); the title
  // when the case is not at hand.
  const planName = (plan, context = {}) => own(isObject(context.names) ? context.names : {}, String(plan?.plan_id ?? "")) || plan?.title || "Plan";
  const catalogue = (key) => own(Tips.AGENTS, key) || {};
  const notYet = '<span class="tv-nr">not recorded yet</span>';
  const SYSTEM_COLOR = {
    "Operations control": "#5aa2ff", Maintenance: "#c9a26b", "Crew system": "#a68bfa", "Passenger system": "#2ec4b6", "Airport capacity": "#f08a5d",
    "EUROCONTROL Network Manager": "#f08a5d", "Lisbon airport (A-CDM)": "#f08a5d", "Slot coordinator": "#f08a5d", "Ground handling": "#2ec4b6",
    ARROW: "#38c6f4", "Domain agent": "#8b9bb3"
  };
  const tint = (system) => own(SYSTEM_COLOR, Tips.system(system)) || (/airport \(A-CDM\)$/.test(String(system)) ? "#f08a5d" : "#8b9bb3");
  // Until the trail records each call's endpoint: the MCP tool behind an agent's operation, and the REST
  // route ARROW's code calls for its own (ops_app/engine.py → airline_systems routes).
  const MCP_TOOL = { assess_crew: "assess_crew_recovery", assess_booking: "assess_passenger_recovery", assess_slots: "assess_slot_recovery" };
  const REST = {
    record_delay: "POST /api/delays", record_defect: "POST /api/defects", get_aircraft_rotation: "GET /api/aircraft/{reg}/rotation",
    get_recovery_options: "GET /api/defects/{case}/options", get_airport_rules: "GET /api/airports",
    assess_crew: "GET /api/defects/{case}/assessment", assess_booking: "GET /api/defects/{case}/assessment",
    assess_slots: "GET /api/defects/{case}/assessment", get_rebooking_plan: "GET /api/defects/{case}/rebooking",
    get_duty_plan: "GET /api/defects/{case}/duty-plan", get_reserve_options: "GET /api/defects/{case}/reserve-options",
    apply_action: "POST /api/actions", get_action: "GET /api/actions/{action_id}", create_recovery_hold: "POST /api/recovery/holds"
  };
  // Steps ARROW logged without Foundry's record name no tool; their titles do (trail._event_steps).
  const TOOL_OF_STEP = {
    "Read the message": "record_disruption", "Could not read the message": "record_disruption", "Read the playbook": "get_playbook", "Wrote the case summary": "record_case",
    "Built a plan": "build_plan", "A plan was blocked": "build_plan", "Recommended a plan": "submit_plan"
  };

  function endpointOf(call, byAgent) {
    if (present(call.endpoint)) return String(call.endpoint);
    const operation = String(call.operation || "");
    return byAgent ? `MCP ${own(MCP_TOOL, operation) || operation}` : own(REST, operation) || operation;
  }
  // The trail now says whether a call read or wrote (access); older rows are read by their method. A recorded report is
  // neither (isReport): it changes no flight, so WRITE stays for the executor's changes (owner: "i thought agents only read").
  const writes = (call) => !isReport(call) && (call.access ? call.access === "write" : /^(POST|PUT|PATCH|DELETE)\b/.test(call.endpoint));
  // Each call now carries its result in words (summary), the system's own route filled in (api) and, for an agent's
  // call, its tool; never the backend's host.
  const callOf = (call, key, byAgent) => ({
    key, system: Tips.system(call.system || ""), endpoint: endpointOf(call, byAgent), ok: call.ok !== false, at: call.at,
    caller: call.caller, request: call.request, response: call.response, label: call.label, detail: call.detail,
    operation: String(call.operation || ""), summary: present(call.summary) ? String(call.summary) : "",
    api: present(call.api) ? String(call.api) : "", tool: present(call.tool) ? String(call.tool) : "",
    access: call.access === "read" || call.access === "write" ? call.access : "",
    transport: call.transport === "mcp" || call.transport === "rest" ? call.transport : "",
    byAgent
  });

  // The orchestrator's own calls: ARROW's tools over MCP, and each question to a domain agent. Each leads with what it did
  // (the step's title; a short detail such as the plan's option beside it).
  function orchestratorCalls(trail) {
    const orch = trail.orchestrator || {};
    const steps = orch.steps || [];
    const caller = { kind: "agent", name: catalogue("orchestrator").name, id: orch.identity?.client_id };
    const used = new Set();
    const calls = [];
    steps.forEach((step, index) => {
      if (step.kind === "tool") {
        const args = step.raw?.arguments;
        const output = present(step.result) ? step.result : step.raw?.output;
        const detail = String(step.detail || "").trim();
        calls.push({ key: `orch:${index}`, system: "ARROW", endpoint: `MCP ${step.tool || own(TOOL_OF_STEP, step.title) || "tool"}`,
          ok: step.ok !== false, at: step.at, caller, request: isObject(args) && Object.keys(args).length ? args : null,
          response: present(output) ? output : null, label: step.title, detail: step.detail,
          title: `${step.title}${detail && detail.length <= 24 ? `: ${detail.replaceAll("_", " ")}` : ""}` });
      } else if (step.kind === "ask") {
        // As in the Conversation view: an answer that names its question by call_id belongs only to that
        // question; agent order pairs only answers that name none (a failed ask keeps no answer).
        let found = step.call_id ? steps.findIndex((s, i) => s.kind === "answer" && !used.has(i) && s.call_id === step.call_id) : -1;
        if (found < 0) found = steps.findIndex((s, i) => i > index && s.kind === "answer" && !used.has(i) && !s.call_id && s.agent === step.agent);
        if (found >= 0) used.add(found);
        const answer = found >= 0 ? steps[found] : null;
        const agent = catalogue(step.agent).name || (step.agent ? words(step.agent) : "an agent");
        calls.push({ key: `orch:${index}`, system: "Domain agent", ask: true, agent,
          endpoint: `ask · ${agent}`, title: `Asked the ${agent}`,
          ok: step.ok !== false && answer?.ok !== false, at: step.at, caller, request: humanize(step.detail) || null,
          response: answer?.detail || null, missing: trail.status === "assessing" ? "no answer yet" : "no answer recorded", label: step.title });
      }
    });
    return calls;
  }

  // The calls behind each node. An agent's own calls include the ARROW tools it called (in newer runs: the Detection agent's
  // record of the disruption, the Passenger agent's EU261 exposure), and the record ARROW made for that tool call follows it.
  // ARROW's calls from the moment a plan was authorised to run are the executor's re-checks and writes (the trail's rechecks
  // now; for older runs, by context.executionFrom); earlier ones built and checked the plans. Each write is now a
  // call to its recipient, listed once with the recipient's own record of it (trail._call_rows).
  function nodeCalls(trail, id, context) {
    const records = (owner) => (recordOwner(trail) === owner ? reportedCalls(trail).map(([call, key]) => callOf(call, key, false)) : []);
    if (id.startsWith("agent:")) {
      const agent = (trail.agents || []).find((entry) => `agent:${entry.key}` === id);
      const own = (agent?.calls || []).map((call, index) => callOf(call, `${id}:${index}`, true));
      return agent?.key === "detection" ? withRecords(own, records("detection")) : own;
    }
    if (id === "orchestrator") return [...orchestratorCalls(trail), ...records("orchestrator")];
    if (id.startsWith("plan:")) {
      // A what-if's planning reads: ARROW built it for a chat turn, apart from the assessment (trail.chat_calls).
      const plan = (trail.plans || []).find((entry) => `plan:${entry.plan_id}` === id);
      const turn = isObject(plan?.what_if) ? plan.what_if.entry_id : null;
      return turn ? (trail.chat_calls || []).map((call, index) => [call, index])
        .filter(([call]) => call.entry_id === turn && call.caller?.kind !== "agent")
        .map(([call, index]) => callOf(call, `chat:${index}`, false)) : [];
    }
    if (id === "message") return records("message");
    if (id !== "rules" && id !== "outcome") return [];
    const from = Date.parse(context?.executionFrom || "");
    const split = !Array.isArray(trail.rechecks);
    const late = (call) => EXECUTING.has(call.operation) || (split && Number.isFinite(from) && Date.parse(call.at || "") >= from);
    const arrow = (trail.arrow_checks || []).map((call, index) => [call, index]).filter(([call]) => !isRecord(call))
      .filter(([call]) => late(call) === (id === "outcome")).map(([call, index]) => callOf(call, `arrow:${index}`, false));
    if (id === "rules") return arrow;
    const again = (Array.isArray(trail.rechecks) ? trail.rechecks : []).filter(isObject).map((call, index) => callOf(call, `again:${index}`, false));
    return [...again, ...arrow, ...sentCalls(trail)];
  }
  const inTime = (calls) => [...calls].sort((a, b) => (Date.parse(a.at || "") || 0) - (Date.parse(b.at || "") || 0));
  // ARROW's record in the disruption's own system is part of the Detection agent's call that asked for it: one row, the record
  // in its detail (owner: no repeated calls). A record no call asked for keeps a row of its own.
  const AGENT_RECORDS = { record_aircraft_defect: "record_defect", record_flight_delay: "record_delay", record_weather_forecast: "record_weather" };
  function withRecords(calls, records) {
    const left = [...records];
    const apart = (a, b) => Math.abs((Date.parse(a.at || "") || 0) - (Date.parse(b.at || "") || 0));
    const merged = calls.map((call) => {
      const wanted = own(AGENT_RECORDS, call.operation);
      const found = wanted && call.ok ? left.filter((record) => record.operation === wanted) : [];
      if (!found.length) return call;
      const record = found.reduce((best, next) => (apart(next, call) < apart(best, call) ? next : best));
      left.splice(left.indexOf(record), 1);
      return { ...call, record };
    });
    return inTime([...merged, ...left]);
  }
  // Who had the disruption recorded, so ARROW's record lists under it (trail.recorded_via, in newer runs): the Detection agent,
  // the orchestrator (older runs), or ARROW itself when a typed event arrived (the report).
  const recordOwner = (trail) => ({ detection: "detection", orchestrator: "orchestrator" })[trail.recorded_via] || "message";
  const RECORDS = new Set(["record_delay", "record_defect", "record_weather"]);
  const isRecord = (call) => isObject(call) && (RECORDS.has(String(call.operation || "")) || /^Recorded the reported\b/.test(String(call.label || call.title || "")));
  // The report as recorded: the Detection agent's typed tool or ARROW's record behind it. It adds the report to the airline's own
  // records (sql.py: a delay or weather report is only added; a defect also marks the aircraft out of service, as reported) and
  // changes no flight.
  const isReport = (call) => isObject(call) && (own(AGENT_RECORDS, String(call.operation || "")) !== undefined || isRecord(call));
  const REPORT_KIND = { record_delay: "delay", record_weather: "weather", record_defect: "defect" };
  // Where the report was recorded (owner): on ARROW's case (newer runs, trail.recorded_in "arrow"), changing no airline system
  // until a plan runs; in an older run ARROW also recorded it at once in the airline system that owns it.
  const inArrow = (trail) => trail.recorded_in === "arrow";
  // A report recorded on ARROW's case alone (newer runs), never an older run's record in an airline system.
  const arrowRecord = (trail, call) => isReport(call) && inArrow(trail) && !isObject(call.record) && !isRecord(call);
  const reportWords = (trail, call) => (arrowRecord(trail, call) ? "Recorded in ARROW" : "Report recorded");
  const LATER = { delay: "ARROW records the delay in operations control as the first action of a plan it carries out.",
    defect: "ARROW records the defect in maintenance, which marks the aircraft out of service, as the first action of a plan it carries out.",
    weather: "A weather forecast is recorded in no airline system: the met service owns it." };
  function reportTip(call, arrow) {
    const kind = own(REPORT_KIND, String(call.record?.operation || own(AGENT_RECORDS, String(call.operation || "")) || call.operation || ""));
    if (arrow) return `Recorded on ARROW's case: no airline system changes. ${own(LATER, kind) || "ARROW records it in the airline's systems when it carries out a plan."}`;
    const system = String(call.record?.system || (call.system === "ARROW" ? "" : call.system) || "");
    const where = system === "Operations control" || system === "Maintenance" ? system.toLowerCase() : system || "the airline's records";
    const adds = { delay: `Adds the reported delay to ${where} as a delay report.`, weather: `Adds the forecast to ${where} as a weather report.`,
      defect: `Adds the reported defect to ${where}, which marks the aircraft out of service as reported.` }[kind] || `Adds the report to ${where}.`;
    return `${adds} It changes no flight: flights change only when ARROW carries out an approved plan.`;
  }
  // The trail's reported_calls (newer runs), and a record left among the checks of an older trail, each with its row key.
  function reportedCalls(trail) {
    const listed = (Array.isArray(trail.reported_calls) ? trail.reported_calls : []).filter(isObject).map((call, index) => [call, `reported:${index}`]);
    const older = (trail.arrow_checks || []).map((call, index) => [call, `arrow:${index}`]).filter(([call]) => isRecord(call));
    return [...listed, ...older];
  }
  // "Recorded in operations control", "Recorded in maintenance": where ARROW recorded the report.
  const recordedIn = (calls) => {
    const systems = [...new Set(calls.map((call) => call.system).filter(Boolean))];
    const where = systems.length === 1 ? systems[0] : "";
    return `Recorded in ${where === "Operations control" || where === "Maintenance" ? where.toLowerCase() : where || "the airline systems"}`;
  };
  const EXECUTING = new Set(["apply_action", "get_action", "update_flight", "submit_duty_changes", "submit_rebooking", "order_transfer",
    "send_nm_message", "update_tobt", "request_slot", "get_slot_request"]);
  // Each call ARROW sent, by its recipient (one vocabulary with What ARROW did, owner).
  function sentCalls(trail) {
    return (trail.outcome?.actions || []).flatMap((action, a) => (Array.isArray(action.calls) ? action.calls : []).filter(isObject)
      .map((sent, index) => ({ key: `call:${sent.call_id || `${a}:${index}`}`, sent, system: Tips.system(sent.recipient || ""),
        access: "write", ok: sent.status === "confirmed", at: sent.at, endpoint: `${sent.method || ""} ${sent.path || ""}`.trim() })));
  }

  // The disruption as recorded, when the Detection agent recorded it or confirmed what ARROW recorded on receipt (newer runs);
  // never for an older run, where the orchestrator read the report and the Detection agent only confirmed it.
  const recordOf = (trail) => (isObject(trail.disruption) && trail.recorded_via !== "orchestrator" ? trail.disruption : null);
  // The disruption as recorded, under the Detection agent that read the report (owner): its type and title, only its type's
  // own fields, an event id only when the airline's system had recorded it (its source, or ARROW in an older run) and what
  // happens if nothing changes. ARROW's code checked every value against the report before it recorded it.
  const EVENT_TIP = "The airline's own reference for this disruption, issued by its system when ARROW recorded it. Every later call names it.";
  const SOURCE_TIP = "The airline's own reference for this disruption: the system that reported it had recorded it already, so ARROW does not record it again.";
  function recordedBlock(trail) {
    const recorded = recordOf(trail);
    if (!recorded) return "";
    const fields = (Array.isArray(recorded.fields) ? recorded.fields : []).filter((field) => Array.isArray(field) && field.length === 2);
    const late = (isObject(recorded.knock_on) && Array.isArray(recorded.knock_on.flights) ? recorded.knock_on.flights : []).filter(isObject)
      .map((flight) => `${flight.flight_no} ${flight.from || ""}–${flight.to || ""} ${timeOf(flight.std_local)} → ${timeOf(flight.new_std_local)}`.replace(/\s+/g, " ").trim());
    const intake = trail.recorded_via !== "detection";
    const arrow = inArrow(trail);
    const where = arrow ? `In ARROW's case${intake ? ", on arrival" : ""}` : `${reportWhere(recorded)}${intake ? ", by ARROW on arrival" : ""}`;
    return `${kindTitle(recorded)}
      <dl class="tv-kv tv-recorded">${fields.map(([name, value]) => `<dt>${esc(name)}</dt><dd>${esc(value)}</dd>`).join("")}${
        present(recorded.event_id) ? `<dt${Tips.attrs(esc, arrow ? SOURCE_TIP : EVENT_TIP, "Event")}>Event</dt><dd><code>${esc(recorded.event_id)}</code></dd>` : ""}${
        present(recorded.knock_on_words) ? `<dt>Knock-on</dt><dd${Tips.attrs(esc, late.join("\n"), "If nothing changes")}>${esc(recorded.knock_on_words)}</dd>` : ""}
        <dt>Recorded</dt><dd${Tips.attrs(esc, `${intake ? "ARROW recorded the event when it arrived." : "The Detection agent read these values from the report; ARROW's code checked each one against it before recording it."} ${
          reportTip({ operation: own(REPORT_OF, recorded.type) || "", system: own(SYSTEM_OF, recorded.type) || "" }, arrow)}`, "Report recorded")}>${esc(where)}</dd>
        <dt>Flights changed</dt><dd>None</dd></dl>`;
  }
  // "Flight delay: AR259 +20 min": the disruption as recorded, its type then its title.
  const kindTitle = (recorded) => big(`${recorded.kind}: ${phrase(recorded.title)}`);
  const REPORT_OF = { flight_delay: "record_delay", aircraft_defect: "record_defect", weather: "record_weather" };
  const SYSTEM_OF = { flight_delay: "Operations control", aircraft_defect: "Maintenance", weather: "Operations control" };
  const reportWhere = (recorded) => own({ flight_delay: "Delay report in operations control", aircraft_defect: "Defect in maintenance",
    weather: "Weather report in operations control" }, recorded.type) || "In the airline's records";
  // The EU261 exposure the Passenger agent read from ARROW (newer runs): computed by ARROW's code, as plans are priced. One row
  // per option: "Delay +15 min" and its figures.
  function exposureLine(agent) {
    const rows = (agent.calls || []).filter((call) => isObject(call) && call.operation === "get_compensation_exposure" && call.ok !== false && present(call.summary))
      .map((call) => {
        const parts = String(call.summary).split(" · ");
        const option = parts.length > 2 && !/^[\d€+−-]/.test(parts[0]) ? parts.shift() : "";
        return `<dt>${esc(option || "All plans")}</dt><dd>${esc(phrase(parts.join(" · ")))}</dd>`;
      });
    return rows.length ? `<p class="tv-sub"${Tips.attrs(esc, Tips.explain("", "EU261"), "EU261 exposure")}>EU261 exposure</p><dl class="tv-kv tv-exposure">${rows.join("")}</dl>` : "";
  }

  // An agent's first answer as an object, when it answered in JSON; its facts may sit at the top level.
  function answerOf(agent) {
    return (answers(agent?.answered) || []).find(isObject) || null;
  }
  function reader(answer) {
    const facts = isObject(answer.facts) ? answer.facts : {};
    return {
      rec: isObject(answer.recommendation) ? answer.recommendation : {},
      get: (name) => (Object.hasOwn(facts, name) ? facts[name] : Object.hasOwn(answer, name) ? answer[name] : undefined)
    };
  }
  // What each agent concluded, in a few words ("Disruption confirmed · 5 flights"); null when its answer does not say.
  function conclusion(key, answer) {
    if (!answer) return null;
    const { rec, get } = reader(answer);
    if (key === "detection") {
      if (get("is_disruption") === false) return "No disruption";
      if (get("is_disruption") !== true) return null;
      const flights = Array.isArray(get("affected_flight_ids")) ? get("affected_flight_ids").length : 0;
      return `Disruption confirmed${flights ? ` · ${plural(flights, "flight")}` : ""}`;
    }
    if (key === "aircraft") {
      const options = Array.isArray(get("options")) ? get("options").length : null;
      if (!present(rec.option_id) && options === null) return null;
      return `${present(rec.option_id) ? words(rec.option_id) : "No option"}${options === null ? "" : ` · ${plural(options, "option")}`}`;
    }
    if (key === "crew") {
      const breaches = get("breaches");
      if (!Array.isArray(breaches)) return null;
      if (!breaches.length) return "No duty breach";
      const ids = standbyIds(rec);
      return `${breaches.length} duty breach${breaches.length === 1 ? "" : "es"}${ids.length ? ` · ${standbyWords(ids)}` : ""}`;
    }
    if (key === "passenger") {
      // Of the passengers who lose their flight or connection, as answerFacts counts them.
      const disrupted = typeof get("misconnected_passengers") === "number" ? get("misconnected_passengers") : get("disrupted_passengers");
      const rebooked = get("rebooked_passengers");
      if (typeof disrupted !== "number") return null;
      if (!disrupted) return "No passenger loses their flight or connection";
      return typeof rebooked === "number" ? `${rebooked} of ${disrupted} rebookable` : `${disrupted} lose flight or connection`;
    }
    if (key === "slot") {
      const fit = get("capacity_fit");
      if (fit === true) return "Fits capacity";
      if (fit !== false) return null;
      const extra = get("required_additional_delay_minutes");
      return typeof extra === "number" ? `+${extra} min to fit capacity` : "Over capacity";
    }
    return null;
  }
  // What it recommends, in one line.
  function recommends(key, answer) {
    if (!answer) return "";
    const { rec, get } = reader(answer);
    if (key === "detection") {
      const flights = Array.isArray(get("affected_flight_ids")) ? get("affected_flight_ids") : [];
      return [flights.length ? `Flights ${flights.join(" · ")}` : "", present(get("aircraft_reg")) ? `aircraft ${get("aircraft_reg")}` : ""]
        .filter(Boolean).join(" · ");
    }
    if (key === "aircraft") {
      if (!present(rec.option_id)) return "";
      const option = (Array.isArray(get("options")) ? get("options") : []).find((o) => isObject(o) && o.option_id === rec.option_id);
      return `Recommends ${words(rec.option_id)}${option?.title ? `: ${option.title}` : ""}`;
    }
    if (key === "crew") {
      if (!Array.isArray(get("breaches")) && !Object.hasOwn(rec, "reserve_option_id") && !Object.hasOwn(rec, "reserve_option_ids")) return "";
      const ids = standbyIds(rec);
      return ids.length ? `Recommends ${standbyWords(ids)}` : "Recommends no standby crew";
    }
    const parts = [];
    if (key === "passenger") {
      if (typeof rec.rebook === "boolean") parts.push(rec.rebook ? "Recommends rebooking" : "Recommends no rebooking");
      if (typeof get("misconnected_passengers") === "number") parts.push(`${get("misconnected_passengers")} lose flight or connection`);
      if (typeof get("unplaced_passengers") === "number") parts.push(`${get("unplaced_passengers")} to Passenger Services`);
    }
    if (key === "slot") {
      if (typeof rec.delay_messages === "boolean") parts.push(rec.delay_messages ? "Recommends delay messages (DLA)" : "No delay messages advised");
      if (typeof rec.additional_delay_minutes === "number" && rec.additional_delay_minutes) parts.push(`+${rec.additional_delay_minutes} min`);
    }
    return parts.join(" · ");
  }

  // The Aircraft agent's answer in newer runs: each option with what it does to the rotation, the levers
  // it also checked (a swap or a cancellation, and why one is or is not available) and when the aircraft
  // is back. Older answers carry none of these and show nothing here.
  const LEVER = { delay: "Delay", absorb: "Absorb in the turnaround", swap: "Aircraft swap", cancel: "Cancellation" };
  const timeOf = (value) => { const m = /(?:T|^)(\d{1,2}):(\d{2})/.exec(String(value ?? "")); return m ? `${m[1].padStart(2, "0")}:${m[2]}` : ""; };
  function aircraftDetail(answer) {
    if (!answer) return "";
    const { rec, get } = reader(answer);
    const options = (Array.isArray(get("options")) ? get("options") : []).filter(isObject);
    const checked = (Array.isArray(get("checked")) ? get("checked") : []).filter(isObject);
    const from = timeOf(get("aircraft_available_from"));
    if (!options.some((o) => present(o.effect)) && !checked.length && !from) return "";
    const list = options.length ? `<h6 class="tv-sub-h">Options</h6><ul class="tv-opts">${options.map((o) => `<li><b>${
      esc(o.title || words(o.option_id || o.kind || "Option"))}</b>${present(rec.option_id) && o.option_id === rec.option_id ? '<span class="tv-chip">Agent\'s choice</span>' : ""}${
      present(o.effect) ? `<span class="tv-effect">${esc(o.effect)}</span>` : ""}</li>`).join("")}</ul>` : "";
    const also = checked.length ? `<h6 class="tv-sub-h">Also checked</h6><ul class="tv-levers">${checked.map((c) => {
      const state = c.available === true ? ["ok", "available"] : c.available === false ? ["bad", "not available"] : ["", "not established"];
      const why = present(c.why) ? String(c.why) : "";
      return `<li><b>${esc(own(LEVER, c.kind) || words(c.kind || "Lever"))}</b><span class="${state[0]}">${state[1]}</span>${
        why ? `<span class="tv-lever-why"${Tips.attrs(esc, why)}>${esc(why)}</span>` : ""}</li>`;
    }).join("")}</ul>` : "";
    return `${list}${also}${from ? `<p class="tv-rec">Aircraft available from <b>${esc(from)}</b></p>` : ""}`;
  }

  // The model by its name (GPT-4.1, GPT-4.1 mini: trail.model_name), never a deployment or a prompt version (owner:
  // "prompt vxx what is that").
  function modelLine(entry) {
    return entry.model || "Model not recorded";
  }
  // The agent itself in Foundry, at the version the case shows (owner: "open in foundry link does not work"): the
  // session's Foundry project link with the agent's name and version; no link when the session has none.
  function foundryUrl(portal, name, version) {
    if (typeof portal !== "string" || !/^https:\/\//i.test(portal) || !present(name)) return "";
    const base = `${portal.replace(/\/+$/, "")}/build/agents/${encodeURIComponent(String(name))}/build`;
    return present(version) ? `${base}?version=${encodeURIComponent(String(version))}` : base;
  }
  function foundryLink(entry, key, context = {}) {
    const name = entry?.foundry?.name || entry?.identity?.name || catalogue(key).foundry;
    const url = foundryUrl(context.portal, name, entry?.foundry?.version ?? (entry?.prompt?.available ? entry.prompt.version : null));
    if (!url) return "";
    return `<a class="tv-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer"${Tips.attrs(esc, "Opens this agent, at this version, in the Foundry portal.", "Open in Foundry")}>Open in Foundry ↗</a>`;
  }
  // The agent's instructions: a toggle beside its model in the panel's header, opening full width below it.
  function promptToggle(prompt, key) {
    if (!prompt?.available) return "<span>Instructions not available</span>";
    return `<button type="button" class="tv-toggle" data-toggle="${esc(key)}:prompt" aria-expanded="false">Instructions</button>`;
  }
  function promptBlock(prompt, key) {
    return prompt?.available ? `<div class="tv-prompt" data-block="${esc(key)}:prompt" hidden><pre>${esc(prompt.instructions)}</pre></div>` : "";
  }
  const modelOf = (entry) => `<span${Tips.attrs(esc, "The AI model this agent runs on.", "Model")}>${esc(modelLine(entry))}</span>`;
  // The scenario's autonomy level and what it allows, as one badge ("L3 | Within rules"); what it means on hover or focus.
  const badge = (entry, key) => {
    const level = entry.autonomy?.level || catalogue(key).level || "";
    const scope = own(BADGE, key) || short(entry.autonomy?.label);
    return level ? `<span class="tv-badge"${Tips.attrs(esc, meaningOf(level), `Autonomy ${level}`)}><b>${esc(level)}</b>${scope ? ` <span>${esc(scope)}</span>` : ""}</span>` : "";
  };

  // ---------- The graph (owner review: "more readable, less AI bloated text and visible boxes etc, also when hovering
  // over nice UX/UI?"): a visible card per step with its name, one key result and compact badges (autonomy level, what it read
  // or wrote, a dot per system it called). The rest waits on the step's card on hover, keyboard focus or a tap (tips.js
  // data-card); a click opens the step's panel below. Layout A, the owner's choice: left to right, the report and the
  // orchestrator, the agents in the order asked, the plans, ARROW's rules over the outcome. ----------
  const icon = (body) => `<svg class="gi" viewBox="0 0 16 16" aria-hidden="true" focusable="false">${body}</svg>`;
  const ICON = {
    read: '<path d="M1.6 8S4 3.6 8 3.6 14.4 8 14.4 8 12 12.4 8 12.4 1.6 8 1.6 8Z"/><circle cx="8" cy="8" r="1.9"/>',
    write: '<path d="m10.4 2.8 2.8 2.8-7.4 7.4H3v-2.8Z"/>',
    report: '<path d="M3.8 1.8h5.6l2.8 2.8v9.6H3.8Z"/><path d="M6 8h4.4M6 10.6h4.4"/>',
    tool: '<path d="M10.2 2.4a3.1 3.1 0 0 0-2.9 4.2l-4.6 4.6a1.2 1.2 0 0 0 1.7 1.7l4.6-4.6a3.1 3.1 0 0 0 4.2-2.9l-1.7 1.7-1.9-.5-.5-1.9Z"/>',
    star: '<path class="solid" d="m8 1.8 1.9 3.9 4.2.6-3 3 .7 4.2L8 11.5l-3.8 2 .7-4.2-3-3 4.2-.6Z"/>',
    message: '<path d="M2.4 3.2h11.2v7.4H7.3l-3.1 2.5v-2.5H2.4Z"/>',
    forecast: '<path d="M4.4 11.2h7a2.7 2.7 0 0 0 .3-5.4 3.7 3.7 0 0 0-7.1-.8 3.1 3.1 0 0 0-.2 6.2Z"/><path d="M5 13.8h6"/>'
  };
  // The agents by their scenario names; their cards say "Agent" (owner review).
  const shortName = (key, fallback = "") => String(catalogue(key).name || fallback).replace(/ Agent$/, "");
  // A recorded "a · b" reads as one phrase: "Strong crosswind at Lisbon, 16:00–18:00"; a range of times or figures never breaks.
  const phrase = (text) => String(text ?? "").replaceAll(" · ", ", ").replace(/(\d)–(\d)/g, "$1\u2060–\u2060$2");
  const ORDINAL = ["first", "second", "third", "fourth", "fifth", "sixth"];
  const isForecast = (trail) => trail.disruption?.type === "weather" || /^(TAF|METAR)\b/.test(String(trail.message?.text || "").trim());
  const cardId = (id) => `gc-${String(id).replace(/[^\w-]/g, "-")}`;
  const chip = (text, cls = "") => `<span class="gn-chip${cls ? ` ${cls}` : ""}">${esc(text)}</span>`;
  // The plan cards' route chip, short (prototype rev 2: Auto · OCC · Blocked); the card says it in full.
  const ROUTE_CHIP = { auto: "Auto", occ: "OCC", blocked: "Blocked" };
  const routeChip = (plan) => `<span class="route ${esc(plan.route || "none")}">${esc(own(ROUTE_CHIP, plan.route) || ROUTE[plan.route] || "Not routed")}</span>`;
  const ROUTE_MEANS = {
    auto: "Within every limit OCC set: ARROW carries it out and informs OCC.",
    occ: "Outside an automatic limit: an OCC duty manager decides.",
    blocked: "A rule forbids it: it cannot run."
  };

  // One key result per agent, from its answer (owner review): "AR259 +20 min", "3 options", "No duty breach",
  // "0 lose flight", "Fits capacity". The Detection agent's is what it recorded.
  function resultOf(trail, key, answer) {
    if (key === "detection" && present(recordOf(trail)?.title)) return phrase(recordOf(trail).title);
    if (!answer) return "";
    const { get } = reader(answer);
    if (key === "detection") {
      if (get("is_disruption") === false) return "No disruption";
      const flights = Array.isArray(get("affected_flight_ids")) ? get("affected_flight_ids").length : 0;
      if (get("is_disruption") === true) return flights ? `${plural(flights, "flight")} affected` : "Disruption confirmed";
    }
    if (key === "aircraft" && Array.isArray(get("options"))) {
      // An option it could not use carries no id (Case 1: swap and cancel, not available): the options are those the plans can use.
      const options = get("options").filter(isObject);
      return plural(options.filter((option) => present(option.option_id)).length || options.length, "option");
    }
    if (key === "crew" && Array.isArray(get("breaches"))) {
      const breaches = get("breaches").length;
      return breaches ? `${breaches} duty breach${breaches === 1 ? "" : "es"}` : "No duty breach";
    }
    if (key === "passenger") {
      // Of the passengers who lose their flight or connection, as the plans count them.
      const lost = typeof get("misconnected_passengers") === "number" ? get("misconnected_passengers") : get("disrupted_passengers");
      if (typeof lost === "number") return `${number.format(lost)} lose flight`;
    }
    if (key === "slot") {
      const fit = get("capacity_fit");
      const extra = get("required_additional_delay_minutes");
      if (fit === true) return "Fits capacity";
      if (fit === false) return typeof extra === "number" && extra > 0 ? `+${extra} min to fit` : "Over capacity";
    }
    return "";
  }
  function keyResult(trail, agent) {
    if (agent.status === "not asked") return "Not asked";
    const result = resultOf(trail, agent.key, answerOf(agent));
    if (result) return result;
    if (agent.status === "working") return "Working…";
    return agent.answered ? "Answered" : "No answer";
  }
  // Each answer an agent gave, by the option it was asked about: an agent asked about two plans answers twice (Case 3).
  function optionResults(trail, key) {
    const steps = trail.orchestrator?.steps || [];
    const used = new Set();
    const out = [];
    steps.forEach((step, index) => {
      if (step.kind !== "ask" || step.agent !== key) return;
      let found = step.call_id ? steps.findIndex((s, i) => s.kind === "answer" && !used.has(i) && s.call_id === step.call_id) : -1;
      if (found < 0) found = steps.findIndex((s, i) => i > index && s.kind === "answer" && !used.has(i) && !s.call_id && s.agent === key);
      if (found < 0) return;
      used.add(found);
      const option = optionOf(step.detail);
      const result = resultOf(trail, key, (answers(steps[found].detail) || []).find(isObject) || null);
      if (option && result) out.push([option.label, result]);
    });
    return out;
  }

  // Reads as an eye, a recorded report as a page and writes as a pen, each with its count, after a dot per system called.
  function callBadges(calls) {
    if (!calls.length) return "";
    const recorded = calls.filter(isReport).length;
    const written = calls.filter(writes).length;
    const read = calls.length - written - recorded;
    const systems = [...new Set(calls.map((call) => call.system).filter(Boolean))];
    return `<span class="gn-calls"><span class="gn-dots">${systems.slice(0, 3).map((system) => `<i style="--c:${tint(system)}"></i>`).join("")}</span>${
      read ? `<span class="gn-r">${icon(ICON.read)}${read}</span>` : ""}${recorded ? `<span class="gn-p">${icon(ICON.report)}${recorded}</span>` : ""}${
      written ? `<span class="gn-w">${icon(ICON.write)}${written}</span>` : ""}</span>`;
  }
  // A step on the graph: a button that opens its panel, with its card on hover, focus or a tap.
  function gnode(id, { lead = "", name, chips = "", value, meta = "", cls = "", label }, selected) {
    return `<button type="button" class="node gn ${cls}" data-node="${esc(id)}" aria-pressed="${selected === id}" aria-label="${esc(label)}"
      data-card="${esc(cardId(id))}" data-card-tap>${lead ? `<span class="gn-lead">${lead}</span>` : ""}<span class="gn-name">${esc(name)}</span>${
      chips ? `<span class="gn-chips">${chips}</span>` : ""}<span class="gn-val">${value}</span>${meta ? `<span class="gn-meta">${meta}</span>` : ""}</button>`;
  }
  // "11 checks pass"; "6 of 22 to OCC": what the checks that do not pass do (owner review: a check that does not pass
  // routes the plan, it is not a failure); the full words on the card.
  function checkShort(rules) {
    const results = rules.filter(isObject).map((rule) => rule.result);
    if (!results.length) return "";
    const count = (kind) => results.filter((result) => result === kind).length;
    const of = `of ${results.length}`;
    if (count("blocked")) return `${count("blocked")} ${of} block`;
    if (count("occ")) return `${count("occ")} ${of} to OCC`;
    if (count("expert")) return `${count("expert")} ${of} to Passenger Services`;
    if (count("pass") === results.length) return `${plural(results.length, "check")} pass${results.length === 1 ? "es" : ""}`;
    return `${results.length - count("pass")} ${of} not established`;
  }

  // ---------- The card behind each step: hover, keyboard focus or a tap (tips.js data-card) ----------
  const template = (id, body) => `<template id="${esc(cardId(id))}"><div class="gc">${body}</div></template>`;
  const gcHead = (title, chips = "") => `<p class="gc-head"><b>${esc(title)}</b>${chips ? `<span class="gc-chips">${chips}</span>` : ""}</p>`;
  const gcSub = (text) => (present(text) ? `<p class="gc-sub">${esc(text)}</p>` : "");
  // Rows of label and value; a value is markup its caller escaped, and a row may carry a mark after its label (AI).
  const gcRows = (rows) => {
    const shown = rows.filter((row) => row && present(row[1]));
    return shown.length ? `<dl class="gc-kv">${shown.map(([name, value, mark = ""]) => `<dt>${esc(name)}${mark}</dt><dd>${value}</dd>`).join("")}</dl>` : "";
  };
  const accessWords = (calls) => {
    const recorded = calls.filter(isReport).length;
    const written = calls.filter(writes).length;
    const read = calls.length - written - recorded;
    return [written ? plural(written, "write") : "", recorded ? `${recorded} report${recorded === 1 ? "" : "s"} recorded` : "",
      read ? plural(read, "read") : ""].filter(Boolean).join(", ");
  };
  const timeSpan = (calls) => {
    const times = calls.map((call) => Date.parse(call.at || "")).filter(Number.isFinite).sort((a, b) => a - b);
    if (!times.length) return "";
    const [first, last] = [clock(new Date(times[0]).toISOString()), clock(new Date(times.at(-1)).toISOString())];
    return first === last ? first : `${first}–${last}`;
  };
  // Each system call by its system: what it was for, read or write, its time and its result in words; at most `limit` per
  // system (none: the counts only), the rest in the panel.
  function gcCalls(trail, calls, limit = 4) {
    if (!calls.length) return "";
    const systems = [...new Set(calls.map((call) => call.system))];
    return `<p class="gc-h">System calls</p>${systems.map((system) => {
      const mine = inTime(calls.filter((call) => call.system === system));
      const rows = mine.slice(0, limit).map((call) => {
        const write = writes(call);
        const access = isReport(call) ? ["p", "report"] : write ? ["w", "write"] : ["", "read"];
        const what = call.sent ? call.sent.says || "Sent" : isReport(call) ? reportWords(trail, call) : call.label || words(call.operation) || "Call";
        const said = call.sent ? (call.sent.status === "confirmed" ? "" : String(call.sent.status || "")) : call.summary;
        const bad = call.sent ? ["refused", "failed"].includes(call.sent.status) : !call.ok;
        return `<li><span class="gc-what">${esc(ids(trail, phrase(what)))}</span><span class="gc-acc${access[0] ? ` ${access[0]}` : ""}">${access[1]}</span>${
          bad ? '<span class="gc-bad">✕</span>' : ""}<time>${esc(clock(call.at))}</time>${said ? `<small>${esc(ids(trail, phrase(said)))}</small>` : ""}</li>`;
      }).join("");
      const more = mine.length > limit ? `<li class="gc-more">${esc(`${mine.length - limit} more in the panel`)}</li>` : "";
      return `<div class="gc-grp"><p class="gc-sys"><i class="tv-dot" style="--c:${tint(system)}"></i><b>${esc(system)}</b><span>${esc(accessWords(mine))}</span></p>${
        limit ? `<ul class="gc-calls">${rows}${more}</ul>` : ""}</div>`;
    }).join("")}`;
  }
  // A tap shows the card; its Details opens the step's panel, as a click does (tips.js data-card-open).
  const gcFoot = (link = "") => `<p class="gc-foot">${link}<button type="button" class="gc-open" data-card-open>Details ↓</button></p>`;
  const identityOf = (entry) => (isObject(entry.identity) && entry.identity.client_id ? "Own Entra agent identity" : notYet);
  const meaningOf = (level) => cap(String(own(Tips.LEVELS, level) || ""));

  function agentTemplate(trail, agent, context) {
    const meta = catalogue(agent.key);
    const level = agent.autonomy?.level || meta.level || "";
    const asked = agent.status !== "not asked";
    const calls = nodeCalls(trail, `agent:${agent.key}`, context);
    const byOption = asked ? optionResults(trail, agent.key) : [];
    // What it recommends, without the counts its conclusion already gives; per option when it answered for several.
    const advice = asked && byOption.length < 2 ? recommends(agent.key, answerOf(agent)).split(" · ")
      .filter((part) => /^Recommends|delay messages|^\+\d+ min/i.test(part)).map((part) => cap(part.replace(/^Recommends /, ""))) : [];
    const result = keyResult(trail, agent);
    // An older answer in prose has no key result: its card gives the answer in words.
    const prose = result === "Answered" ? ids(trail, plainAnswer(agent.answered).summary) : "";
    const concluded = !asked ? "Not asked for this case"
      : byOption.length > 1 ? `<ul class="gc-opts">${byOption.map(([option, value]) => `<li><b>${esc(option)}</b>${esc(value)}</li>`).join("")}</ul>`
        : esc(prose || result);
    const recorded = agent.key === "detection" ? recordOf(trail) : null;
    return template(`agent:${agent.key}`, `${gcHead(meta.name || agent.name, level ? chip(level, "lvl") : "")}${gcSub(meta.does)}
      ${gcRows([["Autonomy", esc(meaningOf(level))], ["Model", esc(modelLine(agent))], ["Identity", identityOf(agent)],
        ["Concluded", concluded, asked ? ` ${aiMark()}` : ""],
        ["Recommends", advice.map(esc).join("<br>")],
        ["Event", recorded && present(recorded.event_id) ? `<code>${esc(recorded.event_id)}</code>` : ""],
        ["Knock-on", recorded ? esc(recorded.knock_on_words || "") : ""],
        ["Time", esc(timeSpan(calls))]])}
      ${gcCalls(trail, calls)}${gcFoot(foundryLink(agent, agent.key, context))}`);
  }

  function orchestratorTemplate(trail, context) {
    const orch = trail.orchestrator || {};
    const plans = trail.plans || [];
    const index = plans.findIndex((plan) => plan.recommended);
    const steps = orch.steps || [];
    const order = [...stepOrder(trail).keys()];
    const asks = (key) => steps.filter((step) => step.kind === "ask" && step.agent === key).length;
    const tools = new Map();
    for (const step of steps.filter((entry) => entry.kind === "tool")) tools.set(step.title, (tools.get(step.title) || 0) + 1);
    const seconds = Number.isFinite(trail.stats?.seconds) ? `${Math.round(trail.stats.seconds)} s` : "";
    const concluded = index >= 0 ? `Plan ${index + 1}, ${planName(plans[index], context)}`
      : trail.status === "assessing" ? "Working…" : "No plan recommended";
    return template("orchestrator", `${gcHead(catalogue("orchestrator").name)}${gcSub(catalogue("orchestrator").does)}
      ${gcRows([["Model", esc(modelLine(orch))], ["Identity", identityOf(orch)], ["Recommends", esc(concluded), index >= 0 ? ` ${aiMark()}` : ""], ["Time", esc(seconds)]])}
      ${order.length ? `<p class="gc-h">Asked, in order</p><ol class="gc-order">${order.map((key) =>
        `<li>${esc(shortName(key, words(key)))}${asks(key) > 1 ? `<span>${esc(`${asks(key)} times`)}</span>` : ""}</li>`).join("")}</ol>` : ""}
      ${tools.size ? `<p class="gc-h">ARROW's tools</p><ul class="gc-tools">${[...tools].map(([title, n]) =>
        `<li>${esc(title)}${n > 1 ? `<span>${esc(`${n} times`)}</span>` : ""}</li>`).join("")}</ul>` : ""}
      ${gcFoot(foundryLink(orch, "orchestrator", context))}`);
  }

  function reportTemplate(trail, context) {
    const message = trail.message || {};
    const text = String(message.text || "").trim();
    const code = /^(TAF|METAR)\b/.exec(text);
    const recorded = isObject(trail.disruption) ? trail.disruption : null;
    const by = { detection: catalogue("detection").name, orchestrator: catalogue("orchestrator").name }[trail.recorded_via] || "ARROW, when it arrived";
    return template("message", `${gcHead(isForecast(trail) ? "Forecast" : "Message")}
      ${code ? `<code class="gc-code">${esc(text)}</code>${gcSub(Tips.explain("", code[1]))}` : `<p class="gc-quote">${esc(text)}</p>`}
      ${gcRows([["From", esc(Tips.system(message.source || "OCC"))], ["Reported", esc(timeOf(message.reported_at))], ["Read by", esc(by)],
        ["Event", recorded && present(recorded.event_id) ? `<code>${esc(recorded.event_id)}</code>` : ""]])}
      ${gcCalls(trail, nodeCalls(trail, "message", context))}${gcFoot()}`);
  }

  function planTemplate(trail, plan, index, context) {
    const asked = isObject(plan.what_if) ? plan.what_if : null;
    const added = !asked && Number(plan.adjustment_minutes) > 0 ? Number(plan.adjustment_minutes) : 0;
    const count = (v) => (Number.isFinite(v) ? esc(number.format(v)) : "");
    return template(`plan:${plan.plan_id}`, `${gcHead(planName(plan, context), `${plan.recommended ? chip("Recommended", "rec") : ""}${
      asked ? chip("What-if", "whatif") : ""}${routeChip(plan)}`)}${gcSub(own(ROUTE_MEANS, plan.route) || "")}
      ${gcRows([["Cost", Number.isFinite(plan.cost_eur) ? `${esc(euro.format(plan.cost_eur))} <span class="gc-mute">indicative</span>` : ""],
        ["Lose flight or connection", count(plan.passengers_disrupted)], ["Owed compensation", count(plan.passengers_owed_compensation)],
        ["Flights", count(plan.flights)], ["Checks", esc(phrase(checkWords(plan.rules)))],
        ["Added", added ? esc(`+${added} min after the agents answered, so its figures can differ from theirs`) : ""],
        ["Asked", asked ? esc(`In chat by ${asked.asked_by || "an operator"}: ${asked.question || ""}`) : ""]])}
      ${plan.reason ? `<p class="gc-why">${aiMark()} ${esc(ids(trail, plan.reason))}</p>` : ""}${gcFoot()}`);
  }

  function rulesTemplate(trail, context) {
    const plans = trail.plans || [];
    const NOT_PASSED = { occ: "OCC", blocked: "blocks", expert: "Passenger Services" };
    const versions = [...new Set(plans.map((plan) => plan.rules_version).filter((v) => v !== null && v !== undefined))];
    return template("rules", `${gcHead("ARROW rules", versions.length ? chip(versions.map((v) => `v${v}`).join(", ")) : "")}
      ${gcSub("Every plan is checked against them. OCC sets the limits; the agents cannot change them.")}
      ${plans.length ? `<ul class="gc-plans">${plans.map((plan, index) => {
        const routed = (plan.rules || []).filter((rule) => isObject(rule) && rule.result !== "pass");
        return `<li><p><b>${esc(`Plan ${index + 1}`)}</b><span>${esc(planName(plan, context))}</span>${routeChip(plan)}</p>
          <small>${esc(phrase(checkWords(plan.rules)) || "No rule checked")}</small>${routed.length ? `<small class="gc-mute">${esc(routed.map((rule) =>
            `${rule.rule}${own(NOT_PASSED, rule.result) ? ` (${NOT_PASSED[rule.result]})` : ""}`).join(", "))}</small>` : ""}</li>`;
      }).join("")}</ul>` : gcSub(trail.status === "assessing" ? "The rules are checked once a plan is built." : "No plan was built.")}
      ${gcCalls(trail, nodeCalls(trail, "rules", context), 0)}${gcFoot()}`);
  }

  function outcomeTemplate(trail, context) {
    const outcome = trail.outcome || {};
    const actions = outcome.actions || [];
    const by = outcome.approved_by === "policy" ? "ARROW's rules, within every limit" : outcome.approved_by ? "the OCC Duty Manager" : "";
    // Why OCC decides, as ARROW's code worded it: "OCC decides: A; B; C".
    const reasons = ["occ", "manual"].includes(outcome.state) ? String(outcome.stage || "").replace(/^[^:]*decides:\s*/i, "").split(/;\s*/).filter(Boolean) : [];
    return template("outcome", `${gcHead(phrase(outcomeLabel(outcome)) || "Outcome")}
      ${gcRows([["Authorised", by ? esc(by) : ""], ["Actions", actions.length ? esc(phrase(outcomeWords(outcome, actions))) : ""]])}
      ${reasons.length ? `<p class="gc-h">Why OCC decides</p><ul class="gc-why-list">${reasons.slice(0, 5).map((reason) => `<li>${esc(reason)}</li>`).join("")}${
        reasons.length > 5 ? `<li class="gc-more">${esc(`${reasons.length - 5} more`)}</li>` : ""}</ul>` : ""}
      ${!reasons.length && !actions.length && present(outcome.stage) ? gcSub(outcome.stage) : ""}
      ${gcCalls(trail, nodeCalls(trail, "outcome", context), 3)}${gcFoot()}`);
  }

  function graph(trail, selected, context = {}) {
    const message = trail.message || {};
    const working = trail.status === "assessing";
    const plans = trail.plans || [];
    const outcome = trail.outcome || {};
    const order = stepOrder(trail);
    const forecast = isForecast(trail);
    const source = Tips.system(message.source || "OCC");
    const kind = isObject(trail.disruption) && present(trail.disruption.kind) ? trail.disruption.kind : forecast ? "Forecast" : "Message";
    const cards = [reportTemplate(trail, context), orchestratorTemplate(trail, context)];
    // The report as received (its full text on its card); ARROW's own record of it shows only when ARROW recorded it on receipt.
    const report = gnode("message", { lead: icon(forecast ? ICON.forecast : ICON.message), name: source, value: esc(kind),
      meta: callBadges(nodeCalls(trail, "message", context)), cls: "gn-report",
      label: `${forecast ? "Forecast" : "Message"} from ${source}: ${kind}` }, selected);
    const chosen = plans.findIndex((plan) => plan.recommended);
    const recommended = chosen >= 0 ? `Recommends plan ${chosen + 1}` : working ? "Working…" : "No plan recommended";
    const tools = nodeCalls(trail, "orchestrator", context).filter((call) => !call.ask).length;
    const orchestrator = gnode("orchestrator", { name: shortName("orchestrator", "Orchestrator"), value: esc(recommended),
      meta: tools ? `<span class="gn-calls"><span class="gn-t">${icon(ICON.tool)}${tools}</span></span>` : "",
      cls: `gn-hub ${working ? "gn-working" : "gn-done"}`, label: `${catalogue("orchestrator").name}: ${recommended}` }, selected);
    const agents = (trail.agents || []).map((agent) => {
      const asked = agent.status !== "not asked";
      const status = !asked ? "idle" : agent.status === "working" ? "working" : "done";
      const result = keyResult(trail, agent);
      const no = order.get(agent.key);
      const level = agent.autonomy?.level || catalogue(agent.key).level || "";
      cards.push(agentTemplate(trail, agent, context));
      return gnode(`agent:${agent.key}`, { lead: no ? `<span class="gn-no">${esc(no)}</span>` : "", name: shortName(agent.key, agent.name),
        chips: level ? chip(level, "lvl") : "", value: esc(result), meta: callBadges(nodeCalls(trail, `agent:${agent.key}`, context)),
        cls: `gn-agent gn-${status}`, label: `${catalogue(agent.key).name || agent.name}${no ? `, asked ${ORDINAL[no - 1] || no}` : ""}: ${result}` }, selected);
    });
    const planNodes = plans.map((plan, index) => {
      cards.push(planTemplate(trail, plan, index, context));
      const whatIf = isObject(plan.what_if);
      // Agents answer at the reported delay; ARROW may add minutes for capacity or standby crews, so the plan's figures can
      // differ from theirs (its card says so). A what-if's minutes were asked for in chat.
      const added = !whatIf && Number(plan.adjustment_minutes) > 0 ? `<small class="gn-add">+${esc(plan.adjustment_minutes)} min</small>` : "";
      const name = plan.kind ? cap(plan.kind) : "Plan";
      return gnode(`plan:${plan.plan_id}`, { lead: plan.recommended ? `<span class="gn-star">${icon(ICON.star)}</span>` : "",
        name: `Plan ${index + 1}`, chips: `${whatIf ? chip("What-if", "whatif") : ""}${routeChip(plan)}`, value: `${esc(name)}${added ? ` ${added}` : ""}`,
        meta: Number.isFinite(plan.cost_eur) ? `<span class="gn-cost">${esc(euro.format(plan.cost_eur))}</span>` : "",
        cls: `gn-plan gn-route-${esc(plan.route || "none")}${whatIf ? " gn-whatif" : ""}${plan.recommended ? " gn-rec" : ""}`,
        label: `Plan ${index + 1}${plan.recommended ? ", recommended" : ""}${whatIf ? ", a what-if asked in chat" : ""}: ${name}, ${ROUTE[plan.route] || "not routed"}` }, selected);
    });
    const empty = `<div class="gn gn-empty">${esc(working ? "Not built yet" : "No plan")}</div>`;
    cards.push(rulesTemplate(trail, context), outcomeTemplate(trail, context));
    const checked = checkShort(plans.flatMap((plan) => plan.rules || []));
    const rules = gnode("rules", { name: "ARROW rules",
      value: esc(checked || (working ? "Waiting" : "No plan")), meta: callBadges(nodeCalls(trail, "rules", context)),
      cls: `gn-rules ${plans.length ? "gn-done" : "gn-idle"}`, label: `ARROW rules: ${checked || "no plan"}` }, selected);
    const state = phrase(outcomeLabel(outcome)) || (working ? "Assessing" : "No outcome");
    const result = gnode("outcome", { name: "Outcome", value: esc(state), meta: callBadges(nodeCalls(trail, "outcome", context)),
      cls: `gn-outcome gn-out-${esc(outcome.state || "none")}`, label: `Outcome: ${state}` }, selected);
    const head = (text) => `<h4>${esc(text)}</h4>`;
    return `<div class="flow"><svg class="wires" aria-hidden="true"></svg>
        <div class="col">${head("Report")}${report}${head("Orchestrator")}${orchestrator}</div>
        <div class="col">${head("Agents")}${agents.join("")}</div>
        <div class="col">${head("Plans")}${planNodes.join("") || empty}</div>
        <div class="col">${head("Rules")}${rules}${head("Outcome")}${result}</div>
      </div><div class="tv-cards" hidden>${cards.join("")}</div>`;
  }

  // Wires, drawn after layout: square with rounded corners, from a right edge into a left one, one trunk per fan so nothing
  // crosses (owner review: the wires "converge into a tangle"); straight down a column from the report to the
  // orchestrator and from ARROW's rules to the outcome. A step under the pointer or in focus lights its own wires.
  const across = (x1, y1, xm, x2, y2) => {
    if (Math.abs(y2 - y1) < 1) return `M${x1},${y1} H${x2}`;
    const r = Math.min(8, Math.abs(y2 - y1) / 2, Math.abs(xm - x1), Math.abs(x2 - xm));
    const dy = Math.sign(y2 - y1);
    return `M${x1},${y1} H${xm - r} Q${xm},${y1} ${xm},${y1 + dy * r} V${y2 - dy * r} Q${xm},${y2} ${xm + r},${y2} H${x2}`;
  };
  // The wires follow the graph's size: the case chat docking beside the case moves the steps without a new render.
  let watched = null;
  function follow(container, trail, flow) {
    if (watched?.flow === flow) { watched.trail = trail; return; }
    watched?.observer.disconnect();
    const observer = new ResizeObserver(() => { if (watched?.flow === flow && flow.isConnected) drawWires(container, watched.trail); });
    watched = { flow, trail, observer };
    observer.observe(flow);
  }
  function drawWires(container, trail) {
    const flow = container.querySelector(".flow");
    const svg = flow?.querySelector("svg.wires");
    if (!flow || !svg) return;
    follow(container, trail, flow);
    // A narrow graph stacks its columns and draws no wires (ui.css).
    if (getComputedStyle(svg).display === "none") return;
    const box = flow.getBoundingClientRect();
    const at = (id) => {
      const r = flow.querySelector(`.node[data-node="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      return r ? { l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, b: r.bottom - box.top,
        x: r.left - box.left + r.width / 2, y: r.top - box.top + r.height / 2 } : null;
    };
    let out = "";
    const wire = (a, b, d, idle = false) => { out += `<path class="wire${idle ? " idle" : ""}" data-a="${esc(a)}" data-b="${esc(b)}" d="${d}"/>`; };
    const report = at("message"), hub = at("orchestrator");
    if (report && hub) wire("message", "orchestrator", `M${hub.x},${report.b} V${hub.t}`);
    const agents = (trail.agents || []).map((agent) => [agent, at(`agent:${agent.key}`)]).filter(([, r]) => r);
    if (hub && agents.length) {
      const bus = (hub.r + Math.min(...agents.map(([, r]) => r.l))) / 2;
      for (const [agent, r] of agents) wire("orchestrator", `agent:${agent.key}`, across(hub.r, hub.y, bus, r.l, r.y), agent.status === "not asked");
    }
    // The orchestrator builds each plan from what the agents answered; a what-if was asked for in chat. ARROW's rules check every
    // plan, a what-if too.
    const asked = agents.filter(([agent]) => agent.status !== "not asked");
    const built = assessed(trail).map((plan) => [plan, at(`plan:${plan.plan_id}`)]).filter(([, r]) => r);
    if (asked.length && built.length) {
      const bus = (Math.max(...asked.map(([, r]) => r.r)) + Math.min(...built.map(([, r]) => r.l))) / 2;
      for (const [agent, r] of asked) {
        for (const [plan, p] of built) wire(`agent:${agent.key}`, `plan:${plan.plan_id}`, across(r.r, r.y, bus, p.l, p.y));
      }
    }
    const rules = at("rules"), outcome = at("outcome");
    const plans = (trail.plans || []).map((plan) => [plan, at(`plan:${plan.plan_id}`)]).filter(([, r]) => r);
    if (rules && plans.length) {
      const bus = (Math.max(...plans.map(([, r]) => r.r)) + rules.l) / 2;
      for (const [plan, p] of plans) wire(`plan:${plan.plan_id}`, "rules", across(p.r, p.y, bus, rules.l, rules.y));
    }
    if (rules && outcome) wire("rules", "outcome", `M${outcome.x},${rules.b} V${outcome.t}`, !plans.length);
    const head = (id) => `<marker id="${id}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M1,1.2 7,4 1,6.8Z"/></marker>`;
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    svg.innerHTML = `<defs>${head("gw-end")}${head("gw-hot")}</defs>${out}`;
    // A step under the pointer or in focus lights its wires, drawn last so a shared trunk shows lit; a redraw keeps them lit.
    const light = (id) => {
      flow.classList.toggle("lit", Boolean(id));
      svg.querySelectorAll(".wire").forEach((path) => {
        const hot = Boolean(id) && (path.dataset.a === id || path.dataset.b === id);
        path.classList.toggle("hot", hot);
        if (hot) svg.append(path);
      });
    };
    const stepOf = (target) => (target instanceof Element ? target.closest(".node[data-node]")?.dataset.node || "" : "");
    const focused = () => (flow.contains(document.activeElement) ? stepOf(document.activeElement) : "");
    flow.onmouseover = (event) => light(stepOf(event.target));
    flow.onmouseleave = () => light(focused());
    flow.onfocusin = (event) => light(stepOf(event.target));
    flow.onfocusout = () => light("");
    light(stepOf(flow.querySelector(".node[data-node]:hover")) || focused());
  }

  // The orchestrator's input: ARROW's case brief, the message as written and the limits in force. A report typed when it arrived
  // (in newer runs) names the operations event and its type's own fields, as the Detection agent's record shows them.
  function briefWords(brief, trail = {}) {
    if (!brief || !Object.keys(brief).length) return `<p>${notYet}</p>`;
    const limits = isObject(brief.limits) ? [
      Number.isFinite(brief.limits.cost_cap_eur) ? `automatic plans up to ${euro.format(brief.limits.cost_cap_eur)}` : "",
      present(brief.limits.rebook_limit) ? `at most ${brief.limits.rebook_limit} rebookings` : "",
      present(brief.limits.max_plans) ? `up to ${brief.limits.max_plans} plans` : ""].filter(Boolean).join(", ") : "";
    const foot = (from) => [from, limits ? `Limits: ${limits}` : ""].filter(Boolean).map((line) => `<p class="muted small">${esc(line)}</p>`).join("");
    if (brief.message) return `<p class="bubble">${esc(brief.message)}</p>${foot(`From ${brief.reported_by || "OCC"}`)}`;
    if (isObject(brief.disruption)) {
      const recorded = isObject(trail.disruption) ? trail.disruption : null;
      const rows = [["Disruption", recorded ? `${recorded.kind}: ${phrase(recorded.title)}` : words(brief.disruption.type || "")],
        ...(recorded && Array.isArray(recorded.fields) ? recorded.fields.filter((field) => Array.isArray(field) && field.length === 2) : []),
        ["Event", brief.event_id]].filter(([, value]) => present(value));
      const at = timeOf(brief.reported_at);
      return `<dl class="tv-kv">${rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>${
        present(brief.description) ? `<p class="bubble">${esc(brief.description)}</p>` : ""}${foot(`From ${brief.reported_by || "a system event"}${at ? ` at ${at}` : ""}`)}`;
    }
    return `<p>Cause <b>${esc(String(brief.cause || "").replaceAll("_", " "))}</b>, flights ${esc((brief.scope?.flights || []).join(", ") || "none")}, aircraft ${
      esc(brief.scope?.aircraft_reg || "unknown")}; allowed options: ${esc((brief.allowed_aircraft_kinds || []).join(", ") || "none")}.</p>${limits ? foot("") : ""}`;
  }

  // One line above the graph: what it took. The End-to-end trace and Foundry links sit in the case header.
  function summary(trail) {
    const s = trail.stats || {};
    const item = (n, one, many, tip) => `<span${Tips.attrs(esc, tip)}><b>${esc(n)}</b> ${esc(n === 1 ? one : many)}</span>`;
    // ARROW's record of the report counts once: in reported_calls in newer runs, among its checks in older ones. An agent's
    // call to one of ARROW's own tools is not an airline system call.
    const recorded = Number.isFinite(s.reported_calls) ? s.reported_calls : Array.isArray(trail.reported_calls) ? trail.reported_calls.length : 0;
    const byAgents = (trail.agents || []).reduce((sum, agent) => sum + (agent.calls || []).filter((call) => isObject(call) && call.system !== "ARROW").length, 0);
    const byArrow = (s.arrow_checks || 0) + (s.rechecks || 0) + recorded + (s.calls || 0);
    const numbers = [
      item(s.agents_asked ?? 0, "agent", "agents", "Domain agents the orchestrator asked"),
      item(byAgents + byArrow, "system call", "system calls", `Calls to the airline systems: ${byAgents} by the agents, ${byArrow} by ARROW's code and executor`),
      item(s.rule_checks ?? 0, "rule check", "rule checks", "Checks ARROW's code ran on the plans"),
      item(s.actions ?? 0, "action", "actions", "Changes made in the airline systems"),
      ...(Number.isFinite(s.seconds) ? [item(Math.round(s.seconds), "s", "s", "From the report to the orchestrator's last step")] : [])
    ].join("");
    return `<p class="tv-nums">${numbers}</p>`;
  }

  // ---------- The detail panel: Decision · System calls ----------
  const keep = (key) => ` data-keep="${esc(key)}"`;
  const TONE = { auto: "ok", occ: "warn", blocked: "bad" };
  const head = (kicker, title, meta = "") => `<header class="tv-head"><p class="kicker">${esc(kicker)}</p><h3>${esc(title)}</h3>${
    meta ? `<span class="tv-meta">${meta}</span>` : ""}</header>`;
  const part = (title, body, count) => `<section class="tv-part"><h5>${esc(title)}${
    count === undefined ? "" : ` <span class="tv-count">${esc(count)}</span>`}</h5>${body}</section>`;
  const big = (text, tone = "") => `<p class="tv-big ${tone}">${esc(text)}</p>`;
  const go = (attr, value, text) => `<p class="tv-go"><button type="button" class="tv-ghost" ${attr}="${esc(value)}">${esc(text)}</button></p>`;
  const RULES_TIP = "The rules in force when the plans were checked. OCC sets the limits, with a second approver; the agents cannot change them.";
  const versionChip = (version) => (version === null || version === undefined ? ""
    : `<span class="gn-chip"${Tips.attrs(esc, RULES_TIP, `Rules v${version}`)}>Rules v${esc(version)}</span>`);

  // A recorded request, response or receipt in words: an object as a list, JSON text read as such,
  // times on the Lisbon clock and ids as the case number. Tool outputs repeat themselves as
  // {"structuredResponse": …}; the first copy is read.
  const jsonOf = (text) => {
    const whole = parsed(text);
    return whole !== undefined ? whole : parsed(text.split(/\n\{\s*"structuredResponse"/)[0]);
  };
  // A recorded field by its name in words (owner: no raw field names); others: underscores as spaces, capitalised.
  // Neutral by default; an executor action's request names its kind and slot text for what they are (ACTION_FIELDS).
  const FIELD_NAMES = {
    trace_id: "Case", case_id: "Case", case: "Case", at: "Time", kind: "Kind", summary: "Summary", status: "Status", reason: "Reason",
    flight_no: "Flight", flight_nos: "Flights", flights: "Flights", std_local: "Departs", sta_local: "Arrives", aircraft_reg: "Aircraft",
    option_id: "Option", aircraft_option_id: "Option", additional_delay_minutes: "Extra delay (min)", reserve_option_id: "Standby crew", reserve_option_ids: "Standby crews",
    delay_minutes: "Delay (min)", unavailable_minutes: "Out of service (min)", eobt: "EOBT", tobt: "TOBT", airport: "Airport",
    cause: "Cause", passengers: "Passengers", type: "Type", affected_flight_ids: "Affected flights", is_disruption: "Disruption",
    event_id: "Event", reading: "Reading", recorded: "Recorded", flights_affected: "Flights affected", max_minutes_late: "Up to (min late)",
    unavailable_until: "Out of service until", passengers_owed_compensation: "Owed compensation", compensation_eur: "Compensation (EUR)",
    care_eur: "Care (EUR)", rebooking_eur: "Rebooking (EUR)", extraordinary_circumstances: "Extraordinary circumstances"
  };
  const ACTION_FIELDS = { kind: "Action", request: "Slot request" };
  const fieldName = (key, names = {}) => own(names, key) || own(FIELD_NAMES, key) || cap(String(key).replaceAll("_", " "));
  function shown(trail, v, depth = 0, names = {}) {
    if (!present(v)) return "none";
    if (typeof v === "string") {
      const text = v.trim();
      const json = depth === 0 && /^[[{]/.test(text) ? jsonOf(text) : undefined;
      if (json !== undefined) return shown(trail, json, depth, names);
      if (/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)$/.test(text) && clock(text)) return esc(clock(text));
      if (/^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?$/.test(text)) return esc(text.slice(11, 16));
      // An identifier such as "retime_flights" reads as words.
      const plain = /^[a-z]+(_[a-z]+)+$/.test(text) ? text.replaceAll("_", " ") : text;
      return `<span class="tv-text">${esc(ids(trail, plain))}</span>`;
    }
    if (typeof v === "number") return esc(number.format(v));
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (Array.isArray(v)) {
      if (!v.length) return "none";
      if (v.every((item) => !item || typeof item !== "object")) return esc(v.map((item) => ids(trail, item)).join(" · "));
      const keys = [...new Set(v.flatMap((item) => (isObject(item) ? Object.keys(item) : [])))];
      if (depth < 2 && v.every(isObject) && keys.length <= 6 && v.every((item) => Object.values(item).every((x) => !x || typeof x !== "object"))) {
        return `<div class="tv-scroll"><table class="tv-mini"><thead><tr>${keys.map((key) => `<th${Tips.attrs(esc, Tips.explain("", fieldName(key)))}>${esc(fieldName(key))}</th>`).join("")}</tr></thead>
          <tbody>${v.map((item) => `<tr>${keys.map((key) => `<td>${shown(trail, item[key], 2)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
      }
      return `<pre>${esc(ids(trail, JSON.stringify(v, null, 2)))}</pre>`;
    }
    if (isObject(v)) {
      const keys = Object.keys(v);
      if (!keys.length) return "none";
      if (depth >= 2) return `<code>${esc(ids(trail, JSON.stringify(v)))}</code>`;
      // The context's names apply to the object they describe, not to the values nested in it.
      return `<dl class="tv-kv">${keys.map((key) => `<dt${Tips.attrs(esc, Tips.explain("", fieldName(key, names)))}>${esc(fieldName(key, names))}</dt><dd>${
        shown(trail, v[key], depth + 1)}</dd>`).join("")}</dl>`;
    }
    return esc(ids(trail, v));
  }
  // names: field names for this record's context (ACTION_FIELDS for an executor action's request).
  const recorded = (trail, v, missing = "not recorded yet", names = {}) => (present(v) ? shown(trail, v, 0, names) : `<span class="tv-nr">${esc(missing)}</span>`);

  // The trail names an agent caller as it names the agent ("Crew"); people read its full name. An
  // older row has no caller identity: the trail reports it unknown, without an id.
  function callerOf(trail, caller) {
    if (!isObject(caller) || (caller.kind === "unknown" && !caller.id && (!caller.name || caller.name === "Unknown caller"))) return null;
    const agent = caller.kind === "agent" ? (trail.agents || []).find((entry) => entry.name === caller.name || entry.key === caller.name) : null;
    const name = (agent && catalogue(agent.key).name) || caller.name || (caller.kind === "arrow" ? "ARROW" : "Unknown caller");
    return { name, kind: caller.kind, id: present(caller.id) ? String(caller.id) : "" };
  }

  // Where a call went, only in its opened detail (owner: "slash mcp at the end, really? dev hardcoded in the url??"):
  // an agent's call is a tool call (MCP) to the system, with no route (owner: a REST route it never called read as a
  // second call); ARROW's own call is the system's route, filled in with the case's values. Never a host; a route whose
  // values were not recorded is not shown.
  const MCP_TIP = "MCP: Model Context Protocol, how an AI agent calls a system's tools.";
  const A2A_TIP = "A2A: agent to agent. The orchestrator asks a domain agent in Foundry and reads its answer.";
  const API_ROUTE = /^(GET|POST|PUT|PATCH|DELETE) \/\S*$/;
  function ifaceLines(call) {
    if (call.ask) return [`<span class="tv-mute"${Tips.attrs(esc, A2A_TIP)}>Agent to agent (A2A)</span> question to ${esc(call.agent || "the agent")}`];
    const tool = call.tool || (/^MCP\s+(\S+)/.exec(call.endpoint) || [])[1] || "";
    if (call.transport === "mcp" || (Boolean(tool) && call.transport !== "rest")) {
      return [`<span class="tv-mute"${Tips.attrs(esc, MCP_TIP)}>Tool call (MCP)</span> to ${esc(call.system)} · <code>${esc(tool)}</code>`];
    }
    const filled = RecoveryView.filledRoute(call.endpoint, call.request);
    const route = call.api || (API_ROUTE.test(filled) && !/\{\w+\}/.test(filled) ? filled : "");
    return route ? [`<code>${esc(route)}</code>`] : [];
  }

  // Owner: "can we add 'write' when it is actually modifying something?" A write carries WRITE, a read a quiet "read"; a
  // recorded report neither, its row says so (callRow).
  function accessBadge(call) {
    if (!call.access || isReport(call)) return "";
    const write = call.access === "write";
    const agent = call.byAgent || call.caller?.kind === "agent";
    const tip = write
      ? `Write: ARROW's code changes data in ${call.system}. The agents' Microsoft Entra ID roles on the airline systems are read roles.`
      : agent
        ? "Read: the agents' Microsoft Entra ID roles on the airline systems are read roles, so an agent's tool looks things up and never changes them."
        : `Read: ARROW's code looked data up in ${call.system} and changed nothing.`;
    return `<span class="tv-acc-b${write ? " w" : ""}"${Tips.attrs(esc, tip, write ? "Write" : "Read")}>${write ? "WRITE" : "read"}</span>`;
  }

  // One row per call (owner): the system, what the call was for and its result, in words; WRITE when it changed
  // something; result and time. Opened: who called, what was sent and what came back, then how: the tool call or the system's own
  // route with the case's values, the call's reference and the caller's ID.
  const REFERENCES = ["action_id", "request_id", "call_id"];
  function callRow(trail, call) {
    if (call.sent) {
      // A call ARROW sent: the same row as in What ARROW did, with the identity the recipient recorded.
      const record = (Array.isArray(call.sent.recorded) ? call.sent.recorded : []).find(isObject);
      const who = callerOf(trail, record?.caller);
      return RecoveryView.callRow(call.sent, { data: { keep: call.key }, identity: who?.id ? who : null });
    }
    const fail = !call.ok && typeof call.response === "string";
    const who = callerOf(trail, call.caller);
    const dot = `<i class="tv-dot" style="--c:${tint(call.system)}"></i>`;
    const status = `<span class="tv-st ${call.ok ? "ok" : "bad"}">${call.ok ? "✓ ok" : "✕ failed"}</span>`;
    const time = `<time>${esc(clock(call.at))}</time>`;
    // The orchestrator's steps keep their own titles; a system call reads as its purpose and its result; a recorded report says so:
    // on ARROW's case (newer runs), or in the airline system that owns it (an older run's record).
    const report = isReport(call);
    // The Detection agent's record: in an older run ARROW also recorded it in the disruption's own system (withRecords).
    const record = isObject(call.record) ? call.record : null;
    const arrowOnly = arrowRecord(trail, call);
    const reported = reportWords(trail, call);
    const [what, said] = report ? [reported, [/^Recorded the reported\b/.test(String(call.label || "")) ? "" : call.label, call.summary].filter(present).join(" · ")]
      : call.title ? [call.title, ""] : call.summary ? [call.label || words(call.operation), call.summary] : [RecoveryView.callWords(call), ""];
    // References belong to the interface, not to what was sent.
    const request = isObject(call.request) ? Object.fromEntries(Object.entries(call.request).filter(([key]) => !REFERENCES.includes(key))) : call.request;
    const refs = isObject(call.request) ? REFERENCES.filter((key) => present(call.request[key])).map((key) => String(call.request[key])) : [];
    const recordRoute = record ? record.api || (API_ROUTE.test(record.endpoint) ? record.endpoint : "") : "";
    const arrow = record ? callerOf(trail, record.caller) : null;
    const lines = [...ifaceLines(call),
      record ? `ARROW recorded it in ${esc(record.system)}${recordRoute ? ` · <code>${esc(recordRoute)}</code>` : ""}` : "",
      refs.length ? `Reference <code>${esc(refs.join(" · "))}</code>` : "",
      who?.id ? `${who.kind === "agent" ? "Entra agent ID" : `${esc(who.name)} ID`} <code>${esc(who.id)}</code>` : "",
      arrow?.id && arrow.id !== who?.id ? `${esc(arrow.name)} ID <code>${esc(arrow.id)}</code>` : ""].filter(Boolean);
    const where = record ? `${record.system}, via ${call.system}` : call.system;
    return `<details class="tv-call"${keep(call.key)}>
      <summary><span class="tv-row">${dot}<span class="tv-call-what"><b${report ? Tips.attrs(esc, reportTip(call, arrowOnly), reported) : ""}>${esc(ids(trail, phrase(what)))}</b>${
        said ? ` <span class="tv-res">${esc(ids(trail, phrase(said)))}</span>` : ""}<small class="tv-sysn">${esc(where)}</small></span>
        ${accessBadge(call) || "<span></span>"}${status}${time}</span></summary>
      <div class="tv-callbody">
        <p class="tv-caller">${who ? `By ${esc(who.name)}` : 'Caller <span class="tv-nr">not recorded yet</span>'}</p>${report ? `<p class="tv-note">${esc(reportTip(call, arrowOnly))}</p>` : ""}
        <div class="tv-io2"><section><h6>${call.ask ? "Question" : "Sent"}</h6>${recorded(trail, request, "not recorded yet",
          call.operation === "apply_action" ? ACTION_FIELDS : {})}</section>
          <section><h6>${call.ask ? "Answer" : "Returned"}</h6>${fail ? `<p class="tv-quote bad">${esc(ids(trail, call.response))}</p>`
            : recorded(trail, call.response, call.missing)}</section></div>
        <div class="tv-iface iface"><h6>Interface</h6>${lines.map((line) => `<p>${line}</p>`).join("")}</div>
      </div></details>`;
  }
  // A call list; the executor's re-checks before it wrote fold into one line above the writes (owner: "I again see
  // repetitive calls ... in the last step").
  function callsPart(trail, calls, title = "System calls") {
    const again = calls.filter((call) => String(call.key).startsWith("again:"));
    const rest = calls.filter((call) => !again.includes(call));
    const rows = (list) => `<div class="tv-callrows">${list.map((call) => callRow(trail, call)).join("")}</div>`;
    const folded = again.length ? `<details class="tv-again"${keep("again")}><summary${Tips.attrs(esc,
      "Before it writes, ARROW reads every source the plan was checked against again, and stops if one has changed since the approval.",
      "Re-checked before writing")}>${esc(`Re-checked ${plural(again.length, "source")} before writing`)}</summary>${rows(again)}</details>` : "";
    return part(title, calls.length ? `${folded}${rest.length ? rows(rest) : ""}` : '<p class="muted small">None for this step.</p>', calls.length);
  }

  function checkList(plan) {
    const detailed = Boolean(plan.checks?.length);
    const list = detailed ? plan.checks : plan.rules || [];
    if (!list.length) return '<p class="muted small">No rule checked.</p>';
    return `<table class="tv-checks"><tbody>${list.map((entry) => {
      const input = detailed ? entry.input : entry.detail;
      return `<tr><td><span class="mark ${esc(entry.result)}">${own(MARK, entry.result) || "?"}</span></td><td>${esc(entry.rule)}</td>
        <td class="tv-io">${esc(cap(phrase(input ?? "")))}${detailed && present(entry.limit) ? `<span>${esc(cap(phrase(entry.limit)))}</span>` : ""}</td></tr>`;
    }).join("")}</tbody></table>`;
  }

  // An agent's answer, pretty when it is JSON; ids read as the case number.
  function fullAnswer(trail, text) {
    const all = answers(text);
    return ids(trail, all ? all.map((one) => JSON.stringify(one, null, 2)).join("\n\n") : String(text || ""));
  }
  // A reason in its author's name, never in quotes (owner), marked as written by AI (EU AI Act transparency).
  const said = (by, text) => `<p class="tv-said"><span class="tv-by">${esc(by)}</span> ${aiMark()} ${esc(text)}</p>`;
  // The one mark, on text an AI agent wrote: discreet, explained on hover or focus. What ARROW's code computed or checked carries
  // none (owner: "code, not AI" read as unprofessional).
  const AI_TIP = "Written by an AI agent. ARROW's code checks every figure and rule, and OCC decides.";
  const aiMark = () => `<span class="mk mk-ai"${Tips.attrs(esc, AI_TIP, "AI")}>AI</span>`;
  function leverWords(levers) {
    const ids = standbyIds(levers);
    const named = Object.hasOwn(levers, "reserve_option_id") || Object.hasOwn(levers, "reserve_option_ids");
    return [present(levers.aircraft_option_id) ? words(levers.aircraft_option_id) : "",
      named ? (ids.length ? standbyWords(ids) : "no standby crew") : "",
      levers.rebook === true ? "rebook" : levers.rebook === false ? "no rebooking" : "",
      levers.delay_messages === true ? "delay messages" : levers.delay_messages === false ? "no delay messages" : ""].filter(Boolean).join(", ");
  }

  function actionRow(trail, action, index, finished = false) {
    // Waiting while the execution runs (a storm's held action too); once it has ended, never sent ("–"), never failed.
    const status = action.status === "confirmed" ? "pass" : NEVER_SENT.has(action.status) ? (finished || action.status === "stood_down" ? "unknown" : "occ")
      : action.status === "pending" ? "occ" : "fail";
    const name = Tips.system(action.label);
    const said = Tips.systemWords(action.message || action.summary || action.status || "");
    return `<li><details class="tv-act"${keep(`outcome:action:${action.action_id || index}`)}>
      <summary${Tips.attrs(esc, Tips.explain(name, said), name)}><span class="mark ${status}">${
        { pass: "✓", occ: "…", fail: "!", unknown: "–" }[status]}</span><b>${esc(name)}</b><span>${esc(said)}</span></summary>
      <div class="tv-callbody">
        <h6>Request</h6>${recorded(trail, action.request, "not recorded yet", ACTION_FIELDS)}
        <h6>Receipt</h6>${recorded(trail, action.receipt)}
        ${action.action_id ? `<div class="tv-iface iface"><h6>Interface</h6><p>Reference <code>${esc(action.action_id)}</code></p></div>` : ""}
      </div></details></li>`;
  }

  function detail(trail, selected, context = {}) {
    const id = selected || "orchestrator";
    const message = trail.message || {};
    const orch = trail.orchestrator || {};
    const plans = trail.plans || [];
    if (id === "message") {
      // The report as received: who, when and the text (owner); the Detection agent reads it. ARROW's record of it shows
      // here only when ARROW recorded a typed event on receipt.
      const reported = nodeCalls(trail, "message", context);
      return `${head("Report", "What was reported")}<div class="tv-parts${reported.length ? "" : " one"}">${part("As received", `<p class="bubble">${esc(message.text)}</p>
        <p class="muted small">From ${esc(message.source)}${message.reported_at ? ` at ${esc(String(message.reported_at).slice(11, 16))} Lisbon` : ""}</p>`)}${
        reported.length ? callsPart(trail, reported, recordedIn(reported)) : ""}</div>`;
    }
    if (id === "orchestrator") {
      const index = plans.findIndex(plan => plan.recommended);
      const chosen = plans[index];
      const steps = orch.steps || [];
      // The recommended plan's own levers as ARROW stored them (review): the orchestrator builds one option at a time,
      // so the last plan built is usually another. No recommendation, no levers.
      const levers = isObject(chosen?.levers) ? leverWords(chosen.levers) : "";
      const reason = chosen?.reason || steps.filter(step => step.tool === "submit_plan").at(-1)?.detail;
      const name = catalogue("orchestrator").name;
      // Owner: input and output on the left, its tool calls on the right; the instructions in the header.
      const output = `${big(chosen ? `Recommends plan ${index + 1}: ${planName(chosen, context)}`
        : trail.status === "assessing" ? "Working…" : "No plan recommended")}
        ${levers ? `<dl class="tv-kv"><dt>Levers</dt><dd>${esc(cap(levers))}</dd></dl>` : ""}
        ${reason ? said("Orchestrator", ids(trail, reason)) : ""}
        ${trail.status !== "assessing" && orch.summary ? `<details class="tv-more"${keep("orchestrator:summary")}><summary>Case summary</summary>
          <p class="tv-text">${esc(ids(trail, orch.summary))}</p></details>` : ""}
        ${go("data-goto", "conversation", "Conversation ›")}`;
      return `${head("Orchestrator", name, `${modelOf(orch)}${promptToggle(orch.prompt, id)}${foundryLink(orch, "orchestrator", context)}`)}
        ${promptBlock(orch.prompt, id)}
        <div class="tv-parts"><div class="tv-stack">${part("Input", briefWords(orch.brief, trail))}${part("Output", output)}</div>
          ${callsPart(trail, nodeCalls(trail, id, context), "Tool calls")}</div>`;
    }
    if (id.startsWith("agent:")) {
      const agent = (trail.agents || []).find(entry => `agent:${entry.key}` === id);
      if (!agent) return "";
      const answer = answerOf(agent);
      const concluded = conclusion(agent.key, answer);
      const summary = ids(trail, plainAnswer(agent.answered).summary);
      const line = recommends(agent.key, answer);
      const header = `${head("Domain agent", catalogue(agent.key).name || agent.name,
        `${badge(agent, agent.key)}${modelOf(agent)}${promptToggle(agent.prompt, id)}${foundryLink(agent, agent.key, context)}`)}${promptBlock(agent.prompt, id)}`;
      const calls = callsPart(trail, nodeCalls(trail, id, context), "Tool calls");
      if (agent.status === "not asked") {
        return `${header}<div class="tv-parts">${part("Input", '<p class="muted">Not asked for this case</p>')}${calls}</div>`;
      }
      // Input: the orchestrator's question; output: what it recorded (Detection) or the exposure ARROW computed for it (Passenger),
      // then its own answer, marked once as written by AI (EU AI Act transparency): its conclusion, what it recommends and its
      // summary, a long one on demand; the raw answer on demand.
      const input = present(agent.asked) ? `<p class="tv-input">${esc(ids(trail, humanize(agent.asked)))}</p>` : `<p>${notYet}</p>`;
      const recorded = agent.key === "detection" ? recordedBlock(trail) : "";
      const exposure = agent.key === "passenger" ? exposureLine(agent) : "";
      const verdict = concluded || (!recorded && summary.length <= 140 ? summary : "");
      const prose = summary && summary !== verdict ? summary : "";
      const more = verdict || line;
      const aircraft = agent.key === "aircraft" ? aircraftDetail(answer) : "";
      const answered = verdict || line || prose || aircraft ? `<div class="tv-ai">${verdict ? `<p class="${recorded ? "tv-rec" : "tv-big"}">${aiMark()} ${esc(phrase(verdict))}</p>`
          : aiMark()}${line ? `<p class="tv-rec">${esc(phrase(line))}</p>` : ""}${aircraft}
          ${prose ? (more && prose.length > 160 ? `<details class="tv-more"${keep(`${id}:summary`)}><summary>Summary</summary><p class="tv-why">${esc(prose)}</p></details>`
            : `<p class="tv-why">${esc(prose)}</p>`) : ""}</div>` : "";
      const output = `${recorded || (answered ? "" : big(agent.status === "working" ? "Working…" : "No answer recorded"))}${answered}${exposure}
        ${agent.answered ? `<details class="tv-raw"${keep(`${id}:raw`)}><summary>Raw answer</summary><pre>${esc(fullAnswer(trail, agent.answered))}</pre></details>` : ""}`;
      return `${header}<div class="tv-parts"><div class="tv-stack">${part("Input", input)}${part("Output", output)}</div>${calls}</div>`;
    }
    if (id.startsWith("plan:")) {
      const index = plans.findIndex(entry => `plan:${entry.plan_id}` === id);
      const plan = plans[index];
      if (!plan) return "";
      const passengers = [Number.isFinite(plan.passengers_disrupted) ? `${number.format(plan.passengers_disrupted)} lose flight or connection` : "",
        Number.isFinite(plan.passengers_owed_compensation) ? `${number.format(plan.passengers_owed_compensation)} owed compensation` : "",
        Number.isFinite(plan.rebooked) ? `${number.format(plan.rebooked)} rebooked` : ""].filter(Boolean).join(", ");
      const flights = [Number.isFinite(plan.flights) ? number.format(plan.flights) : "", plan.cancelled ? `${plan.cancelled} cancelled` : ""].filter(Boolean).join(", ");
      // The four cost parts the plan cards show; plans priced by earlier versions have none.
      const PARTS = [["airline", "Airline"], ["compensation", "EU261 compensation"], ["care", "Passenger care"], ["rebooking", "Rebooking"]];
      const parts = isObject(plan.cost_parts) ? PARTS.filter(([key]) => Number.isFinite(plan.cost_parts[key]))
        .map(([key, label]) => `${label} ${euro.format(plan.cost_parts[key])}`).join(", ") : "";
      const asked = isObject(plan.what_if) ? plan.what_if : null;
      const at = asked ? RecoveryView.lisbonTime(asked.asked_at) : "";
      // A plan in labels and numbers: who decides, its cost and its parts, its passengers and flights; a what-if asked in the case
      // chat, who asked, when and what.
      const rows = [["Cost", Number.isFinite(plan.cost_eur) ? `${euro.format(plan.cost_eur)} indicative` : "Unknown"], ["Cost parts", parts],
        ["Passengers", passengers], ["Flights", flights],
        ["Asked", asked ? `In chat by ${asked.asked_by || "an operator"}${at ? ` at ${at}` : ""}: ${asked.question || ""}` : ""]].filter(([, value]) => present(value));
      const decision = part("Decision", `${big(ROUTE[plan.route] || plan.route || "Not routed", own(TONE, plan.route) || "")}
          <dl class="tv-kv">${rows.map(([name, value]) => `<dt>${esc(name)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>${plan.reason ? said("Orchestrator", ids(trail, plan.reason)) : ""}`);
      const checked = part("Rule checks", `${checkList(plan)}${go("data-node", "rules", "ARROW rules ›")}`);
      const tag = asked ? chip("What-if", "whatif") : plan.recommended ? chip("Recommended", "rec") : "";
      if (asked) {
        // Beside it, the reads ARROW made to build it for the chat turn.
        return `${head(`Plan ${index + 1}`, planName(plan, context), tag)}
          <div class="tv-parts"><div class="tv-stack">${decision}${checked}</div>${callsPart(trail, nodeCalls(trail, id, context), "How ARROW built it")}</div>`;
      }
      return `${head(`Plan ${index + 1}`, planName(plan, context), tag)}
        <div class="tv-parts">${decision}${checked}</div>`;
    }
    if (id === "rules") {
      const plan = plans.find(entry => entry.recommended) || plans[0];
      const calls = callsPart(trail, nodeCalls(trail, id, context));
      if (!plan) {
        return `${head("Rules", "How the plan is routed")}<div class="tv-parts">${part("Decision",
          `<p class="muted">${trail.status === "assessing" ? "The rules are checked once a plan is built." : "No plan was built, so no rule was checked."}</p>`)}${calls}</div>`;
      }
      const others = plans.filter(entry => entry !== plan).map(entry =>
        `<li>Plan ${plans.indexOf(entry) + 1}: ${esc(planName(entry, context))} ${routeChip(entry)}</li>`).join("");
      const counted = checkWords(plan.rules);
      return `${head("Rules", plans.length > 1 ? "How each plan was routed" : "How the plan was routed", versionChip(plan.rules_version))}
        <div class="tv-parts">${part("Decision", `${big(ROUTE[plan.route] || plan.route || "Not routed", own(TONE, plan.route) || "")}
          <p class="tv-rec">Plan ${plans.indexOf(plan) + 1}: ${esc(planName(plan, context))}${plan.recommended ? ` ${chip("Recommended", "rec")}` : ""}</p>
          ${counted ? `<p class="tv-rec">${esc(phrase(counted))}</p>` : ""}
          ${checkList(plan)}${others ? `<ul class="tv-others">${others}</ul>` : ""}
          ${go("data-step", "rules", "All rule checks ›")}`)}${calls}</div>`;
    }
    if (id === "outcome") {
      const outcome = trail.outcome || {};
      const actions = outcome.actions || [];
      const done = actions.filter(action => action.status === "confirmed").length;
      const by = outcome.approved_by ? `<span>Authorised by ${esc(outcome.approved_by === "policy" ? "ARROW's rules (within every limit)" : "the OCC Duty Manager")}</span>` : "";
      // An action's calls are listed once, under System calls; an action without calls (older, or not started) is listed here.
      const unsent = actions.map((action, index) => [action, index]).filter(([action]) => !(Array.isArray(action.calls) && action.calls.length));
      // A storm plan's actions by stage, with what its held part waits for.
      const storm = outcome.storm && typeof outcome.storm === "object" ? outcome.storm : null;
      const waits = storm && outcome.state === "held" ? [storm.decide_by ? `Decide by ${storm.decide_by}` : "",
        (storm.confirm_by || []).length ? `Confirms: ${storm.confirm_by.join("; ")}` : "", (storm.stand_down_by || []).length ? `Stands down: ${storm.stand_down_by.join("; ")}` : ""]
        .filter(Boolean) : storm?.returned ? [`Back to OCC: ${storm.returned}`] : [];
      const rows = isStaged(actions)
        ? `${waits.map((line) => `<p class="tv-why">${esc(line)}</p>`).join("")}${STAGES.map(([key, label]) => {
          const list = actions.filter(action => stageOf(action) === key);
          if (!list.length) return "";
          const own = unsent.filter(([action]) => stageOf(action) === key);
          return `<h6 class="tv-stage">${esc(stageLine(list, label, outcome))}</h6>${own.length ? `<ul class="tv-acts">${own.map(([action, index]) => actionRow(trail, action, index, ended(outcome))).join("")}</ul>` : ""}`;
        }).join("")}`
        : unsent.length ? `<ul class="tv-acts">${unsent.map(([action, index]) => actionRow(trail, action, index, ended(outcome))).join("")}</ul>` : "";
      const body = actions.length
        ? `${big(isStaged(actions) && outcome.state !== "executed" ? `${outcomeLabel(outcome)}: ${phrase(outcomeWords(outcome, actions))}` : `${done} of ${actions.length} actions confirmed`,
          done === actions.length ? "ok" : "warn")}
          ${outcome.stage ? `<p class="tv-why">${esc(outcome.stage)}</p>` : ""}
          ${rows}`
        : `${big(outcome.stage || "No action")}`;
      return `${head("Outcome", outcomeLabel(outcome), by)}
        <div class="tv-parts">${part("Actions", body, actions.length || undefined)}${callsPart(trail, nodeCalls(trail, id, context))}</div>`;
    }
    return "";
  }

  function stepList(trail) {
    const names = Object.fromEntries((trail.agents || []).map(agent => [agent.key, agent.name]));
    return (trail.orchestrator?.steps || []).map((step, index) => {
      const text = step.kind === "answer" ? plainAnswer(step.detail).summary || step.detail
        : step.kind === "ask" ? humanize(step.detail) : step.detail;
      const target = step.agent ? `agent:${step.agent}` : step.tool === "record_disruption" ? "message"
        : step.tool === "build_plan" || step.tool === "submit_plan" ? "rules" : "orchestrator";
      return `<li class="${step.ok === false ? "bad" : ""}"><div><button type="button" data-node="${esc(target)}">${esc(step.title)}</button>
        ${step.agent ? `<span class="muted small">${esc(names[step.agent] || step.agent)}</span>` : ""}
        ${text ? `<span class="text">${esc(text)}</span>` : ""}
        ${step.raw && Object.keys(step.raw).length ? `<details class="hd-raw" data-raw="step-${index}"><summary>Raw</summary><pre>${esc(JSON.stringify(step.raw, null, 2))}</pre></details>` : ""}</div></li>`;
    }).join("") || '<li><div class="muted">The orchestrator\'s steps appear here when it finishes. Agents light up above as they read the airline systems.</div></li>';
  }

  // ---------- Conversation (the Assessed step) and rule checks (the Rules step) (control tower and rules spec §5.3) ----------

  const number = new Intl.NumberFormat("en-GB");
  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const LABELS = {
    aircraft_reg: "Aircraft", capacity_fit: "Fits capacity", disrupted_passengers: "Lose flight or connection",
    rebooked_passengers: "Rebooked within policy", unplaced_passengers: "To Passenger Services",
    misconnected_passengers: "Lose flight or connection",
    additional_delay_minutes: "Extra delay (min)", required_additional_delay_minutes: "Extra delay (min)",
    breaches: "Duty breaches", reserve_options: "Standby crew options", reserve_option_id: "Standby crew", reserve_option_ids: "Standby crews", checked: "Also checked",
    compensation_eur: "EU261 compensation", passengers_owed_compensation: "Owed compensation", care_eur: "Passenger care", rebooking_eur: "Rebooking",
    extraordinary_circumstances: "Extraordinary circumstances", flights_affected: "Flights affected", max_minutes_late: "Up to (min late)", knock_on: "Knock-on"
  };
  // Cell words in the Option × Agent table: "107 lose flight or connection · 54 rebooked".
  const SHORT = {
    disrupted_passengers: "lose flight or connection", rebooked_passengers: "rebooked", unplaced_passengers: "to review",
    misconnected_passengers: "lose flight or connection", additional_delay_minutes: "min extra delay",
    required_additional_delay_minutes: "min extra delay", passengers_owed_compensation: "owed compensation", compensation_eur: "EU261 compensation",
    care_eur: "care", rebooking_eur: "rebooking"
  };
  const HIDDEN = new Set(["reason", "trace_id", "case_id", "event_id"]);
  // An amount in euros, by its field's name (compensation_eur).
  const amount = (key, v) => (typeof v === "number" && /_eur$/.test(key) ? euro.format(v) : null);
  const VERDICT_KEYS = new Set(["is_disruption", "constraint_status"]);
  const BADGES = { detection: "DET", aircraft: "AIR", crew: "CRW", passenger: "PAX", slot: "SLT" };
  const PER_OPTION = ["crew", "passenger", "slot"];

  // "affected_flight_ids" → "Affected flights"; a trailing _id or _ids names the thing itself.
  function label(key) {
    const known = own(LABELS, key);
    if (known) return known;
    const text = String(key).replace(/_ids$/, "s").replace(/_id$/, "").replaceAll("_", " ").trim();
    return text ? text[0].toUpperCase() + text.slice(1) : "";
  }
  // Codes stay as written ("CS-TQA", "AR101"); lower-case identifiers become words ("aircraft_defect").
  function words(text) {
    const value = String(text ?? "");
    if (!/^[a-z][a-z0-9_]*$/.test(value)) return value;
    const spaced = value.replaceAll("_", " ");
    return spaced[0].toUpperCase() + spaced.slice(1);
  }
  const present = (v) => v !== null && v !== undefined && v !== "";
  // The standby crews an answer or a plan's levers name: a list since Task 2, one id before.
  const standbyIds = (from) => (Array.isArray(from?.reserve_option_ids) ? from.reserve_option_ids.filter(present).map(String)
    : present(from?.reserve_option_id) ? [String(from.reserve_option_id)] : []);
  const standbyWords = (ids) => `standby crew${ids.length === 1 ? "" : "s"} ${ids.join(", ")}`;
  const cap = (text) => { const value = String(text ?? ""); return value ? value[0].toUpperCase() + value.slice(1) : value; };

  function objectsTable(items, choice) {
    return `<table class="hd-mini"><tbody>${items.map((item) => {
      if (!isObject(item)) return `<tr><td>${esc(words(item))}</td></tr>`;
      // A lever the Aircraft agent checked: "Aircraft swap · not available · why" (newer runs).
      if (typeof item.available === "boolean" && present(item.kind) && !present(item.option_id)) {
        return `<tr><td>${esc(own(LEVER, item.kind) || words(item.kind))} · <span class="${item.available ? "ok" : "bad"}">${item.available ? "available" : "not available"}</span>${
          present(item.why) ? `<span class="muted"> · ${esc(item.why)}</span>` : ""}</td></tr>`;
      }
      const name = item.title || item.option_id || item.kind;
      const chosen = present(choice) && item.option_id === choice ? '<span class="hd-tag">Agent\'s choice</span>' : "";
      return `<tr><td>${name ? esc(words(name)) : factList(Object.entries(item))}${chosen}${present(item.effect) ? `<span class="muted"> · ${esc(item.effect)}</span>` : ""}</td></tr>`;
    }).join("")}</tbody></table>`;
  }

  function factValue(v, choice) {
    if (v === true) return "Yes";
    if (v === false) return "No";
    if (!present(v)) return "None";
    if (typeof v === "number") return esc(number.format(v));
    if (typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v)) return esc(timeOf(v));
    if (Array.isArray(v)) {
      if (!v.length) return "None";
      if (v.every((item) => !item || typeof item !== "object")) {
        return `<span class="hd-chips">${v.map((item) => `<span>${esc(words(item))}</span>`).join("")}</span>`;
      }
      return objectsTable(v, choice);
    }
    if (isObject(v)) return factList(Object.entries(v)) || "None";
    return esc(words(v));
  }

  function factList(entries, choice) {
    const rows = entries.filter(([key, v]) => !HIDDEN.has(key) && present(v))
      .map(([key, v]) => `<dt>${esc(label(key))}</dt><dd>${amount(key, v) ? esc(amount(key, v)) : factValue(v, choice)}</dd>`).join("");
    return rows ? `<dl class="hd-facts">${rows}</dl>` : "";
  }

  // An answer object as facts: its own keys, then the agent's facts, then its recommendation.
  function answerFacts(facts) {
    const rec = isObject(facts.recommendation) ? facts.recommendation : {};
    const inner = isObject(facts.facts) ? facts.facts : {};
    const merged = new Map();
    for (const [key, v] of Object.entries(facts)) if (key !== "recommendation" && key !== "facts") merged.set(key, v);
    for (const [key, v] of Object.entries(inner)) if (!merged.has(key)) merged.set(key, v);
    const choice = rec.option_id;
    const listed = [...merged.values()].some((v) => Array.isArray(v) && v.some((item) => isObject(item) && item.option_id === choice));
    for (const [key, v] of Object.entries(rec)) {
      if (key === "option_id") { if (!listed) merged.set("recommended_option", v); continue; }
      if (!merged.has(key)) merged.set(key, v);
    }
    // One count under two names prints once (review). Who loses their flight or connection: misconnected_passengers,
    // the passenger system's count; the rebooking plan's disrupted_passengers only alone (the final Dev run's agent put its
    // booking count there). An extra delay the Slot agent recommends that repeats the one capacity requires.
    if (typeof merged.get("misconnected_passengers") === "number") merged.delete("disrupted_passengers");
    if (merged.get("additional_delay_minutes") === merged.get("required_additional_delay_minutes")) merged.delete("additional_delay_minutes");
    const reason = [rec.reason, facts.reason, inner.reason].find((r) => typeof r === "string" && r.trim()) || "";
    return { entries: [...merged.entries()], reason, choice };
  }

  function verdict(entries) {
    const found = Object.fromEntries(entries.filter(([key]) => VERDICT_KEYS.has(key)));
    if (found.is_disruption === true) return '<p class="hd-verdict ok">✓ Disruption confirmed</p>';
    if (found.is_disruption === false) return '<p class="hd-verdict muted">✕ No disruption</p>';
    if (found.constraint_status === "pass") return '<p class="hd-verdict ok">✓ Pass</p>';
    if (found.constraint_status === "fail") return '<p class="hd-verdict bad">✕ Fail</p>';
    if (present(found.constraint_status)) return `<p class="hd-verdict occ">? ${esc(words(found.constraint_status))}</p>`;
    return "";
  }

  const parsed = (text) => { try { return JSON.parse(String(text || "").trim()); } catch { return undefined; } };
  function raw(text, key) {
    const value = parsed(text);
    const shown = value === undefined ? String(text || "") : JSON.stringify(value, null, 2);
    return `<details class="hd-raw" data-raw="${esc(key)}"><summary>Raw answer</summary><pre>${esc(shown)}</pre></details>`;
  }

  // The answer in words: a verdict, the facts, the reason last; the original text only on demand.
  // skip: facts already shown above the answer (what the Detection agent recorded), so they are not listed twice.
  function answerBody(answer, key, working, skip = new Set()) {
    if (!answer) return `<p class="hd-why muted">${working ? "Working…" : "No answer recorded"}</p>`;
    if (answer.ok === false) return `<p class="hd-why bad">✕ ${esc(answer.detail || "The agent did not answer")}</p>`;
    if (isObject(answer.facts)) {
      const { entries, reason, choice } = answerFacts(answer.facts);
      return `${verdict(entries)}${factList(entries.filter(([k]) => !VERDICT_KEYS.has(k) && !skip.has(k)), choice)}
        ${reason ? `<p class="hd-why">${esc(reason)}</p>` : ""}${raw(answer.detail || JSON.stringify(answer.facts), key)}`;
    }
    const list = parsed(answer.detail);
    if (Array.isArray(list)) return `<div class="hd-list">${factValue(list)}</div>${raw(answer.detail, key)}`;
    return answer.detail ? `<p class="hd-why">${esc(answer.detail)}</p>` : '<p class="hd-why muted">Empty answer</p>';
  }

  // One line per cell in the Option × Agent table.
  function answerCell(answer, working) {
    if (!answer) return `<span class="muted">${working ? "Working…" : "No answer"}</span>`;
    if (answer.ok === false) return `<span class="bad">✕ ${esc(answer.detail || "No answer")}</span>`;
    if (!isObject(answer.facts)) {
      const list = parsed(answer.detail);
      return Array.isArray(list) ? factValue(list) : esc(answer.detail || "Empty answer");
    }
    const { entries, reason } = answerFacts(answer.facts);
    const mark = verdict(entries).replace(/<\/?p[^>]*>/g, "");
    const cls = /✓/.test(mark) ? "ok" : /✕/.test(mark) ? "bad" : "occ";
    const parts = entries.filter(([key, v]) => !VERDICT_KEYS.has(key) && !HIDDEN.has(key) && present(v) && key !== "recommended_option")
      .map(([key, v]) => {
        if (typeof v === "number") return `${amount(key, v) || number.format(v)} ${own(SHORT, key) || label(key).toLowerCase()}`;
        if (typeof v === "boolean") return `${label(key)}: ${v ? "Yes" : "No"}`;
        if (Array.isArray(v)) {
          if (!v.length) return `No ${label(key).toLowerCase()}`;
          return v.every((item) => !item || typeof item !== "object")
            ? `${label(key)}: ${v.map(words).join(", ")}` : `${v.length} ${label(key).toLowerCase()}`;
        }
        return isObject(v) ? "" : `${label(key)}: ${words(v)}`;
      }).filter(Boolean);
    return `<span class="hd-line"${reason ? ` title="${esc(reason)}"` : ""}>${mark ? `<b class="${cls}">${mark}</b>${parts.length ? " " : ""}` : ""}${
      esc(parts.join(", "))}</span>`;
  }

  function optionOf(question) {
    const option = /\boption_id\b["']?[:= ]*["']?([\w-]+)/i.exec(String(question || ""));
    if (!option) return null;
    const minutes = Number((/\badditional_delay_minutes\b["']?[:= ]*(\d+)/i.exec(question) || [])[1] || 0);
    return { key: `${option[1]}|${minutes}`, label: `${words(option[1])}${minutes ? ` +${minutes} min` : ""}` };
  }

  // full: who the short avatar stands for, on hover. Every turn here is an AI agent's, marked as such.
  function bubble(badge, title, tag, asked, body, orch = false, full = "") {
    const name = orch ? catalogue("orchestrator").name : full;
    return `<div class="hd-step"><span class="hd-who${orch ? " orch" : ""}"${Tips.attrs(esc, name)}>${esc(badge)}</span><div class="hd-bubble">
      <header><b>${esc(title)}</b>${aiMark()}${tag ? `<span class="hd-tag">${esc(tag)}</span>` : ""}${asked ? `<span class="hd-q">${esc(asked)}</span>` : ""}</header>
      ${body}</div></div>`;
  }

  const READING = { "aircraft defect": ["Disruption", "Aircraft"], "flight delay": ["Disruption", "Flight", "Delay", "Cause"] };
  function readBubble(trail, step) {
    const message = trail.message || {};
    const parts = String(message.reading || "").split(" · ").map((part) => part.trim()).filter(Boolean);
    const names = own(READING, (parts[0] || "").toLowerCase());
    const rows = names && names.length === parts.length ? parts.map((part, i) => [names[i], cap(part)])
      : parts.length ? [["Read as", cap(parts.join(" · "))]] : [];
    if (message.source) rows.push(["Reported by", message.source]);
    const note = step?.detail || message.reading_note;
    return bubble("ORC", "Orchestrator", "", "read the message",
      `<dl class="hd-facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      ${note ? `<p class="hd-why">${esc(note)}</p>` : ""}`, true);
  }

  // What the Detection agent recorded, as facts in its turn of the conversation (newer runs); its answer's own copy of them is
  // not repeated below.
  const RECORDED_KEYS = new Set(["readable", "type", "event_id", "aircraft", "aircraft_reg", "unavailable_minutes", "flight_no", "delay_minutes",
    "cause", "knock_on", "flights_affected", "max_minutes_late"]);
  function recordedFacts(trail) {
    const recorded = trail.disruption;
    const rows = [["Recorded", `${recorded.kind}: ${phrase(recorded.title)}`], ...(Array.isArray(recorded.fields) ? recorded.fields : []),
      ...(present(recorded.event_id) ? [["Event", recorded.event_id]] : []), ...(present(recorded.knock_on_words) ? [["Knock-on", recorded.knock_on_words]] : [])];
    return `<dl class="hd-facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  }

  function conversation(trail, context = {}) {
    const steps = trail.orchestrator?.steps || [];
    const agents = Object.fromEntries((trail.agents || []).map((agent) => [agent.key, agent]));
    const working = trail.status === "assessing";
    const items = [];
    const open = [];
    let read = null;
    for (const step of steps) {
      if (step.kind === "ask") {
        const pair = { ask: step, answer: null, agent: step.agent };
        open.push(pair);
        items.push(pair);
      } else if (step.kind === "answer") {
        // The trail names each answer's question by call_id; parallel answers may arrive in any order.
        let at = step.call_id ? open.findIndex((pair) => pair.ask.call_id === step.call_id) : -1;
        if (!step.call_id) {
          at = open.findIndex((pair) => step.agent && pair.agent === step.agent);
          if (at < 0) at = open.findIndex((pair) => !step.agent || !pair.agent);
        }
        if (at >= 0) {
          open[at].answer = step;
          open[at].agent = open[at].agent || step.agent;
          open.splice(at, 1);
        } else {
          items.push({ ask: null, answer: step, agent: step.agent });
        }
      } else if (step.kind === "tool" && step.tool === "record_disruption" && !read) {
        read = step;
      }
    }
    // Crew, passenger and slot answers about each option form one Option × Agent table.
    const grouped = items.filter((pair) => PER_OPTION.includes(pair.agent) && optionOf(pair.ask?.detail));
    const rows = new Map();
    for (const pair of grouped) {
      const option = optionOf(pair.ask.detail);
      if (!rows.has(option.key)) rows.set(option.key, { label: option.label, cells: {} });
      rows.get(option.key).cells[pair.agent] = pair;
    }
    const columns = PER_OPTION.filter((key) => grouped.some((pair) => pair.agent === key));
    const matrix = () => {
      const levels = [...new Set(columns.map((key) => agents[key]?.autonomy?.level).filter(Boolean))].join(", ");
      const names = columns.map((key) => agents[key]?.name || words(key));
      const raws = grouped.filter((pair) => pair.answer?.detail && (isObject(pair.answer.facts) || Array.isArray(parsed(pair.answer.detail))));
      return bubble(`${columns.length}×`, names.join(", "), levels, "asked about each option",
        `<div class="hd-scroll"><table class="hd-matrix"><thead><tr><th>Option</th>${names.map((name) => `<th>${esc(name)}</th>`).join("")}</tr></thead>
        <tbody>${[...rows.values()].map((row) => `<tr><td><b>${esc(row.label)}</b></td>${columns.map((key, i) =>
          `<td data-label="${esc(names[i])}">${row.cells[key] ? answerCell(row.cells[key].answer, working) : '<span class="muted">Not asked</span>'}</td>`).join("")}</tr>`).join("")}
        </tbody></table></div>
        ${raws.length ? `<details class="hd-raw" data-raw="matrix"><summary>Raw answers</summary><pre>${esc(raws.map((pair) =>
          `${agents[pair.agent]?.name || pair.agent} · ${optionOf(pair.ask.detail).label}\n${JSON.stringify(parsed(pair.answer.detail) ?? pair.answer.facts, null, 2)}`).join("\n\n"))}</pre></details>` : ""}`,
        false, columns.map((key) => catalogue(key).name || words(key)).join(", "));
    };

    const out = [];
    // In newer runs the Detection agent reads the report and what it recorded follows its answer; older runs: the orchestrator read it.
    const recorded = Boolean(recordOf(trail));
    if (!recorded && (read || trail.message?.reading)) out.push(readBubble(trail, read));
    items.forEach((pair, index) => {
      if (grouped.includes(pair)) {
        if (pair === grouped[0]) out.push(matrix());
        return;
      }
      const agent = agents[pair.agent];
      const name = agent?.name || (pair.agent ? words(pair.agent) : "An agent");
      const facts = recorded && pair.agent === "detection" ? recordedFacts(trail) : "";
      out.push(bubble(BADGES[pair.agent] || "AGT", name, agent?.autonomy?.level, pair.ask ? `asked: ${humanize(pair.ask.detail)}` : "",
        `${facts}${answerBody(pair.answer, `answer-${index}`, working && !!pair.ask, facts ? RECORDED_KEYS : new Set())}`, false, catalogue(pair.agent).name || name));
    });

    // The orchestrator's plans only: a what-if was asked for in the case chat.
    const plans = assessed(trail);
    const built = steps.filter((step) => step.kind === "tool" && step.tool === "build_plan");
    const refused = built.filter((step) => step.ok === false).length;
    const submitted = steps.filter((step) => step.kind === "tool" && step.tool === "submit_plan").at(-1);
    const chosen = plans.find((plan) => plan.recommended);
    if (plans.length || built.length || submitted) {
      const count = plans.length || built.length - refused;
      const did = `built ${count} plan${count === 1 ? "" : "s"}${refused ? `, ${refused} refused,` : ""}${chosen ? " and recommended one" : ""}`;
      const reason = chosen?.reason || submitted?.detail;
      out.push(bubble("ORC", "Orchestrator", "", did,
        `${chosen ? `<p class="hd-verdict rec">Recommended: ${esc(planName(chosen, context))}</p>` : ""}
        ${reason ? `<p class="hd-why">${esc(reason)}</p>` : ""}`, true));
    }
    if (!out.length) {
      return `<p class="muted">${working ? "The orchestrator's steps appear here as it works." : "No steps recorded for this case."}</p>`;
    }
    // The orchestrator's own step list lives here, on demand, not on the graph (owner).
    return `<div class="hd-convo">${out.join("")}</div>
      <details class="tv-steps" data-raw="steps"><summary>Step by step <span class="tv-count">${esc(steps.length)}</span></summary>
        <ol class="trail-steps">${stepList(trail)}</ol></details>`;
  }

  // policy.RULE_CATALOGUE order; GET /api/rules has it, but this view must not depend on that call.
  const CHECK_ORDER = ["cost", "rebooking", "aircraft", "cancellation", "airport", "declined_rebooking", "weather", "standby", "crew", "eu261",
    "capacity", "answered", "passenger_services"];
  const RULE_NOTE = {
    cost: "Editable, OCC above it", rebooking: "Editable, OCC above it", aircraft: "Always OCC", cancellation: "Always OCC",
    airport: "Always OCC", declined_rebooking: "Always OCC", weather: "Always OCC", standby: "OCC decides", crew: "Legal limit, blocks",
    eu261: "OCC decides", capacity: "Over capacity blocks", answered: "Unknown blocks", passenger_services: "Passenger Services decides"
  };
  const RESULT = { pass: ["✓ Pass", "ok"], occ: ["→ OCC", "occ"], expert: ["→ Expert", "exp"], blocked: ["✕ Blocked", "bad"] };
  const ROUTE_CLASS = { auto: "ok", occ: "occ", blocked: "bad" };

  function result(value) {
    const [text, cls] = own(RESULT, value) || [`? ${words(value || "unknown")}`, "muted"];
    return `<span class="hd-result ${cls}">${esc(text)}</span>`;
  }

  function bar(measure, outcome) {
    const value = Number(measure?.value), limit = Number(measure?.limit);
    if (!Number.isFinite(value) || !Number.isFinite(limit)) return "";
    const width = limit > 0 ? Math.max(0, Math.min(100, (value / limit) * 100)) : value > 0 ? 100 : 0;
    return `<span class="hd-bar ${esc(own(RESULT, outcome)?.[1] || "muted")}" aria-hidden="true"><i style="width:${width.toFixed(1)}%"></i></span>`;
  }

  function checkCell(plan, key) {
    if (plan.checks) {
      const found = plan.checks.find((entry) => entry.key === key);
      if (!found) return '<span class="muted">—</span>';
      const limit = cap(found.measure ? `≤ ${found.limit}` : found.limit);
      // "Always OCC" and the like already read in the rule's own column.
      const shown = present(found.limit) && limit !== RULE_NOTE[key];
      return `<div class="hd-cell"><div class="hd-io"><strong>${esc(cap(phrase(found.input)))}</strong>
        ${shown ? `<span>${esc(phrase(limit))}</span>` : ""}${found.measure ? bar(found.measure, found.result) : ""}</div>${result(found.result)}</div>`;
    }
    const rule = (plan.rules || []).find((entry) => entry.key === key);
    if (!rule) return '<span class="muted">—</span>';
    return `<div class="hd-cell"><div class="hd-io"><strong>${esc(rule.detail)}</strong><span>inputs not recorded</span></div>${result(rule.result)}</div>`;
  }

  function checks(trail, context = {}) {
    const plans = [...(trail.plans || [])].sort((a, b) => Number(!!b.recommended) - Number(!!a.recommended));
    if (!plans.length) return `<p class="muted">${trail.status === "assessing" ? "Rule checks appear once a plan is built." : "No plan was built, so no rule was checked."}</p>`;
    const keys = [...CHECK_ORDER];
    const names = {};
    for (const plan of plans) {
      for (const entry of plan.checks || plan.rules || []) {
        if (!keys.includes(entry.key)) keys.push(entry.key);
        names[entry.key] = names[entry.key] || entry.rule;
      }
    }
    const used = keys.filter((key) => names[key]);
    const tag = (plan) => (plan.recommended ? "recommended" : isObject(plan.what_if) ? "what-if" : "");
    const head = (plan) => {
      const cost = Number.isFinite(plan.cost_eur) ? euro.format(plan.cost_eur) : "cost unknown";
      const people = Number.isFinite(plan.passengers_disrupted) ? `${number.format(plan.passengers_disrupted)} lose flight or connection` : "passengers unknown";
      return `<th><div class="hd-plan"><span class="${plan.recommended ? "rec" : ""}">${esc(planName(plan, context))}${tag(plan) ? ` ${chip(cap(tag(plan)), plan.recommended ? "rec" : "whatif")}` : ""}</span>
        <small>${esc(cost)}</small><small>${esc(people)}</small></div></th>`;
    };
    const versions = [...new Set(plans.map((plan) => plan.rules_version).filter((v) => v !== null && v !== undefined))]
      .sort((a, b) => a - b);
    // On a phone each cell is labelled with its plan.
    const cell = (plan) => `data-plan="${esc(planName(plan, context))}${tag(plan) ? ` (${tag(plan)})` : ""}"${plan.recommended ? ' class="rec"' : ""}`;
    return `<div class="hd-checks-wrap"><table class="hd-checks"><thead><tr><th>Rule</th>${plans.map(head).join("")}</tr></thead><tbody>
      ${used.map((key) => `<tr data-check="${esc(key)}"><td class="hd-rule"><b>${esc(names[key])}</b>${RULE_NOTE[key] ? `<small>${esc(RULE_NOTE[key])}</small>` : ""}</td>
        ${plans.map((plan) => `<td ${cell(plan)}>${checkCell(plan, key)}</td>`).join("")}</tr>`).join("")}
      <tr class="hd-route"><td class="hd-rule"><b>Route</b></td>${plans.map((plan) =>
        `<td ${cell(plan)}><span class="hd-result ${ROUTE_CLASS[plan.route] || "muted"}">${esc(ROUTE[plan.route] || plan.route || "Not routed")}</span></td>`).join("")}</tr>
    </tbody></table></div>
    ${versions.length ? `<p class="hd-foot">${versions.map((v) => versionChip(v)).join(" ")}</p>` : ""}`;
  }

  // How a call went, in the trail's words, for the case chat's calls too: the tool call (MCP), the question to an agent
  // (A2A), or ARROW's own route with the case's values.
  const iface = (call, byAgent) => ifaceLines(call.ask ? call : callOf(call, "", byAgent));
  return Object.freeze({ graph, drawWires, detail, summary, steps: stepList, plainAnswer, humanize, conversation, checks, foundryUrl, iface, aiMark });
})();
