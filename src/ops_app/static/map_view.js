"use strict";
// Operations map (operations map spec §3): the network at a glance with today's disruptions.
// Reads /api/network and the control tower's /api/cases rows; drawing lives in map_draw.js.

class MapView {
  constructor(api, { openCase, openTower }) {
    this.api = api;
    this.openCase = openCase;
    this.openTower = openTower;
    this.data = null;
    this.rows = [];
    this.rowsKey = "";
    this.session = null;
    this.legs = [];
    this.airports = {};
    this.now = 585;
    this.t = 585;
    this.cam = { x: 0, y: 0, s: 1 };
    this.fitted = false;
    this.free = false;
    this.anim = 0;
    this.hot = null;
    this.loading = null;
    this.playing = 0;
    this.drag = null;
    this.moved = false;
    // Focus: a case ("case"), an aircraft found or clicked ("aircraft"). Beside it, one flight card or route card at a time
    // (Task 3b): this.flight is the flight whose card is open, this.route the route whose flights are listed.
    this.mode = null;
    this.trace = null;
    this.view = null;
    this.viewError = null;
    this.planId = null;
    this.registration = null;
    this.flight = null;
    this.route = null;
    this.focusLegs = [];
    // A storm case's own reading (title, window, rate) by trace, read once from the case (Task 4c).
    this.storms = new Map();
    // Each case's own plan flights (new arrival, minutes held in the air) by trace, read from the case when a flight card needs them.
    this.docs = new Map();
    this.cities = {};
    this.peeked = null;
    this.keyed = false;
    this.root = document.getElementById("map");
    this.root.innerHTML = this.skeleton();
    this.$ = (selector) => this.root.querySelector(selector);
    this.svg = this.$("#map-svg");
    this.bind();
    this.setTime(this.now);
  }

  skeleton() {
    const legend = ["defect", "delay", "weather", "other"].map(kind => `<span>${MapDraw.icon(kind, 14)}${MapDraw.KINDS[kind]}</span>`).join("");
    return `<div class="mp">
      <aside class="mp-side" aria-label="Disruptions today">
        <div class="mp-head">
          <button type="button" class="mp-back" data-go="tower">← Control tower</button>
          <h2>Disruptions today</h2>
          <div class="mp-counts" id="map-counts"></div>
        </div>
        <div class="mp-list" id="map-list"></div>
        <div class="mp-legend">${legend}<span><i class="mp-ring"></i>Pulsing: needs a person</span></div>
        <p class="mp-note">Positions estimated from the timetable, not live tracking</p>
      </aside>
      <div class="mp-main">
        <div class="mp-stage" id="map-stage">
          <svg id="map-svg" class="mp-svg" role="img" aria-label="Network map: click an aircraft or a route for its flights; from the keyboard, use Find flight or aircraft">
            <g id="map-world"><path id="map-land" class="mp-land"/><g id="map-routes"></g></g>
            <g id="map-aps"></g><g id="map-traffic"></g><g id="map-rotation"></g><g id="map-markers"></g>
          </svg>
          <div class="mp-chips"><span class="mp-chip"><b class="num" id="map-flights">–</b> flights today</span>
            <span class="mp-chip"><b class="num" id="map-airborne">–</b> in the air</span>
            <span class="mp-chip"><b class="num" id="map-count">0</b> <span id="map-count-word">disruptions</span></span></div>
          <div class="mp-tools">
            <button type="button" data-zoom="1.5" title="Zoom in" aria-label="Zoom in">+</button>
            <button type="button" data-zoom="0.6667" title="Zoom out" aria-label="Zoom out">−</button>
            <button type="button" class="wide" data-fit="europe">Europe</button>
            <button type="button" class="wide" data-fit="world">Whole network</button>
            <input id="map-find" type="search" placeholder="Find flight or aircraft" aria-label="Find flight or aircraft" autocomplete="off"
              title="A flight number or an aircraft registration. Aircraft and routes on the map open with a click; from the keyboard, find them here.">
          </div>
          <div class="mp-status" id="map-status" role="status" hidden></div>
          <div class="mp-peek" id="map-peek" role="tooltip" hidden></div>
        </div>
        <div class="mp-dock" id="map-dock">
          <div class="mp-rot" id="map-rot" hidden><div class="mp-rot-head" id="map-rot-head"></div><svg id="map-strip" class="mp-strip"></svg></div>
          <div class="mp-time">
            <button type="button" id="map-play">▶ Play</button>
            <div class="mp-tlabel" id="map-time" aria-live="polite"></div>
            <div class="mp-range"><input id="map-slider" type="range" min="360" max="1800" step="5" value="585" aria-label="Time of day">
              <div class="mp-ticks" id="map-ticks"></div></div>
            <button type="button" id="map-now">Now</button>
          </div>
        </div>
      </div>
      <div class="mp-cards" id="map-cards">
        <aside class="mp-card" id="map-card" aria-live="polite" hidden></aside>
        <aside class="mp-fc" id="map-fc" aria-label="Flight" aria-live="polite" tabindex="-1" hidden></aside>
      </div>
    </div>`;
  }

