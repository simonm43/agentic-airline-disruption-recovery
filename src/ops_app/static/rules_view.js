"use strict";
// Rules screen (control tower and rules spec §3.2, §5.4): the editable autonomy limits under four-eyes approval,
// the fixed rules from policy.RULE_CATALOGUE, the OCC playbook (read only, PlanView.catalogue), the waiting change and the history.
// Renders GET /api/rules; proposals and decisions post through the operator api() helper, then reload().

class RulesView {
  static RELEASE = "arrow-release";
  static GROUPS = [
    ["occ", "Always decided by OCC", "Reserved for OCC"],
    ["safety", "Legal and safety", "Changed only by a reviewed release"],
    ["expert", "Passenger Services", "Expert review"]
  ];
  static WHEN = {
    aircraft: "Any swap or standby aircraft",
    cancellation: "Any cancelled flight",
    airport: "Night quota, curfew, new slot",
    declined_rebooking: "Rebooking declined for passengers who lose their flight",
    weather: "A weather forecast moves flights across the network",
    crew: "Duty over limit and no standby crew resolves it",
    standby: "More than two standby crews, or a base's last complete standby crew of a qualification",
    capacity: "Plan does not fit",
    answered: "Missing or unknown domain check",
    passenger_services: "No rebooking within policy"
  };
  // EU261 Art. 6 care thresholds, as policy.care_threshold_minutes applies them to each plan's departure delays.
  static LONG_DELAYS = ["Late 2 h+ · ≤ 1,500 km", "Late 3 h+ · ≤ 3,500 km or in the EU", "Late 4 h+ · longer"];
  static TONE = { "OCC decides": "needs", "Blocks plan": "stopped", "Passenger Services decides": "expert" };
  static ROUTE = { occ: "gone to OCC", auto: "run automatically", blocked: "been blocked" };

