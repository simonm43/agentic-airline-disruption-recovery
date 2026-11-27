"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const PENDING = new Set(["Waiting for ARROW", "Assessing", "Executing"]);
const TONES = {
  "Executed automatically": "good", "Executed after approval": "good", "No disruption": "good",
  "Waiting for ARROW": "wait", "Assessing": "wait", "Executing": "wait",
  "Waiting for OCC": "human", "Manual intervention": "human", "Needs attention": "human",
  "Stopped": "bad", "Rejected": "bad", "Not received by ARROW": "bad", "Closed by reset": "wait",
  "Added to open case": "good",
  // A weather plan (spec 2026-11-23 §2): its Now part done and the rest waiting for the forecast to be confirmed, or stood down by a dropped forecast.
  "Held until confirmed": "wait", "Stood down": "good"
};
const RESETTING = new Set(["closing", "loading_day"]);
// What each outcome means, on hover (owner, 21 Nov: "what is the difference between expected OCC
// and manual intervention?"). Both reach OCC; only the second has no plan ARROW may propose.
const OUTCOME_MEANS = {
  "Executed automatically": "Within every limit: ARROW carries the plan out and informs OCC.",
  "Waiting for OCC": "ARROW built legal plans; the OCC Duty Manager compares them and approves one.",
  "Manual intervention": "No plan ARROW may propose is legal (for example crew over the flight-time limit and nothing else offered): OCC acts outside ARROW.",
  "Executed after approval": "OCC approved a plan in ARROW and ARROW carried it out. A weather plan's held part goes once the forecast is confirmed and ARROW's re-check finds nothing worse.",
  "Held until confirmed": "OCC approved the weather plan: passengers are warned and standby crews on alert; the rest waits until the forecast is confirmed.",
  "Stood down": "The forecast was dropped: ARROW released the standby crews and told the warned passengers no change is expected; nothing held was sent.",
  "Needs attention": "The plan stopped, or the confirmed weather came back to OCC worse than planned: OCC decides in ARROW."
};
const state = { status: null, events: [], eventsError: "", open: new Map(), timer: null, busy: false,
  showEarlier: false, reset: null, resetActive: false,  // resetActive: this page started a reset
  awaiting: new Map() };  // sent event id -> give-up time: polled quickly until ARROW lists it

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Testbench-Request": "1", ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body ?? {}) });

function lisbonTime(iso) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZone: "Europe/Lisbon" }).format(new Date(iso));
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  const loaded = status.day_loaded_at ? ` · loaded ${lisbonTime(status.day_loaded_at).slice(0, 5)}` : "";
  $("#day").textContent = `Day ${status.today}${loaded}`;
  renderReset(status.reset);
  renderControls();
}

// Nothing is sent while the day is being reset, and Reset stays off until the server reports
// that the reset ended (review 20 Nov).
function resetting() {
  return state.resetActive || RESETTING.has(state.reset?.state);
}

function renderControls() {
  const canSend = state.status?.can_send === true && !resetting();
  $("#send").disabled = !canSend;
  document.querySelectorAll("#presets button").forEach((button) => { button.disabled = !canSend; });
  $("#reset").disabled = resetting();
}

// A reload usually takes one to two minutes (a cloud job), longer when the Test Bench has to
// start first; the bar is that estimate, not measured progress.
const RESET_TYPICAL_S = 90;
const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
let resetTicker = null;

function resetElapsed() {
  // This page's own click is the start; otherwise the server's count since Reset was pressed.
  if (state.resetActive && state.resetClickedAt) return (Date.now() - state.resetClickedAt) / 1000;
  const base = state.reset?.elapsed_s ?? 0;
  return base + (Date.now() - (state.resetSeenAt || Date.now())) / 1000;
}

function drawReset() {
  const reset = state.reset;
  const progress = $("#reset-progress");
  progress.dataset.state = reset.state;
  if (reset.state === "idle") { progress.replaceChildren(); return; }
  const text = document.createElement("span");
  if (RESETTING.has(reset.state)) {
    const elapsed = resetElapsed();
    text.textContent = `${reset.message} · ${clock(elapsed)}`;
    const bar = document.createElement("span");
    bar.className = "bar";
    bar.title = "Usually 1–2 minutes";
    const fill = document.createElement("span");
    fill.style.width = `${Math.min(95, (elapsed / RESET_TYPICAL_S) * 100).toFixed(0)}%`;
    bar.append(fill);
    progress.replaceChildren(text, bar);
  } else {
    const took = Number.isFinite(reset.took_s) ? ` in ${clock(reset.took_s)}` : "";
    text.textContent = `${reset.message}${reset.state === "done" ? took : ""}`;
    progress.replaceChildren(text);
  }
}