  bind() {
    this.root.addEventListener("click", (event) => {
      if (event.target.closest("[data-go=tower]")) { this.openTower(); return; }
      if (event.target.closest("#map-retry")) { if (this.data) this.reload(); else this.load(); return; }
      const zoom = event.target.closest("[data-zoom]");
      if (zoom) { this.zoomAt(Number(zoom.dataset.zoom)); return; }
      const fit = event.target.closest("[data-fit]");
      if (fit) { this.free = fit.dataset.fit === "world"; this.fly(this.free ? this.fitBox(this.WORLD, 30) : this.home()); return; }
      if (event.target.closest("#map-play")) { this.play(); return; }
      if (event.target.closest("#map-now")) { this.setTime(this.now); return; }
      if (event.target.closest("[data-close]")) { this.clear(); return; }
      if (event.target.closest("[data-fc-close]")) { this.closeFlight(); return; }
      if (event.target.closest("[data-fc-back]")) { this.backToRoute(); return; }
      if (event.target.closest("[data-open-case]")) { this.openCase(this.trace); return; }
      if (event.target.closest("[data-retry-case]")) { this.focus(this.trace); return; }
      const aircraft = event.target.closest("[data-aircraft-open]");
      if (aircraft) { this.focusAircraft(aircraft.dataset.aircraftOpen, this.flight?.no || null); return; }
      const listed = event.target.closest("[data-flight-open]");
      if (listed) { this.openFlight(listed.dataset.flightOpen, false, listed.closest("#map-fc") ? this.route : null); return; }
      const choice = event.target.closest("[data-plan]");
      if (choice) { this.planId = choice.dataset.plan; this.refocus(); return; }
      // A flight drawn on the map (an aircraft, its number, a leg's label or line) opens its card; a route line lists its flights.
      const drawn = event.target.closest("#map-svg [data-flight]");
      if (drawn && !this.moved) { this.openFlight(drawn.dataset.flight, drawn.matches(".mp-ac, .mp-ac-no")); return; }
      const route = event.target.closest("#map-svg [data-route]");
      if (route && !this.moved) { this.openRoute(route.dataset.route, route.dataset.kind === "plan" ? "plan" : "network"); return; }
      const target = event.target.closest("[data-case]");
      if (target && !this.moved) this.select(target.dataset.case);
    });
    this.$("#map-find").addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); this.find(event.target.value); }
    });
    // Markers, leg labels and route lines are drawn as SVG buttons: Enter and Space act as a click.
    this.svg.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const button = event.target.closest('[role="button"]');
      if (!button) return;
      event.preventDefault();
      this.moved = false;
      this.keyed = true;
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      this.keyed = false;
    });
    // Hovering or focusing a flight or a route previews it (Task 3b).
    this.svg.addEventListener("pointermove", (event) => this.peek(event));
    this.svg.addEventListener("pointerleave", () => this.peek(null));
    this.svg.addEventListener("focusin", (event) => this.peek(event, true));
    this.svg.addEventListener("focusout", () => this.peek(null));
    this.$("#map-strip").addEventListener("click", (event) => {
      if (!this.stripScale) return;
      const box = this.$("#map-strip").getBoundingClientRect();
      this.setTime(this.stripScale(event.clientX - box.left));
    });
    // Esc closes the flight card first, then the case or aircraft.
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || this.root.hidden || document.querySelector("dialog[open]")) return;
      if (this.flight || this.route) this.closeFlight();
      else if (this.mode) this.clear();
    });
    this.$("#map-list").addEventListener("mouseover", (event) => {
      const item = event.target.closest("[data-case]");
      this.highlight(item ? item.dataset.case : null);
    });
    this.$("#map-list").addEventListener("mouseleave", () => this.highlight(null));
    this.$("#map-slider").addEventListener("input", (event) => this.setTime(Number(event.target.value)));
    this.svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      const box = this.svg.getBoundingClientRect();
      this.zoomAt(event.deltaY < 0 ? 1.2 : 1 / 1.2, event.clientX - box.left, event.clientY - box.top);
    }, { passive: false });
    this.svg.addEventListener("pointerdown", (event) => {
      this.drag = { x: event.clientX, y: event.clientY, cx: this.cam.x, cy: this.cam.y };
      this.moved = false;
    });
    window.addEventListener("pointermove", (event) => {
      if (!this.drag) return;
      const dx = event.clientX - this.drag.x, dy = event.clientY - this.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) { this.moved = true; this.svg.classList.add("dragging"); this.peek(null); }
      if (this.moved) { this.free = true; this.cam = { ...this.cam, x: this.drag.cx - dx / this.cam.s, y: this.drag.cy - dy / this.cam.s }; this.draw(); }
    });
    window.addEventListener("pointerup", () => {
      this.drag = null;
      this.svg.classList.remove("dragging");
      setTimeout(() => { this.moved = false; }, 0);
    });
    window.addEventListener("resize", () => {
      if (this.root.hidden) return;
      if (this.data && !this.free) this.cam = this.home();
      this.ticks();
      if (this.mode) this.drawStrip();
      this.draw();
      this.phoneFit();
      this.fade();
    });
    this.$("#map-card").addEventListener("scroll", () => this.fade(), true);
  }

  // ---------- Data ----------
  async open() {
    this.renderList();
    if (this.data) { this.ticks(); this.draw(); return; }
    await this.load();
  }

  async load() {
    if (this.loading) return this.loading;
    this.status("Loading today's schedule…");
    this.loading = (async () => {
      try {
        const data = await this.api("/api/network");
        if (!Array.isArray(data.flights) || !Array.isArray(data.airports) || !Array.isArray(data.aircraft)) {
          throw new Error("The schedule returned an incomplete network.");
        }
        this.data = data;
        this.prepare();
        this.status(null);
        this.ticks();
        this.cam = this.home();
        this.fitted = true;
        this.setTime(this.now);
        // A disruption opened while the schedule was unavailable is drawn now.
        if (this.mode) this.refocus(true);
      } catch (error) {
        this.status(`Schedule unavailable · ${error.message || "The schedule could not be read."}`, true);
      } finally {
        this.loading = null;
      }
    })();
    return this.loading;
  }
  // A quiet re-read that keeps the camera, the time and the open card; a failure says so and keeps the last schedule.
  async reload() {
    if (this.loading) return;
    try {
      const data = await this.api("/api/network");
      if (!Array.isArray(data.flights) || !Array.isArray(data.airports) || !Array.isArray(data.aircraft)) {
        throw new Error("The schedule returned an incomplete network.");
      }
      this.data = data;
      this.prepare();
      this.status(null);
      if (this.mode) this.refocus(); else this.draw();
      this.renderFlight();
    } catch (error) {
      this.status(`Schedule not refreshed · the map may not show the latest changes. ${error.message || ""}`.trim(), true);
    }
  }

  prepare() {
    const day = this.data.operating_date;
    this.airports = {};
    this.cities = {};
    for (const airport of this.data.airports) {
      const position = airport.position;
      if (airport.iata && Array.isArray(position) && position.length === 2) this.airports[airport.iata] = MapDraw.project(position);
      // A city the schedule names wins over the map's own list.
      if (airport.iata) this.cities[airport.iata] = typeof airport.city === "string" && airport.city.trim() ? airport.city.trim() : MapDraw.CITY[airport.iata] || "";
    }
    // The current times, and the published timetable they may have moved from (an executed change).
    this.legs = this.data.flights.map(f => {
      const std = MapDraw.minutes(f.std_local, day), sta = MapDraw.minutes(f.sta_local, day);
      return { no: f.flight_no, from: f.origin, to: f.destination, reg: f.aircraft_reg, std, sta, status: f.status,
        pstd: MapDraw.minutes(f.published_std_local, day) ?? std, psta: MapDraw.minutes(f.published_sta_local, day) ?? sta };
    }).filter(leg => leg.std !== null && leg.sta !== null);
    this.fleet = new Map(this.data.aircraft.map(a => [a.registration, a]));
    this.now = MapDraw.minutes(this.data.as_of_local, day) ?? this.now;
    const box = (lon0, lat0, lon1, lat1) => { const a = MapDraw.project([lon0, lat1]), b = MapDraw.project([lon1, lat0]); return [a[0], a[1], b[0], b[1]]; };
    this.EUROPE = box(-30, 28, 22, 58);
    this.IBERIA = box(-28, 30, 4, 46);
    this.WORLD = box(-92, -30, 36, 60);
    this.$("#map-land").setAttribute("d", MapDraw.landPath(typeof LAND === "undefined" ? [] : LAND));
    this.$("#map-routes").innerHTML = MapDraw.routes(this.legs, this.airports);
    this.marked = null;
    this.markRoute();
    this.$("#map-flights").textContent = String(this.data.flights.length);
  }

  status(message, retry = false) {
    const box = this.$("#map-status");
    box.hidden = !message;
    box.innerHTML = message ? `<span>${MapDraw.esc(message)}</span>${retry ? '<button type="button" id="map-retry" class="secondary">Retry</button>' : ""}` : "";
  }

  setCases(rows, session) {
    this.session = session;
    const key = JSON.stringify([rows, session?.day]);
    if (key === this.rowsKey) return;
    this.rowsKey = key;
    const day = session?.day || "";
    const listed = this.mode === "case" && this.trace && this.modelOf(this.trace);
    this.rows = (Array.isArray(rows) ? rows : []).filter(row => row.state !== "closed")
      .map(row => TowerView.model(row, day)).sort(TowerView.byUrgency);
    if (!this.root.hidden) { this.renderList(); this.draw(); }
    // An executed action, or a Reset that closes cases and reloads the day, changes the schedule: read it again.
    const done = JSON.stringify(this.rows.filter(m => Number(m.row.impact?.actions_done) > 0)
      .map(m => [m.row.trace_id, m.row.impact.actions_done]).sort());
    if (this.doneKey !== undefined && done !== this.doneKey && this.data) this.reload();
    this.doneKey = done;
    // A Reset closes the open case: its card closes with it.
    if (listed && !this.modelOf(this.trace)) { this.clear(); return; }
    // The open card follows the refresh: read the focused case again when its row changed.
    if (this.mode === "case" && this.trace && this.view && this.rowKey(this.trace) !== this.focusKey) this.focus(this.trace, true);
  }
  rowKey(trace) { return JSON.stringify(this.modelOf(trace)?.row ?? null); }

  // ---------- Disruption list ----------
  renderList() {
    const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
    const esc = MapDraw.esc;
    this.$("#map-count").textContent = String(this.rows.length);
    this.$("#map-count-word").textContent = this.rows.length === 1 ? "disruption" : "disruptions";
    const count = (tones) => this.rows.filter(m => tones.includes(m.tone)).length;
    this.$("#map-counts").innerHTML = [[["needs", "manual", "stopped"], "needs", "need OCC"], [["expert"], "expert", "with Passenger Services"],
      [["progress"], "progress", "in progress"], [["handled"], "handled", "handled"]]
      .map(([tones, tone, label]) => [count(tones), tone, label]).filter(([value]) => value > 0)
      .map(([value, tone, label]) => `<span class="mp-count" data-tone="${tone}"><b>${value}</b> ${label}</span>`).join("");
    if (!this.rows.length) {
      this.$("#map-list").innerHTML = '<p class="mp-empty">No disruptions today.</p>';
      return;
    }
    this.$("#map-list").innerHTML = this.rows.map(m => {
      const row = m.row;
      const facts = !row.station ? "<span>No location</span>"
        : m.blocked ? "<span>Plan blocked</span>"
        : m.passengers === null ? "" : `<span><b class="num">${esc(m.passengers)}</b> lose flight or connection</span>${m.cost === null ? "" : `<span><b class="num">${esc(euro.format(m.cost))}</b></span>`}`;
      return `<button type="button" class="mp-item" data-case="${esc(row.trace_id)}" data-tone="${esc(m.tone)}" aria-pressed="false"
        style="--c:${MapDraw.TONES[m.tone] || MapDraw.TONES.progress}">
        <span class="mp-badge">${MapDraw.icon(MapDraw.kindOf(row.disruption_type))}</span>
        <span class="mp-text"><b>${TowerView.headline(row)}</b><span class="mp-state">${esc(TowerView.status(m))}</span>
          <span class="mp-facts">${facts}</span></span></button>`;
    }).join("");
    this.markPressed();
  }

  highlight(trace) {
    if (trace === this.hot) return;
    this.hot = trace;
    this.drawMarkers();
  }

  select(trace) {
    this.focus(trace);
  }

  // ---------- Focus ----------
  modelOf(trace) { return this.rows.find(m => m.row.trace_id === trace) || null; }
  planOf() {
    const plans = this.view?.plans || [];
    return plans.find(p => p.option_id === this.planId) || null;
  }
  // quiet: a refresh of the open case; keeps the card, the chosen plan and the camera.
  async focus(trace, quiet = false) {
    const keep = quiet && this.trace === trace ? this.planId : null;
    this.mode = "case";
    this.trace = trace;
    this.focusKey = this.rowKey(trace);
    if (!quiet) {
      this.view = null;
      this.viewError = null;
      this.flight = null;
      this.route = null;
      this.registration = null;
      this.focusLegs = [];
      this.markPressed();
      this.renderCard();
      this.renderFlight();
      this.drawStrip();
      this.draw();
    }
    if (this.loading) await this.loading;
    try {
      const view = await this.api(`/api/cases/${encodeURIComponent(trace)}/map`);
      if (this.trace !== trace || this.mode !== "case") return;
      this.view = view;
    } catch (error) {
      if (this.trace !== trace || quiet) return;
      this.viewError = error.message || "The service could not be reached.";
      this.renderCard();
      return;
    }
    // The plan carried out comes first, then the recommended one: the map shows what really ran.
    const plans = this.view.plans || [];
    const chosen = plans.find(p => p.option_id === keep) || plans.find(p => p.execution) || plans.find(p => p.recommended) || plans[0];
    this.planId = chosen?.option_id || null;
    // A storm has no single aircraft: the view's aircraft is only its first affected flight's (Task 4c).
    this.registration = this.isStorm() ? null : this.view.aircraft_reg || null;
    if (this.isStorm()) this.readCase(trace).then(read => { if (read && this.trace === trace) { this.renderCard(); this.draw(); this.renderFlight(); } });
    this.refocus(!quiet);
  }
  isStorm() { return this.mode === "case" && this.view?.disruption_type === "weather"; }
  // The case itself, read once for each version of its row: a storm's window and rate (until read, or if it cannot be, the ring is
  // named by the headline), and each plan's flights as ARROW planned them, with their new arrival and minutes held in the air, which
  // the map view does not carry (without them a card times the arrival by the timetable's block). Resolves true once read.
  readCase(trace) {
    const key = this.rowKey(trace), known = this.docs.get(trace);
    if (known && known.key === key) return known.ready;
    const entry = { key, plans: null };
    entry.ready = (async () => {
      try {
        const doc = await this.api(`/api/cases/${encodeURIComponent(trace)}`);
        entry.plans = new Map((Array.isArray(doc?.options) ? doc.options : []).filter(o => o && o.option_id && Array.isArray(o.flights))
          .map(o => [o.option_id, new Map(o.flights.filter(f => f && typeof f.flight_no === "string").map(f => [f.flight_no, f]))]));
        const storm = RecoveryView.storm(doc?.disruption);
        if (storm) this.storms.set(trace, storm);
        return true;
      } catch {
        // Read again next time; nothing else depends on it.
        if (this.docs.get(trace) === entry) this.docs.delete(trace);
        return false;
      }
    })();
    this.docs.set(trace, entry);
    return entry.ready;
  }
  stormTip() {
    const storm = this.storms.get(this.trace);
    if (!storm) return { title: String(this.view?.headline || "Weather"), text: "", tag: "" };
    return { title: [storm.hazard, storm.window].filter(Boolean).join(" "), tag: storm.window, overlay: storm.overlay,
      text: [storm.across ? `${storm.across[0].toUpperCase()}${storm.across.slice(1)}` : "", storm.rate ? `${storm.place} at ${storm.rate}` : "", storm.basis]
        .filter(Boolean).join("; ") };
  }
  // What a plan is now: carried out, being carried out, stopped, not chosen, blocked or a proposal.
  planState(plan) {
    if (!plan) return { tone: "progress", colour: MapDraw.TONES.progress, tag: "", who: "", line: null, strip: null };
    const decided = (this.view?.plans || []).some(p => p.execution);
    const run = plan.execution;
    const confirmed = run ? `${run.actions_done} of ${run.actions_total} actions confirmed — see the case.` : "";
    // A storm plan held until confirmed, or stood down (case summary and held storm spec §2): neither carried out nor being
    // carried out; nothing it would move is drawn as done.
    if (run?.status === "held") {
      return { tone: "progress", colour: MapDraw.TONES.progress, tag: "Held until confirmed", who: "Held until confirmed",
        line: "Held until confirmed · passengers warned, standby crews on alert; no flight moved yet — see the case.", strip: "Held plan" };
    }
    if (run?.status === "stood_down") {
      return { tone: "muted", colour: MapDraw.TONES.muted, tag: "Stood down", who: "Stood down",
        line: "Stood down · nothing changed — the forecast dropped; see the case.", strip: "Stood down" };
    }
    if (plan.executed) {
      return { tone: "handled", colour: MapDraw.TONES.handled, tag: "Done", who: plan.authorised_by === "policy" ? "Ran automatically" : "OCC approved",
        line: "Executed by ARROW — receipts are in the case.", strip: "Now flying" };
    }
    if (run && ["needs_attention", "failed"].includes(run.status)) {
      return { tone: "stopped", colour: MapDraw.TONES.stopped, tag: "Stopped", who: "Stopped", line: `Stopped · ${confirmed}`, strip: "Stopped plan" };
    }
    if (run) {
      return { tone: "progress", colour: MapDraw.TONES.progress, tag: "Being carried out", who: "Being carried out",
        line: `Being carried out · ${confirmed}`, strip: "Being carried out" };
    }
    if (decided) {
      return { tone: "muted", colour: MapDraw.TONES.muted, tag: "Not chosen", who: "Not chosen",
        line: "Not chosen — another plan was carried out; see the case.", strip: "Not chosen" };
    }
    if (plan.route === "blocked") {
      return { tone: "stopped", colour: MapDraw.TONES.stopped, tag: "Blocked", who: "Blocked", line: "Blocked — nothing has changed in the airline systems.",
        strip: "Blocked plan" };
    }
    return { tone: plan.route === "auto" ? "progress" : "needs", colour: MapDraw.TONES.needs, tag: "Proposal",
      who: plan.route === "auto" ? "Runs automatically" : "OCC decides", line: null, strip: "Proposed plan" };
  }
  // Redraw the focused day for the selected plan; fly to it the first time.
  refocus(fly = false) {
    const day = this.data?.operating_date || this.session?.day || "";
    this.focusLegs = this.registration || this.planOf() ? MapDraw.dayLegs(this.legs, this.registration, this.planOf(), day) : [];
    this.renderCard();
    this.renderFlight();
    this.drawStrip();
    if (fly) this.flyToFocus();
    this.draw();
  }
  focusAircraft(registration, flightNo = null) {
    this.mode = "aircraft";
    this.trace = null;
    this.view = null;
    this.viewError = null;
    this.planId = null;
    this.registration = registration;
    this.flight = null;
    this.route = null;
    this.markPressed();
    this.refocus(true);
    if (flightNo) this.openFlight(flightNo);
  }
  flyToFocus() {
    // A storm is about its airport: the camera frames the airport and its neighbours; the routes run on out of view.
    const station = this.isStorm() ? this.airports[this.view.station] : null;
    if (station && this.data) {
      this.free = true;
      requestAnimationFrame(() => this.fly(this.fitBox([station[0] - 6, station[1] - 4.5, station[0] + 6, station[1] + 4.5], 40)));
      return;
    }
    const points = this.focusLegs.filter(l => !l.absent).flatMap(l => [this.airports[l.from], this.airports[l.to]]).filter(Boolean);
    if (!points.length || !this.data) return;
    const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
    const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys), pad = Math.max(w, h, 2.5) * 0.14;
    this.free = true;
    requestAnimationFrame(() => this.fly(this.fitBox([Math.min(...xs) - pad, Math.min(...ys) - pad, Math.max(...xs) + pad, Math.max(...ys) + pad], 80)));
  }
  find(value) {
    const query = String(value || "").trim().toUpperCase();
    if (!query || !this.data) return;
    const flight = this.legs.find(l => String(l.no).toUpperCase() === query);
    if (flight) { this.focusAircraft(flight.reg, flight.no); return; }
    const aircraft = this.legs.find(l => String(l.reg).toUpperCase() === query);
    if (aircraft) { this.focusAircraft(aircraft.reg); return; }
    this.status(`No flight or aircraft "${query}" in today's schedule.`);
    setTimeout(() => this.status(null), 4000);
  }
  // One flight card at a time (Task 3b). From an aircraft on the map outside a case, the aircraft's day opens with it; back returns to
  // the route it was chosen from.
  async openFlight(flightNo, fromTraffic = false, back = null) {
    const leg = this.legs.find(l => l.no === flightNo);
    if (!leg) return;
    if (fromTraffic && this.mode !== "case") { this.focusAircraft(leg.reg, flightNo); return; }
    const keyed = this.keyed;
    this.route = null;
    this.flight = { no: flightNo, details: null, error: null, loading: true, back };
    this.showFlight(keyed);
    const trace = this.mode === "case" ? this.trace : null;
    if (trace) this.readCase(trace).then(read => { if (read && this.flight?.no === flightNo && this.trace === trace) this.renderFlight(); });
    try {
      const details = await this.api(`/api/flights/${encodeURIComponent(flightNo)}`);
      if (this.flight?.no !== flightNo) return;
      this.flight = { ...this.flight, details, loading: false };
    } catch (error) {
      if (this.flight?.no !== flightNo) return;
      this.flight = { ...this.flight, error, loading: false };
    }
    this.renderFlight();
  }
  // A route's flights: today's on a network route, or those the shown plan changes on a storm's route.
  openRoute(key, kind = "network") {
    const [a, b] = String(key || "").split("-");
    if (!a || !b) return;
    const keyed = this.keyed;
    this.flight = null;
    this.route = { a, b, kind };
    this.showFlight(keyed);
    const trace = kind === "plan" && this.mode === "case" ? this.trace : null;
    if (trace) this.readCase(trace).then(read => { if (read && this.route?.a === a && this.route?.b === b && this.trace === trace) this.renderFlight(); });
  }
  showFlight(keyed) {
    this.peek(null);
    this.renderFlight();
    this.draw();
    this.reveal();
    this.phoneFit(true);
    // Opened from the keyboard, the card takes the focus; Esc gives it back to the map.
    if (keyed) this.$("#map-fc").focus({ preventScroll: true });
  }
  backToRoute() {
    if (!this.flight?.back) return;
    this.route = this.flight.back;
    this.flight = null;
    this.renderFlight();
    this.draw();
  }
  closeFlight() {
    const no = this.flight?.no, route = this.route || this.flight?.back || null, inside = this.$("#map-fc").contains(document.activeElement);
    this.flight = null;
    this.route = null;
    this.renderFlight();
    this.draw();
    if (!inside) return;
    // The focus goes back to what opened the card: the flight's own button on the map, else the storm route it was chosen from.
    const button = (no && this.svg.querySelector(`[role="button"][data-flight="${CSS.escape(no)}"]`))
      || (route?.kind === "plan" && this.svg.querySelector(`.mp-spread-g[data-route="${CSS.escape(MapDraw.routeKey(route.a, route.b))}"]`));
    if (button) button.focus({ preventScroll: true });
  }
  markPressed() {
    this.root.querySelectorAll("#map-list [data-case]").forEach(item =>
      item.setAttribute("aria-pressed", String(this.mode === "case" && item.dataset.case === this.trace)));
  }

  // ---------- Cards ----------
  renderCard() {
    const card = this.$("#map-card");
    this.root.classList.toggle("mp-focused", Boolean(this.mode));
    card.hidden = !this.mode;
    this.cardsOpen();
    if (!this.mode) { card.innerHTML = ""; return; }
    card.innerHTML = this.mode === "case" ? this.caseCard() : this.aircraftCard();
    this.svg.classList.toggle("dim", Boolean(this.mode));
    this.fade();
  }
  // A case card squeezed by the flight card below it fades out where more of it scrolls into view.
  fade() {
    const body = this.$("#map-card .mp-card-body");
    if (body) body.classList.toggle("mp-more", body.scrollTop + body.clientHeight < body.scrollHeight - 2);
  }
  cardsOpen() {
    this.$("#map-cards").classList.toggle("open", Boolean(this.mode || this.flight || this.route));
  }
  plane(registration) {
    const aircraft = this.fleet?.get(registration);
    return [registration, aircraft?.fleet_type, aircraft?.seats ? `${aircraft.seats} seats` : ""].filter(Boolean).join(" · ");
  }
  caseCard() {
    const esc = MapDraw.esc, m = this.modelOf(this.trace), row = m?.row || this.view || {};
    const tone = m?.tone || "progress";
    const head = (headline, sub, label) => `<div class="mp-card-head" data-tone="${esc(tone)}" style="--c:${MapDraw.TONES[tone]}">
      <span class="mp-badge">${MapDraw.icon(MapDraw.kindOf(row.disruption_type), 22)}</span>
      <div><h3>${esc(headline)}</h3><small>${esc(sub)}${label ? ` <span class="mp-pill">${esc(label)}</span>` : ""}</small></div>
      <button type="button" class="mp-close" data-close aria-label="Back to all disruptions">✕</button></div>`;
    const foot = `<div class="mp-card-foot"><button type="button" class="secondary" data-close>← All disruptions</button>
      <button type="button" class="primary" data-open-case>Open case →</button></div>`;
    if (!this.view) {
      const body = this.viewError
        ? `<div class="mp-error"><span>The plans could not be read. ${esc(this.viewError)}</span><button type="button" class="secondary" data-retry-case>Retry</button></div>`
        : '<p class="mp-muted">Reading the plans…</p>';
      return `${head(TowerView.shortHeadline(row) || "Disruption", row.station || "", m ? TowerView.status(m) : "")}<div class="mp-card-body">${body}</div>${foot}`;
    }
    const view = this.view, plans = view.plans || [], plan = this.planOf();
    const where = [view.aircraft_reg && !this.isStorm() ? this.plane(view.aircraft_reg) : "", view.station || "No location"].filter(Boolean).join(" · ");
    const source = view.source === "OCC" ? "Reported in ARROW" : view.source || "";
    // When ARROW received it, as on the case page; the demo-clock time only when that is unknown.
    const received = Date.parse(view.created_at || "");
    const at = MapDraw.minutes(view.reported_at, String(view.reported_at || "").slice(0, 10));
    const reported = Number.isFinite(received)
      ? new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Lisbon" }).format(new Date(received))
      : at === null ? "" : MapDraw.clock(at);
    // A storm reads as the case page reads it; the forecast as received stays on hover.
    const storm = this.isStorm() ? this.storms.get(this.trace) : null;
    let body = `<p class="mp-report" title="${esc(view.report)}"><span>${esc(storm ? storm.reading : view.report || "")}</span><small>${esc([source, reported].filter(Boolean).join(" · "))}</small></p>`;
    const title = TowerView.shortHeadline({ ...view, disruption_type: view.disruption_type || m?.row.disruption_type });
    if (!plans.length) return `${head(title, where, m ? TowerView.status(m) : view.label)}<div class="mp-card-body">${body}<p class="mp-sec">Plan</p><p class="mp-muted">No plan yet</p></div>${foot}`;
    body += `<div><p class="mp-sec">${plans.length > 1 ? "Compare plans" : "Plan"}</p><div class="mp-plans">${plans.map(p => `<button type="button" class="mp-plan"
      data-plan="${esc(p.option_id)}" aria-pressed="${p.option_id === this.planId}" style="--pc:${this.planState(p).colour}"><span>${esc(RecoveryView.planName(p))}</span>
      <small>${p.recommended && plans.length > 1 ? '<b class="mp-rec">Recommended</b> · ' : ""}${this.planState(p).tag}</small></button>`).join("")}</div></div>`;
    const euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
    const figures = [[plan.passengers, "lose flight or connection"], [plan.cost_eur, "estimated cost", (v) => euro.format(v)], [plan.rebooked, "rebooked"],
      [plan.review, "to Passenger Services"]];
    if (plan.route !== "blocked" && figures.some(([v]) => v !== null && v !== undefined)) {
      body += `<div class="mp-nums">${figures.map(([value, label, format = String]) => `<div${value === null || value === undefined ? ' class="na"' : ""}>
        <b class="num">${value === null || value === undefined ? "—" : esc(format(value))}</b><span>${label}</span></div>`).join("")}</div>`;
    }
    // Without the schedule nothing can be drawn or called missing.
    if (!this.data) body += "<p class=\"mp-note-line\">Schedule unavailable — the plan's flights cannot be drawn.</p>";
    const absent = this.data ? this.focusLegs.filter(l => l.absent) : [];
    if (absent.length) body += `<ul class="mp-absent">${absent.map(l => `<li>${esc(l.flight_no)} not in today's schedule</li>`).join("")}</ul>`;
    const state = this.planState(plan);
    // The plan's reason is the orchestrator's own words: marked as AI, as on the case page.
    body += `<div class="mp-decide" style="--dc:${MapDraw.TONES[state.tone]}"><b>${state.who}</b>${plan.reason ? `<p>${TrailView.aiMark()} ${esc(plan.reason)}</p>` : ""}
      ${(plan.reasons || []).length ? `<div class="mp-reasons">${plan.reasons.map(r => `<span>${esc(r)}</span>`).join("")}</div>` : ""}</div>`;
    const changed = this.focusLegs.filter(l => !l.absent && (l.cancelled || l.delta || l.moved)).length;
    const count = this.data ? ` ${changed} flight${changed === 1 ? "" : "s"} would change.` : "";
    body += `<p class="mp-muted">${state.line || `Proposal only — nothing has changed in the airline systems.${count}`}</p>`;
    return `${head(title, where, m ? TowerView.status(m) : view.label)}<div class="mp-card-body">${body}</div>${foot}`;
  }
  aircraftCard() {
    const esc = MapDraw.esc, legs = this.focusLegs.filter(l => !l.absent);
    return `<div class="mp-card-head" data-tone="progress" style="--c:${MapDraw.TONES.progress}"><span class="mp-badge">✈</span>
      <div><h3>${esc(this.registration)}</h3><small>${esc(this.plane(this.registration))}</small></div>
      <button type="button" class="mp-close" data-close aria-label="Back to all disruptions">✕</button></div>
      <div class="mp-card-body"><p class="mp-sec">Today's flights</p><ul class="mp-legs">${legs.map(l => `<li><button type="button" class="mp-link"
        data-flight-open="${esc(l.flight_no)}">${esc(l.flight_no)}</button><span>${esc(l.from)} → ${esc(l.to)}</span><span class="num">${MapDraw.clock(l.std)}–${MapDraw.clock(l.sta)}</span></li>`).join("")}</ul></div>
      <div class="mp-card-foot"><button type="button" class="secondary" data-close>← All disruptions</button></div>`;
  }
  // ---------- Flight card and route card (Task 3b) ----------
  city(code) { return this.cities[code] || MapDraw.CITY[code] || ""; }
  // Lisbon, the hub, first; else as the route's key reads.
  routeEnds(route) { return route.b === "LIS" ? [route.b, route.a] : [route.a, route.b]; }
  routeName(route) { return this.routeEnds(route).map(code => this.city(code) || code).join(" ⇄ "); }
  // A flight as the map shows it: under the plan on screen in a case, else today's schedule (published → current). Minutes after
  // the day's midnight. The plan's own arrival and minutes held in the air come from the case once read; until then, or when it
  // cannot be read, a moved flight's arrival is its new departure plus the timetable's block time, marked as estimated, with no
  // minutes held or made up.
  facts(no) {
    const leg = this.legs.find(l => l.no === no);
    if (!leg) return null;
    const day = this.data?.operating_date || "";
    const plan = this.mode === "case" ? this.planOf() : null;
    const f = plan ? (plan.flights || []).find(x => x?.flight_no === no) || null : null;
    const own = f ? this.docs.get(this.trace)?.plans?.get(plan.option_id)?.get(no) || null : null;
    let cancelled, depWas, depNow, arrWas, arrNow, held = null, estimated = false;
    if (f) {
      cancelled = f.status === "cancelled";
      depWas = MapDraw.minutes(f.std_local, day) ?? leg.pstd;
      arrWas = depWas === leg.pstd ? leg.psta : depWas === leg.std ? leg.sta : depWas + (leg.psta - leg.pstd);
      depNow = cancelled ? null : MapDraw.minutes(f.planned_local, day) ?? depWas;
      const planned = own ? MapDraw.minutes(own.sta_local, day) : null;
      // The schedule already flies this departure (unchanged, or carried out): its arrival is the schedule's.
      estimated = !cancelled && planned === null && depNow !== leg.std;
      arrNow = cancelled ? null : planned ?? (estimated ? depNow + (arrWas - depWas) : leg.sta);
      if (Number.isSafeInteger(own?.held_airborne_minutes)) held = Math.max(0, own.held_airborne_minutes);
      else if (estimated) held = 0;
    } else {
      cancelled = leg.status === "cancelled";
      [depWas, arrWas] = [leg.pstd, leg.psta];
      [depNow, arrNow] = cancelled ? [null, null] : [leg.std, leg.sta];
    }
    // A longer flight than published holds in the air; a shorter one makes up minutes.
    const change = cancelled ? 0 : (arrNow - depNow) - (arrWas - depWas);
    const movedTo = f?.aircraft_reg && f.aircraft_reg !== leg.reg ? f.aircraft_reg : null;
    const plans = this.view?.plans || [];
    return { no, from: leg.from, to: leg.to, reg: movedTo || leg.reg, was: leg.reg, movedTo, cancelled, depWas, depNow, arrWas, arrNow, estimated,
      late: cancelled ? 0 : depNow - depWas, held: held ?? Math.max(0, change),
      madeUp: estimated ? 0 : held === null ? Math.max(0, -change) : Math.max(0, held - change),
      plan: f ? { number: plans.indexOf(plan) + 1, name: RecoveryView.planName(plan), state: this.planState(plan) } : null };
  }
  tags(x) {
    if (x.cancelled) return [["stopped", "Cancelled"]];
    const tags = [];
    if (x.movedTo) tags.push(["expert", `Moves to ${x.movedTo}`]);
    if (x.late > 0) tags.push(["needs", `Later by ${x.late} min`]);
    if (x.late < 0) tags.push(["needs", `Earlier by ${-x.late} min`]);
    if (x.held > 0) tags.push(["needs", `Held ${x.held} min in the air`]);
    if (x.madeUp > 0) tags.push(["muted", `Makes up ${x.madeUp} min in the air`]);
    return tags.length ? tags : [["handled", "On time"]];
  }
  short(x) {
    if (x.cancelled) return "Cancelled";
    return [x.late ? `${x.late > 0 ? "+" : "−"}${Math.abs(x.late)} min` : "", x.held ? `held ${x.held} min` : "", x.movedTo ? `on ${x.movedTo}` : ""]
      .filter(Boolean).join(" · ") || "On time";
  }
  airText(x) {
    if (x.cancelled || !(x.depNow <= this.t && this.t < x.arrNow)) return "";
    return this.t === this.now ? "In the air now" : `In the air at ${MapDraw.clock(this.t)}`;
  }
  // The flight's line between its airports: how far it has flown at the map's time, its block time or that it is in the air.
  track(x) {
    const esc = MapDraw.esc, air = this.airText(x);
    if (x.cancelled) return '<span class="mp-fc-line x"></span>';
    const k = Math.min(1, Math.max(0, (this.t - x.depNow) / Math.max(1, x.arrNow - x.depNow))), at = `${(k * 100).toFixed(1)}%`;
    const block = x.arrNow - x.depNow, time = block >= 60 ? `${Math.floor(block / 60)} h ${String(block % 60).padStart(2, "0")}` : `${block} min`;
    return `<span class="mp-fc-line"></span><span class="mp-fc-done" style="width:${at}"></span>
      <svg class="mp-fc-plane" style="left:${at}" viewBox="-8 -8 16 16" aria-hidden="true"><path d="${MapDraw.PLANE}" transform="rotate(90)"/></svg>
      <small class="${air ? "mp-fc-air" : ""}">${esc(air || time)}</small>`;
  }
  liveFlight() {
    const live = this.$("#map-fc-live"), x = this.flight ? this.facts(this.flight.no) : null;
    if (live && x) live.innerHTML = this.track(x);
  }
  renderFlight() {
    const box = this.$("#map-fc"), open = Boolean(this.flight || this.route);
    box.hidden = !open;
    box.innerHTML = this.flight ? this.flightCard() : this.route ? this.routeCard() : "";
    this.root.querySelector(".mp").classList.toggle("mp-fc-open", open);
    this.cardsOpen();
    this.markRoute();
    this.phoneFit();
    this.fade();
  }
  flightCard() {
    const esc = MapDraw.esc, clock = MapDraw.clock, f = this.flight, x = this.facts(f.no);
    const close = '<button type="button" class="mp-close" data-fc-close aria-label="Close the flight">✕</button>';
    const back = f.back ? `<button type="button" class="mp-fc-back" data-fc-back>← ${esc(this.routeName(f.back))}</button>` : "";
    if (!x) return `${back}<div class="mp-fc-head"><div><h3>${esc(f.no)}</h3><small>Not in today's schedule</small></div>${close}</div>`;
    const tags = this.tags(x).map(([tone, text]) => `<span class="mp-fc-tag" data-tone="${tone}">${esc(text)}</span>`).join("");
    const time = (was, now, est) => {
      if (x.cancelled) return `<s class="num">${clock(was)}</s>`;
      const shown = est ? `<b class="num mp-fc-est" title="Estimated from the timetable">${clock(now)}</b>` : `<b class="num">${clock(now)}</b>`;
      return was === now ? shown : `<span class="mp-fc-was num">${clock(was)}</span> → ${shown}`;
    };
    const end = (code, side, word, was, now, est = false) => `<div class="mp-fc-end ${side}"><b>${esc(code)}</b>${this.city(code) ? `<span>${esc(this.city(code))}</span>` : ""}
      <small>${word}</small><div class="mp-fc-time">${time(was, now, est)}</div></div>`;
    const type = this.fleet?.get(x.reg)?.fleet_type || "";
    // Outside a case the registration opens the aircraft's day.
    const reg = this.mode === "case" || (this.mode === "aircraft" && this.registration === x.reg) ? esc(x.reg)
      : `<button type="button" class="mp-link" data-aircraft-open="${esc(x.reg)}" title="Show the aircraft's day">${esc(x.reg)}</button>`;
    const facts = [`<div><dt>Aircraft</dt><dd>${reg}${type ? ` · ${esc(type)}` : ""}${x.movedTo ? ` <span class="mp-fc-muted">from ${esc(x.was)}</span>` : ""}</dd></div>`,
      this.passengers()].join("");
    const plan = x.plan ? `<div class="mp-fc-plan" style="--pc:${x.plan.state.colour}"><span class="mp-fc-name" title="${esc(`Plan ${x.plan.number} · ${x.plan.name}`)}"><b>Plan ${x.plan.number}</b> · ${esc(x.plan.name)}</span>
      ${x.plan.state.tag ? `<span class="mp-fc-state">${esc(x.plan.state.tag)}</span>` : ""}<button type="button" class="mp-link" data-open-case>Open case →</button></div>` : "";
    return `${back}<div class="mp-fc-head"><div class="mp-fc-tags"><h3>${esc(x.no)}</h3>${tags}</div>${close}</div>
      <div class="mp-fc-body"><div class="mp-fc-route">${end(x.from, "from", "Departs", x.depWas, x.depNow)}
        <div class="mp-fc-track${x.cancelled ? " x" : ""}" id="map-fc-live" title="Estimated from the timetable, not live tracking">${this.track(x)}</div>
        ${end(x.to, "to", "Arrives", x.arrWas, x.arrNow, x.estimated)}</div>
        <dl class="mp-fc-facts">${facts}</dl>${plan}</div>`;
  }
  // Passengers and connections from the passenger system, shown only when its figures add up; none recorded, nothing shown.
  passengers() {
    const esc = MapDraw.esc, f = this.flight, row = (value, title = "") =>
      `<div data-pax${title ? ` title="${esc(title)}"` : ""}><dt>Passengers</dt><dd>${value}</dd></div>`;
    if (f.loading) return row('<span class="mp-fc-muted">…</span>');
    if (f.error) return f.error.status === 404 ? "" : row('<span class="mp-fc-muted">Not available</span>', f.error.message || "The passenger system could not be reached.");
    const b = f.details?.flight?.flight_no === f.no ? f.details.booking : null;
    if (!b || b.status !== "available") return "";
    const counts = ["passenger_count", "seat_capacity", "held_seat_count", "available_seats", "connecting_passenger_count"].map(key => b[key]);
    const connections = b.connections;
    const complete = counts.every(v => Number.isSafeInteger(v) && v >= 0) && b.seat_capacity > 0
      && b.held_seat_count <= b.seat_capacity - b.passenger_count
      && b.available_seats === b.seat_capacity - b.passenger_count - b.held_seat_count
      && b.connecting_passenger_count <= b.passenger_count && b.flight_no === f.no && Array.isArray(connections)
      && connections.every(c => c && typeof c.flight_no === "string" && Number.isSafeInteger(c.passenger_count) && c.passenger_count >= 0);
    const total = complete ? connections.reduce((sum, c) => sum + c.passenger_count, 0) : -1;
    if (!complete || total !== b.connecting_passenger_count) return row('<span class="mp-fc-muted">Not available</span>', "The passenger figures do not add up; no totals are shown.");
    const seats = `${b.seat_capacity} seats · ${b.available_seats} free${b.held_seat_count ? ` · ${b.held_seat_count} held` : ""}`;
    const onward = connections.length ? `Connections: ${connections.map(c => `${c.flight_no} · ${c.passenger_count}`).join(", ")}` : "";
    return row(`<b class="num">${esc(b.passenger_count)}</b>${b.connecting_passenger_count ? ` · ${esc(b.connecting_passenger_count)} connecting` : ""}`,
      [seats, onward].filter(Boolean).join("\n"));
  }
  // The flights on a route: today's both ways on a network route, or those the plan on screen changes on a storm's route.
  routeFlights(route) {
    const on = (from, to) => (from === route.a && to === route.b) || (from === route.b && to === route.a);
    if (route.kind === "plan") return this.focusLegs.filter(l => !l.absent && on(l.from, l.to) && (l.cancelled || l.delta || l.moved)).map(l => l.flight_no);
    return this.legs.filter(l => on(l.from, l.to)).sort((p, q) => p.std - q.std || String(p.no).localeCompare(String(q.no))).map(l => l.no);
  }
  routeCard() {
    const esc = MapDraw.esc, clock = MapDraw.clock, route = this.route, [a, b] = this.routeEnds(route);
    const flights = this.routeFlights(route).map(no => this.facts(no)).filter(Boolean);
    const plan = route.kind === "plan" ? this.planOf() : null, count = `${flights.length} flight${flights.length === 1 ? "" : "s"}`;
    const sub = plan ? `Plan ${(this.view?.plans || []).indexOf(plan) + 1} changes ${count}` : `${count} today`;
    const rows = flights.map(x => {
      const time = x.cancelled ? `<s>${clock(x.depWas)}</s>` : clock(x.depNow);
      return `<li><button type="button" class="mp-fc-row" data-flight-open="${esc(x.no)}"><b>${esc(x.no)}</b><span>${esc(x.from)} → ${esc(x.to)}</span>
        <span class="num">${time}</span><span class="mp-fc-st" data-tone="${this.tags(x)[0][0]}">${esc(this.short(x))}</span></button></li>`;
    }).join("");
    return `<div class="mp-fc-head"><div><h3>${esc(this.routeName(route))}</h3><small>${esc(a)} ⇄ ${esc(b)} · ${esc(sub)}</small></div>
      <button type="button" class="mp-close" data-fc-close aria-label="Close the route">✕</button></div>
      <div class="mp-fc-body">${rows ? `<ul class="mp-fc-list">${rows}</ul>` : '<p class="mp-muted">No flights</p>'}</div>`;
  }
  // The pair of airports of the open card's route.
  selectedPair() {
    if (this.route) return [this.route.a, this.route.b];
    const leg = this.flight ? this.legs.find(l => l.no === this.flight.no) : null;
    return leg ? [leg.from, leg.to] : null;
  }
  // Outside a case, the network route of the open card stands out.
  markRoute() {
    const pair = this.mode ? null : this.selectedPair(), key = pair ? MapDraw.routeKey(...pair) : null;
    if (key === this.marked) return;
    this.root.querySelectorAll("#map-routes .mp-route.sel").forEach(path => path.classList.remove("sel"));
    if (key) this.root.querySelector(`#map-routes .mp-route[data-route="${CSS.escape(key)}"]`)?.classList.add("sel");
    this.marked = key;
  }
  // A card over the map never hides the route it is about: the map slides the route clear of it (a phone fits it instead).
  reveal() {
    if (!this.data || matchMedia("(max-width: 680px)").matches) return;
    requestAnimationFrame(() => {
      const pair = this.selectedPair(), card = this.$("#map-fc");
      if (!pair || card.hidden) return;
      const [A, B] = pair.map(code => this.airports[code]);
      if (!A || !B) return;
      const box = card.getBoundingClientRect(), stage = this.$("#map-stage").getBoundingClientRect();
      const points = [A, B, MapDraw.along(A, MapDraw.ctrl(A, B), B, 0.5)].map(p => this.toScreen(p)).map(([x, y]) => [x + stage.left, y + stage.top]);
      const under = ([x, y]) => x >= box.left - 8 && x <= box.right + 8 && y >= box.top - 8 && y <= box.bottom + 8;
      if (!under(points[2]) && !(under(points[0]) && under(points[1]))) return;
      const right = box.left > stage.left + stage.width / 2;
      const dx = right ? Math.min(0, box.left - 24 - Math.max(...points.map(p => p[0]))) : 0;
      const dy = right ? 0 : Math.min(0, box.top - 24 - Math.max(...points.map(p => p[1])));
      if (!dx && !dy) return;
      this.free = true;
      this.fly({ ...this.cam, x: this.cam.x - dx / this.cam.s, y: this.cam.y - dy / this.cam.s }, 500);
    });
  }
  // On a phone the flight card is a bottom sheet and the case card keeps only its header: the chosen route is fitted into the band
  // of map between them, once for each flight, route, screen size and sheet height (a refresh leaves the user's panning alone).
  phoneFit(force = false) {
    const sheet = this.$("#map-fc");
    if (!this.data || sheet.hidden || !matchMedia("(max-width: 680px)").matches) { this.phoneKey = null; return; }
    const pair = this.selectedPair(), ends = pair ? pair.map(code => this.airports[code]) : [];
    if (!pair || !ends[0] || !ends[1]) return;
    const box = sheet.getBoundingClientRect(), stage = this.$("#map-stage").getBoundingClientRect(), card = this.$("#map-card");
    const key = `${pair.join("-")}|${innerWidth}x${innerHeight}|${Math.round(box.height / 24)}`;
    if (!force && key === this.phoneKey) return;
    this.phoneKey = key;
    const top = Math.max(0, stage.top, card.hidden ? 0 : card.getBoundingClientRect().bottom) + 14;
    const bottom = Math.min(innerHeight, stage.bottom, box.top, this.$(".mp-tools").getBoundingClientRect().top) - 14;
    if (bottom - top < 80 || stage.width < 80) return;
    // The ends and the arc's top, bent as far as a leg's arc is drawn.
    const [A, B] = ends, points = [A, B, MapDraw.along(A, MapDraw.ctrl(A, B, 0.45), B, 0.5), MapDraw.along(A, MapDraw.ctrl(A, B, -0.2), B, 0.5)];
    const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
    const w = Math.max(...xs) - Math.min(...xs), h = Math.max(...ys) - Math.min(...ys);
    const s = Math.min(400, Math.max(2, 0.86 * Math.min((stage.width - 28) / Math.max(w, 0.3), (bottom - top) / Math.max(h, 0.3))));
    const middle = (top + bottom) / 2 - stage.top;
    this.free = true;
    this.fly({ x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2 - (middle - stage.height / 2) / s, s }, 450,
      () => this.phoneNudge(top, bottom, ends));
  }
  // After the fit, the labels and marks in the band slide sideways onto the screen when they fit across it.
  phoneNudge(top, bottom, ends) {
    const stage = this.$("#map-stage").getBoundingClientRect();
    const boxes = [...this.svg.querySelectorAll("#map-rotation .mp-lbl, #map-rotation .mp-me, #map-rotation .mp-storm-badge, #map-rotation .mp-storm-tag")]
      .map(el => el.getBoundingClientRect()).filter(b => b.width && b.bottom > top && b.top < bottom).map(b => [b.left, b.right])
      .concat(ends.map(end => this.toScreen(end)[0] + stage.left).map(x => [x, x]));
    const left = Math.min(...boxes.map(b => b[0])), right = Math.max(...boxes.map(b => b[1]));
    if (right - left > stage.width - 16) return;
    const dx = left < stage.left + 8 ? stage.left + 8 - left : right > stage.right - 8 ? stage.right - 8 - right : 0;
    if (!dx) return;
    this.cam = { ...this.cam, x: this.cam.x - dx / this.cam.s };
    this.draw();
  }
  // A short preview of the flight or route under the pointer, or of the one focused from the keyboard.
  preview(target) {
    const no = target.dataset.flight;
    if (no) {
      const x = this.facts(no);
      if (!x) return null;
      const times = x.cancelled ? MapDraw.clock(x.depWas) : `${MapDraw.clock(x.depNow)}–${MapDraw.clock(x.arrNow)}`;
      return { title: `${no} · ${this.city(x.from) || x.from} → ${this.city(x.to) || x.to}`, line: [times, this.short(x), this.airText(x)].filter(Boolean).join(" · ") };
    }
    const [a, b] = String(target.dataset.route || "").split("-");
    if (!a || !b) return null;
    const route = { a, b, kind: target.dataset.kind === "plan" ? "plan" : "network" };
    const flights = this.routeFlights(route).map(n => this.facts(n)).filter(Boolean), word = `${flights.length} flight${flights.length === 1 ? "" : "s"}`;
    if (route.kind === "network") return { title: this.routeName(route), line: `${word} today` };
    const most = Math.max(0, ...flights.map(x => x.late || 0));
    return { title: this.routeName(route), line: `${word} in the plan${most ? ` · up to ${most} min later` : ""}` };
  }
  peek(event, focus = false) {
    const box = this.$("#map-peek"), esc = MapDraw.esc;
    const target = event && !this.moved && event.target instanceof Element ? event.target.closest("#map-svg [data-flight], #map-svg [data-route]") : null;
    const text = target ? this.preview(target) : null;
    if (!text) { box.hidden = true; this.peeked = null; return; }
    if (this.peeked !== target || box.hidden) {
      box.innerHTML = `<b>${esc(text.title)}</b><span>${esc(text.line)}</span>`;
      this.peeked = target;
      box.hidden = false;
    }
    const stage = this.$("#map-stage").getBoundingClientRect();
    let x, y;
    if (focus || !Number.isFinite(event.clientX)) {
      const r = target.getBoundingClientRect();
      [x, y] = [r.left + r.width / 2 - stage.left, r.bottom - stage.top];
    } else [x, y] = [event.clientX - stage.left, event.clientY - stage.top];
    const w = box.offsetWidth, h = box.offsetHeight;
    const left = Math.min(Math.max(8, x + 14), Math.max(8, stage.width - w - 8));
    const top = y + 18 + h <= stage.height - 8 ? y + 18 : Math.max(8, y - 12 - h);
    box.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  // ---------- Rotation strip ----------
  drawStrip() {
    const rot = this.$("#map-rot"), legs = this.focusLegs.filter(l => !l.absent);
    // A storm moves dozens of aircraft: no single rotation to compare.
    rot.hidden = !this.mode || !legs.length || this.isStorm();
    this.stripScale = null;
    if (rot.hidden) { this.dock(); return; }
    const plan = this.planOf(), esc = MapDraw.esc;
    const out = this.unavailable();
    const label = this.planState(plan).strip;
    this.$("#map-rot-head").innerHTML = `<b>${esc(this.registration || "Aircraft")} rotation</b>${plan ? `<span>Schedule vs plan: ${esc(RecoveryView.planName(plan))}</span>` : ""}
      ${out ? `<span class="out">▨ Out of service ${MapDraw.clock(out[0])}–${MapDraw.clock(out[1])}</span>` : ""}`;
    const strip = this.$("#map-strip"), width = strip.getBoundingClientRect().width || 900;
    const colour = this.planState(plan).colour;
    const drawn = MapDraw.strip(legs, { unavailable: out, now: this.now, t: this.t, width, label, colour });
    strip.innerHTML = drawn.svg;
    strip.style.height = `${drawn.height}px`;
    this.stripScale = drawn.toMinutes;
    this.dock();
  }
  unavailable() {
    const u = this.mode === "case" ? this.view?.unavailable : null, day = this.data?.operating_date || "";
    const from = u ? MapDraw.minutes(u.from, day) : null, until = u ? MapDraw.minutes(u.until, day) : null;
    return from !== null && until !== null ? [from, until] : null;
  }
  // The card stops above the dock, whatever the dock's height.
  dock() { this.root.querySelector(".mp").style.setProperty("--dock", `${this.$("#map-dock").getBoundingClientRect().height}px`); }

  clear() {
    const moved = this.mode && this.focusLegs.some(l => !l.absent);
    this.mode = null;
    this.trace = null;
    this.view = null;
    this.flight = null;
    this.route = null;
    this.registration = null;
    this.focusLegs = [];
    this.markPressed();
    this.renderCard();
    this.renderFlight();
    this.svg.classList.remove("dim");
    this.drawStrip();
    if (moved && this.data) { this.free = false; this.fly(this.home()); } else this.draw();
  }

  // ---------- Camera ----------
  viewport() {
    const box = this.$("#map-stage").getBoundingClientRect();
    // A wide screen keeps the card over the map's right side; narrower screens put it over the list.
    return { w: box.width, h: box.height, padR: this.mode && innerWidth > 1100 ? 400 : 0 };
  }
  // Europe on a wide screen; Iberia and the Atlantic islands on a narrow one, so Lisbon's markers stay in view.
  home() { return this.fitBox(this.viewport().w < 700 ? this.IBERIA : this.EUROPE, 30); }
  fitBox([x0, y0, x1, y1], margin = 60) {
    const v = this.viewport(), w = Math.max(80, v.w - v.padR - margin * 2), h = Math.max(80, v.h - margin * 2);
    return { x: (x0 + x1) / 2, y: (y0 + y1) / 2, s: Math.min(w / Math.max(x1 - x0, 0.5), h / Math.max(y1 - y0, 0.5)) };
  }
  toScreen = ([x, y]) => {
    const v = this.viewport();
    return [(x - this.cam.x) * this.cam.s + (v.w - v.padR) / 2, (y - this.cam.y) * this.cam.s + v.h / 2];
  };
  fly(to, ms = 750, done = null) {
    const from = { ...this.cam }, start = performance.now();
    cancelAnimationFrame(this.anim);
    const step = (now) => {
      const k = Math.min(1, (now - start) / ms), e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
      const s = Math.exp(Math.log(from.s) + (Math.log(to.s) - Math.log(from.s)) * e);
      this.cam = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, s };
      this.draw();
      if (k < 1) this.anim = requestAnimationFrame(step);
      else if (done) done();
    };
    this.anim = requestAnimationFrame(step);
  }
  zoomAt(factor, px, py) {
    const v = this.viewport(), cx = (v.w - v.padR) / 2, cy = v.h / 2;
    px ??= cx; py ??= cy;
    const wx = this.cam.x + (px - cx) / this.cam.s, wy = this.cam.y + (py - cy) / this.cam.s;
    const s = Math.min(400, Math.max(2, this.cam.s * factor));
    this.free = true;
    this.cam = { x: wx - (px - cx) / s, y: wy - (py - cy) / s, s };
    this.draw();
  }

  // ---------- Drawing ----------
  draw() {
    if (!this.data || this.root.hidden) return;
    const v = this.viewport();
    if (!this.fitted && v.w) { this.cam = this.home(); this.fitted = true; }
    this.$("#map-world").setAttribute("transform",
      `translate(${(v.w - v.padR) / 2 - this.cam.x * this.cam.s} ${v.h / 2 - this.cam.y * this.cam.s}) scale(${this.cam.s})`);
    const labelled = new Set(["LIS", "OPO", "FAO", "FNC", "PDL", "TER", "MAD", "BCN", "CDG", "LHR", "FRA", "AMS", "GRU", "JFK", "BEL", "RAK"]);
    const focused = this.focusLegs.filter(l => !l.absent);
    const onDay = new Set(focused.flatMap(l => [l.from, l.to]));
    const me = this.mode && focused.length ? MapDraw.aircraftAt(this.focusLegs, this.t).ground : null;
    const names = [];  // airport names on screen, kept clear of flight numbers
    this.$("#map-aps").innerHTML = Object.entries(this.airports).map(([code, point]) => {
      const [x, y] = this.toScreen(point), hub = code === "LIS" || code === "OPO", on = onDay.has(code);
      if (x < -40 || y < -40 || x > v.w + 40 || y > v.h + 40) return "";
      const show = code !== me && (on || (!this.mode && (hub || (this.cam.s > 9 && labelled.has(code)) || this.cam.s > 26)));
      if (show) names.push([x + 3, y - 9, x + 9 + code.length * (hub || on ? 8.4 : 7.4), y + 7]);
      return `<g class="mp-ap${hub ? " hub" : ""}${on ? " on" : ""}"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${code === "LIS" ? 5 : hub || on ? 3.5 : 2}"/>${show
        ? `<text x="${(x + 7).toFixed(1)}" y="${(y + 4).toFixed(1)}">${MapDraw.esc(code)}</text>` : ""}</g>`;
    }).join("");
    const selected = this.flight?.no || null, pair = this.mode === "case" ? this.selectedPair() : null;
    const plan = this.planOf(), m = this.modelOf(this.trace);
    const colour = this.planState(plan).colour;
    // A weather case moves flights on many aircraft: every aircraft stays in view, those whose flight the plan moves in its colour
    // (owner: "when you click on the storm the planes should still somehow appear").
    const weather = this.isStorm();
    const traffic = MapDraw.traffic(this.legs, this.airports, this.t, this.toScreen, weather ? new Set() : new Set(focused.map(l => l.flight_no)),
      { labels: !this.mode, avoid: this.mode ? names : names.concat(MapDraw.markerBoxes(this.markerModels(), this.airports, this.toScreen)),
        limit: Math.round(12 * (this.cam.s / this.home().s) ** 2), selected,
        marked: weather ? new Set(focused.filter(l => l.cancelled || l.delta || l.moved).map(l => l.flight_no)) : null });
    this.$("#map-traffic").innerHTML = traffic.svg;
    this.$("#map-traffic").style.setProperty("--pc", colour);
    this.$("#map-airborne").textContent = String(traffic.airborne);
    if (this.isStorm()) {
      // The storm rings its airport; the plan's moved flights read as routes, its cancelled flights one by one (Task 4c).
      const station = this.airports[this.view.station];
      const at = station ? this.toScreen(station) : null, tip = this.stormTip();
      // The forecast's mark (a crosswind's runway and wind) above the routes that meet at its airport, then the airport's name.
      this.$("#map-rotation").innerHTML = MapDraw.spread(focused, this.airports, this.toScreen, { colour, selected: pair ? MapDraw.routeKey(...pair) : null,
          label: (key) => `${this.routeName({ a: key.split("-")[0], b: key.split("-")[1] })}: the plan's flights` })
        + MapDraw.rotation(focused.filter(l => l.cancelled), this.airports, this.t, this.toScreen, { colour, me: null, selected,
          avoid: names.concat(MapDraw.stormBoxes(at, this.cam.s, tip.tag)) })
        + MapDraw.storm(at, this.cam.s, tip)
        + MapDraw.stormStation(at, this.view.station);
    } else {
      this.$("#map-rotation").innerHTML = this.mode && focused.length ? MapDraw.rotation(this.focusLegs, this.airports, this.t, this.toScreen, {
        colour, avoid: names, selected, me: { registration: this.registration, tone: m?.tone || "progress", unavailable: this.unavailable(),
          kind: this.mode === "case" && this.view ? MapDraw.kindOf(this.view.disruption_type) : null } }) : "";
    }
    this.drawMarkers();
    // A redraw replaces what the preview was about: it closes until the pointer moves again.
    if (this.peeked && !this.peeked.isConnected) this.peek(null);
  }

  markerModels() {
    return this.rows.map(m => ({ trace: m.row.trace_id, station: m.row.station, tone: m.tone, headline: m.row.headline || "",
      kind: MapDraw.kindOf(m.row.disruption_type), tag: String(m.row.headline || m.row.display_id || "").split(" · ")[0].slice(0, 14) }));
  }
  drawMarkers() {
    if (!this.data || this.root.hidden) return;
    if (this.mode) { this.$("#map-markers").innerHTML = ""; return; }
    this.$("#map-markers").innerHTML = MapDraw.markers(this.markerModels(), this.airports, this.toScreen, this.hot);
  }

  // ---------- Time ----------
  ticks() {
    const step = this.$("#map-slider").getBoundingClientRect().width < 640 ? 360 : 180;
    const marks = [];
    for (let t = 360; t <= 1800; t += step) {
      const edge = t === 360 ? ' class="start"' : t + step > 1800 ? ' class="end"' : "";
      marks.push(`<span${edge} style="left:${((t - 360) / 1440) * 100}%">${MapDraw.clock(t)}</span>`);
    }
    this.$("#map-ticks").innerHTML = marks.join("");
  }
  setTime(value) {
    this.t = Math.round(Math.min(1800, Math.max(360, value)) / 5) * 5;
    this.$("#map-slider").value = String(this.t);
    const tag = this.t === this.now ? '<span class="now">Now</span>' : this.t > this.now ? "<span>Planned</span>" : "<span>Earlier</span>";
    this.$("#map-time").innerHTML = `<b class="num">${MapDraw.clock(this.t)}</b>${tag}`;
    this.draw();
    if (this.mode) this.drawStrip();
    this.liveFlight();
  }
  play() {
    const button = this.$("#map-play"), time = this.$("#map-time");
    if (this.playing) {
      clearInterval(this.playing);
      this.playing = 0;
      button.textContent = "▶ Play";
      time.setAttribute("aria-live", "polite");
      return;
    }
    if (this.t >= 1800) this.setTime(360);
    button.textContent = "❚❚ Pause";
    // A screen reader would otherwise read the clock every 60 ms while the day plays.
    time.setAttribute("aria-live", "off");
    this.playing = setInterval(() => {
      if (this.t >= 1800 || this.root.hidden) { this.play(); return; }
      this.setTime(this.t + 5);
    }, 60);
  }

}