  constructor(api, reload, dialog = document.getElementById("rule-dialog")) {
    this.api = api;
    this.reload = reload;
    this.dialog = dialog;
    this.container = null;
    this.rules = null;
    this.session = null;
    this.cases = [];
    this.html = null;
    this.busy = false;
    this.error = "";
    this.euro = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
    this.count = new Intl.NumberFormat("en-GB");
    this.clock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Lisbon" });
    this.date = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Lisbon" });
    this.dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "Europe/Lisbon" });
    dialog.querySelector("form").addEventListener("submit", (event) => this.propose(event));
    dialog.querySelector("form").addEventListener("input", () => this.notice(""));
    dialog.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => dialog.close()));
  }

  esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  // Names, not object IDs: there is no directory look-up.
  who(oid) {
    if (oid === RulesView.RELEASE) return "ARROW release";
    if (oid && oid === this.session?.user?.oid) return this.session.user.name || "You";
    return "Another duty manager";
  }
  when(value) {
    const time = Date.parse(value || "");
    if (!Number.isFinite(time)) return "";
    const at = new Date(time);
    const clock = this.clock.format(at);
    return this.date.format(at) === this.session?.day ? clock : `${this.dayMonth.format(at)} ${clock}`;
  }
  unit(n) { return `<small>passenger${n === 1 ? "" : "s"}</small>`; }
  rule(key, fallback) { return (this.rules?.catalogue || []).find(r => r.key === key)?.name || fallback; }
  message(error) {
    // The API's own reason for a refused proposal or decision (403, 409, 422).
    return [403, 409, 422].includes(error.status) && error.detail ? error.detail : (error.message || "The request failed.");
  }

  render(container, rules, session, cases = []) {
    this.container = container;
    this.rules = rules;
    this.session = session;
    this.cases = Array.isArray(cases) ? cases : [];
    if (!container.dataset.bound) {
      container.dataset.bound = "true";
      container.addEventListener("click", (event) => this.click(event));
    }
    const html = rules?.in_force ? this.page() : this.unavailable();
    // Unchanged rules keep the page as it is (focus, scroll).
    if (html !== this.html) {
      this.html = html;
      container.innerHTML = html;
    }
  }

  unavailable() {
    return `<div class="rl">
      <div class="rl-page-head"><div><h1>Rules</h1></div></div>
      <div class="tw-card rl-empty" role="alert"><b>Rules could not be loaded</b>
        <button class="secondary" type="button" data-retry ${this.busy ? "disabled" : ""}>Retry</button></div>
    </div>`;
  }

  page() {
    const { in_force: limits } = this.rules;
    const source = limits.approved_by === RulesView.RELEASE ? "ARROW release values"
      : `approved ${this.when(limits.approved_at)} · ${this.who(limits.approved_by)}`;
    const groups = RulesView.GROUPS.map(([group, title, note]) => {
      const rows = (this.rules.catalogue || []).filter(r => r.group === group);
      return rows.length ? `${this.title(title, note)}<div class="rl-fixed">${this.columns()}${rows.map(r => this.row(r)).join("")}</div>` : "";
    }).join("");
    return `<div class="rl">
      <div class="rl-page-head"><div><h1>Rules</h1>
        <p>Version ${this.esc(limits.version)} in force · ${this.esc(source)} · each case records the version it used</p></div></div>
      ${this.error ? `<p class="rl-notice" role="alert">${this.esc(this.error)}</p>` : ""}
      <div class="rl-grid">
        <div class="rl-main">
          ${this.title("Autonomy limits", "OCC can change · a second duty manager approves")}
          <div class="rl-limits">${this.limits()}</div>
          ${groups}
          ${typeof PlanView === "undefined" ? "" : PlanView.catalogue(this.rules.playbook)}
        </div>
        <aside class="rl-side">${this.pending()}${this.history()}</aside>
      </div>
    </div>`;
  }
  title(title, note) {
    return `<div class="rl-title"><h2>${this.esc(title)}</h2><span>${this.esc(note)}</span></div>`;
  }

  // Today's case nearest each limit, from the control tower's rows; a blocked plan cannot run, so it is left out.
  nearest() {
    const rows = this.cases.filter(row => row.impact && row.impact.route !== "blocked");
    const name = (row) => String(row.headline || row.display_id || "").split(" · ")[0];
    const numbered = (key) => rows.filter(row => typeof row.impact[key] === "number" && Number.isFinite(row.impact[key]));
    const cap = this.rules.in_force.cost_cap_eur;
    const cost = numbered("cost_eur").sort((a, b) => Math.abs(a.impact.cost_eur - cap) - Math.abs(b.impact.cost_eur - cap))[0];
    // The rebooking limit checks rebooked passengers (impact.rebooked), not disrupted ones.
    const rebooked = numbered("rebooked").sort((a, b) => b.impact.rebooked - a.impact.rebooked)[0];
    return {
      cost: cost ? `Closest today: ${name(cost)} ${this.euro.format(cost.impact.cost_eur)}` : "No plans today",
      rebooked: rebooked ? `Highest today: ${name(rebooked)} ${this.count.format(rebooked.impact.rebooked)} rebooked` : "No plans today"
    };
  }
  limits() {
    const { in_force: limits, pending, can_change: canChange } = this.rules;
    const near = this.nearest();
    const button = (changing) => {
      const why = pending ? "A change is already waiting for approval" : canChange ? "" : "Only an OCC duty manager changes the rules";
      return `<button class="secondary" type="button" data-propose ${pending || !canChange || this.busy ? "disabled" : ""}
        ${why ? `title="${this.esc(why)}"` : ""}>${changing ? "Change proposed" : "Propose change"}</button>`;
    };
    const card = (key, value, name, effect, foot, changing) => `<div class="rl-limit" data-rule="${key}">
      <div class="rl-val num">${value}</div><h3>${this.esc(name)}</h3><p>${this.esc(effect)}</p>
      <div class="rl-foot"><span>${this.esc(foot)}</span>${button(changing)}</div></div>`;
    return card("cost", this.esc(this.euro.format(limits.cost_cap_eur)), this.rule("cost", "Cost limit for automatic plans"),
      "Above it, OCC decides.", near.cost, pending && pending.cost_cap_eur !== limits.cost_cap_eur) +
      card("rebooking", `${this.esc(this.count.format(limits.rebook_limit))} ${this.unit(limits.rebook_limit)}`,
        this.rule("rebooking", "Rebooking limit"), "More rebooked, OCC decides.", near.rebooked,
        pending && pending.rebook_limit !== limits.rebook_limit);
  }
  // ARROW's code checks every plan against the fixed rules; the pill is what follows when a rule triggers.
  columns() {
    return '<div class="rl-cols" aria-hidden="true"><span></span><span>Rule</span><span>Checked automatically · triggers when</span><span>Then</span></div>';
  }
  row(rule) {
    const when = rule.key === "eu261"
      ? `<div class="rl-eu">${RulesView.LONG_DELAYS.map(t => `<span>${this.esc(t)}</span>`).join("")}</div>`
      : `<small class="rl-when">${this.esc(RulesView.WHEN[rule.key] || "")}</small>`;
    return `<div class="rl-row" data-rule="${this.esc(rule.key)}"><span class="rl-lock" role="img" aria-label="Rule set by release"
      title="Not editable in ARROW: changed only by a reviewed release">🔒</span>
      <div class="rl-name"><b>${this.esc(rule.name)}</b><small>${this.esc(String(rule.source || "").replace(/;\s*/g, " · "))}</small></div>
      ${when}<span class="rl-state" data-tone="${RulesView.TONE[rule.effect] || "needs"}">${this.esc(rule.effect)}</span></div>`;
  }

  pending() {
    const { in_force: limits, pending, can_change: canChange } = this.rules;
    if (!pending) return "";
    const changes = [];
    if (pending.cost_cap_eur !== limits.cost_cap_eur) {
      changes.push([this.rule("cost", "Cost limit for automatic plans"),
        `<s>${this.esc(this.euro.format(limits.cost_cap_eur))}</s><span aria-hidden="true">→</span>${this.esc(this.euro.format(pending.cost_cap_eur))}`]);
    }
    if (pending.rebook_limit !== limits.rebook_limit) {
      changes.push([this.rule("rebooking", "Rebooking limit"),
        `<s>${this.esc(this.count.format(limits.rebook_limit))}</s><span aria-hidden="true">→</span>${this.esc(this.count.format(pending.rebook_limit))} ${this.unit(pending.rebook_limit)}`]);
    }
    const mine = Boolean(pending.proposed_by) && pending.proposed_by === this.session?.user?.oid;
    const off = this.busy ? "disabled" : "";
    const actions = mine
      ? `<button class="primary" type="button" disabled>Approve</button>
         <button class="secondary" type="button" data-action="withdraw" ${canChange ? off : "disabled"}>Withdraw</button>`
      : `<button class="primary" type="button" data-action="approve" ${canChange ? off : "disabled"}>Approve</button>
         <button class="secondary" type="button" data-action="reject" ${canChange ? off : "disabled"}>Reject</button>`;
    const hint = mine ? "You proposed this change, so another duty manager approves it. Applies to plans built after approval."
      : canChange ? "Applies to plans built after approval." : "Only an OCC duty manager decides. Applies to plans built after approval.";
    return `<section class="rl-change" aria-labelledby="rl-change-title" data-change="${this.esc(pending.change_id)}">
      <h2 id="rl-change-title">Change waiting for approval</h2>
      ${changes.map(([, diff]) => `<div class="rl-diff num">${diff}</div>`).join("")}
      <dl class="rl-kv">
        <dt>Rule</dt><dd>${changes.map(([name]) => this.esc(name)).join("<br>")}</dd>
        <dt>Proposed by</dt><dd>${this.esc(this.who(pending.proposed_by))} · ${this.esc(this.when(pending.proposed_at))}</dd>
        <dt>Reason</dt><dd>${this.esc(pending.reason)}</dd>
      </dl>
      ${pending.effect === null ? "" : `<div class="rl-effect">${this.effect(pending.effect)}</div>`}
      <div class="rl-actions">${actions}</div>
      <p class="rl-hint">${this.esc(hint)}</p>
    </section>`;
  }
  // The listed plans whose route would differ under the proposed limits (spec §3.4); plans already built keep theirs.
  // A null effect (today's cases could not be read) shows no line at all.
  effect(items) {
    const byRoute = new Map();
    (Array.isArray(items) ? items : []).forEach(item => {
      const name = String(item.headline || item.display_id || "").split(" · ")[0] || item.display_id;
      const names = byRoute.get(item.proposed) || new Set();
      names.add(name);
      byRoute.set(item.proposed, names);
    });
    if (!byRoute.size) return "On today's plans: no change.";
    const parts = [...byRoute].map(([route, names]) =>
      `<b>${[...names].map(n => this.esc(n)).join(", ")}</b> would have ${this.esc(RulesView.ROUTE[route] || `gone to ${route}`)}`);
    return `On today's plans: ${parts.join("; ")}. Others unchanged.`;
  }

  history() {
    const history = Array.isArray(this.rules.history) ? this.rules.history : [];
    if (!history.length) return "";
    const rows = history.map(change => {
      const seed = change.proposed_by === RulesView.RELEASE;
      const base = history.find(c => c.status === "approved" && c.version === change.base_version);
      const diff = seed || !base ? [] : [
        change.cost_cap_eur !== base.cost_cap_eur ? `Cost limit → ${this.euro.format(change.cost_cap_eur)}` : "",
        change.rebook_limit !== base.rebook_limit ? `Rebooking → ${this.count.format(change.rebook_limit)}` : ""
      ].filter(Boolean);
      const what = diff.length ? diff.join(" · ")
        : `Cost limit ${this.euro.format(change.cost_cap_eur)} · rebooking ${this.count.format(change.rebook_limit)}`;
      const status = {
        proposed: `Proposed ${this.when(change.proposed_at)} · waiting`,
        approved: `Approved ${this.when(change.decided_at)} · ${this.who(change.decided_by)}`,
        rejected: `Rejected ${this.when(change.decided_at)} · ${this.who(change.decided_by)}`,
        withdrawn: `Withdrawn ${this.when(change.decided_at)}`
      }[change.status] || change.status;
      const version = change.version ?? (change.status === "proposed" ? change.base_version + 1 : null);
      return `<tr data-status="${this.esc(change.status)}"><td>${version === null ? "–" : `v${this.esc(version)}`}</td>
        <td${seed ? "" : ` title="${this.esc(change.reason)}"`}>${this.esc(what)}<br><small>${this.esc(seed ? change.reason : status)}</small></td></tr>`;
    }).join("");
    return `<section class="tw-card rl-history"><h2>History</h2><table><tbody>${rows}</tbody></table></section>`;
  }

  click(event) {
    if (event.target.closest("[data-retry]")) return this.retry();
    if (event.target.closest("[data-propose]")) return this.open();
    const action = event.target.closest("[data-action]");
    if (action) this.decide(action.dataset.action);
  }
  async retry() {
    this.busy = true;
    this.redraw();
    try { await this.reload(); } finally { this.busy = false; this.redraw(); }
  }
  redraw() { if (this.container) this.render(this.container, this.rules, this.session, this.cases); }
  clearError() {
    this.error = "";
    this.redraw();
  }

  open() {
    const limits = this.rules?.in_force;
    if (!limits || this.rules.pending || !this.rules.can_change) return;
    const form = this.dialog.querySelector("form");
    form.elements.cost_cap_eur.value = String(limits.cost_cap_eur);
    form.elements.rebook_limit.value = String(limits.rebook_limit);
    form.elements.reason.value = "";
    this.notice("");
    this.dialog.showModal();
    form.elements.cost_cap_eur.focus();
  }
  notice(text) {
    const notice = this.dialog.querySelector("[data-notice]");
    notice.hidden = !text;
    notice.textContent = text;
  }
  async propose(event) {
    event.preventDefault();
    const form = event.target;
    const send = form.querySelector("[type=submit]");
    if (send.disabled) return;
    const values = {};
    for (const [key, label] of [["cost_cap_eur", "Cost limit"], ["rebook_limit", "Rebooking limit"]]) {
      const text = form.elements[key].value.trim();
      if (!/^\d+$/.test(text)) {
        this.notice(`${label} must be a whole number from 0`);
        return;
      }
      values[key] = Number(text);
    }
    const reason = form.elements.reason.value.trim();
    if (reason.length < 3) {
      this.notice("A reason of 3 to 300 characters is required");
      return;
    }
    send.disabled = true;
    this.notice("");
    try {
      await this.api("/api/rules/proposals", { method: "POST", body: JSON.stringify({ ...values, reason }) });
      this.error = "";
      this.dialog.close();
    } catch (error) {
      this.notice(this.message(error));
    } finally {
      send.disabled = false;
    }
    await this.reload();
  }
  async decide(action) {
    const pending = this.rules?.pending;
    if (!pending || this.busy) return;
    this.busy = true;
    this.error = "";
    this.redraw();
    try {
      await this.api(`/api/rules/proposals/${encodeURIComponent(pending.change_id)}/${action}`, { method: "POST" });
    } catch (error) {
      this.error = this.message(error);
    }
    this.busy = false;
    // Reload either way: a refused decision usually means the page was stale.
    await this.reload();
    this.redraw();
  }
}
