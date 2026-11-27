"use strict";
// Operations map drawing (operations map spec §3): pure functions that return SVG markup.
// World coordinates are Mercator degrees; screen positions come from the caller's toScreen. Ported from
// the operations map prototype.

const MapDraw = (() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const TONES = { needs: "#f5a524", manual: "#f0524f", stopped: "#f0524f", expert: "#a68bfa", progress: "#38c6f4",
    handled: "#3ed68d", muted: "#6b7a92" };
  const ICONS = {
    defect: '<path d="M4.5 19.5 12 12"/><path d="M14.5 4.5a4.6 4.6 0 0 0-2.9 6.9l.4.6m2.5-7.5 2.2 2.2-.5 2.3 2.3-.5 2.2 2.2a4.6 4.6 0 0 1-6.9 2.9"/>',
    delay: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    weather: '<path d="M7 16.5a4.25 4.25 0 0 1-.6-8.46 5.75 5.75 0 0 1 11.1 1.46A3.5 3.5 0 0 1 17.5 16.5"/><path d="m12.5 12-2.5 4.5h4L11.5 21"/>',
    other: '<path d="M12 3.5 21 19.5H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>'
  };
  const KINDS = { defect: "Aircraft defect", delay: "Delay", weather: "Weather", other: "Other report" };
  // The city each airport serves, as operations control names it (airline_systems.operating_day.CITY); /api/network serves only
  // codes and positions, so a code without a city here reads as its code.
  const CITY = Object.freeze({
    LIS: "Lisbon", OPO: "Porto", FAO: "Faro", FNC: "Funchal", PDL: "Ponta Delgada", TER: "Terceira", MAD: "Madrid", BCN: "Barcelona",
    SVQ: "Seville", AGP: "Málaga", BIO: "Bilbao", VLC: "Valencia", LHR: "London Heathrow", LGW: "London Gatwick", MAN: "Manchester",
    BHX: "Birmingham", EDI: "Edinburgh", DUB: "Dublin", CDG: "Paris Charles de Gaulle", ORY: "Paris Orly", NCE: "Nice", LYS: "Lyon",
    MRS: "Marseille", TLS: "Toulouse", BOD: "Bordeaux", NTE: "Nantes", BRU: "Brussels", AMS: "Amsterdam", LUX: "Luxembourg",
    FRA: "Frankfurt", MUC: "Munich", DUS: "Düsseldorf", HAM: "Hamburg", BER: "Berlin", CPH: "Copenhagen", ARN: "Stockholm", OSL: "Oslo",
    GVA: "Geneva", ZRH: "Zurich", FCO: "Rome", MXP: "Milan", VCE: "Venice", BLQ: "Bologna", PRG: "Prague", VIE: "Vienna", WAW: "Warsaw",
    BUD: "Budapest", CMN: "Casablanca", RAK: "Marrakesh", DSS: "Dakar", RAI: "Praia", SID: "Sal", BKO: "Bamako", GRU: "São Paulo",
    GIG: "Rio de Janeiro", REC: "Recife", FOR: "Fortaleza", SSA: "Salvador", BSB: "Brasília", CNF: "Belo Horizonte", BEL: "Belém",
    NAT: "Natal", VCP: "Campinas", EWR: "Newark", JFK: "New York", BOS: "Boston", IAD: "Washington Dulles", MIA: "Miami", ORD: "Chicago",
    YYZ: "Toronto", YUL: "Montreal", ATL: "Atlanta", LAD: "Luanda", MPM: "Maputo", ACC: "Accra", ABJ: "Abidjan"
  });
  // One key per route, whichever way it is flown.
  const routeKey = (a, b) => [a, b].sort().join("-");
  const PLANE = "M0,-7 L1.6,-2 L7,1.2 L7,2.6 L1.6,1.2 L1.1,5 L3,6.4 L3,7.3 L0,6.5 L-3,7.3 L-3,6.4 L-1.1,5 L-1.6,1.2 L-7,2.6 L-7,1.2 L-1.6,-2 Z";
  // Fan direction of several markers at one airport, in degrees on screen; Lisbon fans towards the ocean.
  const FAN = { LIS: -150 };

  const mercY = (lat) => -Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) * 180 / Math.PI;
  const project = ([lon, lat]) => [lon, mercY(lat)];
  const ctrl = (a, b, bend = 0.16) => {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    return [(a[0] + b[0]) / 2 + dy * bend, (a[1] + b[1]) / 2 - dx * bend];
  };
  const along = (a, c, b, t) => {
    const u = 1 - t;
    return [u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]];
  };
  const heading = (a, c, b, t) => {
    const dx = 2 * (1 - t) * (c[0] - a[0]) + 2 * t * (b[0] - c[0]);
    const dy = 2 * (1 - t) * (c[1] - a[1]) + 2 * t * (b[1] - c[1]);
    return Math.atan2(dy, dx) * 180 / Math.PI;
  };
  // Minutes after the operating day's midnight, so a flight after midnight plots after 23:59.
  const minutes = (value, day) => {
    const match = String(value ?? "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/);
    if (!match) return null;
    const days = day ? Math.round((Date.parse(match[1]) - Date.parse(day)) / 86400000) : 0;
    return (Number.isFinite(days) ? days : 0) * 1440 + Number(match[2]) * 60 + Number(match[3]);
  };
  const clock = (t) => {
    const m = ((Math.round(t) % 1440) + 1440) % 1440;
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}${t >= 1440 ? "⁺¹" : ""}`;
  };
  const kindOf = (type) => (type === "aircraft_defect" ? "defect" : type === "flight_delay" ? "delay" : type === "weather" ? "weather" : "other");
  const icon = (kind, size = 20) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[kind] || ICONS.other}</svg>`;
  const n = (value) => value.toFixed(1);

  function landPath(rings) {
    return rings.map(ring => `M${ring.map(point => project(point).map(n).join(",")).join("L")}Z`).join("");
  }

  // Each route once, weighted by how many flights fly it today; a wider invisible line over each takes the click (Task 3b).
  function routes(legs, airports) {
    const counts = new Map();
    for (const leg of legs) {
      if (leg.status === "cancelled") continue;
      const key = routeKey(leg.from, leg.to);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    const most = Math.max(1, ...counts.values());
    let hits = "";
    const lines = [...counts].map(([key, count]) => {
      const [a, b] = key.split("-").map(code => airports[code]);
      if (!a || !b) return "";
      const share = count / most, d = `M${a.map(n)}Q${ctrl(a, b).map(n)} ${b.map(n)}`;
      hits += `<path class="mp-route-hit" data-route="${esc(key)}" d="${d}"/>`;
      return `<path class="mp-route" data-route="${esc(key)}" style="opacity:${(0.12 + 0.4 * share).toFixed(2)};stroke-width:${(0.6 + 1.6 * share).toFixed(2)}"
        d="${d}"/>`;
    }).join("");
    return lines + hits;
  }

  // Aircraft in the air at time t, from the timetable: not live tracking. Each previews its flight on hover (map_view);
  // at most `limit` flight numbers show, Lisbon's first, each above, below or beside its aircraft where it
  // overlaps nothing in `avoid` (zoom in to see more; owner: 150 labels were unreadable).
  // marked: flights a weather plan moves, drawn in the plan's colour while the other aircraft stay in view (owner).
  function traffic(legs, airports, t, toScreen, skip = new Set(), { labels = true, avoid = [], limit = Infinity, selected = null, marked = null } = {}) {
    let airborne = 0, tags = "";
    const placed = [...avoid];
    const clear = (box) => !placed.some(p => box[0] < p[2] + 2 && p[0] < box[2] + 2 && box[1] < p[3] + 1 && p[1] < box[3] + 1);
    const candidates = [];
    const svg = legs.map(leg => {
      if (leg.status === "cancelled" || !(leg.std <= t && t < leg.sta)) return "";
      airborne += 1;
      const a = airports[leg.from], b = airports[leg.to];
      if (skip.has(leg.no) || !a || !b) return "";
      const c = ctrl(a, b), k = (t - leg.std) / (leg.sta - leg.std);
      const [x, y] = toScreen(along(a, c, b, k));
      if (labels) candidates.push({ no: leg.no, x, y, hub: leg.from === "LIS" || leg.to === "LIS" });
      // Owner: "plane is a bit too small the icon": about 14 px across, readable at 1440 px.
      return `<g class="mp-ac${leg.no === selected ? " sel" : ""}${marked?.has(leg.no) ? " plan" : ""}" data-flight="${esc(leg.no)}" transform="translate(${n(x)} ${n(y)}) rotate(${Math.round(heading(a, c, b, k) + 90)})">
        <circle r="11" fill="transparent"/><path d="${PLANE}" transform="scale(1)"/></g>`;
    }).join("");
    let shown = 0;
    for (const { no, x, y } of candidates.sort((a, b) => Number(b.hub) - Number(a.hub))) {
      if (shown >= limit) break;
      const w = String(no).length * 6.4 + 4;
      // [text x, baseline y, anchor, box]: above, below, right, left of the aircraft.
      const spot = [[x, y - 13, "middle", [x - w / 2, y - 24, x + w / 2, y - 11]], [x, y + 21, "middle", [x - w / 2, y + 10, x + w / 2, y + 23]],
        [x + 12, y + 4, "start", [x + 10, y - 7, x + 12 + w, y + 6]], [x - 12, y + 4, "end", [x - 12 - w, y - 7, x - 10, y + 6]]].find(s => clear(s[3]));
      if (spot) {
        placed.push(spot[3]);
        shown += 1;
        tags += `<text class="mp-ac-no" data-flight="${esc(no)}" x="${n(spot[0])}" y="${n(spot[1])}" text-anchor="${spot[2]}">${esc(no)}</text>`;
      }
    }
    return { svg: svg + tags, airborne };
  }

  // One marker per disruption at its station; several at one airport fan out with leader lines.
  function markerSpots(models, airports, toScreen) {
    const groups = {};
    for (const m of models) if (m.station && airports[m.station]) (groups[m.station] ||= []).push(m);
    return Object.entries(groups).flatMap(([station, list]) => {
      const base = toScreen(airports[station]);
      return list.map((m, i) => {
        const angle = ((FAN[station] ?? -90) + (i - (list.length - 1) / 2) * 44) * Math.PI / 180;
        const radius = list.length > 1 ? 92 : 56;
        return { m, base, x: base[0] + Math.cos(angle) * radius, y: base[1] + Math.sin(angle) * radius };
      });
    });
  }
  // Screen boxes of the markers (disc and tag), for labels to keep clear of.
  function markerBoxes(models, airports, toScreen) {
    return markerSpots(models, airports, toScreen).map(({ m, x, y }) => {
      const half = Math.max(19, String(m.tag).length * 3.8);
      return [x - half, y - 40, x + half, y + 19];
    });
  }
  function markers(models, airports, toScreen, hot) {
    let svg = "";
    for (const { m, base, x, y } of markerSpots(models, airports, toScreen)) {
      const r = 17;
      const pulse = m.tone === "needs" || m.tone === "manual" ? `<circle class="mp-pulse" cx="${n(x)}" cy="${n(y)}" r="${r}"/>` : "";
      svg += `<g class="mp-mk${hot === m.trace ? " hot" : ""}" data-case="${esc(m.trace)}" data-tone="${esc(m.tone)}" data-kind="${esc(m.kind)}"
        style="--c:${TONES[m.tone] || TONES.progress}" role="button" tabindex="0" aria-label="${esc(m.headline)}">
        <line class="mp-lead" x1="${n(base[0])}" y1="${n(base[1])}" x2="${n(x)}" y2="${n(y)}"/>${pulse}
        <circle class="mp-disc" cx="${n(x)}" cy="${n(y)}" r="${r}"/>
        <g class="mp-glyph" transform="translate(${n(x - 11)} ${n(y - 11)})">${icon(m.kind, 22)}</g>
        <text class="mp-tag" x="${n(x)}" y="${n(y - r - 6)}">${esc(m.tag)}</text></g>`;
    }
    return svg;
  }

  // The focused aircraft's day under a plan: its own legs plus any leg the plan names (a leg moved to
  // another aircraft stays visible). Times in minutes; durations come from the schedule, which already
  // carries an executed change. Plan flights missing from today's schedule come back absent, not drawn.
  function dayLegs(legs, registration, plan, day) {
    const planned = new Map((plan?.flights || []).map(f => [f.flight_no, f]));
    const known = new Set(legs.map(l => l.no));
    const result = legs.filter(l => (registration && l.reg === registration) || planned.has(l.no)).map(l => {
      const f = planned.get(l.no);
      const cancelled = f ? f.status === "cancelled" : l.status === "cancelled";
      const was = f ? minutes(f.std_local, day) ?? l.std : l.std;
      const std = f && !cancelled ? minutes(f.planned_local, day) ?? was : f ? was : l.std;
      const moved = f && f.aircraft_reg && registration && f.aircraft_reg !== registration ? f.aircraft_reg : null;
      return { flight_no: l.no, from: l.from, to: l.to, was, std, sta: std + (l.sta - l.std), dur: l.sta - l.std,
        delta: cancelled ? 0 : std - was, cancelled, moved, absent: false };
    });
    for (const f of planned.values()) {
      if (known.has(f.flight_no)) continue;
      const was = minutes(f.std_local, day);
      result.push({ flight_no: f.flight_no, from: null, to: null, was, std: was, sta: was, dur: 0, delta: 0,
        cancelled: f.status === "cancelled", moved: null, absent: true });
    }
    return result.sort((a, b) => (a.was ?? 0) - (b.was ?? 0));
  }
  const flown = (legs) => legs.filter(l => !l.absent && !l.cancelled && !l.moved);
  // Where the aircraft is at time t: in flight on a leg, or on the ground at an airport.
  function aircraftAt(legs, t) {
    const active = flown(legs);
    for (const leg of active) if (leg.std <= t && t < leg.sta) return { leg, k: (t - leg.std) / (leg.sta - leg.std) };
    const done = active.filter(l => l.sta <= t).pop();
    return { ground: done ? done.to : (active[0] || legs.find(l => !l.absent) || {}).from || null };
  }

  // The aircraft's legs as arcs with labels outside each arc, and the aircraft itself. Labels keep clear of each other and of the
  // boxes in avoid (airport names on screen, a storm's mark), so an airport reads in full. Each arc and label opens its flight
  // (Task 3b); `selected` marks the flight whose card is open.
  function rotation(legs, airports, t, toScreen, { colour, me, avoid = [], selected = null }) {
    let paths = "", labels = "";
    const seen = {}, placed = [];
    const bendOf = (leg) => 0.3 + 0.2 * (seen[`${leg.from}${leg.to}`] = (seen[`${leg.from}${leg.to}`] ?? -1) + 1);
    const bends = new Map();
    for (const leg of legs) {
      if (leg.absent || !airports[leg.from] || !airports[leg.to]) continue;
      const bend = bendOf(leg);
      bends.set(leg.flight_no, bend);
      const A = toScreen(airports[leg.from]), B = toScreen(airports[leg.to]), C = toScreen(ctrl(airports[leg.from], airports[leg.to], bend));
      const state = leg.cancelled ? "cancel" : leg.delta || leg.moved ? "moved" : leg.sta <= t ? "done" : "sched";
      const tint = state === "cancel" ? TONES.stopped : state === "moved" ? colour : state === "done" ? "#4a5a74" : TONES.progress;
      const sel = leg.flight_no === selected ? " sel" : "", d = `M${A.map(n)}Q${C.map(n)} ${B.map(n)}`;
      paths += `<path class="mp-leg ${state}${sel}" data-flight="${esc(leg.flight_no)}" style="--pc:${colour}" d="${d}"/>`;
      const [ax, ay] = along(A, C, B, 0.62);
      paths += `<path class="mp-arrow" fill="${tint}" d="M-5,-4 L4,0 L-5,4 Z" transform="translate(${n(ax)} ${n(ay)}) rotate(${Math.round(heading(A, C, B, 0.62))})"/>`;
      paths += `<path class="mp-hit" data-flight="${esc(leg.flight_no)}" d="${d}"/>`;
      if (state === "done") continue;
      const parts = leg.cancelled
        ? `<tspan class="no">${esc(leg.flight_no)}</tspan><tspan class="was"> ${clock(leg.was)}</tspan><tspan class="x"> Cancelled</tspan>`
        : leg.moved ? `<tspan class="no">${esc(leg.flight_no)}</tspan><tspan class="delta"> → ${esc(leg.moved)}</tspan>`
        : leg.delta ? `<tspan class="no">${esc(leg.flight_no)}</tspan><tspan class="was"> ${clock(leg.was)}</tspan><tspan> ${clock(leg.std)}</tspan><tspan class="delta"> +${leg.delta}</tspan>`
        : `<tspan class="no">${esc(leg.flight_no)}</tspan><tspan> ${clock(leg.std)}</tspan>`;
      const w = leg.cancelled ? 148 : leg.moved ? 130 : leg.delta ? 158 : 88;
      const [mx, my] = along(A, C, B, 0.5), cx = (A[0] + B[0]) / 2, cy = (A[1] + B[1]) / 2;
      const len = Math.hypot(C[0] - cx, C[1] - cy) || 1, nx = (C[0] - cx) / len, ny = (C[1] - cy) / len;
      let lx = mx + nx * 12 + nx * w / 2, ly = my + ny * 12 + ny * 11;
      const clear = (x, y) => !placed.some(r => Math.abs(r[0] - x) < (r[2] + w) / 2 + 4 && Math.abs(r[1] - y) < 26) &&
        !avoid.some(b => x - w / 2 < b[2] + 3 && b[0] - 3 < x + w / 2 && y - 11 < b[3] + 3 && b[1] - 3 < y + 11);
      for (let i = 0; i < 8 && !clear(lx, ly); i++) {
        lx += nx * 14; ly += (ny >= 0 ? 1 : -1) * 14;
      }
      // Still blocked (a crowded hub): the nearest clear place around the arc's middle, else the first one tried.
      if (!clear(lx, ly)) {
        const first = [mx + nx * 12 + nx * w / 2, my + ny * 12 + ny * 11];
        const near = [];
        for (let d = 14; d <= 168; d += 14) for (let a = 0; a < 360; a += 30) near.push([first[0] + Math.cos(a * Math.PI / 180) * d, first[1] + Math.sin(a * Math.PI / 180) * d]);
        [lx, ly] = near.find(([x, y]) => clear(x, y)) || first;
      }
      placed.push([lx, ly, w]);
      labels += `<g class="mp-lbl${leg.cancelled ? " cancel" : ""}${sel}" data-flight="${esc(leg.flight_no)}" style="--pc:${colour}" role="button" tabindex="0"
        transform="translate(${n(lx - w / 2)} ${n(ly - 11)})"><rect width="${w}" height="22" rx="6"/><text x="8" y="15">${parts}</text></g>`;
    }
    if (!me) return paths + labels;
    const at = aircraftAt(legs, t);
    let pos = null, angle = 0, where = "";
    if (at.leg) {
      const a = airports[at.leg.from], b = airports[at.leg.to], c = ctrl(a, b, bends.get(at.leg.flight_no) ?? 0.3);
      pos = toScreen(along(a, c, b, at.k));
      angle = heading(toScreen(a), toScreen(c), toScreen(b), at.k) + 90;
      where = `${at.leg.flight_no} in flight`;
    } else if (at.ground && airports[at.ground]) {
      pos = toScreen(airports[at.ground]);
      where = `At ${at.ground}`;
    }
    if (!pos) return paths + labels;
    const out = me.unavailable && t >= me.unavailable[0] && t < me.unavailable[1];
    // In flight, the aircraft opens the flight it flies.
    const flying = at.leg ? ` data-flight="${esc(at.leg.flight_no)}" role="button" tabindex="0" aria-label="${esc(`${me.registration}, ${where}`)}"` : "";
    return `${paths}${labels}<g class="mp-me${at.leg && at.leg.flight_no === selected ? " sel" : ""}"${flying} style="--c:${TONES[me.tone] || TONES.progress}" transform="translate(${n(pos[0])} ${n(pos[1])})">
      <circle class="mp-me-ring" r="17"/><path fill="#fff" d="${PLANE}" transform="rotate(${Math.round(angle)}) scale(1.35)"/>
      ${me.kind ? `<g transform="translate(-41 -12)" style="color:var(--c)"><circle cx="12" cy="12" r="13" class="mp-me-badge"/><g transform="translate(3 3)">${icon(me.kind, 18)}</g></g>` : ""}
      <text x="24" y="-6">${esc(me.registration)}</text><text x="24" y="10" class="${out ? "out" : "sub"}">${esc(where)}${out ? ` · out of service until ${clock(me.unavailable[1])}` : ""}</text></g>`;
  }

  // A storm at its airport (Task 4c): a dashed area sized to the zoom and, on its ocean side, the weather mark with the window; the
  // mark names the storm and its rate on hover or focus (tips.js).
  function stormSpot(at, scale) {
    const [x, y] = at, r = Math.max(44, Math.min(160, scale * 1.5));
    const angle = (FAN.LIS ?? -150) * Math.PI / 180;
    return { x, y, r, bx: x + Math.cos(angle) * r, by: y + Math.sin(angle) * r };
  }
  // Where the mark and its window sit on screen, for labels to keep clear of.
  function stormBoxes(at, scale, tag = "") {
    if (!at) return [];
    const { bx, by } = stormSpot(at, scale);
    return [[bx - 14, by - 14, bx + 14, by + 14], ...(tag ? [[bx - 20 - String(tag).length * 7, by - 9, bx - 20, by + 5]] : [])];
  }
  // The storm's airport name drawn again above its routes, which all meet there, so it reads in full.
  function stormStation(at, code) {
    return at && code ? `<text class="mp-storm-ap" x="${n(at[0] + 7)}" y="${n(at[1] + 4)}">${esc(code)}</text>` : "";
  }
  // A crosswind (final review: "strong crosswinds ... only one runway can be used"): the runway in use and the wind blowing
  // across it, instead of a storm cell; the runway stays open at a reduced rate. Bearings in degrees from north.
  function crosswind(x, y, scale, { runway, from }) {
    const L = Math.max(48, Math.min(120, scale * 0.8));
    const unit = (deg) => [Math.sin(deg * Math.PI / 180), -Math.cos(deg * Math.PI / 180)];
    const [rx, ry] = unit(runway);
    const strip = `<line class="mp-runway" x1="${n(x - rx * L / 2)}" y1="${n(y - ry * L / 2)}" x2="${n(x + rx * L / 2)}" y2="${n(y + ry * L / 2)}"/>`;
    const [dx, dy] = unit((from + 180) % 360), [px, py] = [-dy, dx];
    const arrows = [-0.42, 0, 0.42].map((offset) => {
      const cx = x + px * offset * L, cy = y + py * offset * L;
      const [sx, sy, ex, ey] = [cx - dx * L * 1.1, cy - dy * L * 1.1, cx + dx * L * 0.15, cy + dy * L * 0.15];
      const head = [[ex, ey], [ex - dx * 10 + px * 5.5, ey - dy * 10 + py * 5.5], [ex - dx * 10 - px * 5.5, ey - dy * 10 - py * 5.5]];
      return `<line x1="${n(sx)}" y1="${n(sy)}" x2="${n(ex - dx * 8)}" y2="${n(ey - dy * 8)}"/><polygon points="${head.map(p => p.map(n).join(",")).join(" ")}"/>`;
    }).join("");
    return `<g class="mp-crosswind">${strip}<g class="mp-wind">${arrows}</g></g>`;
  }
  // overlay: a crosswind's runway and wind direction; without one, the forecast's area rings the airport.
  function storm(at, scale, { title, text, tag, overlay = null }) {
    if (!at) return "";
    const { x, y, r, bx, by } = stormSpot(at, scale);
    const area = overlay ? crosswind(x, y, scale, overlay) : `<circle class="mp-storm-area" cx="${n(x)}" cy="${n(y)}" r="${n(r)}"/>`;
    return `<g class="mp-storm">${area}
      <g class="mp-storm-badge" transform="translate(${n(bx - 14)} ${n(by - 14)})" tabindex="0" role="img" aria-label="${esc(title)}"
        data-tip-title="${esc(title)}" data-tip="${esc(text || title)}"><circle cx="14" cy="14" r="14"/><g transform="translate(3 3)">${icon("weather", 22)}</g></g>
      ${tag ? `<text class="mp-storm-tag" x="${n(bx - 20)}" y="${n(by + 4)}" text-anchor="end">${esc(tag)}</text>` : ""}</g>`;
  }
  // A storm's plan moves dozens of flights on many aircraft (Task 4c): one arc per route it moves flights on, in the plan's colour,
  // weighted by how many. No aircraft is followed; cancelled legs are drawn by rotation. Each arc opens the plan's flights on that
  // route and previews them on hover (Task 3b); `label` names the route for assistive technology.
  function spread(legs, airports, toScreen, { colour, selected = null, label = (key) => key }) {
    const byRoute = new Map();
    for (const leg of legs) {
      if (leg.absent || leg.cancelled || !(leg.delta || leg.moved) || !airports[leg.from] || !airports[leg.to]) continue;
      const key = routeKey(leg.from, leg.to);
      byRoute.set(key, [...(byRoute.get(key) || []), leg]);
    }
    return [...byRoute].map(([key, moved]) => {
      const [a, b] = key.split("-").map(code => airports[code]);
      const A = toScreen(a), B = toScreen(b), C = toScreen(ctrl(a, b, 0.22)), d = `M${A.map(n)}Q${C.map(n)} ${B.map(n)}`;
      return `<g class="mp-spread-g${key === selected ? " sel" : ""}" data-route="${esc(key)}" data-kind="plan" role="button" tabindex="0"
        aria-label="${esc(label(key))}"><path class="mp-leg moved mp-spread" style="--pc:${colour};stroke-width:${(1.2 + Math.min(moved.length, 6) * 0.3).toFixed(2)}"
        d="${d}"/><path class="mp-hit" d="${d}"/></g>`;
    }).join("");
  }

  // Schedule against the selected plan, 06:00 to 06:00 the next day; a defect's window hatched.
  // Without a plan (label null) the schedule is drawn once.
  function strip(legs, { unavailable, now, t, width, label, colour }) {
    const L = 128, R = 12, T0 = 360, T1 = 1800, span = Math.max(80, width - L - R);
    const bottom = label ? 78 : 46, height = bottom + 12;
    const X = (m) => L + (Math.min(Math.max(m, T0), T1) - T0) / (T1 - T0) * span;
    const step = span < 700 ? 240 : 120;
    let svg = '<defs><pattern id="mp-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" fill="rgba(240,82,79,.35)"/></pattern></defs>';
    for (let m = T0; m <= T1; m += step) {
      const anchor = m === T0 ? "start" : m + step > T1 ? "end" : "middle";
      svg += `<line x1="${n(X(m))}" x2="${n(X(m))}" y1="8" y2="${bottom}" class="mp-grid"/><text x="${n(X(m))}" y="${bottom + 10}" text-anchor="${anchor}">${clock(m)}</text>`;
    }
    if (unavailable) svg += `<rect class="mp-out" x="${n(X(unavailable[0]))}" y="6" width="${n(X(unavailable[1]) - X(unavailable[0]))}" height="${bottom - 12}" fill="url(#mp-hatch)"/>`;
    const bar = (leg, start, y, style) => {
      const x = X(start), w = Math.max(3, X(start + leg.dur) - x);
      return `<g class="mp-bar"><rect x="${n(x)}" y="${y}" width="${n(w)}" height="22" rx="5" fill="${style.fill}" stroke="${style.stroke || "none"}"
        stroke-dasharray="${style.dash || ""}"/>${w > 32 ? `<text x="${n(x + 3)}" y="${y + 15}" class="${leg.cancelled && style.dash ? "struck" : ""}"
        style="fill:${style.text || "#04121c"}">${esc(leg.flight_no)}</text>` : ""}</g>`;
    };
    const drawn = legs.filter(l => !l.absent);
    const base = (leg, start) => ({ fill: start + leg.dur <= now ? "#3a4a64" : "#2a6f8c" });
    svg += `<text x="0" y="27" class="mp-row">Schedule</text>${drawn.map(l => bar(l, l.was, 12, base(l, l.was))).join("")}`;
    if (label) {
      svg += `<text x="0" y="59" class="mp-row">${esc(label)}</text>${drawn.map(l => bar(l, l.std, 44,
        l.cancelled ? { fill: "transparent", stroke: TONES.stopped, dash: "4 3", text: TONES.stopped } : l.delta || l.moved ? { fill: colour } : base(l, l.std))).join("")}`;
    }
    svg += `<line x1="${n(X(now))}" x2="${n(X(now))}" y1="6" y2="${bottom - 2}" class="mp-nowline"/><line x1="${n(X(t))}" x2="${n(X(t))}" y1="6" y2="${bottom - 2}" class="mp-tline"/>`;
    return { svg, height, toMinutes: (x) => T0 + (x - L) / span * (T1 - T0) };
  }

  return Object.freeze({ TONES, KINDS, CITY, PLANE, esc, project, ctrl, along, heading, minutes, clock, kindOf, icon, landPath, routeKey,
    routes, traffic, markers, markerBoxes, dayLegs, aircraftAt, rotation, strip, storm, stormBoxes, stormStation, spread });
})();
