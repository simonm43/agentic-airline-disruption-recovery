"use strict";

// Hover and focus explanations: any element with data-tip (and optional data-tip-title) shows a
// styled card. Text is set with textContent, never parsed as HTML. An element with data-card shows a
// richer card from a <template> its view built with escaped markup (an agent on the graph).
const Tips = (() => {
  const TERMS = {
    DLA: "Delay message to EUROCONTROL's Network Manager; needed when a departure moves more than 15 minutes.",
    CHG: "Change message to EUROCONTROL's Network Manager: changes a filed flight plan, here the aircraft registration.",
    CNL: "Cancel message to EUROCONTROL's Network Manager: withdraws a filed flight plan.",
    EOBT: "Estimated off-block time: when the aircraft is expected to leave the stand.",
    TOBT: "Target off-block time: when the aircraft will be ready, shared with the airport's CDM system.",
    MVT: "Movement message: tells stations and partners the flight's new times.",
    "A-CDM": "Airport Collaborative Decision Making: the airport, airlines, handlers and air traffic control share target times to sequence departures.",
    CDM: "Airport Collaborative Decision Making: the airport, airlines, handlers and air traffic control share target times to sequence departures.",
    FDP: "Flight duty period: the crew's working time from report to the last landing, capped by EU rules.",
    EU261: "EU passenger-rights rule: care, rebooking and compensation for long delays and cancellations.",
    OCC: "Operations Control Centre: the airline team that runs the day and approves major changes.",
    AOG: "Aircraft on ground: grounded by a technical problem.",
    LIS: "Lisbon airport, the airline's hub.",
    TAF: "Terminal Aerodrome Forecast: the airport's official weather forecast for the next 24–30 hours.",
    METAR: "Meteorological Aerodrome Report: the airport's routine weather observation, issued every half hour.",
    EES: "Entry/Exit System: the EU's automated register of non-EU travellers crossing the Schengen area's external borders. Its checks can lengthen border-control queues.",
    "Standby crew": "A crew on standby at home or at the airport, called out the same day (EASA standby).",
  };
  // "standby crews", in any case and number, explained once as the term.
  const STANDBY = /\bstandby crews?\b/i;
  // One vocabulary for the airline systems (owner): what each screen calls a system or a recipient. Older
  // records and labels keep the names they were written with; they read as these.
  const NAMES = {
    Schedule: "Operations control", "Ops control": "Operations control", Crew: "Crew system", "Crew roster": "Crew system",
    Booking: "Passenger system", Passengers: "Passenger system", Reservations: "Passenger system",
    Slots: "Airport capacity", "Airport slots": "Airport capacity", "Network manager": "EUROCONTROL Network Manager",
    "Network Manager": "EUROCONTROL Network Manager", "Airport CDM": "Lisbon airport (A-CDM)", "Airport (A-CDM)": "Lisbon airport (A-CDM)"
  };
  const system = (name) => (Object.hasOwn(NAMES, String(name)) ? NAMES[String(name)] : String(name ?? ""));
  // A recorded message that names a system the old way ("Not sent: Ops control did not complete").
  const OLD_NAMES = new RegExp(`\\b(${["Ops control", "Network manager", "Airport CDM", "Crew roster", "Reservations", "Airport slots"].join("|")})\\b`, "g");
  const systemWords = (text) => String(text ?? "").replace(OLD_NAMES, (name) => NAMES[name]);
  const SYSTEMS = {
    "Operations control": "The airline's operations control system: flights, aircraft rotations and departure times.",
    Maintenance: "The airline's maintenance system: aircraft defects and when each aircraft returns to service.",
    "Crew system": "The airline's crew system: duties and flight-time limits. Release is when a duty ends.",
    "Passenger system": "Bookings, seat inventory and rebooking within the airline's policy.",
    "Airport capacity": "Airport capacity: how many movements each airport hour can take, and the airport rules.",
    "EUROCONTROL Network Manager": "EUROCONTROL's Network Manager balances European air traffic with capacity and issues take-off slots. It answers each message ACK (accepted) or REJ (rejected).",
    "Lisbon airport (A-CDM)": "Lisbon airport's Collaborative Decision Making system: it sequences departures by their target off-block times.",
    "Slot coordinator": "The airport slot coordinator: allocates arrival and departure slots and answers each request separately.",
    "Ground handling": "The Lisbon ground handler. A direct transfer takes connecting passengers by car from the late aircraft to their next flight.",
    ARROW: "ARROW's own tools: record the disruption and the case, read the EU261 exposure, build plans and submit one. Its code checks every value.",
  };
  // Longest first, so A-CDM is explained once rather than as CDM.
  // The scenario's autonomy levels, by what each allows (an agent's badge and card).
  const LEVELS = {
    L4: "Autonomous: acts on its own. Here it reads the report and records it in ARROW; it changes no airline system, flight, crew or booking.",
    L3: "Acts within pre-approved rules; outside them OCC decides.",
    L2: "Recommends only; OCC decides.",
  };
  // foundry: the agent's name in the Foundry project; tools: what its Foundry definition may call.
  const AGENTS = {
    orchestrator: { name: "Recovery Orchestrator Agent", level: "", reads: "ARROW's tools", foundry: "recovery-orchestrator-agent",
      does: "Has the Detection agent read the report, asks the other agents what the case needs, builds complete plans through ARROW's tools and recommends one. It cannot approve or carry anything out.",
      inputs: "ARROW's case brief: the report and the limits in force", outputs: "Plan levers · one recommended plan · case summary",
      tools: [["ARROW", "get_playbook, record_case, build_plan, submit_plan"], ["Agents", "asks the five domain agents"]] },
    detection: { name: "Disruption Detection Agent", level: "L4", scope: "Records on its own", reads: "Operations control", foundry: "disruption-detection-agent",
      does: "Reads the report, records the disruption in ARROW and predicts its knock-on: the flights and aircraft it makes late if nothing changes.",
      inputs: "The report", outputs: "Disruption type and facts · knock-on",
      tools: [["ARROW", "record_aircraft_defect, record_flight_delay, record_weather_forecast"], ["Operations control", "get_disruption, get_aircraft_rotation"]] },
    aircraft: { name: "Aircraft Recovery Agent", level: "L2", scope: "Recommends; OCC decides", reads: "Operations control", foundry: "aircraft-recovery-agent",
      does: "Lists the aircraft options: absorb the delay, delay the rotation, swap to a free aircraft or cancel at a safe point.",
      inputs: "The disruption and what is known about it", outputs: "Aircraft options · recommended option",
      tools: [["Operations control", "get_recovery_options"]] },
    crew: { name: "Crew Recovery Agent", level: "L3", scope: "Within crew rules", reads: "Crew system", foundry: "crew-recovery-agent",
      does: "Checks each option against crew duty limits and finds standby crews when a duty would run over.",
      inputs: "The disruption · aircraft option · added minutes", outputs: "Duty breaches · standby crews",
      tools: [["Crew system", "assess_crew_recovery, get_reserve_options"]] },
    passenger: { name: "Passenger Re-accommodation Agent", level: "L3", scope: "Rebooking within policy", reads: "Passenger system", foundry: "passenger-reaccommodation-agent",
      does: "Counts the passengers whose connections break, plans their rebooking within policy and reports the EU261 exposure.",
      inputs: "The disruption · aircraft option · added minutes", outputs: "Lose flight or connection · rebooked · unplaced passengers · EU261 exposure",
      tools: [["Passenger system", "assess_passenger_recovery, get_rebooking_plan"], ["ARROW", "get_compensation_exposure"]] },
    slot: { name: "Slot Negotiation Agent", level: "L3", scope: "Within allocated slots", reads: "Airport capacity", foundry: "slot-negotiation-agent",
      does: "Checks that the new times fit airport capacity and which delay messages are needed. ARROW sends the messages; the agent never requests slots.",
      inputs: "The disruption · aircraft option · added minutes", outputs: "Capacity fit · extra minutes · delay messages",
      tools: [["Airport capacity", "assess_slot_recovery"]] },
  };

  const WORD = new RegExp(`(?<![\\w-])(${Object.keys(TERMS).filter(term => !term.includes(" ")).sort((a, b) => b.length - a.length).join("|")})(?![\\w-])`, "g");

  // What a system and the abbreviations in a line of text mean, one per line.
  function explain(name, text) {
    const lines = [];
    const known = system(name);
    if (Object.hasOwn(SYSTEMS, known)) lines.push(SYSTEMS[known]);
    const seen = new Set();
    for (const [term] of String(text || "").matchAll(WORD)) {
      if (!seen.has(term)) { seen.add(term); lines.push(`${term}: ${TERMS[term]}`); }
    }
    if (STANDBY.test(String(text || ""))) lines.push(`Standby crew: ${TERMS["Standby crew"]}`);
    return lines.join("\n");
  }

  // Attribute string for markup built in template literals; escape() is the caller's HTML escaper.
  function attrs(escape, tip, title = "") {
    if (!tip) return "";
    return ` data-tip="${escape(tip)}"${title ? ` data-tip-title="${escape(title)}"` : ""} tabindex="0"`;
  }

  let card = null;
  let owner = null;

  function hide() {
    if (!card || !owner) return;
    card.hidden = true;
    owner.removeAttribute("aria-describedby");
    owner = null;
  }

  function show(target) {
    if (owner === target) return;
    hide();
    if (!card) {
      card = document.createElement("div");
      card.id = "tip";
      card.className = "tip";
      card.setAttribute("role", "tooltip");
      card.hidden = true;
      document.body.append(card);
    }
    const heading = target.dataset.tipTitle;
    const parts = [];
    if (heading) {
      const h = document.createElement("strong");
      h.textContent = heading;
      parts.push(h);
    }
    // A message as received (a TAF), shown as written in monospace above the explanation.
    if (target.dataset.tipCode) {
      const code = document.createElement("code");
      code.className = "tip-code";
      code.textContent = target.dataset.tipCode;
      parts.push(code);
    }
    const body = document.createElement("span");
    body.textContent = target.dataset.tip;
    parts.push(body);
    card.replaceChildren(...parts);
    card.hidden = false;
    owner = target;
    opened = Date.now();
    target.setAttribute("aria-describedby", "tip");
    const box = target.getBoundingClientRect();
    const tip = card.getBoundingClientRect();
    const below = box.bottom + 8 + tip.height <= window.innerHeight;
    const top = below ? box.bottom + 8 : Math.max(8, box.top - 8 - tip.height);
    const left = Math.min(Math.max(8, box.left), window.innerWidth - tip.width - 8);
    card.style.top = `${top}px`;
    card.style.left = `${left}px`;
  }

  // A richer card (a step of the agents' graph): data-card names a <template> of escaped markup, shown beside
  // the element on hover or keyboard focus, or on a tap. It stays open while the pointer is on it, so its link can be
  // followed.
  let panel = null;
  let panelOwner = null;
  let closing = 0;
  const inPanel = (node) => Boolean(panel && node instanceof Node && panel.contains(node));

  function hidePanel() {
    clearTimeout(closing);
    if (!panel || !panelOwner) return;
    panel.hidden = true;
    panelOwner.removeAttribute("aria-describedby");
    panelOwner = null;
  }
  function hidePanelSoon() {
    clearTimeout(closing);
    closing = setTimeout(hidePanel, 160);
  }

  // Anchors that are a line of words rather than a figure or a chip.
  const LINES = ".rec-why, .verdict [data-card], .why, .part";
  function showPanel(target) {
    clearTimeout(closing);
    if (panelOwner === target) return;
    const template = document.getElementById(target.dataset.card || "");
    if (!(template instanceof HTMLTemplateElement)) return;
    hidePanel();
    if (!panel) {
      panel = document.createElement("div");
      panel.id = "tip-card";
      panel.className = "tip-card";
      panel.setAttribute("role", "tooltip");
      panel.hidden = true;
      panel.addEventListener("mouseover", () => clearTimeout(closing));
      panel.addEventListener("mouseleave", (event) => {
        if (!(event.relatedTarget instanceof Node && panelOwner?.contains(event.relatedTarget))) hidePanelSoon();
      });
      document.body.append(panel);
    }
    panel.replaceChildren(template.content.cloneNode(true));
    panel.style.maxHeight = "";
    panel.hidden = false;
    if (getComputedStyle(panel).display === "none") { panel.hidden = true; return; }
    panelOwner = target;
    opened = Date.now();
    target.setAttribute("aria-describedby", "tip-card");
    // A figure or a chip opens its card beside it, right if it fits, else left, else below or above it. A line of words (the
    // summary's verdict, the Decision step's why) opens it below, or above when there is no room, never beside, so the card
    // never covers the line it explains (prototype compare-case3-hover-rec). A card taller than the room on either side (a
    // phone) takes the larger side and scrolls, so it never covers the element that opened it.
    const box = target.getBoundingClientRect();
    let size = panel.getBoundingClientRect();
    const fits = (x) => x >= 8 && x + size.width <= window.innerWidth - 8;
    const beside = !target.matches(LINES);
    let left = box.right + 10;
    let top = box.top - 4;
    if (beside && !fits(left)) left = box.left - 10 - size.width;
    if (!beside || !fits(left)) {
      left = Math.min(Math.max(8, box.left), window.innerWidth - size.width - 8);
      const below = window.innerHeight - box.bottom - 16;
      const above = box.top - 16;
      if (size.height > below && size.height > above) {
        panel.style.maxHeight = `${Math.max(120, Math.floor(Math.max(below, above)))}px`;
        size = panel.getBoundingClientRect();
      }
      top = size.height <= below ? box.bottom + 8 : box.top - 8 - size.height;
    }
    panel.style.left = `${left}px`;
    panel.style.top = `${Math.max(8, Math.min(top, window.innerHeight - size.height - 8))}px`;
  }

  function from(event) {
    return event.target instanceof Element ? event.target.closest("[data-tip]") : null;
  }
  function cardFrom(event) {
    return event.target instanceof Element ? event.target.closest("[data-card]") : null;
  }
  const into = (event, element) => event.relatedTarget instanceof Node && element.contains(event.relatedTarget);
  // The pointer last used. A touch leaves no pointer behind, so the mouse events a browser emits after a tap (a mouseout when
  // it puts back a mouse it also has) never close the card the tap opened; a real mouse moving, or a key, takes over again.
  let pointer = "mouse";
  document.addEventListener("pointerdown", (event) => { pointer = event.pointerType || "mouse"; }, true);
  document.addEventListener("pointermove", (event) => { if (event.pointerType === "mouse") pointer = "mouse"; }, true);
  document.addEventListener("keydown", () => { pointer = "keyboard"; }, true);

  document.addEventListener("mouseover", (event) => {
    const t = from(event); if (t) show(t);
    const c = cardFrom(event); if (c) showPanel(c);
  });
  document.addEventListener("mouseout", (event) => {
    if (pointer === "touch") return;
    const t = from(event);
    if (t && !into(event, t)) hide();
    const c = cardFrom(event);
    if (c && c === panelOwner && !into(event, c) && !inPanel(event.relatedTarget)) hidePanelSoon();
  });
  document.addEventListener("focusin", (event) => {
    const t = from(event); if (t) show(t); else hide();
    const c = cardFrom(event);
    if (c) showPanel(c); else if (!inPanel(event.target)) hidePanel();
  });
  document.addEventListener("focusout", (event) => {
    hide();
    if (event.target === panelOwner && !inPanel(event.relatedTarget) && !panelOwner.matches(":hover")) hidePanel();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { hide(); hidePanel(); } });
  // A click (a tab, a button) or a redraw that removes the element closes the card too. A tap or click on a figure, a
  // chip or a line that is not itself a control opens its card instead, and a second one closes it: on a phone a tap
  // is how the details are read (owner: details on hover).
  let opened = 0;
  // A tap on a step of the agents' graph (data-card-tap) shows its card first; a second tap, or the card's Details
  // (data-card-open), opens the step as a click does (owner review: the graph's cards on hover, focus or tap).
  let passing = false;
  const CONTROL = "button, a[href], summary, input, select, textarea, label";
  document.addEventListener("click", (event) => {
    const element = event.target instanceof Element ? event.target : null;
    const details = element?.closest("[data-card-open]");
    if (details && inPanel(details) && panelOwner) {
      const step = panelOwner;
      hide(); hidePanel();
      passing = true;
      try { step.click(); } finally { passing = false; }
      return;
    }
    const tapped = element?.closest("[data-card-tap]");
    if (tapped && !passing && pointer === "touch" && !inPanel(tapped) && !(panelOwner === tapped && Date.now() - opened > 350)) {
      event.preventDefault();
      event.stopPropagation();
      hide();
      showPanel(tapped);
      return;
    }
    const anchor = element ? element.closest("[data-card], [data-tip]") : null;
    const plain = anchor && !anchor.matches(CONTROL) && !anchor.closest(CONTROL) && !inPanel(anchor);
    if (plain) {
      const mine = anchor.dataset.card ? panelOwner === anchor : owner === anchor;
      if (mine && Date.now() - opened > 350) { hide(); hidePanel(); return; }
      if (anchor.dataset.card) { hide(); showPanel(anchor); } else { hidePanel(); show(anchor); }
      return;
    }
    hide();
    if (!inPanel(event.target)) hidePanel();
  }, true);
  new MutationObserver(() => {
    if (owner && (!owner.isConnected || !owner.offsetParent)) hide();
    if (panelOwner && (!panelOwner.isConnected || !panelOwner.offsetParent)) {
      // A live redraw replaces the element: the card follows its successor while the pointer is on it.
      const id = panelOwner.dataset.card;
      hidePanel();
      const next = id ? document.querySelector(`[data-card="${CSS.escape(id)}"]`) : null;
      if (next && next.offsetParent && next.matches(":hover")) showPanel(next);
    }
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "class"] });
  // Any scroll closes the cards, except scrolling inside a tall card itself.
  window.addEventListener("scroll", (event) => { if (event.target === panel) return; hide(); hidePanel(); }, true);

  const isOpen = () => Boolean((card && !card.hidden) || (panel && !panel.hidden));
  return { explain, attrs, hide, hidePanel, isOpen, AGENTS, LEVELS, system, systemWords };
})();
