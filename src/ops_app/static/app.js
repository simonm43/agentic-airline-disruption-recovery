"use strict";
// ARROW operator app (business UI and free-text spec §5, control tower and rules spec §5.1): the control tower, the Today list,
// the case decision, how ARROW decided and the rules.

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const lisbonClock = (value) => {
  const time = Date.parse(value || "");
  return Number.isFinite(time) ? new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit",
    timeZone: "Europe/Lisbon" }).format(new Date(time)) : "";
};
const activeStatuses = new Set(["assessing", "approved", "executing"]);
const state = {
  session: null, cases: [], casesLoaded: false, rules: null, rulesRead: false, towerKey: null, current: null, selected: null,
  timer: null, listTimer: null, generation: 0, request: 0, caseRead: "empty", decision: null, step: null, othersOpen: false,
  trail: null, node: null, reviewConfirm: null, listNotice: false, trailLoading: false,
  trailAgain: false, pollAfter: null, chatOpen: false, chatSending: false, chatKey: null, chatAsks: null, chatTrace: null,
  chatScroll: false, flashTimer: null, chatChecks: { trace: null, calls: [] }, chatChecksFor: null, chatChecksLoading: false,
  // What-ifs asked from this screen (the chat entries posted, by case) and those already shown landing; the seconds ticker.
  asked: new Map(), landed: new Set(), landing: null, ticker: null
};
// The case as a journey (owner-approved case screen prototype, journey.html, laid out as chosen): six
// steps under the summary act as tabs, and the chosen step's content follows them full width in the page.
const STEPS = [["reported", "Reported"], ["assessed", "Assessed"], ["plans", "Plans"], ["rules", "Rules"], ["decision", "Decision"],
  ["carried", "Carried out"]];
const TRAIL_STEPS = new Set(["assessed", "rules"]);
// With no step chosen, a case shows Decision while it waits for OCC, else Carried out.
function stepShown(item = state.current) {
  if (state.step || !item) return state.step;
  return stepsOf(item).find(entry => entry.id === "decision")?.state === "current" ? "decision" : "carried";
}
// Passengers waiting for Passenger Services are their expert's work, not an OCC decision (a Dev run): Action needed (owner).
const GROUPS = [
  ["needs", "Needs you"], ["expert", "Action needed"], ["progress", "In progress"], ["handled", "Handled"], ["stopped", "Stopped"]
];
const groupOf = (entry) => (entry.label === "Passenger Services review" ? "expert" : entry.state || "progress");

function showNotice(message, signIn = false, notice = $("#notice")) {
  if (notice.id === "notice") state.listNotice = false;
  notice.hidden = false;
  notice.textContent = message;
  if (signIn) {
    notice.append(" ");
    const link = document.createElement("a");
    link.href = `/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(location.pathname + location.search)}`;
    link.textContent = "Sign in";
    notice.append(link);
  }
}
function clearNotice(notice = $("#notice")) {
  notice.hidden = true;
  if (notice.id === "notice") state.listNotice = false;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-ARROW-Request"] = "operator";
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials: "same-origin",
      redirect: "error", signal: AbortSignal.timeout(15000) });
  } catch (error) {
    if (error.name === "TimeoutError") throw new Error("The request timed out. Its outcome is unconfirmed; refresh before retrying.");
    throw error;
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    let detail = null;
    try { const parsed = JSON.parse(await response.text()); if (typeof parsed.detail === "string") detail = parsed.detail; } catch { /* keep */ }
    if (response.status === 401) message = "Your session has expired or sign-in is required.";
    else if (response.status === 403) message = "Access denied. Your account is not permitted to perform this action.";
    else if (response.status === 503) message = "The service is waking up or temporarily unavailable. Please refresh and try again.";
    else if (detail) message = detail;
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return response.json();
}
function reportError(error) {
  showNotice(error.message || "The service could not be reached. Please try again.", error.status === 401);
}

// ---------- Today list ----------
function caseStateOf(item) {
  const listed = state.cases.find(entry => entry.trace_id === item?.trace_id);
  return listed?.state || "progress";
}
// Today's rows read as the approved prototype writes them (rev 2): "AR689 · +60 min", "CS-TQA · out 4 h"; the full headline is in
// the row's hover. A storm keeps its window on one line (TowerView.headline).
function railHeadline(entry) {
  if (entry.disruption_type === "weather") return TowerView.headline(entry);
  return escapeHtml(String(entry.headline || entry.display_id || "").replace(/ · (\d+) min late$/, " · +$1 min").replace(/ · out of service (.+)$/, " · out $1"));
}
function renderCases() {
  const container = $("#cases");
  if (!state.cases.length) {
    container.innerHTML = '<p class="rail-empty">No disruptions today. Report one, or send an event from the Test Bench.</p>';
    return;
  }
  container.innerHTML = GROUPS.map(([key, title]) => {
    const rows = state.cases.filter(entry => groupOf(entry) === key);
    if (!rows.length) return "";
    // One line per case (prototype rev 2): its headline and, where OCC decides, the first departure it affects; passengers
    // still waiting for Passenger Services in amber below. Its state, source and report time on hover.
    return `<section class="case-group ${key}"><h2>${escapeHtml(title)}</h2>
      ${rows.map(entry => {
        const review = entry.label === "Passenger Services review" ? Number(entry.impact?.review_passengers) || 0 : 0;
        const first = key === "needs" ? RecoveryView.hhmm(entry.first_departure?.std_local) : null;
        // A storm held until confirmed shows its decide-by time (case summary and held storm spec §2).
        const held = entry.storm?.state === "awaiting" && /^Held\b/.test(String(entry.label || "")) && entry.storm.decide_by ? String(entry.storm.decide_by) : null;
        const about = [entry.headline, entry.label || entry.status, entry.source || "OCC", lisbonClock(entry.created_at) ? `reported ${lisbonClock(entry.created_at)}` : "",
          first ? `first departure ${entry.first_departure?.flight_no || ""} ${first}`.replace("  ", " ") : ""].filter(Boolean).join(" · ");
        // Its details on hover and focus as every other hover in ARROW (review: not only in a native title).
        return `<button type="button" class="case-row ${key}" data-case="${escapeHtml(entry.trace_id)}"${Tips.attrs(escapeHtml, about)}
        aria-current="${entry.trace_id === state.selected}">
        <strong${entry.disruption_type === "weather" ? ' class="wrap"' : ""}>${railHeadline(entry)}</strong>${first ? `<time class="tag">${escapeHtml(first)}</time>` : ""}${
          held ? `<time class="tag held">${escapeHtml(`Held · ${held}`)}</time>` : ""}
        ${review ? `<span class="state">${actionNeeded(review)}</span>` : ""}
      </button>`;
      }).join("")}</section>`;
  }).join("");
}
async function loadCases() {
  const result = await api("/api/cases");
  state.cases = result.cases || [];
  state.casesLoaded = true;
  renderCases();
  renderTower();
  renderRules();
  mapView.setCases(state.cases, state.session);
}
async function loadRules() {
  try { state.rules = await api("/api/rules"); } catch { state.rules = null; }
  state.rulesRead = true;
  $("#rules-count").textContent = state.rules?.pending ? "1" : "";
  renderTower();
  renderRules();
}
// The list keeps itself current: a failed read says why (a 401 offers sign-in), and the next good
// read clears that notice, never one shown for something else (review).
async function refreshList() {
  try {
    await loadCases();
    if (state.listNotice) clearNotice();
  } catch (error) {
    reportError(error);
    state.listNotice = true;
  }
}
function scheduleList() {
  clearTimeout(state.listTimer);
  // System events open cases by themselves, so the list keeps itself current.
  state.listTimer = setTimeout(async () => {
    await Promise.all([refreshList(), loadRules()]);
    scheduleList();
  }, 10000);
}

