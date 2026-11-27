"use strict";

// Ask about this case (case chat spec §5, case chat prototype): the questions and the Case
// Assistant's answers as kept on the case, the questions to suggest and what may still be asked. Presentation only:
// app.js posts a question and keeps reading the case while an answer is pending. Questions, answers and call details
// are always escaped; a plan's number in an answer becomes a link to its card.
const ChatView = (() => {
  const MAX_WHAT_IFS = 3;
  // A pending answer older than this was interrupted (ARROW restarted); the next question marks it failed (chat.py).
  const STALE_MS = 5 * 60 * 1000;
  const INTERRUPTED = "No answer (interrupted); ask again";
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const plain = new Intl.NumberFormat("en-GB");
  const isObj = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  // A recorded "a · b" reads as a list (owner: no "word · word" chains).
  const commas = (text) => String(text ?? "").replaceAll(" · ", ", ");

  const entries = (item) => (Array.isArray(item?.chat) ? item.chat.filter(isObj) : []);
  const askedAt = (entry) => Date.parse(entry.asked_at || "");
  const interrupted = (entry, now) => entry.status === "pending" && !(now - askedAt(entry) <= STALE_MS);

  // The answer still to come, or null: one question at a time per case.
  function pending(item, now = Date.now()) {
    return entries(item).find(entry => entry.status === "pending" && !interrupted(entry, now)) || null;
  }

  // What-ifs only while the case waits for OCC (tools.py _not_waiting), at most three per case.
  function waiting(item) {
    return item?.status === "assessment_ready" && !item.execution;
  }
  function whatIfsLeft(item) {
    return Math.max(0, MAX_WHAT_IFS - (item?.options || []).filter(option => isObj(option?.what_if)).length);
  }
  // A storm's plans take no what-if (Task 4: no added delay at the reduced rate).
  const takesWhatIf = (item) => waiting(item) && item?.disruption?.type !== "weather" && whatIfsLeft(item) > 0;
  // What may still be asked: the what-ifs left while the case waits for OCC. What the assistant cannot do is on its card.
  function limits(item) {
    return waiting(item) && item?.disruption?.type !== "weather" ? `What-if plans left: ${whatIfsLeft(item)} of ${MAX_WHAT_IFS}` : "";
  }
  // A pending question that asks a what-if, while the case can take one, and whose plan has not landed yet. ARROW records the
  // what-if's question only once built (tools.py), so a pending one is read from the question: the placeholder and the chat's
  // progress say "building" for it, and go once the answer lands, with or without a plan.
  function whatIfPending(item, now = Date.now()) {
    const entry = pending(item, now);
    if (!entry || !takesWhatIf(item) || !/\bwhat[\s-]*if\b/i.test(String(entry.question || ""))) return null;
    return (item.options || []).some(option => option?.what_if?.entry_id === entry.id) ? null : entry;
  }
  // "12 s", "2 min": how long a pending answer has taken so far (app.js counts it on).
  function since(at, now = Date.now()) {
    const seconds = Math.max(0, Math.round((now - Date.parse(at || "")) / 1000));
    if (!Number.isFinite(seconds)) return "";
    return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
  }

  // Templates from the case's state, not the model (spec §9): the cost of the plan carried out or recommended, why the
  // recommendation (two plans or more), a crew check of it, and a what-if while the case waits for OCC. A question
  // already asked on the case is not offered again.
  function suggestions(item) {
    const shown = RecoveryView.visible(item?.options);
    const number = (option) => shown.indexOf(option) + 1;
    const recommended = shown.find(option => option.recommended === true && !isObj(option.what_if)) || null;
    const subject = shown.find(option => item?.execution && option.option_id === item.execution.option_id) || recommended;
    const list = [];
    // A storm (Task 4c): its plans follow the reduced airport rate and take no added delay, so no what-if; the questions are why
    // the delay plan is blocked and what the cancellation costs.
    const storm = item?.disruption?.type === "weather";
    if (subject && Number.isFinite(subject.cost?.total_eur)) {
      list.push(storm && subject.kind === "cancel" ? "What makes up the cancellation's cost?" : `What makes up the ${euro.format(subject.cost.total_eur)}?`);
    }
    if (recommended && shown.length > 1) list.push(`Why is Plan ${number(recommended)} recommended?`);
    if (storm && shown.some(option => option.kind === "delay" && option.policy?.route === "blocked")) list.push("Why is the delay plan blocked?");
    if (subject) list.push(`Does Plan ${number(subject)} keep the crew within their limits?`);
    // The 30 more minutes, unless a what-if already took them (however it was asked).
    const took30 = (item?.options || []).some(option => isObj(option?.what_if) && option.levers?.extra_delay_minutes === 30);
    if (waiting(item) && whatIfsLeft(item) && !storm && !took30) {
      list.push(item.disruption?.type === "aircraft_defect" ? "What if maintenance needs 30 more minutes?" : "What if the delay grows by 30 minutes?");
    }
    const asked = new Set(entries(item).map(entry => String(entry.question || "").trim().toLowerCase()));
    return list.filter(question => !asked.has(question.toLowerCase()));
  }

  function took(entry) {
    const seconds = Math.round((Date.parse(entry.answered_at || "") - askedAt(entry)) / 1000);
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
  }

  // The answer as written: paragraphs, "- " lists and **bold**, every "Plan N" of this case a link to its card.
  function inline(text, count) {
    return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\bPlan (\d+)\b/g, (words, n) =>
      (Number(n) >= 1 && Number(n) <= count ? `<button type="button" class="plan-ref" data-plan="${Number(n)}">${words}</button>` : words));
  }
  function answerHtml(text, count) {
    let html = "", lines = [], items = [];
    const paragraph = () => { if (lines.length) html += `<p>${lines.map(line => inline(line, count)).join("<br>")}</p>`; lines = []; };
    const list = () => { if (items.length) html += `<ul>${items.map(line => `<li>${inline(line, count)}</li>`).join("")}</ul>`; items = []; };
    for (const line of String(text || "").split(/\r?\n/)) {
      const bullet = line.match(/^\s*[-•*]\s+(.*)$/);
      if (bullet) { paragraph(); items.push(bullet[1]); }
      else if (!line.trim()) { paragraph(); list(); }
      else { list(); lines.push(line); }
    }
    paragraph();
    list();
    return html;
  }

  // The plans an answer names, in order, as links under it; a what-if it added has its own card.
  function mentioned(text, count, skip) {
    const found = [];
    for (const [, n] of String(text || "").matchAll(/\bPlan (\d+)\b/g)) {
      const number = Number(n);
      if (number >= 1 && number <= count && !skip.includes(number) && !found.includes(number)) found.push(number);
    }
    return found;
  }
  // The what-if an answer added: its name, three numbers, who decides and a link to its card (owner: "not that intuitive").
  function whatIfCard(option, number) {
    const route = option.policy?.route === "blocked" ? ["blocked", "Blocked"] : ["occ", "OCC decides"];
    const nums = [[RecoveryView.disrupted(option), "Lose flight", plain], [RecoveryView.owed(option), "Owed compensation", plain],
      [option.cost?.total_eur, "Cost", euro]].filter(([value]) => Number.isFinite(value));
    return `<div class="chat-whatif" data-whatif="${number}"><p class="cw-head"><span class="rank">${number}</span> <b>${esc(RecoveryView.planName(option))}</b> <span class="cw-tag">What-if</span></p>
      ${nums.length ? `<dl class="cw-nums">${nums.map(([value, label, format]) => `<div><dt>${esc(label)}</dt> <dd>${esc(format.format(value))}</dd></div>`).join(" ")}</dl>` : ""}
      <div class="cw-foot"><span class="route ${route[0]}">${route[1]}</span> <button type="button" data-plan="${number}">Show plan ${number} ›</button></div></div>`;
  }

  // An ARROW tool's arguments read as words (the case number, then each lever); the question is the turn's own.
  // Turns kept by earlier versions hold the arguments as JSON; chat.py now words them.
  function argsText(detail) {
    const text = String(detail || "");
    const cut = text.indexOf(" → ");
    const [head, tail] = cut < 0 ? [text, ""] : [text.slice(0, cut), text.slice(cut)];
    let args;
    try { args = JSON.parse(head); } catch { return text; }
    if (!isObj(args)) return text;
    const parts = [];
    const add = (object) => {
      for (const [key, value] of Object.entries(object)) {
        if (key === "question" || value === null || value === undefined || value === "") continue;
        if (isObj(value)) add(value);
        else parts.push(key === "case_id" ? String(value) : `${key} ${Array.isArray(value) ? value.join(", ") : value}`);
      }
    };
    add(args);
    return `${parts.join(", ")}${tail}`;
  }
  function agentKey(name) {
    return Object.keys(Tips.AGENTS).find(key => Tips.AGENTS[key].foundry === name) || null;
  }
  function agentName(name) {
    const key = agentKey(name);
    return key ? Tips.AGENTS[key].name : String(name || "Agent");
  }
  // Field names an ARROW tool's arguments carry, in words (owner: no field names on screen).
  const ARG_WORDS = [[/\baircraft_option_id (\S+)/g, "option $1"], [/\breserve_option_ids ([^·→]+?)(?= ·| →|$)/g, (m, ids) => `standby crews ${ids.trim()}`],
    [/\breserve_option_id (\S+)/g, "standby crew $1"],
    [/\bextra_delay_minutes (\d+)/g, "+$1 min"], [/\badditional_delay_minutes (\d+)/g, "+$1 min"], [/\b([a-z]+)_([a-z_]+)\b/g, (m) => m.replaceAll("_", " ")]];
  const plainArgs = (text) => ARG_WORDS.reduce((out, [pattern, to]) => out.replace(pattern, to), String(text || ""));
  // A call in the order the run made it: who or what was called, what it did and found, read only or adding a plan; then what it
  // asked, in words; last, small, how it went, in the decision trail's words (iface: its HTML lines).
  const row = (system, what, result, cls, access, detail, iface) => `<div class="chat-call"><span class="sys">${esc(system)}</span> <span class="what">${esc(what)}${
      result ? ` <span class="res">${esc(commas(result))}</span>` : ""}</span>
      <span class="acc ${cls}">${esc(access)}</span>${detail ? ` <span class="det">${esc(commas(detail))}</span>` : ""}${
        iface.length ? ` <span class="ep iface"><i>Interface</i> ${iface.join(" · ")}</span>` : ""}</div>`;
  // A call the run made (chat.py): ARROW's tool, or a domain agent asked.
  function callRow(call) {
    const tool = call.kind === "tool";
    const adds = call.access === "adds a plan";
    const detail = tool ? plainArgs(argsText(call.detail)) : String(call.detail || "");
    const iface = tool ? TrailView.iface({ system: "ARROW", endpoint: call.endpoint || `MCP ${call.name || ""}`, tool: call.name, transport: "mcp" }, true)
      : TrailView.iface({ ask: true, agent: agentName(call.name) });
    return row(tool ? "ARROW" : agentName(call.name), tool ? call.title || "" : "Live check", "", adds ? "adds" : "ro", adds ? "Adds a plan" : "Read only",
      detail, iface);
  }
  // A system call a domain agent made for the turn (trail.chat_calls): the system, what it was for and its result, and for which agent.
  function systemRow(call) {
    const write = call.access === "write";
    const key = Object.keys(Tips.AGENTS).find(name => name === String(call.caller?.name || "").toLowerCase());
    const by = call.caller?.name ? `By the ${key ? Tips.AGENTS[key].name : call.caller.name}` : "";
    return row(Tips.system(call.system || "Airline system"), call.summary ? call.label || RecoveryView.callWords(call) : RecoveryView.callWords(call),
      call.summary || "", write ? "adds" : "ro", write ? "WRITE" : "Read only", by, TrailView.iface(call, call.caller?.kind === "agent"));
  }
  // The run's calls in order, each agent asked followed by the system calls it made for the turn.
  function callRows(calls, checks) {
    const left = [...checks];
    const rows = [];
    for (const call of calls) {
      rows.push(callRow(call));
      const key = call.kind === "agent" ? agentKey(call.name) : null;
      const made = key ? left.filter(check => String(check.caller?.name || "").toLowerCase() === key) : [];
      made.forEach(check => { rows.push(systemRow(check)); left.splice(left.indexOf(check), 1); });
    }
    return [...rows, ...left.map(systemRow)];
  }

  // The assistant's byline: its name, the one AI mark, when it answered and how long it took.
  const assistant = (entry = null) => {
    const when = entry ? RecoveryView.lisbonTime(entry.answered_at) : "";
    const span = entry ? took(entry) : "";
    return `<div class="chat-by"><b>Case Assistant</b> ${TrailView.aiMark()}${when ? ` <time>${esc(when)}</time>` : ""}${
      span ? ` <span class="took"${Tips.attrs(esc, `Answered in ${span}`)}>${esc(span)}</span>` : ""}</div>`;
  };
  function answer(entry, item, now, checks) {
    const shown = RecoveryView.visible(item?.options);
    const count = shown.length;
    if (entry.status === "pending" && !interrupted(entry, now)) {
      // An answer on its way: a sliding bar and the seconds so far; a what-if, where its plan will land (owner: "I dont see
      // any progress bar or anything").
      const whatIf = whatIfPending(item, now)?.id === entry.id;
      const doing = whatIf ? "Building the what-if plan" : "Answering";
      return `<div class="chat-turn">${assistant()}<div class="chat-wait${whatIf ? " what-if" : ""}" role="status" aria-label="${doing}">
        <span class="busy-bar" aria-hidden="true"><i></i></span><p>${doing} <span class="took" data-since="${esc(entry.asked_at)}">${esc(since(entry.asked_at, now))}</span></p>
        ${whatIf ? '<button type="button" data-wip>Show in Decision ›</button>' : ""}</div></div>`;
    }
    if (entry.status !== "answered") {
      const why = entry.status === "pending" ? INTERRUPTED : entry.error || "No answer; ask again";
      return `<div class="chat-turn">${assistant()}<div class="chat-a failed"><p>${esc(why)}</p></div></div>`;
    }
    const added = (Array.isArray(entry.what_if_plan_ids) ? entry.what_if_plan_ids : [])
      .map(id => shown.find(option => option.option_id === id || option.plan_id === id)).filter(Boolean);
    const numbers = added.map(option => shown.indexOf(option) + 1);
    const links = mentioned(entry.answer, count, numbers);
    const calls = (Array.isArray(entry.calls) ? entry.calls : []).filter(isObj);
    // The system calls the agents it asked made for this turn: read only, listed under the agent.
    const checked = checks.filter(check => check.entry_id === entry.id && check.caller?.kind === "agent");
    const total = calls.length + checked.length;
    return `<div class="chat-turn">${assistant(entry)}
      <div class="chat-a${entry.refused === true ? " refused" : ""}">${answerHtml(entry.answer, count)}
        ${added.map((option, index) => whatIfCard(option, numbers[index])).join("")}
        ${links.length ? `<div class="chat-src">${links.map(n => `<button type="button" data-plan="${n}">Plan ${n}</button>`).join("")}</div>` : ""}
        ${total ? `<details class="chat-how" data-how="${esc(entry.id)}"><summary>Calls <span class="tv-count">${total}</span></summary>
          ${callRows(calls, checked).join("")}</details>` : ""}</div></div>`;
  }

  // The conversation, oldest first: each question with who asked and when, then its answer. ``checks``: the system
  // calls the agents made for the turns (trail.chat_calls), when read.
  function log(item, now = Date.now(), checks = []) {
    const list = entries(item);
    if (!list.length) return '<p class="chat-empty">No questions on this case yet.</p>';
    const made = (Array.isArray(checks) ? checks : []).filter(isObj);
    return list.map(entry => {
      const at = RecoveryView.lisbonTime(entry.asked_at);
      return `<div class="chat-entry" data-entry="${esc(entry.id)}">
      <div class="chat-turn mine"><div class="chat-by"><b>${esc(entry.asked_by?.name || "Operator")}</b>${at ? ` <time>${esc(at)}</time>` : ""}</div>
        <div class="chat-q">${esc(entry.question)}</div></div>
      ${answer(entry, item, now, made)}</div>`;
    }).join("");
  }

  // The answered turns, whose system calls the decision trail lists (trail.chat_calls).
  const answered = (item) => entries(item).filter(entry => entry.status === "answered").map(entry => String(entry.id));

  return Object.freeze({ MAX_QUESTION: 500, pending, waiting, whatIfsLeft, whatIfPending, since, limits, suggestions, log, answered,
    count: (item) => entries(item).length });
})();
