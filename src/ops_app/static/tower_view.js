"use strict";
// Control tower (control tower and rules spec §5.2): today's cases by urgency and time pressure.
// Pure rendering from GET /api/cases rows, GET /api/rules (undefined until read, null if unreadable) and the
// session; app.js owns loading. Emits "open-case" (detail.trace), "show-map" (detail.trace) and "open-rules"
// on the container.

const TowerView = (() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  const count = new Intl.NumberFormat("en-GB");
  const START = 9 * 60, END = 24 * 60;
  const SORTS = [["urgency", "Urgency"], ["departure", "Next departure"], ["passengers", "Passengers"], ["cost", "Cost"]];
  const LEGEND = [["needs", "Needs OCC"], ["manual", "Manual action"], ["stopped", "Stopped"],
    ["expert", "Action needed"], ["progress", "In progress"], ["handled", "Handled"]];
  const ROUTE = { occ: "OCC decides", auto: "automatic" };
  let sortKey = "urgency";
  let last = null;
  const bound = new WeakSet();

  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const number = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
  const hhmm = (value) => {
    const match = String(value ?? "").match(/(?:T|^)(\d{1,2}):(\d{2})/);
    return match ? `${match[1].padStart(2, "0")}:${match[2]}` : "";
  };
  // Minutes from the operating day's midnight, so a flight after midnight sorts and plots after 23:59.
  const minutesOf = (value, day) => {
    const match = String(value ?? "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
    if (!match) return null;
    const days = day ? Math.round((Date.parse(match[1]) - Date.parse(day)) / 86400000) : 0;
    return (Number.isFinite(days) ? days : 0) * 1440 + Number(match[2]) * 60 + Number(match[3]);
  };
  const clockMinutes = (value) => {
    const match = String(value ?? "").match(/^(\d{1,2}):(\d{2})$/);
    return match ? Number(match[1]) * 60 + Number(match[2]) : null;
  };
  const span = (minutes) => {
    const h = Math.floor(minutes / 60), m = minutes % 60;
    return h ? `${h} h${m ? ` ${String(m).padStart(2, "0")}` : ""}` : `${m} min`;
  };
  const pct = (minutes) => Math.min(100, Math.max(0, ((minutes - START) / (END - START)) * 100));

  // OCC's decisions: a plan to approve, or a storm confirmed after its stand-down (decide again; case summary and held storm spec §2).
  const DECISIONS = new Set(["Needs your decision", "Forecast confirmed after stand-down"]);
  function toneOf(row) {
    const urgency = row.urgency;
    if (urgency === 1) return DECISIONS.has(row.label) ? "needs" : "manual";
    if (urgency === 2) return "stopped";
    if (urgency === 3) return "expert";
    if (urgency === 5) return "handled";
    return "progress";
  }
  function model(row, day) {
    const impact = row.impact || {};
    const blocked = impact.route === "blocked";
    const first = row.first_departure;
    const original = first ? minutesOf(first.std_local, day) : null;
    const planned = first ? minutesOf(first.planned_local, day) : null;
    // Executed only once the execution completed (case_card impact.execution_status); a stood-down storm confirmed every action it
    // sent and sent nothing it held. Without the field: once every action is confirmed (a slot request confirmed before a refused
    // retime is not).
    const total = number(impact.actions_total) || 0;
    const status = typeof impact.execution_status === "string" ? impact.execution_status : null;
    const executed = status ? status === "completed" : total > 0 && number(impact.actions_done) === total;
    // A storm plan held until confirmed, or stood down (case summary and held storm spec §2).
    const storm = status === "held" || status === "stood_down" ? status
      : row.storm?.state === "awaiting" && /^Held\b/.test(String(row.label || "")) ? "held" : row.storm?.state === "dropped" && row.label === "Stood down" ? "stood_down" : null;
    return {
      row, impact, blocked, executed, storm, tone: toneOf(row),
      urgency: number(row.urgency) ?? 4, original,
      departure: executed && planned !== null ? planned : original,
      // A blocked plan cannot run, so its figures are not this case's impact.
      passengers: blocked ? null : number(impact.passengers),
      cost: blocked ? null : number(impact.cost_eur)
    };
  }

  const nullsLast = (a, b, desc) => (a === null && b === null ? 0 : a === null ? 1 : b === null ? -1 : desc ? b - a : a - b);
  const byUrgency = (a, b) => (a.urgency - b.urgency) || nullsLast(a.original, b.original, false);
  const ORDER = {
    urgency: byUrgency,
    departure: (a, b) => nullsLast(a.departure, b.departure, false) || byUrgency(a, b),
    passengers: (a, b) => nullsLast(a.passengers, b.passengers, true) || byUrgency(a, b),
    cost: (a, b) => nullsLast(a.cost, b.cost, true) || byUrgency(a, b)
  };

  function kpis(models) {
    const occ = models.filter(m => m.urgency === 1);
    const decisions = occ.filter(m => DECISIONS.has(m.row.label));
    const review = models.filter(m => (number(m.impact.review_passengers) || 0) > 0);
    const reviewTotal = review.reduce((sum, m) => sum + m.impact.review_passengers, 0);
    const handled = models.filter(m => m.row.label === "Handled automatically");
    const confirmed = handled.reduce((sum, m) => sum + (number(m.impact.actions_done) || 0), 0);
    // Only plans waiting for approval add to what awaits a decision.
    const awaiting = decisions.filter(m => m.row.label === "Needs your decision").reduce((sum, m) => sum + (m.cost || 0), 0);
    const executed = models.filter(m => m.executed).reduce((sum, m) => sum + (m.cost || 0), 0);
    // A tile's label explains its abbreviation on hover (OCC: Operations Control Centre).
    const tile = (key, tone, value, label, sub) => `<div class="tw-kpi" data-kpi="${key}" data-tone="${tone}">
      <b class="num">${esc(value)}</b> <span${Tips.attrs(esc, Tips.explain("", label))}>${esc(label)}</span> <small>${esc(sub)}</small></div>`;
    return `<div class="tw-kpis">
      ${tile("occ", "needs", occ.length, "Need OCC",
        `${plural(decisions.length, "decision")} · ${plural(occ.length - decisions.length, "manual action")}`)}
      ${tile("review", "expert", count.format(reviewTotal), "Action needed",
        `passengers for Passenger Services${review.length ? ` · ${review.map(m => `${m.row.first_departure?.flight_no || m.row.headline} ${m.impact.review_passengers}`).join(" · ")}` : " · none waiting"}`)}
      ${tile("handled", "handled", handled.length, "Handled automatically",
        handled.length ? `OCC informed · ${plural(confirmed, "action")} confirmed` : "None yet")}
      ${tile("decision", "accent", euro.format(awaiting), "Awaiting your decision", `${euro.format(executed)} executed today · indicative`)}
    </div>`;
  }

  function lane(m, day) {
    // A weather case's lane names its hazard, then the place and window, so the window stays in view ("Strong crosswind" · "Lisbon ·
    // 16:00–18:00").
    const text = shortHeadline(m.row);
    const storm = m.row.disruption_type === "weather" ? /^(.+?) at (.+)$/.exec(text) : null;
    const [name, ...rest] = storm ? [storm[1], storm[2]] : text.split(" · ");
    const flights = Array.isArray(m.row.lane?.flights) ? m.row.lane.flights : [];
    const out = m.row.lane?.unavailable;
    let band = "";
    if (out) {
      const from = minutesOf(out.from, day), until = minutesOf(out.until, day);
      if (from !== null && until !== null && until > from) {
        band = `<div class="tw-band" style="left:${pct(from)}%;width:${pct(until) - pct(from)}%"
          title="Out of service ${esc(hhmm(out.from))}–${esc(hhmm(out.until))}"></div>`;
      }
    }
    const dots = flights.map((flight, index) => {
      // The lane carries the plan's proposed status; a cancellation is shown only once it is carried out.
      const cancelled = m.executed && flight.status === "cancelled";
      const moved = m.executed && flight.planned_local && !cancelled;
      const time = moved ? flight.planned_local : flight.std_local;
      const at = minutesOf(time, day);
      if (at === null) return "";
      const label = index === 0 ? `${flight.flight_no} ${hhmm(time)}` : flight.flight_no;
      const title = `${flight.flight_no} ${hhmm(flight.std_local)}${moved && hhmm(flight.planned_local) !== hhmm(flight.std_local)
        ? ` → ${hhmm(flight.planned_local)}` : ""}${cancelled ? " · cancelled" : ""}`;
      return `<div class="tw-dot ${index === 0 ? "first" : "ghost"}${cancelled ? " cancelled" : ""}" style="left:${pct(at)}%"
        title="${esc(title)}"><i></i><span>${esc(label)}</span></div>`;
    }).join("");
    return `<div class="tw-lane" data-tone="${m.tone}"><div class="tw-who">${esc(name)}<small>${esc(rest.join(" · "))}</small></div>
      <div class="tw-track">${band}${dots}</div></div>`;
  }
  function timeline(models, now, day, demoTime) {
    const scale = [9, 12, 15, 18, 21, 24].map((h, i, all) =>
      `<span class="${i === 0 ? "start" : i === all.length - 1 ? "end" : ""}" style="left:${pct(h * 60)}%">${String(h % 24).padStart(2, "0")}:00</span>`).join("");
    const present = new Set(models.map(m => m.tone));
    const legend = LEGEND.filter(([tone]) => present.has(tone))
      .map(([tone, label]) => `<span data-tone="${tone}">${esc(label)}</span>`).join("");
    const ordered = [...models].sort(byUrgency);
    return `<section class="tw-card" aria-labelledby="tw-time-title">
      <div class="tw-card-head"><h2 id="tw-time-title">Affected departures</h2><div class="tw-legend">${legend}</div></div>
      <div class="tw-scroll"><div class="tw-tl">
        <div class="tw-axis"><span></span><div class="tw-scale">${scale}</div></div>
        ${ordered.map(m => lane(m, day)).join("")}
        ${now === null ? "" : `<div class="tw-overlay"><div class="tw-now" style="left:${pct(now)}%"><span>Now ${esc(demoTime)}</span></div></div>`}
      </div></div>
    </section>`;
  }

  function departure(m, now) {
    const first = m.row.first_departure;
    if (!first) return '<span class="tw-none">–</span>';
    const original = hhmm(first.std_local);
    if (m.executed) {
      if (!first.planned_local) return `<b class="num">${esc(first.flight_no)} ${esc(original)}</b><small>executed · cancelled</small>`;
      const shift = m.original === null ? null : m.departure - m.original;
      const words = shift === null ? "executed" : shift === 0 ? "executed · on time"
        : `executed · ${shift > 0 ? "+" : "−"}${Math.abs(shift)} min`;
      return `<b class="num">${esc(first.flight_no)} ${esc(hhmm(first.planned_local))}</b><small>${esc(words)}</small>`;
    }
    let sub = "";
    if (now !== null && m.original !== null) {
      const left = m.original - now;
      sub = left > 0 ? `<small class="${left <= 60 ? "soon" : ""}">in ${esc(span(left))}</small>` : `<small>${esc(span(-left))} ago</small>`;
    }
    return `<b class="num">${esc(first.flight_no)} ${esc(original)}</b>${sub}`;
  }
  function plan(m) {
    const impact = m.impact;
    const total = number(impact.actions_total) || 0;
    const name = RecoveryView.planName({ kind: impact.plan_kind, title: impact.plan_title,
      flights: (impact.plan_cancelled || []).map(flight_no => ({ flight_no, status: "cancelled" })) });
    if (total) {
      // Who authorised it, not the plan's route: OCC may approve an automatic plan handed to it.
      const by = impact.authorised_by === "policy" ? "Ran automatically · " : impact.authorised_by === "occ" ? "OCC approved · " : "";
      // A storm held until confirmed or stood down (case summary and held storm spec §2): what waits and by when, or that
      // nothing was carried out.
      if (m.storm === "held") return `${esc(name || "Plan")}<small>${by}${esc(m.row.storm?.decide_by ? `Held · decide by ${m.row.storm.decide_by}` : "Held until confirmed")}</small>`;
      if (m.storm === "stood_down") return `${esc(name || "Plan")}<small>${by}Stood down</small>`;
      return `${esc(name || "Plan")}<small>${by}${number(impact.actions_done) || 0} of ${total} actions</small>`;
    }
    if (m.blocked) return `No plan allowed<small>${esc(plural(number(impact.plans) || 0, "plan"))} · manual action</small>`;
    if (impact.plan_title) {
      return `${esc(name)}<small>${esc(plural(number(impact.plans) || 0, "plan"))}${ROUTE[impact.route] ? ` · ${ROUTE[impact.route]}` : ""}</small>`;
    }
    return '<span class="tw-none">–</span>';
  }
  // Owner: passengers still waiting for Passenger Services read as an action needed, in amber in the tower.
  const reviewOf = (m) => (m.row.label === "Passenger Services review" ? number(m.impact.review_passengers) || 0 : 0);
  function status(m) {
    const review = reviewOf(m);
    return review ? RecoveryView.actionNeeded(review) : (m.row.label || m.row.status || "");
  }
  const statusCell = (m) => `<span class="tw-state${reviewOf(m) ? " act" : m.storm === "held" ? " held" : ""}">${esc(status(m))}</span>`;
  function source(row) { return row.source === "OCC" ? "Reported in ARROW" : (row.source || ""); }
  // A weather case in a list: its subject and window ("Strong crosswind at Lisbon · 16:00–18:00"); the case's title now also
  // carries the gusts and the rate, which the case shows. Any other case by its headline.
  function shortHeadline(row) {
    const text = String(row?.headline || row?.display_id || "");
    return row?.disruption_type === "weather" ? text.split(" · ").slice(0, 2).join(" · ") : text;
  }
  // The window stays on one line (Task 4c), in the tower, Today and the map list.
  function headline(row) {
    const [subject, window] = shortHeadline(row).split(" · ");
    return row.disruption_type === "weather" && window ? `${esc(subject)} · <span class="tw-keep">${esc(window)}</span>` : esc(shortHeadline(row));
  }
  function table(models, now) {
    const seg = SORTS.map(([key, label]) =>
      `<button type="button" data-sort="${key}" aria-pressed="${key === sortKey}">${esc(label)}</button>`).join("");
    const head = `<div class="tw-card-head"><h2 id="tw-table-title">Disruptions today</h2>
      <div class="tw-seg" role="group" aria-label="Sort">${seg}</div></div>`;
    if (!models.length) {
      return `<section class="tw-card" aria-labelledby="tw-table-title">${head}
        <div class="tw-empty"><b>No disruptions today</b><span>System events and reports open cases here by themselves.</span></div></section>`;
    }
    const rows = [...models].sort(ORDER[sortKey]).map(m => `<tr data-trace="${esc(m.row.trace_id)}" data-tone="${m.tone}">
      <td class="tw-what"><div class="tw-head"><i></i><div><strong>${headline(m.row)}</strong><small>${esc(source(m.row))}</small></div></div></td>
      <td class="tw-status">${statusCell(m)}</td>
      <td class="tw-dep">${departure(m, now)}</td>
      <td class="tw-pax r num" data-label="Passengers">${m.passengers === null ? '<span class="tw-none">–</span>' : esc(count.format(m.passengers))}</td>
      <td class="tw-cost r num" data-label="Est. cost">${m.cost === null ? '<span class="tw-none">–</span>' : esc(euro.format(m.cost))}</td>
      <td class="tw-plan">${plan(m)}</td>
      <td class="tw-go"><button type="button" class="tw-map" data-map="${esc(m.row.trace_id)}" aria-label="Show on map" title="Show on map"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 4 3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5z"/><path d="M9 4v13.5M15 6.5V20"/></svg></button><button type="button" class="tw-open" data-trace="${esc(m.row.trace_id)}">Open →</button></td>
    </tr>`).join("");
    return `<section class="tw-card" aria-labelledby="tw-table-title">${head}
      <table class="tw-table"><thead><tr><th>Disruption</th><th>Status</th><th>First affected departure</th>
        <th class="r">Passengers</th><th class="r">Est. cost</th><th>Plan</th><th><span class="sr-only">Open</span></th></tr></thead>
        <tbody id="tower-rows">${rows}</tbody></table></section>`;
  }

  function dayLabel(day) {
    const time = Date.parse(`${day}T12:00:00Z`);
    return Number.isFinite(time) ? new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short",
      timeZone: "UTC" }).format(new Date(time)) : (day || "");
  }

  // Hide a later label that would overlap the one before it, and keep labels inside the track.
  function layout(container) {
    container.querySelectorAll(".tw-track").forEach(track => {
      const box = track.getBoundingClientRect();
      if (!box.width) return;
      let right = -Infinity;
      [...track.querySelectorAll(".tw-dot")].sort((a, b) => a.offsetLeft - b.offsetLeft).forEach(dot => {
        dot.classList.remove("bare", "flip");
        if (dot.getBoundingClientRect().right > box.right) dot.classList.add("flip");
        const rect = dot.getBoundingClientRect();
        if (rect.left < right + 2) dot.classList.add("bare");
        right = Math.max(right, dot.getBoundingClientRect().right);
      });
    });
  }

  function bind(container) {
    if (bound.has(container)) return;
    bound.add(container);
    container.addEventListener("click", (event) => {
      const sort = event.target.closest("[data-sort]");
      if (sort) {
        sortKey = sort.dataset.sort;
        if (last) render(...last);
        return;
      }
      if (event.target.closest("[data-open-rules]")) {
        container.dispatchEvent(new CustomEvent("open-rules", { bubbles: true }));
        return;
      }
      const map = event.target.closest("[data-map]");
      if (map) {
        container.dispatchEvent(new CustomEvent("show-map", { bubbles: true, detail: { trace: map.dataset.map } }));
        return;
      }
      const row = event.target.closest("[data-trace]");
      if (row) container.dispatchEvent(new CustomEvent("open-case", { bubbles: true, detail: { trace: row.dataset.trace } }));
    });
  }

  function render(container, rows, session, rules) {
    last = [container, rows, session, rules];
    bind(container);
    const day = session?.day || "";
    const demoTime = session?.demo_time || "";
    const now = clockMinutes(demoTime);
    const models = (Array.isArray(rows) ? rows : []).map(row => model(row, day));
    container.innerHTML = `<div class="tw">
      <div class="tw-page-head">
        <div><h1>Control tower</h1><p>Lisbon hub · ${esc(dayLabel(day))}${demoTime ? ` · <span class="clock"${Tips.attrs(esc, "The development environment replays one operating day; this is its time now.", "Operating clock")}>${esc(demoTime)}</span>` : ""}</p></div>
        ${rules?.pending ? '<button class="tw-chip" type="button" data-open-rules>Rule change waiting for a second approver</button>' : ""}
      </div>
      ${kpis(models)}
      <div class="tw-stack">${timeline(models, now, day, demoTime)}${table(models, now)}</div>
    </div>`;
    requestAnimationFrame(() => layout(container));
  }

  return { render, layout, model, status, byUrgency, headline, shortHeadline };
})();