// ---------- Control tower and views ----------
function renderTower() {
  if (!state.casesLoaded) return;
  // Unchanged data keeps the page as it is (focus, scroll, sort).
  const key = JSON.stringify([state.cases, state.rulesRead, state.rules, state.session?.day, state.session?.demo_time]);
  if (key === state.towerKey) return;
  state.towerKey = key;
  // Rules not read yet are passed as undefined (still loading), a failed read as null (unavailable).
  TowerView.render($("#tower"), state.cases, state.session, state.rulesRead ? state.rules : undefined);
}
function renderRules() {
  // Rendered once the rules have been read (or failed to be read) for a signed-in user.
  if (state.session && state.rulesRead) rulesView.render($("#rules"), state.rules, state.session, state.cases);
}
function setCaseLink(traceId) {
  const params = new URLSearchParams(location.search);
  if (traceId) params.set("case", traceId);
  else params.delete("case");
  // The open step is part of the link (?case=…&step=assessed), so every view of a case can be shared.
  if (traceId && state.step) params.set("step", state.step);
  else params.delete("step");
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}
function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    document.getElementById(button.dataset.tab).hidden = !selected;
  });
  if (!screensLoaded) return;
  if (name === "tower") {
    setCaseLink(null);
    TowerView.layout($("#tower"));
  }
  if (name === "rules") {
    setCaseLink(null);
    rulesView.clearError();
    // Opening Rules reads them again, so a decision is never taken on an old page.
    if (state.session) loadRules();
  }
  if (name === "workspace" && state.selected) setCaseLink(state.selected);
  if (name === "map" && state.session) mapView.open();
}

// ---------- Case header and status ----------
// The case's title in a few words, as the approved prototype writes it (rev 2): "CS-TQA out of service · 4 h".
function shortSpan(minutes) {
  if (!Number.isFinite(minutes)) return "unknown time";
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return hours ? `${hours} h${rest ? ` ${rest} min` : ""}` : `${rest} min`;
}
function titleOf(item) {
  const d = item.disruption || {};
  if (d.type === "flight_delay") return `${d.flight_no} leaves ${d.delay_minutes} min late`;
  if (d.type === "weather") return RecoveryView.storm(d).title;
  if (d.type === "aircraft_defect") {
    return `${d.scope?.aircraft_reg || item.defect?.aircraft_reg} out of service · ${shortSpan(item.defect?.unavailable_minutes)}`;
  }
  return "New disruption report";
}
// The report as received carries no reading of ARROW's: the Detection agent reads it (owner). A weather forecast stays as
// written, in monospace, with what a TAF (or METAR) is on hover or focus.
const FORECASTS = {
  TAF: ["TAF (Terminal Aerodrome Forecast) as received", "A TAF is the airport's official weather forecast for the next 24–30 hours."],
  METAR: ["METAR (Meteorological Aerodrome Report) as received", "A METAR is the airport's routine weather observation, issued every half hour."]
};
function sourceOf(item) {
  const source = item.source || {};
  return source.system || (["event", "fabric"].includes(source.kind) ? "a system event" : "OCC");
}
function plansOf(item) { return RecoveryView.visible(item.options || []); }
// Passengers still waiting for Passenger Services, in amber on the status line, Today and the control tower (owner).
function actionNeeded(passengers, attrs = "", tag = "span") {
  return `<${tag} class="act-needed"${attrs ? ` ${attrs}` : ""}>${escapeHtml(RecoveryView.actionNeeded(passengers))}</${tag}>`;
}
function focusPlan(item) {
  const plans = item.options || [];
  return plans.find(p => p.option_id === item.execution?.option_id) || plans.find(p => p.recommended) || plans[0] || null;
}
function banner(item) {
  const execution = item.execution;
  const plan = focusPlan(item);
  const listed = state.cases.find(entry => entry.trace_id === item.trace_id);
  if (item.status === "assessing") {
    const seconds = Math.max(0, Math.round((Date.now() - Date.parse(item.reassessed_at || item.created_at)) / 1000));
    return { tone: "progress", icon: "…", title: `ARROW is assessing with the agents · ${seconds} s`,
      sub: item.stage || "", action: stepShown() === "assessed" ? "" : '<button class="secondary sm" type="button" data-step="assessed">Watch it live</button>' };
  }
  if (item.manual_intervention) {
    return { tone: "stopped", icon: "!", title: "Needs manual action", sub: item.manual_intervention.message || item.stage };
  }
  if (execution) {
    const actions = execution.actions || [];
    const confirmed = actions.filter(a => a.status === "confirmed").length;
    const review = RecoveryView.awaitingReview(execution);
    const handled = RecoveryView.handledReview(execution);
    const handledText = !review && handled ? ` · ${handled.passengers} passengers handled by Passenger Services` : "";
    if (execution.status === "completed") {
      const auto = execution.route === "auto" && execution.approved_by === "policy";
      return { tone: review ? "needs" : "handled", icon: "✓",
        title: auto ? "Carried out automatically" : `OCC approved "${planName(plan)}"`,
        sub: `${confirmed} of ${actions.length} actions confirmed${handledText}${auto ? " · OCC informed" : ""}`,
        action: review ? actionNeeded(review.passengers, 'type="button" data-step="carried"', "button") : "" };
    }
    const heading = RecoveryView.executionHeading(execution);
    return { tone: heading.tone === "attention" ? "stopped" : "progress", icon: heading.tone === "attention" ? "!" : "…",
      title: heading.heading, sub: heading.message };
  }
  if (item.status === "assessment_ready") {
    const recommended = (item.options || []).find(p => p.recommended);
    const open = plansOf(item).some(option => option.policy?.route !== "blocked");
    const waiting = listed?.label === "Needs your decision" ? "Nothing changes until you approve" : "";
    const sub = [plan && !recommended ? "No plan recommended" : "", waiting].filter(Boolean).join(" · ");
    return { tone: "needs", icon: "?", title: !open ? "No plan can run" : state.session?.capabilities?.approve === true ? "Waiting for your decision" : "Waiting for OCC",
      sub: sub || (plan ? "" : item.stage) };
  }
  if (item.status === "no_disruption") return { tone: "handled", icon: "✓", title: "No disruption", sub: item.stage };
  if (item.status === "failed") {
    const reason = String(item.error || item.stage || "No reason recorded").trim();
    return { tone: "stopped", icon: "✕", title: "ARROW could not finish",
      sub: /nothing was (changed|executed)/i.test(reason) ? reason : `${reason} Nothing was changed.` };
  }
  if (item.status === "closed") return { tone: "stopped", icon: "–", title: "Closed", sub: item.stage };
  return { tone: "progress", icon: "…", title: item.status, sub: item.stage };
}
function renderHead() {
  const item = state.current;
  $("#case-empty").hidden = Boolean(item || state.selected);
  $("#case-view").hidden = !(item || state.selected);
  if (!item) {
    $("#case-title").textContent = state.selected ? "Loading the case…" : "";
    $("#case-meta").textContent = "";
    $("#status-banner").innerHTML = "";
    return;
  }
  const d = item.disruption || {};
  // The case's end-to-end transaction in Application Insights, offered while tracing is on (a template in the session);
  // ARROW redirects to it. It stays in the header beside Open in Foundry, whichever step is open.
  const template = state.session?.trace_url_template;
  const trace = $("#case-trace");
  trace.hidden = !(typeof template === "string" && /^https:\/\//i.test(template) && /^[0-9a-f]{32}$/.test(item.trace_id || ""));
  if (!trace.hidden) trace.href = `/api/cases/${item.trace_id}/trace`;
  $("#case-meta").textContent = [item.display_id, sourceOf(item), lisbonClock(item.created_at)].filter(Boolean).join(" · ");
  // A storm's window stays on one line on a phone ("Thunderstorms at Lisbon · 16:00–18:00").
  const storm = RecoveryView.storm(d);
  if (storm?.window) $("#case-title").innerHTML = `${escapeHtml(`${storm.hazard} at ${storm.place}`)} · <span class="keep">${escapeHtml(storm.window)}</span>`;
  else $("#case-title").textContent = titleOf(item);
  const b = banner(item);
  $("#status-banner").className = `banner ${b.tone}`;
  $("#status-banner").innerHTML = `<span class="icon" aria-hidden="true"></span><span class="words"><strong>${escapeHtml(b.title)}</strong>${
    b.sub ? `<span class="sub">${escapeHtml(b.sub)}</span>` : ""}</span>${b.action || ""}`;
  renderFoundryLink(item);
}
// Open in Foundry opens the case's orchestrator agent at the version the case shows (owner: "open in foundry link does not
// work"): the session's Foundry project link with the agent's name and version from the trail; no link without one.
function renderFoundryLink(item) {
  const link = $("#case-foundry");
  const trail = state.trail?.trace_id === item?.trace_id ? state.trail : null;
  const orchestrator = trail?.orchestrator || {};
  const url = screensLoaded && item ? TrailView.foundryUrl(state.session?.foundry_portal_url,
    orchestrator.foundry?.name || "recovery-orchestrator-agent", orchestrator.foundry?.version) : "";
  link.hidden = !url;
  if (url) link.href = url;
  else link.removeAttribute("href");
}
// Reported: the report as received (owner: the Detection agent, not ARROW, reads it): its text as written, later reports of
// the same disruption, and who reported it, when.
function renderReported() {
  const item = state.current;
  if (!item) return;
  const d = item.disruption || {};
  const message = $("#case-message");
  const text = String(d.message || d.text || d.description || "");
  const forecast = FORECASTS[(/^(TAF|METAR)\b/.exec(text.trim()) || [])[1]] || null;
  message.textContent = text;
  message.classList.toggle("forecast", Boolean(forecast));
  for (const key of ["tip", "tipTitle"]) delete message.dataset[key];
  message.removeAttribute("tabindex");
  if (forecast) {
    message.dataset.tipTitle = forecast[0];
    message.dataset.tip = forecast[1];
    message.tabIndex = 0;
  }
  // Later reports of the same disruption, added here instead of opening a second case. Notes written by earlier versions read
  // in today's wording.
  const repeats = (item.events || []).filter(e => e.event === "repeat_report" || e.event === "conflicting_report");
  const repeatWords = (text) => String(text || "")
    .replace(/^Reported again by (.+?) \(([^)]+)\): the same disruption; nothing was recorded twice\.$/, "Also reported by $1 ($2) · same disruption")
    .replace(/^Reported again by (.+?) \(([^)]+)\) with (.+?): stopped for OCC; nothing was recorded\.$/, "Also reported by $1 ($2) with $3 · stopped for OCC");
  $("#case-repeats").hidden = !repeats.length;
  $("#case-repeats").innerHTML = repeats.map(e => `<li class="${e.event === "conflicting_report" ? "warn" : ""}">${
    escapeHtml(lisbonClock(e.at))} · ${escapeHtml(repeatWords(e.message))}</li>`).join("");
  const facts = [["From", sourceOf(item)], ["At", lisbonClock(item.created_at)], ["Reference", item.display_id]].filter(([, value]) => value);
  $("#case-facts").innerHTML = facts.map(([key, value]) => `<dt>${escapeHtml(key)}</dt> <dd>${escapeHtml(value)}</dd>`).join(" ");
}

