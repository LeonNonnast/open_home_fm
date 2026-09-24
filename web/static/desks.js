// "Redaktion" (desks.html): status card per desk + detail with tabs Einstellungen | Prompt | Verlauf.
// Phase 1 has only the music desk. Hash links: #music, #music/prompt, #music/verlauf.

(() => {
  const DESK = "music";
  const NUMBER_FIELDS = [
    "fill_threshold_minutes",
    "block_minutes",
    "max_queued_program_minutes",
    "songs_per_announcement",
    "no_repeat_minutes",
    "max_tool_iterations",
  ];
  const TABS = { settings: "", prompt: "prompt", history: "verlauf" };
  const TRIGGERS = {
    fill: "Füllstand",
    manual: "von Hand",
    heartbeat: "Heartbeat",
    broadcast_start: "Sendebeginn",
    wish: "Hörerwunsch",
  };

  let desk = null;
  let plugins = [];
  let fastPoll = null;

  // ---------- status card ----------

  function renderCard(d) {
    const summary = deskSummary(d);
    $("music-lamp").dataset.state = summary.state;
    $("music-lamp-label").textContent = summary.label;
    const enabled = $("music-enabled");
    if (document.activeElement !== enabled) enabled.checked = d.enabled !== false;

    const fill = d.fill || {};
    const meter = $("music-fill");
    const cap = fill.cap_minutes || 45;
    meter.max = cap;
    meter.low = fill.threshold_minutes || 10;
    meter.high = Math.min(cap, (fill.threshold_minutes || 10) * 2);
    meter.optimum = cap;
    meter.value = Math.min(cap, (fill.remaining_seconds || 0) / 60);
    const [first, ...rest] = summary.lines;
    $("music-fill-text").textContent = first?.text || "";
    const lines = rest.map((l) => `<p class="desk-line${l.kind === "err" ? " err" : ""}">${esc(l.text)}</p>`);
    if (d.reserve) lines.push(`<p class="desk-line">Reserve: ${d.reserve.count} Titel fürs Füllprogramm</p>`);
    if (d.followup_pending) lines.push(`<p class="desk-line">Ein weiterer Lauf ist vorgemerkt.</p>`);
    $("music-lines").innerHTML = lines.join("");
    $("music-run").disabled = d.state === "running";
  }

  async function refreshDesk() {
    try {
      desk = await api(`/api/desks/${DESK}`);
    } catch {
      return null;
    }
    renderCard(desk);
    return desk;
  }

  // While a run is going, poll faster and report when it's done.
  function watchRun() {
    clearInterval(fastPoll);
    fastPoll = setInterval(async () => {
      const d = await refreshDesk();
      if (!d || d.state === "running") return;
      clearInterval(fastPoll);
      fastPoll = null;
      if (d.state === "error") note($("music-run-note"), `Lauf fehlgeschlagen: ${d.last_error || "unbekannter Fehler"}`, "err");
      else note($("music-run-note"), "Fertig – das Programm ist eingeplant.", "ok");
      loadHistory();
      loadReserve();
    }, 3000);
  }

  async function runNow() {
    const btn = $("music-run");
    btn.disabled = true;
    try {
      const res = await api(`/api/desks/${DESK}/run`, { method: "POST" });
      if (res.status === "started") {
        note($("music-run-note"), "Plant jetzt – das dauert meist 10–60 Sekunden.");
        watchRun();
      } else if (res.status === "queued") {
        note($("music-run-note"), `Läuft schon – ${res.reason || "ein weiterer Lauf ist vorgemerkt"}.`);
        watchRun();
      } else {
        note($("music-run-note"), res.reason || "Gerade nicht möglich.", "err");
      }
    } catch (e) {
      note($("music-run-note"), "Konnte nicht gestartet werden (" + e.message + ").", "err");
    }
    await refreshDesk();
  }

  // ---------- settings ----------

  function fillSettings(settings) {
    for (const name of NUMBER_FIELDS) $(`s-${name}`).value = settings[name] ?? "";
    renderPlugins(settings);
  }

  function renderPlugins(settings) {
    const allowed = new Set(settings.plugins || []);
    const context = new Set(settings.context_plugins || []);
    const rows = plugins.map((p) => ({ ...p, installed: true }));
    // Keep plugins named in the settings that aren't installed (e.g. a typo or a removed folder).
    for (const name of new Set([...allowed, ...context])) {
      if (!rows.some((p) => p.name === name)) rows.push({ name, description: "nicht installiert", enabled: false, installed: false });
    }
    $("desk-plugins").innerHTML = rows.length
      ? rows.map((p, i) => `
        <tr>
          <th scope="row">
            <span class="name">${esc(p.name)}</span>
            <span class="desc">${esc(p.description)}${p.installed && !p.enabled ? " · global ausgeschaltet" : ""}</span>
          </th>
          <td class="c"><input type="checkbox" data-kind="plugins" data-name="${esc(p.name)}" id="dp-${i}-a" aria-label="${esc(p.name)} nutzen" ${allowed.has(p.name) ? "checked" : ""}></td>
          <td class="c"><input type="checkbox" data-kind="context_plugins" data-name="${esc(p.name)}" id="dp-${i}-c" aria-label="${esc(p.name)} als Kontext" ${context.has(p.name) ? "checked" : ""}></td>
        </tr>`).join("")
      : `<tr><td colspan="3" class="hint">Keine Plugins installiert.</td></tr>`;
  }

  function settingsFields() {
    const partial = {};
    for (const name of NUMBER_FIELDS) {
      const value = $(`s-${name}`).valueAsNumber;
      if (!Number.isFinite(value)) throw new Error(`„${$(`s-${name}`).labels[0].textContent}“ ist leer`);
      partial[name] = Math.round(value);
    }
    if (partial.fill_threshold_minutes >= partial.max_queued_program_minutes) {
      throw new Error("„Nachplanen unter“ muss kleiner sein als „Höchstens eingeplant“");
    }
    for (const kind of ["plugins", "context_plugins"]) {
      partial[kind] = [...document.querySelectorAll(`#desk-plugins input[data-kind="${kind}"]:checked`)].map((i) => i.dataset.name);
    }
    return partial;
  }

  async function saveSettings() {
    let partial;
    try {
      partial = settingsFields();
    } catch (e) {
      note($("desk-note"), e.message + ".", "err");
      return;
    }
    try {
      desk = await api(`/api/desks/${DESK}`, { method: "PATCH", ...jsonBody(partial) });
      fillSettings(desk.settings);
      renderCard(desk);
      note($("desk-note"), "Gespeichert – gilt ab dem nächsten Lauf.", "ok");
    } catch (e) {
      note($("desk-note"), "Speichern fehlgeschlagen (" + e.message + ").", "err");
    }
  }

  async function toggleEnabled() {
    const input = $("music-enabled");
    try {
      desk = await api(`/api/desks/${DESK}`, { method: "PATCH", ...jsonBody({ enabled: input.checked }) });
      renderCard(desk);
      note($("music-run-note"), input.checked ? "Musikredaktion eingeschaltet." : "Musikredaktion ausgeschaltet – es läuft nur noch Füllprogramm.", "ok");
    } catch (e) {
      input.checked = !input.checked;
      note($("music-run-note"), "Umschalten fehlgeschlagen (" + e.message + ").", "err");
    }
  }

  async function loadReserve() {
    try {
      const reserve = await api(`/api/desks/${DESK}/reserve`);
      $("reserve-meta").textContent = `${reserve.tracks.length} Titel${reserve.updated_at ? ` · Stand ${fmtWhen(reserve.updated_at)}` : ""}`;
      syncList($("reserve-list"), reserve.tracks, {
        key: (t) => t.uri,
        sig: (t) => t.title || t.uri,
        render: (t) => esc(t.title || t.uri),
        empty: "Noch leer – die Musikredaktion legt sie beim nächsten Lauf an.",
      });
    } catch {
      $("reserve-meta").textContent = "";
    }
  }

  // ---------- prompt ----------

  function renderPromptState(customized) {
    const badge = $("prompt-badge");
    badge.textContent = customized ? "angepasst" : "Standard";
    badge.dataset.kind = customized ? "custom" : "default";
    $("prompt-meta").textContent = customized ? "data/prompts/music.md" : "config/desks/music.md";
    $("reset-prompt").hidden = !customized;
  }

  async function loadPrompt() {
    const prompt = await api(`/api/desks/${DESK}/prompt`);
    $("desk-prompt").value = prompt.text;
    renderPromptState(prompt.customized);
  }

  function initPrompt() {
    $("save-prompt").addEventListener("click", async () => {
      try {
        const { customized } = await api(`/api/desks/${DESK}/prompt`, {
          method: "PUT",
          ...jsonBody({ text: $("desk-prompt").value }),
        });
        renderPromptState(customized);
        note($("prompt-note"), "Gespeichert – gilt ab dem nächsten Lauf.", "ok");
      } catch (e) {
        note($("prompt-note"), "Speichern fehlgeschlagen (" + e.message + ").", "err");
      }
    });

    $("reset-prompt").addEventListener("click", async () => {
      if (!confirm("Angepassten Prompt verwerfen und den Standard-Prompt wiederherstellen?")) return;
      try {
        const { text, customized } = await api(`/api/desks/${DESK}/prompt/reset`, { method: "POST" });
        $("desk-prompt").value = text;
        renderPromptState(customized);
        note($("prompt-note"), "Standard wiederhergestellt.", "ok");
      } catch (e) {
        note($("prompt-note"), "Zurücksetzen fehlgeschlagen (" + e.message + ").", "err");
      }
    });
  }

  // ---------- history ----------

  async function loadHistory() {
    let transcripts;
    try {
      ({ transcripts } = await api(`/api/transcripts?desk=${DESK}`));
    } catch (e) {
      $("history-list").innerHTML = `<p class="note err">Verlauf konnte nicht geladen werden (${esc(e.message)}).</p>`;
      return;
    }
    syncList($("history-list"), transcripts, {
      tag: "details",
      className: "run",
      key: (t) => t.id,
      sig: (t) => `${t.final_message}|${t.error}`,
      empty: `<p class="hint">Noch keine Läufe gespeichert.</p>`,
      render: (t) => `
        <summary>
          <span class="when">${esc(fmtDateTime(new Date(t.created_at)))}</span>
          <span class="what">${t.final_message ? esc(plain(t.final_message)) : "<em>ohne Abschlussnotiz</em>"}</span>
          <span class="count${t.error ? " err" : ""}">${t.error ? "Fehler" : `${TRIGGERS[t.trigger] || esc(t.trigger || "")} · ${t.segment_count} Seg.`}</span>
        </summary>
        ${t.error ? `<p class="note err" style="margin:8px 0 0">${esc(t.error)}</p>` : ""}
        <pre class="raw">Lädt…</pre>`,
    });
  }

  function initHistory() {
    // "toggle" doesn't bubble - listen in the capture phase for all <details>.
    $("history-list").addEventListener("toggle", async (e) => {
      const d = e.target;
      if (!(d instanceof HTMLDetailsElement)) return;
      const pre = d.querySelector(".raw");
      if (!d.open || !pre || pre.dataset.loaded) return;
      try {
        const full = await api(`/api/transcripts/${encodeURIComponent(d.dataset.key)}`);
        pre.textContent = JSON.stringify(full.messages, null, 2);
        pre.dataset.loaded = "1";
      } catch (err) {
        pre.textContent = "Konnte nicht geladen werden: " + err.message;
      }
    }, true);
  }

  // ---------- tabs & hash ----------

  const tabButtons = () => [...document.querySelectorAll('#detail-music [role="tab"]')];

  function selectTab(tab, { focus = false, updateHash = true } = {}) {
    if (!(tab in TABS)) tab = "settings";
    for (const btn of tabButtons()) {
      const selected = btn.dataset.tab === tab;
      btn.setAttribute("aria-selected", String(selected));
      btn.tabIndex = selected ? 0 : -1;
      $(btn.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) btn.focus();
    }
    if (tab === "history") loadHistory();
    if (updateHash) history.replaceState(null, "", `#${DESK}${TABS[tab] ? `/${TABS[tab]}` : ""}`);
  }

  function tabFromHash() {
    const [name, sub] = location.hash.replace(/^#/, "").split("/");
    if (name && name !== DESK) return "settings";
    return Object.keys(TABS).find((k) => TABS[k] === (sub || "")) || "settings";
  }

  function initTabs() {
    const list = document.querySelector('#detail-music [role="tablist"]');
    list.addEventListener("click", (e) => {
      const btn = e.target.closest('[role="tab"]');
      if (btn) selectTab(btn.dataset.tab);
    });
    list.addEventListener("keydown", (e) => {
      const buttons = tabButtons();
      const current = buttons.findIndex((b) => b.getAttribute("aria-selected") === "true");
      let next = null;
      if (e.key === "ArrowRight") next = (current + 1) % buttons.length;
      else if (e.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = buttons.length - 1;
      if (next === null) return;
      e.preventDefault();
      selectTab(buttons[next].dataset.tab, { focus: true });
    });
    window.addEventListener("hashchange", () => selectTab(tabFromHash(), { updateHash: false }));
    selectTab(tabFromHash(), { updateHash: false });
    if (location.hash) $("detail-music").scrollIntoView({ block: "start" });
  }

  // ---------- boot ----------

  Pages.desks = async () => {
    initTabs();
    initPrompt();
    initHistory();
    $("music-run").addEventListener("click", runNow);
    $("music-enabled").addEventListener("change", toggleEnabled);
    $("save-desk").addEventListener("click", saveSettings);

    try {
      ({ plugins } = await api("/api/plugins"));
    } catch {
      plugins = [];
    }
    const d = await refreshDesk();
    if (d) fillSettings(d.settings);
    if (d?.state === "running") watchRun();
    loadReserve();
    loadPrompt().catch((e) => note($("prompt-note"), "Prompt konnte nicht geladen werden (" + e.message + ").", "err"));

    // The header status polls every 15 s anyway - refresh the card along with it.
    Status.on(() => { if (!fastPoll) refreshDesk(); });
  };
})();
