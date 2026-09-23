// ---------- helpers ----------

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res.text();
}

const jsonBody = (data) => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data),
});

function note(el, message, kind) {
  el.textContent = message;
  el.className = "note" + (kind ? ` ${kind}` : "");
  clearTimeout(el._timer);
  if (kind === "ok") el._timer = setTimeout(() => { el.textContent = ""; }, 3500);
}

const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const pad = (n) => String(n).padStart(2, "0");
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function fmtDateTime(d) {
  return `${WEEKDAYS[d.getDay()]} ${pad(d.getDate())}.${pad(d.getMonth() + 1)}. · ${hhmm(d)}`;
}

// The model tends to answer in light Markdown; the UI shows plain text.
function plain(text) {
  return String(text ?? "")
    .replace(/\*\*|__/g, "")
    .replace(/(^|[\s(])[*_]([^*_\n]+)[*_]/g, "$1$2")
    .replace(/^\s*[-*]\s+/gm, "– ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return "–:––";
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

// Inbox filenames are UTC stamps like 20260923T071915043891.txt
function stampToDate(filename) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(filename || "");
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
}

// ---------- header (both pages) ----------

function tickClock() {
  const el = $("clock");
  if (el) el.textContent = fmtDateTime(new Date());
}

function renderLamp(onAir) {
  const lamp = $("lamp");
  if (!lamp) return;
  lamp.dataset.state = onAir ? "on" : "off";
  $("lamp-label").textContent = onAir ? "On Air" : "Sendepause";
}

// ---------- 24h dial ----------

function minutesOf(value) {
  const [h, m] = (value || "00:00").split(":").map(Number);
  return h * 60 + m;
}

function renderDial(config) {
  const track = $("dial-track");
  if (!track) return;
  const schedule = config.schedule || {};
  const day = 24 * 60;
  const pct = (min) => `${(min / day) * 100}%`;
  const parts = [];

  if (!schedule.enabled) {
    parts.push(`<div class="dial-window" style="left:0;width:100%"></div>`);
  } else {
    const start = minutesOf(schedule.start_time);
    const end = minutesOf(schedule.end_time);
    if (start <= end) {
      parts.push(`<div class="dial-window bounded" style="left:${pct(start)};width:${pct(end - start)}"></div>`);
    } else {
      parts.push(`<div class="dial-window bounded open-right" style="left:${pct(start)};width:${pct(day - start)}"></div>`);
      parts.push(`<div class="dial-window bounded open-left" style="left:0;width:${pct(end)}"></div>`);
    }
  }

  for (let h = 1; h < 24; h++) {
    parts.push(`<div class="dial-tick${h % 6 === 0 ? " major" : ""}" style="left:${pct(h * 60)}"></div>`);
  }
  for (const h of [0, 6, 12, 18, 24]) {
    const cls = h === 0 ? " first" : h === 24 ? " last" : "";
    parts.push(`<span class="dial-label${cls}" style="left:${pct(h * 60)}">${pad(h)}:00</span>`);
  }

  const now = new Date();
  parts.push(`<div class="dial-needle" style="left:${pct(now.getHours() * 60 + now.getMinutes())}" title="Jetzt ${hhmm(now)}"></div>`);
  track.innerHTML = parts.join("");

  $("dial-window").textContent = schedule.enabled
    ? `Sendefenster ${schedule.start_time} – ${schedule.end_time} Uhr`
    : "Rund um die Uhr auf Sendung";
  const interval = config.agent?.loop_interval_seconds ?? 300;
  $("dial-interval").textContent = interval >= 60
    ? `Neuer Durchlauf alle ${Math.round(interval / 60)} min`
    : `Neuer Durchlauf alle ${interval} s`;
}

// ---------- Studio page ----------

let currentConfig = {};

function fillConfigFields(cfg) {
  $("llm-provider").value = cfg.llm?.provider || "ollama";
  $("llm-model").value = cfg.llm?.[$("llm-provider").value]?.model || "";
  $("music-provider").value = cfg.music?.provider || "local";
  $("loop-interval").value = cfg.agent?.loop_interval_seconds ?? 300;
  $("schedule-enabled").checked = !!cfg.schedule?.enabled;
  $("schedule-start").value = cfg.schedule?.start_time ?? "06:00";
  $("schedule-end").value = cfg.schedule?.end_time ?? "23:00";
  $("raw-config").value = JSON.stringify(cfg, null, 2);
}

async function saveConfig(noteEl) {
  let updated;
  try {
    updated = JSON.parse($("raw-config").value);
  } catch (e) {
    note(noteEl, "Das JSON unter „Erweitert“ ist ungültig: " + e.message, "err");
    return;
  }
  const provider = $("llm-provider").value;
  updated.llm = updated.llm || {};
  updated.llm.provider = provider;
  updated.llm[provider] = { ...(updated.llm[provider] || {}), model: $("llm-model").value };
  updated.music = { ...(updated.music || {}), provider: $("music-provider").value };
  updated.agent = { ...(updated.agent || {}), loop_interval_seconds: parseInt($("loop-interval").value, 10) || 300 };
  updated.schedule = {
    ...(updated.schedule || {}),
    enabled: $("schedule-enabled").checked,
    start_time: $("schedule-start").value || "06:00",
    end_time: $("schedule-end").value || "23:00",
  };

  try {
    await api("/api/config", { method: "PUT", ...jsonBody(updated) });
    currentConfig = updated;
    fillConfigFields(updated);
    renderDial(updated);
    refreshStatus();
    note(noteEl, "Gespeichert – gilt ab dem nächsten Durchlauf.", "ok");
  } catch (e) {
    note(noteEl, "Speichern fehlgeschlagen (" + e.message + ").", "err");
  }
}

async function refreshStatus() {
  let data;
  try {
    data = await api("/api/status");
  } catch {
    return;
  }
  renderLamp(data.on_air);
  if (!$("run-order")) return;

  const { state, player_current_segment_index: playing } = data;
  const list = $("run-order");
  const moderation = $("moderation");
  const runError = $("run-error");

  if (!state) {
    $("program-meta").textContent = "";
    moderation.hidden = true;
    runError.hidden = true;
    list.innerHTML = `<li class="empty" style="display:block">Noch kein Sendeablauf. Starte einen Durchlauf oder warte auf den nächsten Takt.</li>`;
    return;
  }

  const segments = state.script?.segments || [];
  const total = segments.reduce((sum, s) => sum + (s.duration_seconds || 0), 0);
  $("program-meta").textContent =
    `${fmtDateTime(new Date(state.last_run))} · ${segments.length} Segmente` +
    (total ? ` · ${fmtDuration(total)}` : "");

  moderation.hidden = !state.final_message;
  moderation.textContent = plain(state.final_message);

  runError.hidden = !state.error;
  runError.textContent = state.error ? `Letzter Durchlauf mit Fehler: ${state.error}` : "";

  list.innerHTML = segments.length
    ? segments.map((s, i) => `
        <li class="${i === playing ? "playing" : ""}">
          <span class="idx">${pad(i + 1)}</span>
          <span class="chip ${s.type === "jingle" ? "jingle" : "track"}">${s.type === "jingle" ? "Ansage" : "Musik"}</span>
          <span class="title">${esc(s.type === "jingle" ? s.text || s.title : s.title)}</span>
          <span class="dur">${s.type === "jingle" ? "" : fmtDuration(s.duration_seconds)}</span>
        </li>`).join("")
    : `<li class="empty" style="display:block">Der letzte Durchlauf hat kein Script erzeugt.</li>`;
}

async function loadPlugins() {
  const list = $("plugin-list");
  const { plugins } = await api("/api/plugins");
  if (!plugins.length) {
    list.innerHTML = `<p class="hint">Keine Plugins gefunden. Lege einen Ordner mit manifest.yaml und plugin.py unter ./plugins an.</p>`;
    return;
  }
  list.innerHTML = plugins.map((p, i) => `
    <div class="plugin">
      <div>
        <div class="plugin-name">${esc(p.name)}${p.context ? `<span class="chip" title="Wird bei jedem Durchlauf automatisch als Kontext geladen">Kontext</span>` : ""}</div>
        <p class="plugin-desc">${esc(p.description)}</p>
      </div>
      <label class="switch">
        <input type="checkbox" id="plugin-${i}" data-name="${esc(p.name)}" ${p.enabled ? "checked" : ""} aria-label="${esc(p.name)} aktiv">
        <span class="track" aria-hidden="true"></span>
      </label>
    </div>`).join("");

  list.querySelectorAll("input[type=checkbox]").forEach((input) => {
    input.addEventListener("change", async () => {
      try {
        await api(`/api/plugins/${encodeURIComponent(input.dataset.name)}/toggle`, {
          method: "POST",
          ...jsonBody({ enabled: input.checked }),
        });
        currentConfig = await api("/api/config");
        $("raw-config").value = JSON.stringify(currentConfig, null, 2);
      } catch {
        input.checked = !input.checked;
      }
    });
  });
}

async function loadHistory() {
  const list = $("history-list");
  const { transcripts } = await api("/api/transcripts");
  if (!transcripts.length) {
    list.innerHTML = `<p class="hint">Noch keine Läufe gespeichert.</p>`;
    return;
  }
  list.innerHTML = transcripts.map((t) => `
    <details class="run" data-id="${esc(t.id)}">
      <summary>
        <span class="when">${esc(fmtDateTime(new Date(t.created_at)))}</span>
        <span class="what">${t.final_message ? esc(plain(t.final_message)) : "<em>ohne Moderation</em>"}</span>
        <span class="count${t.error ? " err" : ""}">${t.error ? "Fehler" : `${t.segment_count} Seg.`}</span>
      </summary>
      ${t.error ? `<p class="note err" style="margin:8px 0 0">${esc(t.error)}</p>` : ""}
      <pre class="raw">Lädt…</pre>
    </details>`).join("");

  list.querySelectorAll("details.run").forEach((d) => {
    d.addEventListener("toggle", async () => {
      const pre = d.querySelector(".raw");
      if (!d.open || pre.dataset.loaded) return;
      try {
        const full = await api(`/api/transcripts/${encodeURIComponent(d.dataset.id)}`);
        pre.textContent = JSON.stringify(full.messages, null, 2);
        pre.dataset.loaded = "1";
      } catch (e) {
        pre.textContent = "Konnte nicht geladen werden: " + e.message;
      }
    });
  });
}

async function initStudio() {
  const [prompt, config] = await Promise.all([
    api("/api/config/system_prompt"),
    api("/api/config"),
  ]);
  currentConfig = config;
  $("system-prompt").value = prompt.text;
  fillConfigFields(config);
  renderDial(config);

  $("llm-provider").addEventListener("change", () => {
    $("llm-model").value = currentConfig.llm?.[$("llm-provider").value]?.model || "";
  });

  $("save-prompt").addEventListener("click", async () => {
    try {
      await api("/api/config/system_prompt", { method: "PUT", ...jsonBody({ text: $("system-prompt").value }) });
      note($("prompt-note"), "Gespeichert.", "ok");
    } catch (e) {
      note($("prompt-note"), "Speichern fehlgeschlagen (" + e.message + ").", "err");
    }
  });

  $("save-schedule").addEventListener("click", () => saveConfig($("schedule-note")));
  $("save-config").addEventListener("click", () => saveConfig($("config-note")));
  $("save-raw").addEventListener("click", () => saveConfig($("raw-note")));
  $("sync-raw").addEventListener("click", () => {
    try {
      fillConfigFields(JSON.parse($("raw-config").value));
      note($("raw-note"), "Felder übernommen – noch nicht gespeichert.");
    } catch (e) {
      note($("raw-note"), "Ungültiges JSON: " + e.message, "err");
    }
  });

  $("trigger-now").addEventListener("click", async () => {
    const btn = $("trigger-now");
    btn.disabled = true;
    note($("trigger-note"), "Durchlauf läuft – das dauert meist 10–30 Sekunden.");
    try {
      await api("/api/status/trigger", { method: "POST" });
      setTimeout(async () => {
        await Promise.all([refreshStatus(), loadHistory()]);
        btn.disabled = false;
        note($("trigger-note"), "Fertig.", "ok");
      }, 15000);
    } catch (e) {
      btn.disabled = false;
      note($("trigger-note"), "Konnte nicht gestartet werden (" + e.message + ").", "err");
    }
  });

  await Promise.all([refreshStatus(), loadPlugins(), loadHistory()]);
  setInterval(refreshStatus, 15000);
  setInterval(() => renderDial(currentConfig), 60000);
}

// ---------- Wishes page ----------

async function refreshQueue() {
  const list = $("inbox-list");
  const { items } = await api("/api/inbox");
  $("queue-meta").textContent = items.length ? `${items.length} offen · kommt im nächsten Durchlauf dran` : "";
  if (!items.length) {
    list.innerHTML = `<li><p class="queue-empty">Gerade ist nichts offen – der nächste Wunsch könnte deiner sein.</p></li>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const when = stampToDate(item.filename);
    return `
      <li>
        <span class="text">${esc(item.text)}</span>
        <span class="when">${when ? hhmm(when) : ""}</span>
      </li>`;
  }).join("");
}

function initInbox() {
  const textArea = $("wish-text");
  const noteEl = $("inbox-note");

  $("send-text").addEventListener("click", async () => {
    const text = textArea.value.trim();
    if (!text) {
      note(noteEl, "Schreib erst deinen Wunsch ins Feld.", "err");
      return;
    }
    try {
      await api("/api/inbox/text", { method: "POST", ...jsonBody({ text }) });
      textArea.value = "";
      note(noteEl, "Angekommen! Läuft im nächsten Durchlauf.", "ok");
      refreshQueue();
    } catch (e) {
      note(noteEl, "Senden fehlgeschlagen (" + e.message + ").", "err");
    }
  });

  const micBtn = $("mic-btn");
  const label = $("rec-label");
  const timer = $("rec-timer");
  const transcript = $("transcript");
  let recorder = null;
  let chunks = [];
  let startedAt = 0;
  let ticker = null;

  function setIdle(message) {
    micBtn.classList.remove("recording");
    micBtn.setAttribute("aria-label", "Aufnahme starten");
    label.textContent = "Tippen zum Aufnehmen";
    timer.textContent = message || "Nochmal tippen beendet die Aufnahme.";
    clearInterval(ticker);
  }

  micBtn.addEventListener("click", async () => {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      setIdle("Kein Mikrofonzugriff – im Browser erlauben (auf dem Handy geht das nur über HTTPS oder localhost).");
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      setIdle("Wird transkribiert…");
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: "audio/webm" }), "wish.webm");
      try {
        const result = await api("/api/inbox/voice", { method: "POST", body: form });
        transcript.hidden = false;
        transcript.textContent = `„${result.text}“`;
        setIdle("Angekommen – so haben wir dich verstanden:");
        refreshQueue();
      } catch (e) {
        setIdle("Transkription fehlgeschlagen (" + e.message + ").");
      }
    };
    recorder.start();
    startedAt = Date.now();
    micBtn.classList.add("recording");
    micBtn.setAttribute("aria-label", "Aufnahme beenden");
    label.textContent = "Aufnahme läuft";
    timer.textContent = "0:00";
    ticker = setInterval(() => { timer.textContent = fmtDuration((Date.now() - startedAt) / 1000); }, 250);
  });

  refreshQueue();
  setInterval(refreshQueue, 15000);
}

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 15000);
  if ($("run-order")) {
    initStudio();
  } else {
    refreshStatus();
    setInterval(refreshStatus, 30000);
  }
  if ($("wish-text")) initInbox();
});