function renderReset(reset) {
  if (!reset) return;
  state.reset = reset;
  state.resetSeenAt = Date.now();
  drawReset();
  clearInterval(resetTicker);
  resetTicker = RESETTING.has(reset.state) ? setInterval(drawReset, 1000) : null;
  renderControls();
}

function step(ok, text) {
  return `<span class="step${ok ? " ok" : ""}">${escapeHtml(text)}</span>`;
}

// A demo case's expected outcome (release rules) beside what actually happened. "Waiting for OCC"
// is met as well once OCC approves in ARROW and the plan runs (18 Nov Dev run), or the weather plan is
// held or stood down. A weather update still held, or sent before OCC approved, is not yet its outcome.
const FOLLOWS = { "Waiting for OCC": new Set(["Executed after approval", "Held until confirmed", "Stood down"]) };
const NOT_YET = { "Executed after approval": new Set(["Held until confirmed", "Waiting for OCC"]), "Stood down": new Set(["Held until confirmed", "Waiting for OCC"]) };
function expectedMarkup(item) {
  const expected = item.expected;
  if (!expected) return "";
  const met = item.outcome === expected.outcome || FOLLOWS[expected.outcome]?.has(item.outcome);
  const match = PENDING.has(item.outcome) || NOT_YET[expected.outcome]?.has(item.outcome) ? "pending" : met ? "ok" : "off";
  const mark = match === "ok" ? " ✓" : match === "off" ? " ≠" : "";
  return `<span class="expect" data-match="${match}">Case ${escapeHtml(expected.case)} · expected ${escapeHtml(expected.outcome)}${mark}</span>`;
}

function eventMarkup(item) {
  const open = state.open.has(item.event_id);
  const tone = TONES[item.outcome] || "wait";
  // A weather update settles a weather case opened earlier: the case, never a negative time to it.
  const after = Number.isFinite(item.case_after_s) && item.case_after_s >= 0 ? ` +${item.case_after_s} s` : "";
  const caseText = item.case ? `${item.case.display_id}${after}` : "No case yet";
  const link = item.case && state.status?.ops_url
    ? `<a class="open-link" href="${escapeHtml(`${state.status.ops_url}/?case=${encodeURIComponent(item.case.trace_id)}`)}" target="_blank" rel="noopener">Open in ARROW</a>`
    : "<span></span>";
  const means = OUTCOME_MEANS[item.outcome];
  return `<li class="event" data-open="${open}">
    <div class="event-row">
      <button class="event-toggle" type="button" data-event="${escapeHtml(item.event_id)}" aria-expanded="${open}">
        <span class="chevron" aria-hidden="true">▸</span>
        <span class="time">${escapeHtml(lisbonTime(item.sent_at))}</span>
        <span class="what">${escapeHtml(item.label)}${expectedMarkup(item)}</span>
        ${step(item.queued_after_s != null, item.queued_after_s != null ? `Queued ✓ +${item.queued_after_s} s` : "Sent")}
        ${step(Boolean(item.case), caseText)}
        <span class="outcome" data-tone="${tone}"${means ? ` title="${escapeHtml(means)}"` : ""}>${escapeHtml(item.outcome)}${item.reason ? `<span class="reason">${escapeHtml(item.reason)}</span>` : ""}</span>
      </button>
      ${link}
    </div>
    ${open ? detailsMarkup(state.open.get(item.event_id)) : ""}
  </li>`;
}

function json(value) {
  return JSON.stringify(value ?? null, null, 2);
}

// Lines of the delivered body that are not in the sent event are highlighted.
function diffMarkup(sent, delivered) {
  const known = new Set(json(sent).split("\n").map((line) => line.replace(/,$/, "")));
  return json(delivered).split("\n").map((line) => known.has(line.replace(/,$/, ""))
    ? escapeHtml(line) : `<span class="diff">${escapeHtml(line)}</span>`).join("\n");
}

// The recipients as the operator app names them (one vocabulary, owner 22 Nov); older labels read as these.
const RECIPIENT_NAMES = { "Ops control": "Operations control", "Network manager": "EUROCONTROL Network Manager",
  "Airport CDM": "Lisbon airport (A-CDM)", Crew: "Crew system", Passengers: "Passenger system" };
