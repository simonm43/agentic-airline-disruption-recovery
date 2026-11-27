async (page) => {
  const base = await page.evaluate(() => new URLSearchParams(location.search).get("base")) ||
    "http://127.0.0.1:8768";
  const entry = "/src/testbench/static/index.html";
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  // Owner: the same professional-screen check as the operator smoke (visible text and hover cards).
  const AMATEUR = [/\bDev\b/, /\bplayed\b/i, /ca-airrec-/, /Crew roster/, /Reservations/, /Airport slots/, /Names never/i, /[0-9a-f]{12,}/i,
    /\bact-\d/, /\b[0-9a-f]{4}-\d{2}-[a-z]/i, /id-airrec-/];
  const professional = async (where) => {
    const { text, tips } = await page.evaluate(() => {
      const seen = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node.parentElement;
        if (el && node.textContent.trim() && !el.closest(".iface, script, style") && el.checkVisibility({ visibilityProperty: true })) seen.push(node.textContent);
      }
      const hover = [...document.querySelectorAll("[data-tip], [data-tip-title], [title]")]
        .map(e => [e.dataset.tip, e.dataset.tipTitle, e.getAttribute("title")].filter(Boolean).join(" "));
      return { text: seen.join(" "), tips: hover.join(" ") };
    });
    for (const pattern of AMATEUR) {
      const hit = (text.match(pattern) || tips.match(pattern) || [])[0];
      check(!hit, `${where}: "${hit}" should not show (${pattern})`);
    }
  };
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  const posts = [];
  let eventsStatus = 200;
  let resetSkip = false;
  let resetFailOnce = false;
  let resetPolls = 0;
  let resetState = { state: "idle", closed: [], skipped: [], message: "" };
  const sentAt = "2026-11-10T09:12:03+00:00";
  const envelope = { specversion: "1.0", id: "e-1", source: "arrow-testbench", type: "com.arrow.disruption.flight_delay",
    time: sentAt, datacontenttype: "application/json",
    data: { flight_no: "AR259", delay_minutes: 20, cause: "staff_shortage", description: "Ground staff shortage at the gate" } };
  const events = [];
  const unstored = [];  // a sent event is listed at once; ARROW reads it from the queue a moment later
  const row = (id, outcome, extra = {}) => ({ event_id: id, sent_at: sentAt, label: "AR259 +20 · staff shortage",
    queued_after_s: 2, case: null, case_after_s: null, outcome, reason: null, ...extra });
  const presets = [
    ["ar259-20", 1, "Case 1 · Simple delay", "AR259 · 20 min late · border-control queue · retimed automatically", "Executed automatically"],
    ["ar444-70", 2, "Case 2 · Delay with rebooking", "AR444 · 70 min late", "Executed automatically"],
    ["cstqa-aog", 3, "Case 3 · Aircraft defect", "CS-TQA · out of service 4 h", "Waiting for OCC"],
    ["ar689-60", 4, "Case 4 · Crew limit", "AR689 · 60 min late", "Waiting for OCC"],
    ["lppt-taf", 5, "Case 5 · Crosswind forecast", "Terminal Aerodrome Forecast (TAF) · Lisbon · strong crosswind 16:00–18:00 · gusts 33 kt across runway 02/20 · 24 movements an hour · plans for OCC", "Waiting for OCC"],
    // The forecast's two weather updates (Task 1), sent once OCC approved the weather plan.
    ["storm-confirmed", 5, "Case 5 · Crosswind confirmed (METAR)", "Meteorological Aerodrome Report (METAR) · Lisbon · strong crosswind 16:00–18:00 confirmed · held actions sent after a re-check",
      "Executed after approval", "weather_update"],
    ["storm-dropped", 5, "Case 5 · Forecast dropped (TAF amended)", "Terminal Aerodrome Forecast (TAF) amended · Lisbon · no strong crosswind 16:00–18:00 · standby crews released, passengers told no change",
      "Stood down", "weather_update"],
  ].map(([key, n, label, title, expected, kind = "message"]) => ({ key, case: n, label, kind, title, expected }));

  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    if (request.method() === "POST") posts.push({ path: url.pathname, body });
    let status = 200;
    let data;
    if (url.pathname === "/api/status") {
      data = { can_send: true, day_loaded_at: "2026-11-10T00:05:00+00:00", today: "2026-11-10",
        ops_url: "https://ops.test", reset: resetState };
    } else if (url.pathname === "/api/presets") {
      data = { presets };
    } else if (url.pathname === "/api/events" && request.method() === "POST") {
      const id = `e-${events.length + unstored.length + 1}`;
      const chosen = presets.find(item => item.key === body?.preset);
      unstored.push(row(id, "Waiting for ARROW", { polls: 0,
        expected: chosen ? { case: chosen.case, title: chosen.title, outcome: chosen.expected } : null }));
      data = { event_id: id, sent_at: sentAt };
    } else if (url.pathname === "/api/events") {
      if (eventsStatus !== 200) { status = eventsStatus; data = { detail: "ARROW unavailable" }; }
      else {
        for (const item of unstored.splice(0).filter(item => ++item.polls > 1 || !unstored.push(item))) {
          item.polls = 0;
          events.unshift(item);
        }
        for (const item of events) {
          if (item.outcome === "Waiting for ARROW" && ++item.polls > 1) {
            Object.assign(item, { outcome: "Executed automatically", case: { display_id: "DSR-20261110-A1B2C3", trace_id: "trace-a1" },
              case_after_s: 5 });
          }
        }
        data = { day: "2026-11-10", events: events.map(({ polls, ...item }) => item) };
      }
    } else if (url.pathname.startsWith("/api/events/")) {
      data = { row: events[0], sent: envelope,
        queued: { message_id: "e-1", sequence_number: 142799, enqueued_at: "2026-11-10T09:12:05+00:00", delivery_count: 1 },
        delivered: [{ at: "2026-11-10T09:12:08Z", sequence_number: 142799, delivery_count: 1, delivery: "new_case",
          body: { ...envelope, data: { ...envelope.data, delay_minutes: 25 } } }],
        rejection: null,
        outcome: { summary: "AR259 leaves 20 minutes late; only the slot moves.", status: "assessment_ready",
          plans: [{ kind: "delay", route: "auto", reasons: ["within every domain's limits"], cost_eur: 900, recommended: true }],
          execution: { status: "completed", approved_by: "policy", actions: [{ label: "Ops control", status: "confirmed", message: "Retimed AR259 10:50" }] } } };
    } else if (url.pathname === "/api/reset" && request.method() === "POST") {
      resetPolls = 0;
      resetState = { state: "closing", closed: [], skipped: [], message: "Closing cases", elapsed_s: 0 };
      status = 202;
      data = resetState;
    } else if (url.pathname === "/api/reset" && resetFailOnce) {
      resetFailOnce = false;  // one failed status check while the server is still resetting
      status = 503;
      data = { detail: "Service unavailable" };
    } else if (url.pathname === "/api/reset") {
      resetPolls += 1;
      if (resetSkip) resetState = { state: "stopped", closed: [], skipped: [{ display_id: "DSR-20261110-B2B2B2", reason: "executing" }],
        message: "DSR-20261110-B2B2B2 still executing; retry in a minute" };
      else if (resetPolls === 1) resetState = { state: "loading_day", closed: ["DSR-20261110-A1B2C3"], skipped: [], message: "Reloading the day", elapsed_s: 42 };
      else resetState = { state: "done", closed: ["DSR-20261110-A1B2C3"], skipped: [], message: "Done · day reloaded", took_s: 83 };
      data = resetState;
    } else {
      status = 404;
      data = { detail: "Not found" };
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}${entry}`);
  // Sending is always on: Service Bus needs no capacity started first.
  await page.locator("#day").getByText("Day 2026-11-10").waitFor();
  check(!await page.locator("#send").isDisabled() && !await page.locator("#presets button").first().isDisabled(),
    "Send was not available");
  check(await page.locator("#fabric-pill").count() === 0, "The page still had a Fabric control");
  check((await page.locator("#events").innerText()).includes("No events"), "An empty day did not say so");

  // Demo cases: numbered, each with its expected outcome; one click, the row moves to its outcome.
  const cards = await page.locator("#presets").innerText();
  check(["Case 1", "Case 2", "Case 3", "Case 4", "Case 5 · Crosswind forecast", "Terminal Aerodrome Forecast (TAF) · Lisbon", "Expected: Waiting for OCC", "Expected: Executed automatically"]
    .every(text => cards.includes(text)), `The demo cases lacked numbers or expected outcomes: ${cards}`);
  // Owner: hovering an expected outcome says what it means.
  const means = await page.locator("#presets .case-expected").first().getAttribute("title");
  check(/ARROW carries the plan out/.test(means || ""), `An expected outcome should explain itself: ${means}`);
  await page.getByRole("button", { name: /Case 1 · Simple delay/ }).click();
  check(JSON.stringify(posts.at(-1)) === JSON.stringify({ path: "/api/events", body: { preset: "ar259-20" } }),
    `The preset did not post its key: ${JSON.stringify(posts.at(-1))}`);
  await page.locator("#events").getByText("Waiting for ARROW").waitFor();
  await page.locator("#events").getByText("Executed automatically").waitFor({ timeout: 10000 });
  await page.locator("#events .expect[data-match=ok]").getByText("Case 1 · expected Executed automatically ✓").waitFor();
  const first = await page.locator("#events .event").first().innerText();
  check(first.includes("Queued ✓ +2 s") && first.includes("DSR-20261110-A1B2C3") && first.includes("+5 s"),
    `The row lacked its timings or case: ${first}`);
  check(await page.getByRole("link", { name: "Open in ARROW" }).getAttribute("href") === "https://ops.test/?case=trace-a1",
    "Open in ARROW did not deep-link to the case");

  // Details: the message at every step, with what changed on the way highlighted.
  await page.locator("#events .event-toggle").first().click();
  for (const title of ["Sent", "Queued", "Received by ARROW", "Outcome"]) {
    await page.locator(".step-box h3").getByText(title, { exact: true }).waitFor();
  }
  const highlighted = await page.locator(".diff").allInnerTexts();
  check(highlighted.length === 1 && highlighted[0].includes("25"), `The changed field was not highlighted alone: ${highlighted}`);
  check((await page.locator(".details").innerText()).includes("Retimed AR259 10:50"), "The outcome lacked the executed actions");
  check((await page.locator(".details").innerText()).includes("Operations control: confirmed") && !(await page.locator(".details").innerText()).includes("Ops control"),
    "The outcome should name the recipient as the operator app does");
  await page.screenshot({ path: ".playwright-cli\\testbench-1440.png", fullPage: true });
  await professional("Test Bench");

  // Form: one text box, as any airline system would write it; limits checked before sending.
  const before = posts.length;
  await page.getByLabel("What happened?").fill("ok");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.locator("#form-error").getByText("3–2,000 characters").waitFor();
  check(posts.length === before, "A too-short message was sent");
  await page.getByLabel("What happened?").fill("Hydraulic leak on CS-TQA, out of service for about 4 hours.");
  await page.getByLabel("Sending system").fill("Maintenance");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#send-result").textContent.includes("Sent"));
  check(JSON.stringify(posts.at(-1).body) === JSON.stringify({ kind: "message",
    data: { text: "Hydraulic leak on CS-TQA, out of service for about 4 hours.", system: "Maintenance" } }),
    `The form posted the wrong body: ${JSON.stringify(posts.at(-1).body)}`);

  // OCC decides in ARROW, not here (a Dev run): the list keeps itself current without a reload,
  // and an approval meets the expected "Waiting for OCC".
  await page.waitForFunction(() => document.querySelectorAll("#events .event").length === 2 &&
    !document.querySelector("#events").innerText.includes("Waiting for ARROW"), null, { timeout: 15000 });
  await page.waitForTimeout(4000);  // the last refresh for the sent event has run; nothing is pending
  const occ = row("e-occ", "Waiting for OCC", { label: "Hydraulic leak found on CS-TQA", case_after_s: 2,
    case: { display_id: "DSR-20261110-C3C3C3", trace_id: "trace-c3" },
    expected: { case: 3, title: "CS-TQA · out of service 4 h", outcome: "Waiting for OCC" } });
  events.unshift(occ);
  await page.locator("#events").getByText("Case 3 · expected Waiting for OCC ✓").waitFor({ timeout: 15000 });
  occ.outcome = "Executed after approval";
  await page.locator("#events").getByText("Executed after approval").waitFor({ timeout: 15000 });
  const approved = await page.locator("#events .event", { hasText: "C3C3C3" }).locator(".expect").innerText();
  check(approved.includes("✓") && !approved.includes("≠"), `An approval should meet "Waiting for OCC": ${approved}`);

  // Reset: close, reload, done. Nothing can be sent while it runs, and a failed status check does
  // not offer Reset again before the server reports the end (review). Then one still
  // executing stops it.
  const sendingOff = async () => await page.locator("#send").isDisabled() &&
    await page.locator("#presets button").first().isDisabled();
  resetFailOnce = true;
  await page.getByRole("button", { name: "Reset" }).click();
  await page.locator("#reset-progress").getByText("Closing cases").waitFor();
  check(await sendingOff(), "Sending stayed on during a reset");
  await page.locator("#reset-progress").getByText("Reloading the day").waitFor({ timeout: 15000 });
  check(await page.locator("#reset").isDisabled() && await sendingOff(),
    "Reset or sending came back while the day was reloading");
  // Owner: a reset showed nothing for 30 s. It now counts from the click, with a bar.
  check(/Reloading the day · \d:\d\d/.test(await page.locator("#reset-progress").innerText()) &&
    await page.locator("#reset-progress .bar span").count() === 1, "The reset showed no time or bar while running");
  await page.locator("#reset-progress").getByText("Done · day reloaded in 1:23").waitFor({ timeout: 10000 });
  check(await page.locator("#reset-progress .bar").count() === 0, "The bar stayed after the reset ended");
  check(!await page.locator("#reset").isDisabled() && !await sendingOff(), "Reset or sending stayed off after the reset");
  resetSkip = true;
  await page.getByRole("button", { name: "Reset" }).click();
  await page.locator("#reset-progress").getByText("DSR-20261110-B2B2B2 still executing; retry in a minute").waitFor({ timeout: 10000 });

  // A rejected event shows ARROW's reason.
  events.unshift(row("e-9", "Rejected", { reason: "data.delay_minutes: multiple of 5" }));
  await page.reload();
  await page.locator("#events").getByText("data.delay_minutes: multiple of 5").waitFor();

  // The storm in two stages (case summary and held storm spec §2): its update presets say when to send them and what to expect;
  // the storm case reads held until confirmed, stood down or executed after approval, each with its tone and meaning on hover. An
  // update settles the case opened earlier: the row shows that case, never a negative time to it; one still held is not yet its
  // outcome.
  const updates = await page.locator("#presets .case.update").allInnerTexts();
  check(updates.length === 2 && updates[0].includes("Case 5 · Crosswind confirmed (METAR)") && updates[0].includes("Expected: Executed after approval") &&
    updates[1].includes("Case 5 · Forecast dropped (TAF amended)") && updates[1].includes("Expected: Stood down") &&
    updates.every(text => text.includes("Weather update · send after OCC approves the weather plan")), `The two update presets should say when to send them: ${updates.join(" | ")}`);
  const updateMeans = await page.locator("#presets .case.update .case-expected").evaluateAll(items => items.map(item => item.getAttribute("title")));
  check(/re-check/.test(updateMeans[0] || "") && /released the standby crews/.test(updateMeans[1] || ""), `An update's expected outcome should explain itself: ${updateMeans.join(" | ")}`);
  const stormCase = { display_id: "DSR-20261110-F7348C", trace_id: "trace-storm" };
  const stormTitle = presets[4].title;
  events.unshift(
    row("e-taf", "Held until confirmed", { label: "TAF AMD LPPT 100920Z 1010/1112 19012KT 9999 SCT020 BKN040 BECMG 1013/1015 21018G…",
      case: stormCase, case_after_s: 3, expected: { case: 5, title: stormTitle, outcome: "Waiting for OCC" } }),
    row("e-early", "Held until confirmed", { label: "METAR LPPT 101420Z 21020G32KT 9999 VCTS FEW015 SCT025CB BKN040 19/15 Q1005 T…",
      case: stormCase, case_after_s: -270, expected: { case: 5, title: presets[5].title, outcome: "Executed after approval" } }),
    row("e-drop", "Stood down", { label: "TAF AMD LPPT 101310Z 1013/1112 21015KT 9999 SCT025 BKN040 TEMPO 1015/1019 2…",
      case: stormCase, case_after_s: -812, expected: { case: 5, title: presets[6].title, outcome: "Stood down" } }),
    // Sent before OCC approved: recorded on the case, which still waits for OCC.
    row("e-before", "Waiting for OCC", { label: "METAR LPPT 101020Z 21020G32KT 9999 VCTS FEW015 SCT025CB BKN040 19/15 Q1005 T…",
      case: stormCase, case_after_s: -60, expected: { case: 5, title: presets[5].title, outcome: "Executed after approval" } }));
  await page.reload();
  const stormRow = (id) => page.locator("#events .event").filter({ has: page.locator(`[data-event="${id}"]`) });
  await stormRow("e-drop").waitFor();
  const dropText = await stormRow("e-drop").innerText();
  const tones = await Promise.all(["e-taf", "e-early", "e-drop"].map(id => stormRow(id).locator(".outcome").getAttribute("data-tone")));
  check(dropText.includes("DSR-20261110-F7348C") && !/[-−]\d+ s|\+-/.test(dropText) && !(await stormRow("e-early").innerText()).includes("-270") &&
    (await stormRow("e-taf").innerText()).includes("DSR-20261110-F7348C +3 s") && tones.join(" ") === "wait wait good",
    `An update should show its storm case without a negative time, and each status its tone: ${dropText} · ${tones.join(" ")}`);
  check((await stormRow("e-taf").locator(".expect").getAttribute("data-match")) === "ok" && (await stormRow("e-drop").locator(".expect").getAttribute("data-match")) === "ok" &&
    (await stormRow("e-early").locator(".expect").getAttribute("data-match")) === "pending" &&
    (await stormRow("e-before").locator(".expect").getAttribute("data-match")) === "pending",
    "A held storm should meet Waiting for OCC; a stand-down its own; an update still held, or sent before OCC approved, is pending, never off");
  const heldMeans = await stormRow("e-taf").locator(".outcome").getAttribute("title");
  const downMeans = await stormRow("e-drop").locator(".outcome").getAttribute("title");
  check(/rest waits until the forecast is confirmed/.test(heldMeans || "") && /no change is expected/.test(downMeans || ""), `A storm status should explain itself on hover: ${heldMeans} | ${downMeans}`);
  await page.screenshot({ path: ".playwright-cli\\testbench-1440-storm.png", fullPage: true });
  await professional("Test Bench storm updates");

  // After a Reset, events whose cases it closed fold into one line; Show brings them back.
  events.push(row("e-old1", "Closed by reset", { earlier: true, case: { display_id: "DSR-20261110-OLD001", trace_id: "old-1" } }),
    row("e-old2", "Rejected", { earlier: true }));
  await page.reload();
  await page.locator("#events").getByText("2 earlier events closed by reset").waitFor();
  check(!(await page.locator("#events").innerText()).includes("DSR-20261110-OLD001"), "An event closed by reset was still listed");
  await page.locator("#events").getByRole("button", { name: "Show" }).click();
  await page.locator("#events").getByText("DSR-20261110-OLD001").waitFor();
  await page.locator("#events").getByRole("button", { name: "Hide" }).click();
  check(!(await page.locator("#events").innerText()).includes("DSR-20261110-OLD001"), "Hide did not fold the earlier events again");

  // ARROW unavailable is never an empty day.
  eventsStatus = 503;
  await page.reload();
  await page.locator("#events").getByText("ARROW unavailable").waitFor();
  check(!(await page.locator("#events").innerText()).includes("No events"), "ARROW unavailable read as an empty day");
  eventsStatus = 200;

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.locator("#events .event").first().waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    "The test bench overflows horizontally at 390 px");
  await page.screenshot({ path: ".playwright-cli\\testbench-390.png", fullPage: true });
  await professional("Test Bench 390 px");
  // Case 5 (Task 4c): the TAF preset sends like the others.
  const sentBefore = posts.length;
  await page.getByRole("button", { name: /Case 5 · Crosswind forecast/ }).click();
  for (let i = 0; i < 20 && posts.length === sentBefore; i++) await page.waitForTimeout(100);
  check(JSON.stringify(posts.at(-1)) === JSON.stringify({ path: "/api/events", body: { preset: "lppt-taf" } }),
    `The TAF preset did not post its key: ${JSON.stringify(posts.at(-1))}`);
  check(pageErrors.length === 0, `Unexpected browser errors: ${pageErrors.join("; ")}`);
  return "Test bench workflow: always-on sending, preset to outcome with timings and deep link, four-step details with the changed field highlighted, free-text form, reset done with its time and bar (sending off throughout, a failed check does not end it) and stopped, recipients named as in the operator app, rejected reason, earlier events folded, ARROW unavailable, 390 px; no live calls";
}
