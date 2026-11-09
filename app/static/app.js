(() => {
  "use strict";

  const HISTORY_KEY = "tarot_oracle_v3_history";
  const HISTORY_LIMIT = 200;      // readings kept in this browser
  const HISTORY_SENT = 60;        // readings sent to the server for synthesis

  const $ = (id) => document.getElementById(id);
  const els = {
    temp: $("temp"), random: $("randomSpread"), question: $("question"),
    manual: $("manualBtn"), auto: $("autoBtn"), stop: $("stopBtn"),
    status: $("status"), final: $("final"), exports: $("exports"),
    historyCount: $("historyCount"), exportAll: $("exportAll"), clear: $("clearHistory"),
  };

  let config = { auto_loop_max: 10, readings_per_hour: 30 };
  let running = false;
  let stopRequested = false;
  let controller = null;
  let lastRecord = null;

  fetch("/api/config").then((r) => r.ok ? r.json() : null).then((c) => { if (c) config = c; }).catch(() => {});

  // ------------------------------------------------------------ storage
  function loadHistory() {
    try {
      const data = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
      return Array.isArray(data) ? data : [];
    } catch { return []; }
  }
  function saveHistory(list) {
    let items = list.slice(-HISTORY_LIMIT);
    while (items.length) {
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(items)); return; }
      catch { items = items.slice(Math.ceil(items.length / 4)); } // storage full: drop oldest
    }
    try { localStorage.removeItem(HISTORY_KEY); } catch {}
  }
  function historyForServer() {
    return loadHistory().slice(-HISTORY_SENT).map((r) => ({
      timestamp: r.timestamp, question: r.question, spread_name: r.spread_name,
      roles: [], cards: (r.cards || []).map((c) => ({ name: c.name })),
      notes: "", reading: (r.reading || "").slice(0, 1200), related_questions: [],
      synthesis_update: r.synthesis_update || "",
      root_question: r.root_question || "", current_question: r.current_question || "",
    }));
  }
  function refreshHistoryUI() {
    const n = loadHistory().length;
    els.historyCount.textContent = n
      ? `${n} reading${n === 1 ? "" : "s"} saved in this browser · used for synthesis in later readings`
      : "No readings saved in this browser yet.";
    els.exportAll.disabled = !n || running;
    els.clear.disabled = !n || running;
  }

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function setStatus(text, mode = "") {
    els.status.textContent = text;
    els.status.className = "status" + (mode ? " " + mode : "");
  }
  function setRunning(on) {
    running = on;
    els.manual.disabled = on;
    els.auto.disabled = on;
    els.stop.disabled = !on;
    els.temp.disabled = on;
    els.random.disabled = on;
    refreshHistoryUI();
  }
  function randomChoice(list) {
    const buf = new Uint32Array(1);
    crypto.getRandomValues(buf);
    return list[buf[0] % list.length];
  }
  function atBottom() {
    const f = els.final;
    return f.scrollHeight - f.scrollTop - f.clientHeight < 24;
  }
  function stickToBottom(wasAtBottom) {
    if (wasAtBottom) els.final.scrollTop = els.final.scrollHeight;
  }

  // Turn the model's raw output format into readable HTML while it streams.
  const SECTION_LABELS = {
    "root research anchor": "Root research anchor",
    "current question / facet": "Current question / facet",
    "cards": "Cards",
    "reading": "Reading",
    "domain / technical lens": "Domain / technical lens",
    "synthesis with previous readings": "Synthesis with previous readings",
    "what to keep": "What to keep",
    "related_questions": "Related questions",
    "synthesis_update": "Synthesis update",
  };
  function inline(text) {
    return esc(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  }
  function renderReading(raw, streaming) {
    let text = raw.replace(/\r/g, "");
    let notesHtml = "";
    const nb = text.search(/SYMBOLIC_NOTES_BEGIN/i);
    if (nb !== -1) {
      const after = text.slice(nb + "SYMBOLIC_NOTES_BEGIN".length);
      const ne = after.search(/SYMBOLIC_NOTES_END/i);
      const notes = (ne === -1 ? after : after.slice(0, ne)).trim();
      notesHtml = `<div class="notes"><span class="notes-label">Symbolic notes</span>${inline(notes)}</div>`;
      text = text.slice(0, nb) + (ne === -1 ? "" : after.slice(ne + "SYMBOLIC_NOTES_END".length));
    }
    text = text.replace(/READING_BEGIN|READING_END/gi, "").replace(/^\s+/, "");

    // Sections already shown in the header above (and, once finished, the
    // clickable related questions) are not repeated in the reading text.
    const skip = new Set(["root research anchor", "current question / facet", "cards"]);
    if (!streaming) skip.add("related_questions");
    const out = [];
    let skipping = false;
    for (const line of text.split("\n")) {
      const t = line.trim();
      const title = /^\**title:\**\s*(.+)$/i.exec(t);
      if (title) { skipping = false; out.push(`<h3>${inline(title[1].replace(/\*+$/, ""))}</h3>`); continue; }
      const head = /^[#*\s]*([A-Za-z_ /]+?)[*\s]*:[*\s]*$/.exec(t);
      const key = head && head[1].trim().toLowerCase();
      if (key && SECTION_LABELS[key]) {
        skipping = skip.has(key);
        if (!skipping) out.push(`<h4>${SECTION_LABELS[key]}</h4>`);
        continue;
      }
      if (!skipping) out.push(inline(line.replace(/^#{1,6}\s+/, "")));
    }
    const body = out.join("\n").replace(/\n+(<h[34]>)/g, "\n$1").replace(/(<\/h[34]>)\n/g, "$1").replace(/^\n+|\n+$/g, "");
    return `${notesHtml}<div class="reading">${body}${streaming ? '<span class="caret"></span>' : ""}</div>`;
  }

  function metaHtml(plan) {
    const cards = plan.cards.map((c, i) => `
      <li class="card" data-suit="${esc(c.suit)}">
        <div><span class="n">${i + 1}.</span> <span class="name">${esc(c.name)}</span></div>
        <div class="role">${esc(plan.roles[i] || "")}</div>
        <div class="kw">${esc(c.keywords)}</div>
      </li>`).join("");
    return `
      <div class="meta">
        <dl>
          <dt>Root anchor</dt><dd>${esc(plan.root_question)}</dd>
          <dt>Current facet</dt><dd>${esc(plan.question)}</dd>
          <dt>Spread</dt><dd>${esc(plan.spread_name)}</dd>
          ${plan.rationale ? `<dt>Planner rationale</dt><dd>${esc(plan.rationale)}</dd>` : ""}
        </dl>
        <ul class="cards" aria-label="Cards drawn by Python RNG">${cards}</ul>
      </div>
      <hr class="divider">`;
  }

  function relatedHtml(record) {
    const qs = record.related_questions || [];
    if (!qs.length) return "";
    return `<div class="related"><p>Related questions: click one to use it as the next facet.</p><ul>${
      qs.map((q, i) => `<li><button type="button" data-q="${i}">${esc(q)}</button></li>`).join("")}</ul></div>`;
  }

  // ------------------------------------------------------------ SSE over fetch
  async function* sseEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) yield { event, data: JSON.parse(data) };
      }
    }
  }

  // ------------------------------------------------------------ one reading
  async function runOne(rootQuestion, question) {
    controller = new AbortController();
    els.exports.hidden = true;
    lastRecord = null;
    els.final.innerHTML = `<div class="notice info">Contacting the oracle… (if the site was asleep, the first reading can take up to a minute to start)</div>`;
    setStatus("Connecting…", "busy");

    const temp = Math.min(1.5, Math.max(0, parseFloat(els.temp.value) || 0.85));
    const response = await fetch("/api/reading", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        root_question: rootQuestion, question, temperature: temp,
        random_fallback: els.random.checked, history: historyForServer(),
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let msg = `Request failed (${response.status}).`;
      try { const j = await response.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : msg; } catch {}
      throw new Error(msg);
    }

    let meta = "";
    let raw = "";
    let pending = false;
    const paint = (streaming) => {
      const keep = atBottom();
      els.final.innerHTML = meta + renderReading(raw, streaming);
      stickToBottom(keep);
    };

    for await (const { event, data } of sseEvents(response)) {
      if (event === "status") setStatus(data.text, "busy");
      else if (event === "plan") { meta = metaHtml(data); raw = ""; els.final.innerHTML = meta; els.final.scrollTop = 0; }
      else if (event === "reset") { raw = ""; paint(true); }
      else if (event === "token") {
        raw += data.text;
        if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; paint(true); }); }
      } else if (event === "error") throw new Error(data.message);
      else if (event === "done") {
        const record = data.record;
        const keep = atBottom();
        els.final.innerHTML = meta + renderReading(raw, false) + relatedHtml(record);
        stickToBottom(keep);
        const list = loadHistory(); list.push(record); saveHistory(list);
        lastRecord = record;
        els.exports.hidden = false;
        refreshHistoryUI();
        return record;
      }
    }
    if (stopRequested) return null;
    throw new Error("The connection closed before the reading finished.");
  }

  // ------------------------------------------------------------ sequences
  async function runSequence(initialQuestion, auto) {
    const rootQuestion = initialQuestion;
    let question = initialQuestion;
    let done = 0;
    stopRequested = false;
    setRunning(true);
    try {
      for (;;) {
        const record = await runOne(rootQuestion, question);
        if (!record || stopRequested) { setStatus("Stopped."); return; }
        done += 1;
        if (!auto) { setStatus("Ready."); return; }
        if (config.auto_loop_max && done >= config.auto_loop_max) {
          setStatus(`Auto-loop finished after ${done} readings.`);
          return;
        }
        let pool = (record.related_questions || []).filter((q) => q.trim());
        if (!pool.length) {
          pool = [
            `What hidden premise of the previous reading matters most for the root inquiry: ${rootQuestion}`,
            `What would be the most misleading interpretation of the previous reading for the root inquiry: ${rootQuestion}`,
            `What does the previous answer require next within the root inquiry: ${rootQuestion}`,
          ];
        }
        question = randomChoice(pool);
        els.question.value = question;
        setStatus(`Auto selected next question by RNG (${done}/${config.auto_loop_max || "∞"}).`, "busy");
        for (let i = 0; i < 15 && !stopRequested; i++) await new Promise((r) => setTimeout(r, 100)); // brief pause between readings
        if (stopRequested) { setStatus("Stopped."); return; }
      }
    } catch (err) {
      if (stopRequested || err.name === "AbortError") { setStatus("Stopped."); return; }
      setStatus("Error.", "error");
      els.final.insertAdjacentHTML("beforeend", `<div class="notice">${esc(err.message || err)}</div>`);
      els.final.scrollTop = els.final.scrollHeight;
    } finally {
      controller = null;
      setRunning(false);
    }
  }

  function start(auto) {
    if (running) return;
    const q = els.question.value.trim();
    if (!q) {
      setStatus(auto ? "Write an initial question or theme first." : "Write a question or theme first.", "error");
      els.question.focus();
      return;
    }
    runSequence(q, auto);
  }

  // ------------------------------------------------------------ export
  async function download(fmt, records) {
    try {
      const r = await fetch(`/api/export/${fmt}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records }),
      });
      if (!r.ok) throw new Error(`Export failed (${r.status}).`);
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
      const plain = /filename="([^"]+)"/.exec(cd);
      const name = star ? decodeURIComponent(star[1]) : (plain ? plain[1] : `tarot_reading.${fmt}`);
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (err) {
      setStatus(err.message || "Export failed.", "error");
    }
  }

  // ------------------------------------------------------------ wiring
  els.manual.addEventListener("click", () => start(false));
  els.auto.addEventListener("click", () => start(true));
  els.stop.addEventListener("click", () => {
    stopRequested = true;
    if (controller) controller.abort();
    setStatus("Stopped.");
  });
  els.question.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); start(false); }
  });
  els.exports.addEventListener("click", (e) => {
    const fmt = e.target.dataset && e.target.dataset.fmt;
    if (fmt && lastRecord) download(fmt, [lastRecord]);
  });
  els.final.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-q]");
    if (!btn || !lastRecord || running) return;
    els.question.value = lastRecord.related_questions[+btn.dataset.q] || "";
    els.question.focus();
    setStatus("Related question copied into the question box. Choose Manual reading to continue.");
  });
  els.exportAll.addEventListener("click", () => {
    const list = loadHistory();
    if (list.length) download("md", list);
  });
  els.clear.addEventListener("click", () => {
    if (!confirm("Delete all readings saved in this browser? Download them first if you want to keep them.")) return;
    try { localStorage.removeItem(HISTORY_KEY); } catch {}
    refreshHistoryUI();
    setStatus("History cleared.");
  });

  refreshHistoryUI();
})();