const recipientName = (label) => (Object.hasOwn(RECIPIENT_NAMES, String(label)) ? RECIPIENT_NAMES[label] : String(label ?? ""));

function outcomeMarkup(detail) {
  if (detail.rejection) return `<p>Rejected: ${escapeHtml(detail.rejection.reason)}</p>`;
  const outcome = detail.outcome;
  if (!outcome) return '<p class="subtle">No ARROW case yet.</p>';
  const plans = (outcome.plans || []).map((plan) => `<li>${escapeHtml(plan.kind)} · ${escapeHtml(plan.route)}${
    plan.recommended ? " · recommended" : ""}${plan.cost_eur != null ? ` · €${Number(plan.cost_eur).toLocaleString("en-GB")}` : ""}${
    (plan.reasons || []).length ? ` — ${escapeHtml(plan.reasons.join("; "))}` : ""}</li>`).join("");
  const execution = outcome.execution;
  const actions = execution ? (execution.actions || []).map((action) =>
    `<li>${escapeHtml(recipientName(action.label))}: ${escapeHtml(action.status)} — ${escapeHtml(action.message)}</li>`).join("") : "";
  return `${outcome.summary ? `<p>${escapeHtml(outcome.summary)}</p>` : ""}
    ${plans ? `<ul>${plans}</ul>` : ""}
    ${execution ? `<p class="subtle">Execution ${escapeHtml(execution.status)} · approved by ${escapeHtml(execution.approved_by)}</p><ul>${actions}</ul>` : ""}`;
}

function detailsMarkup(detail) {
  if (!detail) return '<div class="details"><p class="subtle">Loading the messages…</p></div>';
  if (detail.error) return `<div class="details"><p class="error">${escapeHtml(detail.error)}</p></div>`;
  const delivered = (detail.delivered || []).map((delivery) =>
    `<p class="subtle">${escapeHtml(lisbonTime(delivery.at))} · #${escapeHtml(delivery.sequence_number)} · delivery ${escapeHtml(delivery.delivery_count)} · ${escapeHtml(delivery.delivery)}</p>
     <pre>${diffMarkup(detail.sent, delivery.body)}</pre>`).join("");
  return `<div class="details">
    <section class="step-box"><h3>Sent</h3><pre>${escapeHtml(json(detail.sent))}</pre></section>
    <section class="step-box"><h3>Queued</h3>${detail.queued
      ? `<pre>${escapeHtml(json(detail.queued))}</pre>` : '<p class="subtle">ARROW has not read it yet.</p>'}</section>
    <section class="step-box"><h3>Received by ARROW</h3>${delivered || (detail.rejection
      ? `<p>Rejected by ARROW: ${escapeHtml(detail.rejection.reason)}</p>` : '<p class="subtle">Not received yet.</p>')}</section>
    <section class="step-box"><h3>Outcome</h3>${outcomeMarkup(detail)}</section>
  </div>`;
}

function renderEvents() {
  const list = $("#events");
  if (state.eventsError) {
    list.innerHTML = `<li class="empty unavailable">${escapeHtml(state.eventsError)}</li>`;
    return;
  }
  // Events whose cases a Reset closed (or that never became a case before it) fold into one line.
  const current = state.events.filter((item) => !item.earlier);
  const earlier = state.events.filter((item) => item.earlier);
  const fold = earlier.length
    ? `<li class="earlier">${earlier.length} earlier event${earlier.length === 1 ? "" : "s"} closed by reset
        <button type="button" class="link-button" data-earlier>${state.showEarlier ? "Hide" : "Show"}</button></li>`
    : "";
  list.innerHTML = (current.length ? current.map(eventMarkup).join("") : '<li class="empty">No events since the last reset. Send one above.</li>')
    + fold + (state.showEarlier ? earlier.map(eventMarkup).join("") : "");
}

async function loadEvents() {
  try {
    const result = await api("/api/events");
    state.events = result.events || [];
    state.eventsError = "";
  } catch (error) {
    state.events = [];
    state.eventsError = error.status === 503 ? error.message : `Events could not be loaded: ${error.message}`;
  }
  for (const id of state.open.keys()) await loadDetail(id);
  renderEvents();
}

async function loadDetail(id) {
  try { state.open.set(id, await api(`/api/events/${encodeURIComponent(id)}`)); }
  catch (error) { state.open.set(id, { error: error.message }); }
}

