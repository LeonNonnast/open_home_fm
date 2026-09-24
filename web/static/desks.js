// "Redaktion" (desks.html): a status card per desk + the detail of the selected desk with tabs
// Einstellungen | Prompt | Verlauf. Hash links: #music, #music/prompt, #dispatch/verlauf, …

(() => {
  const TABS = { settings: "", prompt: "prompt", history: "verlauf" };
  const TRIGGERS = {
    fill: "Füllstand",
    manual: "von Hand",
    heartbeat: "Heartbeat",
    broadcast_start: "Sendebeginn",
    wish: "Hörerwunsch",
    call: "Zwischenruf",
    poll: "Nachzügler",
    slot: "Sendezeit",
    catch_up: "nachgeholt",
  };
  const MAIL_STATUS = { noted: "offen", used: "eingeplant", expired: "verfallen", removed: "verworfen", repeats: "wird wiederholt" };

  const DESKS = {
    music: {
      label: "Musikredaktion",
      numbers: ["fill_threshold_minutes", "block_minutes", "max_queued_program_minutes", "songs_per_announcement", "no_repeat_minutes", "max_tool_iterations"],
      bools: [],
      contextPlugins: true,
      started: "Plant jetzt – das dauert meist 10–60 Sekunden.",
      done: () => "Fertig – das Programm ist eingeplant.",
      toggled: (on) => (on ? "Musikredaktion eingeschaltet." : "Musikredaktion ausgeschaltet – es läuft nur noch Füllprogramm."),
    },
    news: {
      label: "Nachrichtenredaktion",
      numbers: ["lead_minutes", "max_delay_minutes", "max_tool_iterations"],
      bools: ["intro"],
      contextPlugins: false,
      started: "Schreibt die nächste Ausgabe – das dauert meist 10–60 Sekunden.",
      done: (d) => (d.prepared && d.next_slot_at ? `Fertig – die Ausgabe ${hhmm(new Date(d.next_slot_at))} ist eingeplant.` : "Fertig."),
      toggled: (on) => (on ? "Nachrichtenredaktion eingeschaltet." : "Nachrichtenredaktion ausgeschaltet – keine Nachrichten."),
    },
    dispatch: {
      label: "Leitstelle",
      numbers: ["min_minutes_between_interrupts", "reply_expires_minutes", "wish_default_valid_hours", "max_tool_iterations"],
      bools: ["allow_interrupt"],
      contextPlugins: false,
      started: "Bearbeitet die offenen Zwischenrufe…",
      done: (d) => (d.open_calls ? `Noch ${d.open_calls} offen.` : "Alles bearbeitet – nichts mehr offen."),
      toggled: (on) => (on ? "Leitstelle eingeschaltet." : "Leitstelle ausgeschaltet – Zwischenrufe bleiben liegen."),
    },
  };

  const state = { desks: {}, plugins: [], selected: "music", fastPoll: null };

  // ---------- status cards ----------

  function renderMusicCard(d, summary) {
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
    const lines = [...rest];
    if (d.open_wishes) lines.push({ text: `${d.open_wishes} ${d.open_wishes === 1 ? "Wunsch" : "Wünsche"} offen` });
    if (d.reserve) lines.push({ text: `Reserve: ${d.reserve.count} Titel fürs Füllprogramm` });
    if (d.followup_pending) lines.push({ text: "Ein weiterer Lauf ist vorgemerkt." });
    $("music-lines").innerHTML = lineHtml(lines);
  }

  function renderDispatchCard(d, summary) {
    const [first, ...rest] = summary.lines;
    $("dispatch-main").textContent = first?.text || "";
    const lines = [...rest];
    if (d.followup_pending) lines.push({ text: "Ein weiterer Durchlauf ist vorgemerkt." });
    $("dispatch-lines").innerHTML = lineHtml(lines);
  }

  function renderNewsCard(d, summary) {
    const [first, ...rest] = summary.lines;
    $("news-main").textContent = first?.text || "";
    const lines = [...rest];
    if (d.followup_pending) lines.push({ text: "Ein weiterer Lauf ist vorgemerkt." });
    if (d.placement_note) lines.push({ text: d.placement_note });
    $("news-lines").innerHTML = lineHtml(lines);
    const last = d.last_bulletin;
    $("news-last").hidden = !last?.text;
    if (last?.text) {
      $("news-last-meta").textContent = `${fmtWhen(last.slot)} · ${NEWS_FORMATS[last.format] || ""}`;
      $("news-last-text").textContent = last.text;
    }
  }

  function renderCard(name) {
    const d = state.desks[name];
    if (!d) return;
    const summary = deskSummary(d, name);
    $(`${name}-lamp`).dataset.state = summary.state;
    $(`${name}-lamp-label`).textContent = summary.label;
    const enabled = $(`${name}-enabled`);
    if (document.activeElement !== enabled) enabled.checked = d.enabled !== false;
    if (name === "music") renderMusicCard(d, summary);
    else if (name === "news") renderNewsCard(d, summary);
    else renderDispatchCard(d, summary);
    $(`${name}-run`).disabled = d.state === "running";
  }

  async function refreshDesks() {
    let desks;
    try {
      ({ desks } = await api("/api/desks"));
    } catch {
      return null;
    }
    for (const d of desks) {
      state.desks[d.name] = d;
      if (DESKS[d.name]) renderCard(d.name);
    }
    return state.desks;
  }

  // While a run is going, poll faster and report when it's done.
  function watchRun(name) {
    clearInterval(state.fastPoll);
    state.fastPoll = setInterval(async () => {
      await refreshDesks();
      const d = state.desks[name];
      if (!d || d.state === "running") return;
      clearInterval(state.fastPoll);
      state.fastPoll = null;
      const noteEl = $(`${name}-run-note`);
      if (d.state === "error") note(noteEl, `Lauf fehlgeschlagen: ${d.last_error || "unbekannter Fehler"}`, "err");
      else note(noteEl, DESKS[name].done(d), "ok");
      if (state.selected === name) loadMailboxes(name);
      const history = $(`${name}-panel-history`);
      if (!history.hidden) loadHistory(name);
      if (name === "music") loadReserve();
    }, 2500);
  }

  async function runNow(name) {
    const btn = $(`${name}-run`);
    const noteEl = $(`${name}-run-note`);
    btn.disabled = true;
    try {
      const res = await api(`/api/desks/${name}/run`, { method: "POST" });
      if (res.status === "started") {
        note(noteEl, DESKS[name].started);
        watchRun(name);
      } else if (res.status === "queued") {
        note(noteEl, `Läuft schon – ${res.reason || "ein weiterer Lauf ist vorgemerkt"}.`);
        watchRun(name);
      } else {
        note(noteEl, res.reason || "Gerade nicht möglich.", "err");
      }
    } catch (e) {
      note(noteEl, "Konnte nicht gestartet werden (" + e.message + ").", "err");
    }
    await refreshDesks();
  }

  async function toggleEnabled(name) {
    const input = $(`${name}-enabled`);
    try {
      state.desks[name] = await api(`/api/desks/${name}`, { method: "PATCH", ...jsonBody({ enabled: input.checked }) });
      renderCard(name);
      note($(`${name}-run-note`), DESKS[name].toggled(input.checked), "ok");
    } catch (e) {
      input.checked = !input.checked;
      note($(`${name}-run-note`), "Umschalten fehlgeschlagen (" + e.message + ").", "err");
    }
  }

  // ---------- settings ----------

  function fillSettings(name) {
    const settings = state.desks[name]?.settings || {};
    for (const field of DESKS[name].numbers) $(`${name}-s-${field}`).value = settings[field] ?? "";
    for (const field of DESKS[name].bools) $(`${name}-s-${field}`).checked = !!settings[field];
    if (name === "news") {
      renderSlots(settings.slots || []);
      $("news-s-placement").value = settings.placement || "after_song";
      const sources = new Set(settings.sources || []);
      for (const input of document.querySelectorAll("#news-sources input")) input.checked = sources.has(input.value);
    }
    renderPlugins(name, settings);
  }

  // ---------- news: slots editor ----------

  function slotRow(slot, index) {
    const minute = Number(slot.minute) || 0;
    return `
      <li>
        <span class="at" aria-hidden="true">hh :</span>
        <input type="number" min="0" max="59" step="1" inputmode="numeric" value="${pad(minute)}" data-slot="minute"
          id="news-slot-${index}-m" aria-label="Minute der Sendezeit ${index + 1}">
        <select data-slot="format" id="news-slot-${index}-f" aria-label="Format der Sendezeit ${index + 1}">
          <option value="full"${slot.format === "full" ? " selected" : ""}>ausführlich (2–3 min)</option>
          <option value="short"${slot.format === "short" ? " selected" : ""}>kurz (30–60 s)</option>
        </select>
        <button class="btn ghost small" type="button" data-remove-slot aria-label="Sendezeit ${index + 1} entfernen">Entfernen</button>
      </li>`;
  }

  function readSlots() {
    return [...document.querySelectorAll("#news-slots li")].map((li) => ({
      minute: li.querySelector('[data-slot="minute"]').value,
      format: li.querySelector('[data-slot="format"]').value,
    }));
  }

  function renderSlots(slots) {
    $("news-slots").innerHTML = slots.length ? slots.map(slotRow).join("") : "";
    $("news-add-slot").disabled = slots.length >= 12;
  }

  function initSlots() {
    $("news-add-slot").addEventListener("click", () => {
      const slots = readSlots();
      const used = new Set(slots.map((s) => Number(s.minute)));
      const minute = [0, 30, 15, 45].find((m) => !used.has(m)) ?? 0;
      renderSlots([...slots, { minute: pad(minute), format: "short" }]);
      document.querySelector("#news-slots li:last-child input")?.focus();
    });
    $("news-slots").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-remove-slot]");
      if (!btn) return;
      btn.closest("li").remove();
      renderSlots(readSlots());
      $("news-add-slot").focus();
    });
  }

  function newsFields(partial) {
    const slots = readSlots();
    if (!slots.length) throw new Error("Mindestens eine Sendezeit angeben");
    const seen = new Set();
    partial.slots = slots.map((s) => {
      const m = Number(s.minute);
      if (!Number.isInteger(m) || m < 0 || m > 59 || String(s.minute).trim() === "") throw new Error("Minute der Sendezeit muss zwischen 0 und 59 liegen");
      if (seen.has(m)) throw new Error(`Die Minute :${pad(m)} ist doppelt`);
      seen.add(m);
      return { minute: pad(m), format: s.format };
    });
    partial.placement = $("news-s-placement").value;
    partial.sources = [...document.querySelectorAll("#news-sources input:checked")].map((i) => i.value);
    return partial;
  }

  function renderPlugins(name, settings) {
    const withContext = DESKS[name].contextPlugins;
    const allowed = new Set(settings.plugins || []);
    const context = new Set(withContext ? settings.context_plugins || [] : []);
    const rows = state.plugins.map((p) => ({ ...p, installed: true }));
    // Keep plugins named in the settings that aren't installed (e.g. a typo or a removed folder).
    for (const plugin of new Set([...allowed, ...context])) {
      if (!rows.some((p) => p.name === plugin)) rows.push({ name: plugin, description: "nicht installiert", enabled: false, installed: false });
    }
    const kind = (p) => (name === "dispatch" && p.installed ? (p.action ? "direkte Aktion · " : "Abfrage · ") : "");
    $(`${name}-plugins`).innerHTML = rows.length
      ? rows.map((p, i) => `
        <tr>
          <th scope="row">
            <span class="name">${esc(p.name)}</span>
            <span class="desc">${kind(p)}${esc(p.description)}${p.installed && !p.enabled ? " · global ausgeschaltet" : ""}</span>
          </th>
          <td class="c"><input type="checkbox" data-kind="plugins" data-name="${esc(p.name)}" id="${name}-p-${i}-a" aria-label="${esc(p.name)} nutzen" ${allowed.has(p.name) ? "checked" : ""}></td>
          ${withContext ? `<td class="c"><input type="checkbox" data-kind="context_plugins" data-name="${esc(p.name)}" id="${name}-p-${i}-c" aria-label="${esc(p.name)} als Kontext" ${context.has(p.name) ? "checked" : ""}></td>` : ""}
        </tr>`).join("")
      : `<tr><td colspan="${withContext ? 3 : 2}" class="hint">Keine Plugins installiert.</td></tr>`;
  }

  function settingsFields(name) {
    const partial = {};
    for (const field of DESKS[name].numbers) {
      const input = $(`${name}-s-${field}`);
      const value = input.valueAsNumber;
      const label = input.labels[0].textContent;
      if (!Number.isFinite(value)) throw new Error(`„${label}“ ist leer`);
      if (value < Number(input.min) || value > Number(input.max)) {
        throw new Error(`„${label}“ muss zwischen ${input.min} und ${input.max} liegen`);
      }
      partial[field] = Math.round(value);
    }
    for (const field of DESKS[name].bools) partial[field] = $(`${name}-s-${field}`).checked;
    if (name === "news") newsFields(partial);
    if (name === "music" && partial.fill_threshold_minutes >= partial.max_queued_program_minutes) {
      throw new Error("„Nachplanen unter“ muss kleiner sein als „Höchstens eingeplant“");
    }
    for (const kind of DESKS[name].contextPlugins ? ["plugins", "context_plugins"] : ["plugins"]) {
      partial[kind] = [...document.querySelectorAll(`#${name}-plugins input[data-kind="${kind}"]:checked`)].map((i) => i.dataset.name);
    }
    return partial;
  }

  function errorText(e) {
    // FastAPI validation errors come back as a JSON list - show the first message.
    const text = String(e.message).replace(/^\d+: /, "");
    return text.startsWith("[object") ? "ungültige Eingabe" : text;
  }

  async function saveSettings(name) {
    const noteEl = $(`${name}-note`);
    let partial;
    try {
      partial = settingsFields(name);
    } catch (e) {
      note(noteEl, e.message + ".", "err");
      return;
    }
    try {
      state.desks[name] = await api(`/api/desks/${name}`, { method: "PATCH", ...jsonBody(partial) });
      fillSettings(name);
      renderCard(name);
      const saved = { dispatch: "Gespeichert – gilt ab dem nächsten Zwischenruf.", news: "Gespeichert – gilt ab der nächsten Ausgabe." };
      note(noteEl, saved[name] || "Gespeichert – gilt ab dem nächsten Lauf.", "ok");
    } catch (e) {
      note(noteEl, "Speichern fehlgeschlagen (" + errorText(e) + ").", "err");
    }
  }

  async function loadReserve() {
    try {
      const reserve = await api("/api/desks/music/reserve");
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

  // ---------- mailboxes: open wishes (music), news notes (news) ----------

  const MAILBOXES = {
    music: { url: "/api/wishes", key: "wishes", list: "music-wishes", meta: "music-wishes-meta", what: "Wunsch", empty: "Keine offenen Wünsche." },
    news: { url: "/api/news-notes", key: "notes", list: "news-notes", meta: "news-notes-meta", what: "Hinweis", empty: "Nichts vorgemerkt." },
  };

  async function loadMailboxes(name) {
    const box = MAILBOXES[name];
    if (!box) return;
    let entries;
    try {
      entries = (await api(box.url))[box.key];
    } catch (e) {
      $(box.meta).textContent = "konnte nicht geladen werden";
      return;
    }
    // A news note that aired and is still valid is repeated in the full bulletins.
    entries = entries.map((e) => (e.repeats ? { ...e, status: "repeats" } : e));
    const isOpen = (e) => e.status === "noted" || e.status === "repeats";
    // Open ones first (newest first), then the rest of the last days, a few only.
    const open = entries.filter(isOpen);
    const done = entries.filter((e) => !isOpen(e)).slice(0, 5);
    $(box.meta).textContent = `${open.length} offen${done.length ? ` · ${done.length} erledigt` : ""}`;
    const when = (e) => {
      if (e.status === "repeats") return `zuletzt ${fmtWhen(e.news_slot)} · gilt bis ${fmtWhen(e.valid_until)}`;
      if (e.status === "noted" && e.valid_until) return `gilt bis ${fmtWhen(e.valid_until)}`;
      if (e.status === "used" && e.news_slot) return `in den Nachrichten ${fmtWhen(e.news_slot)}`;
      return fmtWhen(e.used_at || e.updated_at || e.created_at);
    };
    syncList($(box.list), [...open, ...done], {
      key: (e) => e.id,
      sig: (e) => JSON.stringify([e.text, e.status, e.valid_until, e.author, e.news_slot]),
      empty: `<p class="hint">${box.empty}</p>`,
      render: (e) => `
        <span class="mail-text">${esc(e.text)}</span>
        <span class="mail-meta">
          <span class="chip" data-status="${esc(e.status)}">${esc(MAIL_STATUS[e.status] || e.status)}</span>
          ${e.author ? `<span>von ${esc(e.author)}</span>` : ""}
          <span>${esc(when(e))}</span>
        </span>
        ${isOpen(e) ? `<button class="btn ghost small" type="button" data-discard="${esc(e.id)}" data-fkey="discard">Verwerfen</button>` : ""}`,
      update: (el, e) => { el.dataset.status = e.status; },
    });
  }

  function initMailboxes() {
    for (const [name, box] of Object.entries(MAILBOXES)) {
      $(box.list).addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-discard]");
        if (!btn) return;
        const li = btn.closest("li");
        const text = li?.querySelector(".mail-text")?.textContent || "";
        if (!confirm(`${box.what} „${text.slice(0, 80)}“ verwerfen?`)) return;
        btn.disabled = true;
        try {
          await api(`${box.url}/${encodeURIComponent(btn.dataset.discard)}`, { method: "DELETE" });
        } catch (err) {
          btn.disabled = false;
          $(box.meta).textContent = "Verwerfen fehlgeschlagen (" + errorText(err) + ")";
          return;
        }
        await loadMailboxes(name);
        refreshDesks();
      });
    }
  }

  // ---------- prompt ----------

  function renderPromptState(name, customized) {
    const badge = $(`${name}-prompt-badge`);
    badge.textContent = customized ? "angepasst" : "Standard";
    badge.dataset.kind = customized ? "custom" : "default";
    $(`${name}-prompt-meta`).textContent = customized ? `data/prompts/${name}.md` : `config/desks/${name}.md`;
    $(`${name}-reset-prompt`).hidden = !customized;
  }

  async function loadPrompt(name) {
    const prompt = await api(`/api/desks/${name}/prompt`);
    $(`${name}-prompt`).value = prompt.text;
    renderPromptState(name, prompt.customized);
  }

  function initPrompt(name) {
    const noteEl = $(`${name}-prompt-note`);
    $(`${name}-save-prompt`).addEventListener("click", async () => {
      try {
        const { customized } = await api(`/api/desks/${name}/prompt`, {
          method: "PUT",
          ...jsonBody({ text: $(`${name}-prompt`).value }),
        });
        renderPromptState(name, customized);
        note(noteEl, "Gespeichert – gilt ab dem nächsten Lauf.", "ok");
      } catch (e) {
        note(noteEl, "Speichern fehlgeschlagen (" + e.message + ").", "err");
      }
    });

    $(`${name}-reset-prompt`).addEventListener("click", async () => {
      if (!confirm(`Angepassten Prompt der ${DESKS[name].label} verwerfen und den Standard-Prompt wiederherstellen?`)) return;
      try {
        const { text, customized } = await api(`/api/desks/${name}/prompt/reset`, { method: "POST" });
        $(`${name}-prompt`).value = text;
        renderPromptState(name, customized);
        note(noteEl, "Standard wiederhergestellt.", "ok");
      } catch (e) {
        note(noteEl, "Zurücksetzen fehlgeschlagen (" + e.message + ").", "err");
      }
    });
  }

  // ---------- history ----------

  async function loadHistory(name) {
    const list = $(`${name}-history`);
    let transcripts;
    try {
      ({ transcripts } = await api(`/api/transcripts?desk=${name}`));
    } catch (e) {
      list.innerHTML = `<p class="note err">Verlauf konnte nicht geladen werden (${esc(e.message)}).</p>`;
      return;
    }
    const byInput = name !== "music"; // dispatch: the call, news: "Ausgabe 07:30 (kurz)"
    const count = (t) => (byInput ? TRIGGERS[t.trigger] || t.trigger || "" : `${TRIGGERS[t.trigger] || esc(t.trigger || "")} · ${t.segment_count} Seg.`);
    syncList(list, transcripts, {
      tag: "details",
      className: "run",
      key: (t) => t.id,
      sig: (t) => `${t.final_message}|${t.error}|${t.inputs}`,
      empty: `<p class="hint">Noch keine Läufe gespeichert.</p>`,
      render: (t) => {
        // The dispatch desk's input is the call ("Mama: Licht an"), the news desk's the slot -
        // the more telling headline.
        const headline = byInput && t.inputs ? t.inputs : t.final_message;
        return `
        <summary>
          <span class="when">${esc(fmtDateTime(new Date(t.created_at)))}</span>
          <span class="what">${headline ? esc(plain(headline)) : "<em>ohne Abschlussnotiz</em>"}</span>
          <span class="count${t.error ? " err" : ""}">${t.error ? "Fehler" : esc(count(t))}</span>
        </summary>
        ${byInput && t.inputs && t.final_message ? `<p class="hint run-final">${esc(plain(t.final_message))}</p>` : ""}
        ${t.error ? `<p class="note err" style="margin:8px 0 0">${esc(t.error)}</p>` : ""}
        <pre class="raw">Lädt…</pre>`;
      },
    });
  }

  function initHistory(name) {
    // "toggle" doesn't bubble - listen in the capture phase for all <details>.
    $(`${name}-history`).addEventListener("toggle", async (e) => {
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

  // ---------- selection, tabs & hash ----------

  const tabButtons = (name) => [...document.querySelectorAll(`#detail-${name} [role="tab"]`)];
  const selectedTab = (name) => tabButtons(name).find((b) => b.getAttribute("aria-selected") === "true")?.dataset.tab || "settings";

  function selectTab(name, tab, { focus = false } = {}) {
    if (!(tab in TABS)) tab = "settings";
    for (const btn of tabButtons(name)) {
      const selected = btn.dataset.tab === tab;
      btn.setAttribute("aria-selected", String(selected));
      btn.tabIndex = selected ? 0 : -1;
      $(btn.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) btn.focus();
    }
    if (tab === "history") loadHistory(name);
  }

  function writeHash() {
    const tab = TABS[selectedTab(state.selected)];
    history.replaceState(null, "", `#${state.selected}${tab ? `/${tab}` : ""}`);
  }

  function selectDesk(name, { tab = null, updateHash = true, scroll = false } = {}) {
    if (!DESKS[name]) name = "music";
    state.selected = name;
    for (const n of Object.keys(DESKS)) {
      $(`detail-${n}`).hidden = n !== name;
      const card = $(`card-${n}`);
      if (n === name) card.setAttribute("aria-current", "true");
      else card.removeAttribute("aria-current");
    }
    selectTab(name, tab || selectedTab(name));
    loadMailboxes(name);
    if (updateHash) writeHash();
    if (scroll) $(`detail-${name}`).scrollIntoView({ block: "start" });
  }

  function fromHash() {
    const [name, sub] = location.hash.replace(/^#/, "").split("/");
    const desk = DESKS[name] ? name : "music";
    const tab = Object.keys(TABS).find((k) => TABS[k] === (sub || "")) || "settings";
    return { desk, tab };
  }

  function initTabs(name) {
    const list = document.querySelector(`#detail-${name} [role="tablist"]`);
    list.addEventListener("click", (e) => {
      const btn = e.target.closest('[role="tab"]');
      if (!btn) return;
      selectTab(name, btn.dataset.tab);
      writeHash();
    });
    list.addEventListener("keydown", (e) => {
      const buttons = tabButtons(name);
      const current = buttons.findIndex((b) => b.getAttribute("aria-selected") === "true");
      let next = null;
      if (e.key === "ArrowRight") next = (current + 1) % buttons.length;
      else if (e.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = buttons.length - 1;
      if (next === null) return;
      e.preventDefault();
      selectTab(name, buttons[next].dataset.tab, { focus: true });
      writeHash();
    });
  }

  function initCards() {
    // Tapping a card (not one of its controls) opens its detail below.
    $("desk-cards").addEventListener("click", (e) => {
      const card = e.target.closest(".desk-card");
      if (!card) return;
      const link = e.target.closest("h2 a");
      if (link) e.preventDefault();
      else if (e.target.closest("button, input, label, a")) return;
      selectDesk(card.dataset.desk, { scroll: !!link || window.innerWidth < 880 });
    });
    window.addEventListener("hashchange", () => {
      const { desk, tab } = fromHash();
      selectDesk(desk, { tab, updateHash: false });
    });
  }

  // ---------- boot ----------

  Pages.desks = async () => {
    for (const name of Object.keys(DESKS)) {
      initTabs(name);
      initPrompt(name);
      initHistory(name);
      $(`${name}-run`).addEventListener("click", () => runNow(name));
      $(`${name}-enabled`).addEventListener("change", () => toggleEnabled(name));
      $(`${name}-save`).addEventListener("click", () => saveSettings(name));
    }
    initCards();
    initMailboxes();
    initSlots();
    const { desk, tab } = fromHash();
    selectDesk(desk, { tab, updateHash: false, scroll: !!location.hash });

    try {
      ({ plugins: state.plugins } = await api("/api/plugins"));
    } catch {
      state.plugins = [];
    }
    await refreshDesks();
    for (const name of Object.keys(DESKS)) {
      if (state.desks[name]) fillSettings(name);
      if (state.desks[name]?.state === "running") watchRun(name);
      loadPrompt(name).catch((e) => note($(`${name}-prompt-note`), "Prompt konnte nicht geladen werden (" + e.message + ").", "err"));
    }
    loadReserve();

    // The header status polls every 15 s anyway - refresh the cards (and open mailbox) with it.
    Status.on(() => {
      if (state.fastPoll) return;
      refreshDesks();
      loadMailboxes(state.selected);
    });
  };
})();