// ---------- The plans and the decision: PlanView (plan_view.js) renders them; this keeps what the operator opened ----------
// The case's flights as scheduled. First the case's own original times (GET /api/cases/{trace} originals, the times ARROW read for
// it and its plans are priced against; review), so a later case never shows an earlier case's executed delay as its own.
// Older reads without them: the case's rotation and affected flights. Then the map's day when it is the case's: once a plan ran,
// its published timetable (its current times may already carry the change, owner), else its current times.
function knownOriginals(item) {
  const originals = new Map();
  const own = item?.originals && typeof item.originals === "object" ? Object.values(item.originals) : [];
  for (const flight of own) if (flight?.flight_no) originals.set(flight.flight_no, flight);
  if (!originals.size) {
    for (const flight of item?.rotation?.flights || []) if (flight?.flight_no) originals.set(flight.flight_no, flight);
    for (const flight of item?.affected_flights || []) if (flight?.flight_no) originals.set(flight.flight_no, flight);
  }
  const day = mapView?.data?.operating_date === item?.operating_date ? (mapView.data.flights || []).filter(flight => flight?.flight_no) : [];
  for (const flight of day) {
    if (originals.has(flight.flight_no)) continue;
    originals.set(flight.flight_no, item?.execution && flight.published_std_local
      ? { ...flight, std_local: flight.published_std_local, sta_local: flight.published_sta_local || flight.sta_local } : flight);
  }
  return originals;
}
// What PlanView needs from the operator's session and screen.
function planContext(item) {
  const trail = state.trail?.trace_id === item?.trace_id ? state.trail : null;
  return { originals: knownOriginals(item), approve: state.session?.capabilities?.approve === true, review: Boolean(state.session?.capabilities?.review),
    ready: state.caseRead === "fresh" && !state.decision, othersOpen: state.othersOpen, reviewConfirm: Boolean(item && state.reviewConfirm === item.trace_id),
    // The summary's "N agents · graph" and an automatic plan's limits (the rules in force); Carried out folds the executor's
    // re-checks before it wrote into one line (the trail's rechecks).
    agents: trail?.stats?.agents_asked ?? null, limits: state.rules?.in_force || null, rechecks: trail?.rechecks || null };
}
// One name per plan on every screen: the plan's own, as RecoveryView names it.
function planName(option) { return RecoveryView.planName(option); }
// Polling redraws a section; a list the operator opened stays open.
function keepOpen(container, draw) {
  const open = new Set([...container.querySelectorAll("details[open][data-keep]")].map(d => d.dataset.keep));
  draw();
  container.querySelectorAll("details[data-keep]").forEach(d => { if (open.has(d.dataset.keep)) d.open = true; });
}
function renderPlans(item, ctx) {
  const box = $("#plans");
  box.style.removeProperty("--cols");
  const view = PlanView.cards(item, ctx);
  if (!view.html) {
    box.className = view.className;
    box.textContent = view.text;
    return;
  }
  keepOpen(box, () => {
    box.className = view.className;
    box.innerHTML = view.html;
  });
  renderWip(item);
  landWhatIf(item);
}
// A what-if being built for a question in the case chat: a placeholder after the plans, with its progress, until its plan lands
// or the answer comes without one (owner: "I dont see any progress bar or anything"). Only it changes while the chat
// alone changes, so the plan cards keep what the operator opened.
function renderWip(item) {
  const box = $("#plans");
  const entry = item && !box.classList.contains("empty") ? ChatView.whatIfPending(item) : null;
  const old = box.querySelector(".plan.wip");
  if (old && entry && old.dataset.wip === String(entry.id)) return;
  old?.remove();
  if (!entry) return;
  box.insertAdjacentHTML("beforeend", PlanView.wipCard(entry, RecoveryView.visible(item.options).length + 1));
  tick();
  // Asked here, beside the Decision step: the placeholder comes into view once, so the operator sees where the plan will land.
  const id = String(entry.id);
  if (state.asked.get(item.trace_id)?.has(id) && !state.landed.has(`wip:${id}`)) {
    state.landed.add(`wip:${id}`);
    if (stepShown() === "decision" && !matchMedia("(max-width: 860px)").matches) {
      box.querySelector(".plan.wip")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}
// The seconds an answer or a what-if has taken so far ([data-since]), counted on while one shows.
function tick() {
  const shown = document.querySelectorAll("[data-since]");
  shown.forEach(node => { const text = ChatView.since(node.dataset.since); if (node.textContent !== text) node.textContent = text; });
  if (shown.length && !state.ticker) state.ticker = setInterval(tick, 1000);
  if (!shown.length && state.ticker) { clearInterval(state.ticker); state.ticker = null; }
}
// A what-if asked from this screen lands: its card comes into view with a brief glow, once. On a phone the chat covers the
// case, so it waits until the chat closes; the answer's own card links to it meanwhile.
function landWhatIf(item) {
  const asked = state.asked.get(item.trace_id);
  if (!asked?.size) return;
  const shown = RecoveryView.visible(item.options);
  const plan = shown.find(option => asked.has(option?.what_if?.entry_id) && !state.landed.has(option.option_id));
  if (!plan) return;
  state.landed.add(plan.option_id);
  state.landing = { trace: item.trace_id, number: shown.indexOf(plan) + 1 };
  if (!(state.chatOpen && matchMedia("(max-width: 860px)").matches)) showLanded();
}
function showLanded() {
  const landing = state.landing;
  state.landing = null;
  if (!landing || landing.trace !== state.selected) return;
  if (stepShown() !== "decision") openStep("decision");
  const card = document.querySelector(`#plans .plan[data-plan-no="${CSS.escape(String(landing.number))}"]`);
  if (!card) return;
  card.classList.remove("landed");
  void card.offsetWidth;
  card.classList.add("landed");
  card.scrollIntoView({ behavior: "smooth", block: card.offsetHeight > innerHeight * 0.8 ? "start" : "center" });
  setTimeout(() => card.classList.remove("landed"), 2600);
}
// The case summary under the header (owner-approved prototype rev 2): it takes the status line's place while it shows. Its
// Your action is announced from a node that persists outside the band (review), only when its words change.
let summaryHtml = "";
function renderSummary(item, ctx) {
  const band = $("#summary");
  const html = item ? PlanView.summary(item, ctx) : "";
  band.hidden = !html;
  $("#status-banner").hidden = Boolean(html);
  // Unchanged markup keeps the page as it is (focus, an open card).
  if (summaryHtml !== html) { band.innerHTML = html; summaryHtml = html; }
  const todo = html ? band.querySelector(".t-todo")?.textContent.replace(/\s+/g, " ").trim() || "" : "";
  const live = $("#summary-live");
  if (live.textContent !== todo) live.textContent = todo;
}
function renderRecommendation(item, ctx) {
  const box = $("#recommendation");
  const html = item ? PlanView.recommendation(item, ctx) : "";
  box.hidden = !html;
  box.innerHTML = html;
}
function renderDecision() {
  const item = state.current;
  if (!item) {
    $("#plans").innerHTML = "";
    $("#rec-note").hidden = true;
    $("#step-plans").innerHTML = $("#decision-more").innerHTML = "";
    $("#done").hidden = true;
    renderSummary(null);
    renderRecommendation(null);
    return;
  }
  const ctx = planContext(item);
  renderSummary(item, ctx);
  // Why other plans: a delay's crew over its duty limit, in one line above the plans (owner).
  const note = PlanView.whyOthers(item, ctx);
  $("#rec-note").hidden = !note;
  $("#rec-note").textContent = note;
  renderPlans(item, ctx);
  renderRecommendation(item, ctx);
  const more = $("#decision-more");
  keepOpen(more, () => { more.innerHTML = PlanView.decisionStep(item, ctx); });
  const plansStep = $("#step-plans");
  keepOpen(plansStep, () => { plansStep.innerHTML = PlanView.plansStep(item, ctx); });
  renderDone(item);
}
// What ARROW did: polling redraws the list; a row the operator opened stays open.
function renderDone(item) {
  const done = $("#done");
  const html = PlanView.done(item, planContext(item));
  if (!html) { done.hidden = true; return; }
  const open = new Set([...done.querySelectorAll("details[open][data-group]")].map(d => d.dataset.group));
  const openCalls = new Set([...done.querySelectorAll("details[open][data-call]")].map(d => d.dataset.call));
  done.hidden = false;
  done.innerHTML = html;
  done.querySelectorAll("details[data-group]").forEach(d => { if (open.has(d.dataset.group)) d.open = true; });
  done.querySelectorAll("details[data-call]").forEach(d => { if (openCalls.has(d.dataset.call)) d.open = true; });
}

// ---------- The journey: six steps under the summary, the chosen one's content below them ----------
function stepsOf(item) {
  const shown = plansOf(item);
  const own = shown.filter(option => !option.what_if);
  const whatIfs = shown.length - own.length;
  const execution = item.execution;
  const assessing = item.status === "assessing";
  const trail = state.trail?.trace_id === item.trace_id ? state.trail : null;
  const agents = trail?.stats?.agents_asked;
  const actions = execution?.actions || [];
  const confirmed = actions.filter(action => action.status === "confirmed").length;
  const review = execution ? RecoveryView.awaitingReview(execution) : null;
  const auto = execution?.route === "auto" && execution?.approved_by === "policy";
  const blocked = shown.filter(option => option.policy?.route === "blocked").length;
  const occ = shown.some(option => option.policy?.route === "occ" || (option.policy?.route === "auto" && option.requires_duty_manager === true));
  const repeats = (item.events || []).filter(e => e.event === "repeat_report" || e.event === "conflicting_report");
  const step = (id, sub, done, tone = "") => ({ id, label: STEPS.find(([key]) => key === id)[1], sub, done, tone });
  const finished = ["no_disruption", "closed"].includes(item.status);
  const decided = (() => {
    if (assessing) return step("decision", "Not yet", false);
    if (item.manual_intervention) return step("decision", "Needs manual action", false, "bad");
    if (item.status === "failed") return step("decision", "Stopped", false, "bad");
    if (item.status === "no_disruption") return step("decision", "Not needed", true);
    if (item.status === "closed") return step("decision", "Closed", true);
    if (execution) return step("decision", auto ? "Automatic" : "Approved by OCC", true, "good");
    if (item.status !== "assessment_ready" || !shown.length) return step("decision", "Not yet", false);
    if (blocked === shown.length) return step("decision", "No plan can run", false, "bad");
    return step("decision", state.session?.capabilities?.approve === true ? "Waiting for you" : "Waiting for OCC", false, "warn");
  })();
  // A storm plan in two stages: the rest held until confirmed, stood down, or back with OCC (case summary and held storm spec §2).
  const storm = execution?.storm && typeof execution.storm === "object" ? execution.storm : null;
  const carried = !execution ? step("carried", finished ? "Nothing to carry out" : "Not yet", finished)
    : storm && execution.status === "held" ? step("carried", "Rest held", false, "progress")
    : storm && execution.status === "stood_down" ? step("carried", "Stood down", true, "good")
    : storm && execution.status === "needs_attention" && storm.returned ? step("carried", "Back to you", false, "bad")
    : execution.status === "completed" ? (review ? step("carried", `${confirmed} of ${actions.length} · ${review.passengers} wait for Passenger Services`, false, "warn")
      : step("carried", `${confirmed} of ${actions.length} confirmed`, true, "good"))
    : execution.status === "needs_attention" ? step("carried", RecoveryView.outcomeLine(execution).text.startsWith("Stopped part-way") ? "Stopped part-way" : "Stopped", false, "bad")
    : step("carried", `${confirmed} of ${actions.length} confirmed`, false, "progress");
  const list = [
    step("reported", [lisbonClock(item.created_at), sourceOf(item), repeats.length ? `${repeats.length + 1} reports` : ""].filter(Boolean).join(" · "), true,
      repeats.some(e => e.event === "conflicting_report") ? "warn" : ""),
    assessing ? step("assessed", "Agents working…", false, "progress")
      : step("assessed", Number.isFinite(agents) ? `${agents} agent${agents === 1 ? "" : "s"}` : "Agents", true),
    !shown.length ? step("plans", assessing ? "Not yet" : "None built", !assessing)
      : step("plans", [`${own.length} built`, whatIfs ? `${whatIfs} what-if` : "", execution ? (execution.status === "completed" ? "1 ran" : "1 approved") : ""]
        .filter(Boolean).join(" · "), true),
    !shown.length ? step("rules", assessing ? "Not yet" : "Not checked", !assessing)
      : execution && auto ? step("rules", "Within every limit", true, "good")
      : occ ? step("rules", `OCC decides${blocked ? ` · ${blocked} blocked` : ""}`, true, "warn")
      : blocked === shown.length ? step("rules", "Blocked", true, "bad") : step("rules", "Within every limit", true, "good"),
    decided, carried];
  const current = list.findIndex(entry => !entry.done);
  list.forEach((entry, index) => {
    entry.state = current < 0 ? (index === list.length - 1 ? "current" : "done") : index < current ? "done" : index === current ? "current" : "later";
  });
  return list;
}
function renderSteps() {
  const nav = $("#steps");
  const item = state.current;
  if (!item) { nav.innerHTML = ""; nav.dataset.key = ""; return; }
  const list = stepsOf(item);
  const now = list.find(entry => entry.state === "current");
  const shown = stepShown(item);
  // Unchanged steps keep their buttons (and the keyboard focus on them).
  const key = JSON.stringify([list, shown]);
  if (nav.dataset.key === key) return;
  const focused = document.activeElement?.closest?.("#steps [data-step]")?.dataset.step;
  nav.dataset.key = key;
  nav.innerHTML = `<ol role="tablist" aria-label="Case journey">${list.map((entry, index) => `<li class="step ${entry.state}${entry.tone ? ` ${entry.tone}` : ""}${
    entry.id === shown ? " shown" : ""}" role="presentation"><button class="step-btn" type="button" role="tab" id="steptab-${entry.id}" data-step="${
    entry.id}" aria-selected="${shown === entry.id}" aria-controls="drawer"${entry.state === "current" ? ' aria-current="step"' : ""}>
      <span class="dot" aria-hidden="true">${entry.state === "done" || (entry.state === "current" && entry.tone === "good") ? "✓" : index + 1}</span>
      <span class="lbl">${escapeHtml(entry.label)}</span> <span class="sub">${escapeHtml(entry.sub)}</span></button></li>`).join("")}</ol>
    <p class="steps-now"><b>${escapeHtml(now.label)}</b> · ${escapeHtml(now.sub)}</p>`;
  if (focused) nav.querySelector(`[data-step="${focused}"]`)?.focus({ preventScroll: true });
}
// The panel under the steps always shows one step (the chosen one, else the default); nothing beside the case changes.
function renderDrawer() {
  const drawer = $("#drawer");
  const item = state.current;
  const shown = stepShown(item);
  const open = Boolean(screensLoaded && item && shown);
  drawer.hidden = !open;
  if (!open) return;
  drawer.setAttribute("aria-labelledby", `steptab-${shown}`);
  drawer.querySelectorAll(".dr-step").forEach(section => { section.hidden = section.dataset.panel !== shown; });
  const sent = Boolean(item.execution?.actions?.length);
  $("#done-empty").hidden = sent;
  $("#done-empty").textContent = sent ? "" : item.status === "assessing" ? "Nothing sent yet."
    : item.status === "assessment_ready" && !item.manual_intervention && plansOf(item).length ? "Nothing sent yet · ARROW acts only after a plan is approved."
    : "Nothing was sent to the airline systems.";
}
// The steps stay where they are: when the content had been scrolled under them, the newly chosen step starts right below them.
function keepPanelInView() {
  const steps = $("#steps").getBoundingClientRect();
  const panel = $("#drawer").getBoundingClientRect();
  if (panel.top >= steps.bottom - 1) return;
  const pane = matchMedia("(max-width: 860px)").matches ? document.scrollingElement : $("#case");
  pane.scrollTop -= steps.bottom - panel.top;
}
function openStep(id) {
  if (!state.current || !STEPS.some(([key]) => key === id)) return;
  const was = stepShown();
  state.step = id;
  setCaseLink(state.selected);
  renderHead();
  renderSteps();
  renderDrawer();
  if (TRAIL_STEPS.has(id)) {
    renderTrail();
    if (state.trail?.trace_id !== state.selected) loadTrail();
  }
  if (was !== id) keepPanelInView();
}
// Back to the default step (leaving the case).
function closeStep() {
  if (!state.step) return;
  state.step = null;
  setCaseLink(state.selected);
  renderHead();
  renderSteps();
  renderDrawer();
}

// ---------- How ARROW decided: the Assessed and Rules steps ----------
// ARROW's calls from the moment a plan was authorised to run are the executor's rechecks and writes.
function trailContext() {
  const authorised = state.current?.trace_id === state.selected ? (state.current.events || []).filter(e =>
    e.event === "auto_execution_authorised" || (e.event === "duty_manager_decision" && e.decision === "approve")) : [];
  // portal: the session's Foundry project link, for each agent's Open in Foundry; names: each plan by its card's name.
  return { executionFrom: authorised.at(-1)?.at || null, portal: state.session?.foundry_portal_url || null, names: planNames() };
}
// Each plan by the name its card shows (owner review: "Cancel AR101 + AR102", not the system's option title).
function planNames() {
  const item = state.current?.trace_id === state.selected ? state.current : null;
  return Object.fromEntries((item?.options || []).filter(option => option?.plan_id || option?.option_id)
    .map(option => [String(option.plan_id || option.option_id), RecoveryView.planName(option)]));
}
// Assessed: the counts, the graph with a card per agent, the panel for the step chosen on it and, on demand, the conversation.
// Rules: every plan, rule by rule.
function renderTrail() {
  const shown = stepShown();
  if (!TRAIL_STEPS.has(shown)) return;
  const trail = state.trail;
  if (!trail || trail.trace_id !== state.selected) {
    const loading = '<p class="muted">Loading how ARROW decided…</p>';
    $("#trail-stats").innerHTML = $("#trail-detail").innerHTML = $("#trail-alt").innerHTML = "";
    $("#trail-graph").innerHTML = $("#trail-checks").innerHTML = loading;
    return;
  }
  if (shown === "rules") {
    $("#trail-checks").innerHTML = `${TrailView.checks(trail, trailContext())}<p class="tv-go"><button type="button" class="tv-ghost" data-goto-node="rules">ARROW rules on the graph ›</button></p>`;
    return;
  }
  $("#trail-stats").innerHTML = TrailView.summary(trail);
  $("#trail-graph").innerHTML = TrailView.graph(trail, state.node, trailContext());
  renderTrailDetail();
  // Polling re-renders the conversation; keep any raw answer the operator opened.
  const convo = $("#trail-alt");
  const open = [...convo.querySelectorAll("details[open][data-raw]")].map(d => d.dataset.raw);
  convo.innerHTML = TrailView.conversation(trail, trailContext());
  convo.querySelectorAll("details[data-raw]").forEach(d => { if (open.includes(d.dataset.raw)) d.open = true; });
  const steps = (trail.orchestrator?.steps || []).length;
  $("#trail-convo > summary").innerHTML = steps ? `Conversation <span class="tv-count">${steps}</span>` : "Conversation";
  requestAnimationFrame(() => TrailView.drawWires($("#trail-graph"), trail));
}
// Polling redraws the panel; a call, answer or instructions block the operator opened stays open.
function renderTrailDetail() {
  const panel = $("#trail-detail");
  const open = new Set([...panel.querySelectorAll("details[open][data-keep]")].map(d => d.dataset.keep));
  const shown = new Set([...panel.querySelectorAll("[data-block]:not([hidden])")].map(b => b.dataset.block));
  panel.innerHTML = TrailView.detail(state.trail, state.node, trailContext());
  panel.querySelectorAll("details[data-keep]").forEach(d => { if (open.has(d.dataset.keep)) d.open = true; });
  panel.querySelectorAll("[data-block]").forEach(block => { if (shown.has(block.dataset.block)) toggleBlock(panel, block.dataset.block, true); });
}
function toggleBlock(panel, key, show) {
  const block = [...panel.querySelectorAll("[data-block]")].find(b => b.dataset.block === key);
  if (!block) return;
  block.hidden = !show;
  panel.querySelectorAll("[data-toggle]").forEach(b => { if (b.dataset.toggle === key) b.setAttribute("aria-expanded", String(show)); });
}
async function loadTrail() {
  if (!state.selected) return;
  // One request at a time, so a slow answer never lands after a newer one and leaves the live
  // view on an old snapshot (review); a refresh asked for meanwhile runs after it.
  if (state.trailLoading) { state.trailAgain = true; return; }
  state.trailLoading = true;
  state.trailAgain = false;
  const selected = state.selected;
  try {
    const trail = await api(`/api/cases/${encodeURIComponent(selected)}/trail`);
    if (selected !== state.selected) return;
    state.trail = trail;
    renderSteps();
    if (state.current) {
      renderSummary(state.current, planContext(state.current));
      renderDone(state.current);
      renderFoundryLink(state.current);
    }
    renderDrawer();
    renderTrail();
  } catch (error) {
    if (selected === state.selected && !state.trailAgain && TRAIL_STEPS.has(stepShown())) {
      $("#trail-graph").innerHTML = $("#trail-checks").innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    }
  } finally {
    state.trailLoading = false;
    if (state.trailAgain) loadTrail();
  }
}

// ---------- Ask about this case (case chat spec §5) ----------
// The header button counts the kept turns; the panel shows the conversation (ChatView), the suggested questions, the box
// and what may still be asked. A redraw only when something changed keeps the operator's scroll and opened answers.
function renderChat() {
  const item = state.current;
  const open = Boolean(screensLoaded && item && state.chatOpen);
  const count = item && screensLoaded ? ChatView.count(item) : 0;
  $("#case-ask").hidden = !item;
  $("#case-ask").setAttribute("aria-expanded", String(open));
  $("#case-ask-count").textContent = count ? String(count) : "";
  // The accessible name keeps the count the button shows (review).
  $("#case-ask").setAttribute("aria-label", count ? `Ask about this case · ${count} question${count === 1 ? "" : "s"}` : "Ask about this case");
  $("#case-ask-count").hidden = !count;
  $("#workspace").classList.toggle("chat-open", open);
  $("#chat").hidden = !open;
  if (!open) return;
  const now = Date.now();
  const pending = ChatView.pending(item, now);
  const busy = Boolean(pending) || state.chatSending;
  const log = $("#chat-log");
  const plans = RecoveryView.visible(item.options).map(option => [option.option_id, option.policy?.route, option.cost?.total_eur]);
  const checks = state.chatChecks.trace === item.trace_id ? state.chatChecks.calls : [];
  const key = JSON.stringify([item.trace_id, item.chat || [], plans, pending?.id || null, checks]);
  let follow = false;
  if (key !== state.chatKey) {
    const opened = new Set([...log.querySelectorAll("details[data-how][open]")].map(details => details.dataset.how));
    // A case just opened, a question just asked or a reader at the end follows the conversation; one reading back stays.
    follow = state.chatTrace !== item.trace_id || state.chatScroll || log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    log.innerHTML = ChatView.log(item, now, checks);
    log.querySelectorAll("details[data-how]").forEach(details => { if (opened.has(details.dataset.how)) details.open = true; });
    state.chatKey = key;
    state.chatTrace = item.trace_id;
    state.chatScroll = false;
  }
  const asks = ChatView.suggestions(item);
  const asksKey = JSON.stringify([asks, busy]);
  if (asksKey !== state.chatAsks) {
    $("#chat-suggest").hidden = !asks.length;
    $("#chat-suggest").innerHTML = asks.map(question =>
      `<button type="button" data-ask="${escapeHtml(question)}"${busy ? " disabled" : ""}>${escapeHtml(question)}</button>`).join("");
    state.chatAsks = asksKey;
  }
  $("#chat-input").disabled = $("#chat-send").disabled = busy;
  $("#chat-input").placeholder = pending ? "Waiting for the answer…" : "Ask about this case…";
  const limits = ChatView.limits(item);
  $("#chat-limits").textContent = limits;
  $("#chat-limits").hidden = !limits;
  // Last, once the suggestions and the footer have taken their height.
  if (follow) log.scrollTop = log.scrollHeight;
  tick();
  loadChatChecks(item);
}
// The system calls the agents made for each answered turn, as the decision trail lists them apart from the assessment
// (trail.chat_calls): read once each time a turn is answered, and only while the panel is open.
async function loadChatChecks(item) {
  const traceId = item.trace_id;
  const answered = ChatView.answered(item);
  const want = JSON.stringify([traceId, answered]);
  if (!answered.length || state.chatChecksFor === want || state.chatChecksLoading) return;
  state.chatChecksLoading = true;
  state.chatChecksFor = want;
  try {
    const trail = await api(`/api/cases/${encodeURIComponent(traceId)}/trail`);
    if (state.selected === traceId) state.chatChecks = { trace: traceId, calls: Array.isArray(trail.chat_calls) ? trail.chat_calls : [] };
  } catch {
    // An answer's Calls keep the run's own calls; the next answered turn reads again.
  } finally {
    state.chatChecksLoading = false;
  }
  if (state.selected === traceId) renderChat();
}
function setChat(open) {
  state.chatOpen = open;
  state.chatKey = state.chatAsks = null;
  // The panel opens on the latest turn; the step shown under the case stays as it is.
  if (open) state.chatScroll = true;
  renderChat();
  // A what-if that landed while the chat covered the case (a phone) comes into view once it closes.
  if (!open && state.landing) showLanded();
}
async function askCase(question) {
  const item = state.current;
  const text = String(question || "").trim();
  const notice = $("#chat-notice");
  if (!item || !text || state.chatSending || ChatView.pending(item)) return;
  if (text.length > ChatView.MAX_QUESTION) {
    showNotice(`A question has at most ${ChatView.MAX_QUESTION} characters.`, false, notice);
    return;
  }
  const traceId = item.trace_id;
  state.chatSending = true;
  clearNotice(notice);
  renderChat();
  try {
    const sent = await api(`/api/cases/${encodeURIComponent(traceId)}/chat`, { method: "POST", body: JSON.stringify({ question: text }) });
    // A what-if this question adds lands in view here (landWhatIf); its turn is known by the entry the server kept.
    if (sent?.entry_id) {
      if (!state.asked.has(traceId)) state.asked.set(traceId, new Set());
      state.asked.get(traceId).add(String(sent.entry_id));
    }
    if ($("#chat-input").value.trim() === text) $("#chat-input").value = "";
    state.chatScroll = true;
    state.chatSending = false;
    // The answer follows on the case: reading it again shows the question pending and keeps reading until it arrives,
    // even when that first read fails (as after an approval).
    if (state.selected === traceId) {
      state.pollAfter = traceId;
      await refreshCase();
    }
  } catch (error) {
    // The server's refusal (empty or too long, not permitted, an answer pending, the most questions) reads as it says.
    if (state.selected === traceId) showNotice(error.message || "The question could not be sent.", error.status === 401, notice);
    // Refused as busy: another operator's question is pending or the case is full; reading the case shows which.
    if (state.selected === traceId && error.status === 409) {
      state.chatSending = false;
      state.pollAfter = traceId;
      await refreshCase();
    }
  } finally {
    state.chatSending = false;
    renderChat();
  }
}
// A plan's number in an answer (or a row of the Plans step) leads to its card; on a phone the sheet closes first, and a
// plan folded under "Other options checked" opens with the others.
function showPlan(number) {
  if (state.landing?.number === Number(number)) state.landing = null;
  if (matchMedia("(max-width: 860px)").matches) setChat(false);
  // The plan cards are the Decision step's first part.
  if (stepShown() !== "decision") openStep("decision");
  const find = () => document.querySelector(`#plans .plan[data-plan-no="${CSS.escape(String(number))}"]`);
  if (!find() && state.current?.execution && !state.othersOpen) { state.othersOpen = true; renderDecision(); }
  const card = find();
  if (!card) return;
  card.classList.add("flash");
  card.scrollIntoView({ behavior: "smooth", block: card.offsetHeight > innerHeight * 0.8 ? "start" : "center" });
  clearTimeout(state.flashTimer);
  state.flashTimer = setTimeout(() => document.querySelectorAll("#plans .plan.flash").forEach(plan => plan.classList.remove("flash")), 1600);
}
// The chat's link to a what-if being built: its placeholder in the Decision step.
function showWip() {
  if (matchMedia("(max-width: 860px)").matches) setChat(false);
  if (stepShown() !== "decision") openStep("decision");
  const card = $("#plans .plan.wip");
  if (!card) return;
  card.classList.add("flash");
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  clearTimeout(state.flashTimer);
  state.flashTimer = setTimeout(() => document.querySelectorAll("#plans .plan.flash").forEach(plan => plan.classList.remove("flash")), 1600);
}

// ---------- Loading a case ----------
function render() { renderCases(); renderHead(); renderSteps(); renderDecision(); renderReported(); renderChat(); renderDrawer(); renderTrail(); }
// ARROW is still assessing or carrying a plan out.
function working(item) {
  return activeStatuses.has(item.status) || ["approved", "executing", "pending_external"].includes(item.execution?.status);
}
// The case is read again while it works, and while an answer in its chat is pending.
function polling(item) {
  return working(item) || Boolean(screensLoaded && ChatView.pending(item));
}
// A read that changed only the chat redraws only the chat: plan cards keep what the operator opened.
const withoutChat = (item) => JSON.stringify({ ...item, chat: null });
function schedulePoll() {
  clearTimeout(state.timer);
  if (state.current && polling(state.current)) state.timer = setTimeout(() => refreshCase(), 2000);
}
async function refreshCase() {
  const selected = state.selected;
  if (!selected) return false;
  clearTimeout(state.timer);
  const generation = state.generation;
  const request = ++state.request;
  try {
    const result = await api(`/api/cases/${encodeURIComponent(selected)}`);
    if (generation !== state.generation || selected !== state.selected || request !== state.request) return false;
    const wasAssessing = state.current?.status === "assessing";
    const changed = state.current?.trace_id === result.trace_id && state.current?.version !== result.version;
    const chatOnly = state.caseRead === "fresh" && state.current?.trace_id === result.trace_id && !working(result) &&
      withoutChat(state.current) === withoutChat(result);
    state.current = result;
    state.caseRead = "fresh";
    state.pollAfter = null;
    if (chatOnly) {
      renderChat();
      renderWip(result);
      schedulePoll();
      return true;
    }
    const listed = state.cases.find(entry => entry.trace_id === selected);
    if (!listed || listed.status !== result.status || wasAssessing || changed) loadCases().catch(() => {});
    render();
    // The trail feeds the Assessed and Rules steps (and the agents' count on the steps): read again while it may change.
    if (TRAIL_STEPS.has(stepShown()) || result.status === "assessing" || wasAssessing || changed) loadTrail();
    clearNotice();
    schedulePoll();
    return true;
  } catch (error) {
    if (generation !== state.generation || request !== state.request) return false;
    state.caseRead = "stale";
    renderDecision();
    reportError(error);
    // Any failed read, automatic or not, retries while the case may still be working: one
    // failure right after an approval or a Refresh click must not stop live updates.
    const live = state.current && (polling(state.current) || state.pollAfter === selected);
    if (live && error.status !== 401 && error.status !== 403) {
      state.timer = setTimeout(() => refreshCase(), 2000);
    }
    return false;
  }
}
// step: a step to open, from the link (?case=…&step=…).
async function selectCase(traceId, step = null) {
  clearTimeout(state.timer);
  state.generation++;
  state.selected = traceId;
  state.current = null;
  state.trail = null;
  state.node = null;
  state.step = null;
  state.othersOpen = false;
  $("#trail-convo").open = false;
  state.caseRead = "loading";
  state.chatKey = state.chatAsks = null;
  clearNotice($("#chat-notice"));
  showTab("workspace");
  $("#workspace").classList.add("case-open");
  setCaseLink(traceId);
  render();
  await refreshCase();
  if (state.selected !== traceId) return;
  // While ARROW is still assessing there is nothing to decide: the inspector shows the agents working. Passengers waiting for
  // Passenger Services are the summary's action needed, in amber, which opens Carried out (prototype rev 2).
  const open = STEPS.some(([key]) => key === step) ? step : state.current?.status === "assessing" ? "assessed" : null;
  if (open && state.current) openStep(open);
  if (!TRAIL_STEPS.has(stepShown())) loadTrail();
  $("#case").scrollTop = 0;
}

// ---------- Decisions ----------
async function decisionIdempotencyKey(traceId, optionId, version, decision) {
  if (!globalThis.crypto?.subtle) throw new Error("Secure decision submission is unavailable in this browser.");
  const source = new TextEncoder().encode(`${traceId}\u001f${optionId}\u001f${version}\u001f${decision}`);
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", source));
  return `decision:${[...digest].map(value => value.toString(16).padStart(2, "0")).join("")}`;
}
function previewDecision(button) {
  if (state.caseRead !== "fresh" || state.decision) return;
  const option = state.current?.options?.find(item => item.option_id === button.dataset.optionId);
  if (!option || option.policy?.route === "blocked") return;
  const approve = button.dataset.decision === "approve";
  state.decision = { traceId: state.selected, optionId: option.option_id, decision: button.dataset.decision, version: state.current.version };
  $("#decision-title").textContent = approve ? `Approve "${planName(option)}"?` : `Reject "${planName(option)}"?`;
  const effects = approve ? PlanView.effects(option, state.current, planContext(state.current)) : [];
  // A storm plan in two stages (case summary and held storm spec §2): what runs on approval, then what waits until the storm is
  // confirmed. A weather update recorded before the approval changes that: a confirmed storm sends both parts after ARROW's
  // re-check; a dropped forecast stands the plan down at once, with nothing sent (Task 2).
  const recorded = state.current?.storm?.state;
  const standDown = approve && recorded === "dropped" && effects.some(([, , stage]) => stage);
  const confirmed = approve && recorded === "confirmed" && effects.some(([, , stage]) => stage);
  // A recorded confirmation worse than the plan was built for: ARROW returns the case to OCC instead, with nothing sent; why, as the
  // executor recorded it (storm.worse).
  const worse = confirmed && state.current?.storm?.worse ? String(state.current.storm.worse) : "";
  const staged = !confirmed && effects.some(([, , stage]) => stage === "when_confirmed");
  const STAGE_HEAD = { now: "Now · on approval", when_confirmed: "When confirmed · after ARROW's re-check" };
  $("#decision-preview").textContent = !approve ? "Rejecting records your decision. Nothing changes in any system."
    : standDown ? "The forecast was dropped: approving stands the plan down at once; nothing is sent."
    : worse ? `${worse[0].toUpperCase()}${worse.slice(1)}: ARROW will return the case to you; nothing is sent.`
    : confirmed ? "The forecast is already confirmed: ARROW re-checks the plan, then sends both parts:"
    : staged ? "On approval ARROW carries out the Now part; the rest waits until the forecast is confirmed and ARROW's re-check passes:"
    : "ARROW will recheck the sources and then make these changes in the airline systems:";
  let stageShown = null;
  $("#decision-effects").innerHTML = standDown || worse ? "" : effects.map(([system, effect, stage]) => {
    const head = staged && stage !== stageShown ? `<li class="eff-stage" data-stage="${escapeHtml(stage)}">${escapeHtml(STAGE_HEAD[stage] || "")}</li>` : "";
    stageShown = stage;
    return `${head}<li${Tips.attrs(escapeHtml, Tips.explain(system, effect), system)}><b>${escapeHtml(system)}</b> · ${escapeHtml(effect)}</li>`;
  }).join("");
  $("#decision-effect").textContent = `${state.current.display_id} · your decision and comment are kept in the case's audit log.`;
  $("#decision-comment").value = "";
  $("#decision-confirm").textContent = approve ? "Approve and carry out" : "Reject";
  $("#decision-dialog").showModal();
  renderDecision();
}
async function recordDecision() {
  const pending = state.decision;
  if (!pending) return;
  const traceId = state.selected;
  const item = state.current;
  const { optionId, decision } = pending;
  const button = $("#decision-confirm");
  if (state.caseRead !== "fresh" || traceId !== pending.traceId || item?.version !== pending.version) {
    showNotice("The case changed while you were reviewing it. Review the current version before deciding.");
    $("#decision-dialog").close();
    return;
  }
  if (!item || item.status !== "assessment_ready" || !["approve", "reject"].includes(decision) ||
      !item.options?.some(option => option.option_id === optionId && option.requires_duty_manager === true)) return;
  const expectedVersion = item.version;
  // Owner: OCC can say why it decided; sent only when written.
  const comment = $("#decision-comment").value.trim();
  button.disabled = true;
  $("#decision-cancel").disabled = true;
  try {
    const idempotencyKey = await decisionIdempotencyKey(traceId, optionId, expectedVersion, decision);
    const receipt = await api(`/api/cases/${encodeURIComponent(traceId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ option_id: optionId, decision, expected_version: expectedVersion, idempotency_key: idempotencyKey,
        ...(comment ? { comment } : {}) })
    });
    if (receipt.status !== "recorded" || receipt.trace_id !== traceId || receipt.option_id !== optionId ||
        receipt.decision !== decision || receipt.version !== expectedVersion + 1) {
      throw new Error("The server did not return a verifiable decision receipt.");
    }
    // ARROW is now carrying the plan out: keep reading the case even if this read fails.
    if (decision === "approve") state.pollAfter = traceId;
    await refreshCase();
  } catch (error) {
    const refreshed = state.selected === traceId && await refreshCase();
    const recorded = refreshed && state.current?.events?.some(event => event.event === "duty_manager_decision" &&
      event.option_id === optionId && event.decision === decision && event.case_version === expectedVersion + 1);
    if (!recorded) reportError(error);
  } finally {
    button.disabled = false;
    $("#decision-cancel").disabled = false;
    $("#decision-dialog").close();
  }
}
async function recordReview(button) {
  const traceId = state.selected;
  button.disabled = true;
  try {
    await api(`/api/cases/${encodeURIComponent(traceId)}/reviews/passenger_services`, {
      method: "POST", body: JSON.stringify({ outcome: "handled" })
    });
    await refreshCase();
  } catch (error) {
    reportError(error);
    button.disabled = false;
  }
}

// ---------- Report ----------
// A typed report carries no event id: event ids belong to Service Bus events (review).
async function sendReport(event) {
  event.preventDefault();
  const button = $("#report-send");
  const text = $("#report-text").value.trim();
  if (text.length < 3) {
    showNotice("Write what happened: the flight or aircraft, and how long.", false, $("#report-notice"));
    return;
  }
  button.disabled = true;
  button.textContent = "Sending…";
  clearNotice($("#report-notice"));
  try {
    const result = await api("/api/cases", { method: "POST", body: JSON.stringify({ type: "message", text }) });
    $("#report-dialog").close();
    $("#report-text").value = "";
    await loadCases().catch(() => {});
    await selectCase(result.trace_id);
  } catch (error) {
    const unconfirmed = error.status === undefined || error.status >= 500;
    showNotice(`${error.message || "The report could not be sent."}${unconfirmed ? " It may have been received: check Today before sending again." : ""}`,
      error.status === 401, $("#report-notice"));
  } finally {
    button.disabled = false;
    button.textContent = "Send to ARROW";
  }
}

// ---------- Events ----------
document.querySelectorAll("[data-tab]").forEach((tab) => tab.addEventListener("click", () => showTab(tab.dataset.tab)));
$("#tower").addEventListener("open-case", (event) => selectCase(event.detail.trace));
$("#tower").addEventListener("open-rules", () => showTab("rules"));
function showOnMap(trace) {
  if (!trace) return;
  showTab("map");
  mapView.focus(trace);
}
$("#tower").addEventListener("show-map", (event) => showOnMap(event.detail.trace));
$("#case-map").addEventListener("click", () => showOnMap(state.selected));
$("#cases").addEventListener("click", (event) => {
  const row = event.target.closest("[data-case]");
  if (row) selectCase(row.dataset.case);
});
$("#back").addEventListener("click", () => { $("#workspace").classList.remove("case-open"); setChat(false); closeStep(); });
$("#case-ask").addEventListener("click", () => {
  setChat(!state.chatOpen);
  if (state.chatOpen && matchMedia("(min-width: 861px)").matches && !$("#chat-input").disabled) $("#chat-input").focus();
});
$("#chat-close").addEventListener("click", () => { setChat(false); $("#case-ask").focus(); });
$("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); askCase($("#chat-input").value); });
$("#chat").addEventListener("click", (event) => {
  const plan = event.target.closest("[data-plan]");
  if (plan) { showPlan(plan.dataset.plan); return; }
  if (event.target.closest("button[data-wip]")) { showWip(); return; }
  const ask = event.target.closest("button[data-ask]");
  if (ask && !ask.disabled) askCase(ask.dataset.ask);
});
$("#refresh").addEventListener("click", async () => {
  // A case read that fails reports itself; only a clean refresh clears the notice.
  try { await loadCases(); if (!state.selected || await refreshCase()) clearNotice(); } catch (error) { reportError(error); }
});
// The case: a step shows its content under the steps, Details and Watch it live show theirs, the other options unfold, and
// Approve or Choose previews the decision. Inside the step's panel its own listener (below) handles steps and decisions.
$("#case-view").addEventListener("click", (event) => {
  const inPanel = Boolean(event.target.closest("#drawer"));
  const step = event.target.closest("[data-step]");
  if (step && !inPanel) { openStep(step.dataset.step); return; }
  const others = event.target.closest("[data-others]");
  if (others) { state.othersOpen = others.dataset.others === "show"; renderDecision(); return; }
  const button = event.target.closest("[data-option-id][data-decision]");
  if (button && !inPanel) previewDecision(button);
});
// Left and right arrows move along the steps.
$("#steps").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
  const buttons = [...$("#steps").querySelectorAll(".step-btn")];
  const index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  buttons[(index + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length].focus();
  event.preventDefault();
});
// Marking passengers handled records an expert decision, so it asks first (a Dev run).
$("#done").addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target || !state.current) return;
  if (target.id === "review-handled") { state.reviewConfirm = state.current.trace_id; renderDone(state.current); }
  else if (target.id === "review-cancel") { state.reviewConfirm = null; renderDone(state.current); }
  else if (target.id === "review-confirm") { state.reviewConfirm = null; recordReview(target); }
});
$("#decision-confirm").addEventListener("click", recordDecision);
$("#decision-cancel").addEventListener("click", () => $("#decision-dialog").close());
// Esc must not close the dialog while the decision is being sent (review).
$("#decision-dialog").addEventListener("cancel", (event) => { if ($("#decision-confirm").disabled) event.preventDefault(); });
$("#decision-dialog").addEventListener("close", () => { state.decision = null; renderDecision(); });
$("#drawer").addEventListener("click", (event) => {
  // A link to another step (Rules ›), a row of the Plans step (its card), the conversation, or ARROW's rules on the graph.
  const step = event.target.closest("[data-step]");
  if (step) { openStep(step.dataset.step); return; }
  const row = event.target.closest(".pl-row[data-plan]");
  if (row) { showPlan(row.dataset.plan); return; }
  if (event.target.closest("[data-goto=conversation]")) {
    $("#trail-convo").open = true;
    $("#trail-convo").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const node = event.target.closest("[data-goto-node]");
  if (node) { state.node = node.dataset.gotoNode; openStep("assessed"); return; }
  const button = event.target.closest("[data-option-id][data-decision]");
  if (button) { previewDecision(button); return; }
  // An agent's instructions open full width under the panel's header.
  const toggle = event.target.closest("[data-toggle]");
  if (toggle) {
    toggleBlock($("#trail-detail"), toggle.dataset.toggle, toggle.getAttribute("aria-expanded") !== "true");
    return;
  }
  // A node on the graph, or a step in the conversation's list, opens its panel under the graph.
  const target = event.target.closest("[data-node]");
  if (!target || !state.trail) return;
  state.node = target.dataset.node;
  $("#trail-graph").querySelectorAll(".node[data-node]").forEach(entry =>
    entry.setAttribute("aria-pressed", String(entry.dataset.node === state.node)));
  renderTrailDetail();
  // The page scrolls: the panel comes into view below the steps, which stay at the top, when less than 40 % of the screen
  // shows it.
  const panel = $("#trail-detail");
  const box = panel.getBoundingClientRect();
  const view = matchMedia("(max-width: 860px)").matches ? { top: 0, bottom: innerHeight } : $("#case").getBoundingClientRect();
  if (box.top > view.bottom - (view.bottom - view.top) * 0.4 || box.bottom < $("#steps").getBoundingClientRect().bottom) {
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});
window.addEventListener("resize", () => {
  if (stepShown() === "assessed" && state.trail) TrailView.drawWires($("#trail-graph"), state.trail);
  if (!$("#tower").hidden) TowerView.layout($("#tower"));
});
$("#report-open").addEventListener("click", () => { clearNotice($("#report-notice")); $("#report-dialog").showModal(); $("#report-text").focus(); });
$("#report-close").addEventListener("click", () => $("#report-dialog").close());
$("#report-cancel").addEventListener("click", () => $("#report-dialog").close());
$("#report-form").addEventListener("submit", sendReport);
document.querySelector(".examples").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-example]");
  if (!chip) return;
  $("#report-text").value = chip.dataset.example;
  $("#report-text").focus();
});

async function init() {
  if (!screensLoaded) {
    // Only the page, app.js, styles.css and recovery_view.js are public; without an ARROW sign-in the
    // other scripts are refused. Offer the sign-in instead of hanging on "Loading".
    $("#tower").innerHTML = '<div class="tw"><p class="muted">Not signed in.</p></div>';
    $("#user-name").textContent = "Not signed in";
    showNotice("Sign in to ARROW to load the operating day.", true);
    return;
  }
  // ARROW opens on the control tower; a link to a case opens that case (and the step it names).
  const linked = new URLSearchParams(location.search).get("case");
  const linkedStep = new URLSearchParams(location.search).get("step");
  showTab(linked ? "workspace" : "tower");
  try {
    state.session = await api("/api/session");
    const name = state.session.user?.name || "Signed-in user";
    $("#user-name").textContent = name;
    $("#user-role").textContent = state.session.user?.role || "";
    $("#user-initials").textContent = state.session.user?.role === "Passenger Services" ? "PS" : "OCC";
    $("#user-initials").dataset.tipTitle = name;
    $("#user-initials").dataset.tip = state.session.user?.role || "Signed in";
    const day = Date.parse(`${state.session.day}T12:00:00Z`);
    // The operating day and its clock on the Today heading's hover or focus; the control tower shows them too.
    $("#today-title").dataset.tip = `${Number.isFinite(day) ? new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" }).format(new Date(day)) : state.session.day} · ${state.session.demo_time}`;
    const reports = state.session.capabilities?.approve === true;
    $("#report-open").disabled = !reports;
    $("#report-open").title = reports ? "" : "Only OCC reports a disruption";
    $("#refresh").disabled = false;
  } catch (error) {
    $("#user-name").textContent = "Session unavailable";
    reportError(error);
    return;
  }
  await Promise.all([refreshList(), loadRules()]);
  scheduleList();
  if (linked) await selectCase(linked, linkedStep);
}
const screensLoaded = [typeof MapView, typeof RulesView, typeof TowerView, typeof TrailView, typeof MapDraw, typeof ChatView, typeof PlanView]
  .every(kind => kind !== "undefined");
const mapView = screensLoaded ? new MapView(api, { openCase: selectCase, openTower: () => showTab("tower") }) : null;
const rulesView = screensLoaded ? new RulesView(api, loadRules) : null;
init();