async function refresh() {
  clearTimeout(state.timer);
  try { state.status = await api("/api/status"); }
  catch (error) { $("#day").textContent = error.status === 401 ? "Sign in" : "Unavailable"; }
  renderStatus();
  await loadEvents();
  schedule();
}

function schedule() {
  clearTimeout(state.timer);
  const listed = new Set(state.events.map((item) => item.event_id));
  for (const [id, until] of state.awaiting) {
    if (listed.has(id) || Date.now() > until) state.awaiting.delete(id);
  }
  const busy = state.awaiting.size > 0 || state.events.some((item) => PENDING.has(item.outcome)) ||
    RESETTING.has(state.status?.reset?.state);
  // Events from other systems and decisions taken in ARROW appear by themselves.
  state.timer = setTimeout(refresh, busy ? 3000 : 10000);
}

async function pollReset() {
  let wait = 1500;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, wait));
    let reset;
    try {
      reset = await api("/api/reset");
    } catch (error) {
      // A failed check is not the end of the reset: keep everything off and check again.
      $("#reset-progress").title = `Checking again: ${error.message}`;
      wait = Math.min(wait * 2, 10000);
      continue;
    }
    wait = 1500;
    renderReset(reset);
    if (!RESETTING.has(reset.state)) break;
  }
  state.resetActive = false;
  await refresh();
}

// The same limits as ARROW (spec 2026-11-10 §3.1), checked before anything is sent.
function formEvent(form) {
  const values = new FormData(form);
  const text = String(values.get("text") || "").trim();
  const system = String(values.get("system") || "").trim();
  const errors = [];
  if (text.length < 3 || text.length > 2000) errors.push("What happened: 3–2,000 characters");
  if (system.length > 100) errors.push("Sending system: at most 100 characters");
  return { errors, body: { kind: "message", data: { text, ...(system ? { system } : {}) } } };
}

async function send(body) {
  if (resetting()) return;
  const result = $("#send-result");
  try {
    const sent = await post("/api/events", body);
    state.awaiting.set(sent.event_id, Date.now() + 60000);
    result.textContent = `Sent ${lisbonTime(sent.sent_at)} · event ${sent.event_id}`;
  } catch (error) {
    result.textContent = `Not sent: ${error.message}`;
  }
  await refresh();
}

$("#event-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const { errors, body } = formEvent(event.currentTarget);
  const error = $("#form-error");
  error.hidden = !errors.length;
  error.textContent = errors.join(" · ");
  if (!errors.length) await send(body);
});
$("#presets").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-preset]");
  if (button) send({ preset: button.dataset.preset });
});
$("#reset").addEventListener("click", async () => {
  state.resetActive = true;
  state.resetClickedAt = Date.now();
  // Count from the click: the first request can wait while the Test Bench starts up.
  renderReset({ state: "closing", message: "Starting", elapsed_s: 0 });
  let started;
  try {
    started = await post("/api/reset");
  } catch (error) {
    state.resetActive = false;
    // 409: a reset is already running; the server's own state says so and keeps Reset off.
    if (error.status === 409) await refresh();
    else renderReset({ state: "failed", message: `Reset: ${error.message}` });
    return;
  }
  renderReset(started);
  await pollReset();
});
$("#events").addEventListener("click", async (event) => {
  if (event.target.closest("a")) return;
  if (event.target.closest("button[data-earlier]")) {
    state.showEarlier = !state.showEarlier;
    renderEvents();
    return;
  }
  const row = event.target.closest("button[data-event]");
  if (!row) return;
  const id = row.dataset.event;
  if (state.open.has(id)) state.open.delete(id);
  else {
    state.open.set(id, null);
    renderEvents();
    await loadDetail(id);
  }
  renderEvents();
});

async function init() {
  try {
    const { presets } = await api("/api/presets");
    $("#presets").innerHTML = presets.map((preset) =>
      `<button type="button" class="case${preset.kind === "weather_update" ? " update" : ""}" data-preset="${escapeHtml(preset.key)}" disabled>
        <strong>${escapeHtml(preset.label)}</strong>
        <span class="case-title">${escapeHtml(preset.title)}</span>
        ${preset.kind === "weather_update" ? '<span class="case-note">Weather update · send after OCC approves the weather plan</span>' : ""}
        <span class="case-expected" title="${escapeHtml(OUTCOME_MEANS[preset.expected] || "")}">Expected: ${escapeHtml(preset.expected)}</span>
      </button>`).join("");
  } catch (error) {
    $("#presets").textContent = `Presets unavailable: ${error.message}`;
  }
  await refresh();
}
init();
