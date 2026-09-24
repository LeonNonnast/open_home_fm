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
  const threshold = config.desks?.music?.fill_threshold_minutes ?? 10;
  $("dial-interval").textContent = `Musikredaktion plant nach, sobald weniger als ${threshold} min Programm übrig sind`;
}

// ---------- Studio page ----------

let currentConfig = {};

function fillConfigFields(cfg) {
  $("llm-provider").value = cfg.llm?.provider || "ollama";
  $("llm-model").value = cfg.llm?.[$("llm-provider").value]?.model || "";
  $("music-provider").value = cfg.music?.provider || "local";
  $("schedule-enabled").checked = !!cfg.schedule?.enabled;
  $("schedule-start").value = cfg.schedule?.start_time ?? "06:00";
  $("schedule-end").value = cfg.schedule?.end_time ?? "23:00";
  $("raw-config").value = JSON.stringify(cfg, null, 2);
}

async function afterConfigSave(config, noteEl) {
  currentConfig = config;
  $("raw-config").value = JSON.stringify(config, null, 2);
  renderDial(config);
  refreshStatus();
  note(noteEl, "Gespeichert – gilt ab dem nächsten Durchlauf.", "ok");
}

// Each card sends only its own fields (PATCH, deep-merged on the server), so saving one card
// never overwrites what another card - or another browser tab - changed in the meantime.
async function patchConfig(partial, noteEl) {
  try {
    await afterConfigSave(await api("/api/config", { method: "PATCH", ...jsonBody(partial) }), noteEl);
  } catch (e) {
    note(noteEl, "Speichern fehlgeschlagen (" + e.message + ").", "err");
  }
}

function scheduleFields() {
  return {
    schedule: {
      enabled: $("schedule-enabled").checked,
      start_time: $("schedule-start").value || "06:00",
      end_time: $("schedule-end").value || "23:00",
    },
  };
}

function agentFields() {
  const provider = $("llm-provider").value;
  return {
    llm: { provider, [provider]: { model: $("llm-model").value } },
    music: { provider: $("music-provider").value },
  };
}

function voiceFields() {
  const { text, ...piper } = voiceSettings();
  return { tts: { piper } };
}

// "Erweitert" replaces the whole config with the JSON as shown (PUT) - keys removed there fall
// back to their defaults.
async function saveRawConfig(noteEl) {
  let updated;
  try {
    updated = JSON.parse($("raw-config").value);
  } catch (e) {
    note(noteEl, "Ungültiges JSON: " + e.message, "err");
    return;
  }
  try {
    await api("/api/config", { method: "PUT", ...jsonBody(updated) });
    const config = await api("/api/config");
    fillConfigFields(config);
    await afterConfigSave(config, noteEl);
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
    list.innerHTML = `<li class="empty" style="display:block">Noch nichts eingeplant. Die Musikredaktion plant gleich – oder starte einen Durchlauf.</li>`;
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
    : `<li class="empty" style="display:block">Die Warteschlange ist leer.</li>`;
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

// ---------- Voice card ----------

const VOICE_PRESETS = {
  neutral: { length_scale: 1.0, pitch_semitones: 0, echo: 0 },
  // Calm, slightly deeper, with a touch of room - the "AI butler" sound.
  jarvis: { length_scale: 1.08, pitch_semitones: -1.5, echo: 0.3 },
  night: { length_scale: 1.2, pitch_semitones: -3, echo: 0.5 },
};

let installedVoices = [];

function voiceSettings() {
  return {
    voice_model: $("voice-model").value,
    speaker: $("voice-speaker-field").hidden ? null : $("voice-speaker").value || null,
    length_scale: parseFloat($("voice-length").value),
    pitch_semitones: parseFloat($("voice-pitch").value),
    echo: parseFloat($("voice-echo").value),
    text: $("voice-text").value.trim(),
  };
}

function renderVoiceOutputs() {
  const length = parseFloat($("voice-length").value);
  const pitch = parseFloat($("voice-pitch").value);
  const fmt = (n) => n.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  $("voice-length-out").textContent =
    length === 1 ? "normal" : `${fmt(length)}× ${length > 1 ? "langsamer" : "schneller"}`;
  $("voice-pitch-out").textContent =
    pitch === 0 ? "original" : `${pitch > 0 ? "+" : ""}${pitch.toLocaleString("de-DE")} Halbtöne`;
  $("voice-echo-out").textContent = `${Math.round(parseFloat($("voice-echo").value) * 100)} %`;
}

function renderSpeakers(selected) {
  const voice = installedVoices.find((v) => v.voice_model === $("voice-model").value);
  const speakers = voice?.speakers || [];
  $("voice-speaker-field").hidden = speakers.length === 0;
  $("voice-speaker").innerHTML = speakers.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  if (selected && speakers.includes(selected)) $("voice-speaker").value = selected;
}

async function loadVoices() {
  const piper = currentConfig.tts?.piper || {};
  const { installed, catalog } = await api("/api/voice");
  installedVoices = installed;

  const configured = piper.voice_model || "";
  const options = installed.map((v) => `<option value="${esc(v.voice_model)}">${esc(v.id)}</option>`);
  if (configured && !installed.some((v) => v.voice_model === configured)) {
    options.unshift(`<option value="${esc(configured)}">${esc(configured)} (fehlt)</option>`);
  }
  $("voice-model").innerHTML = options.join("") || `<option value="">Keine Stimme installiert</option>`;
  $("voice-model").value = configured || installed[0]?.voice_model || "";
  renderSpeakers(piper.speaker);

  $("voice-length").value = piper.length_scale ?? 1;
  $("voice-pitch").value = piper.pitch_semitones ?? 0;
  $("voice-echo").value = piper.echo ?? 0;
  renderVoiceOutputs();

  $("voice-catalog").innerHTML = catalog.length
    ? catalog.map((v) => `<option value="${esc(v.id)}" ${v.installed ? "disabled" : ""}>` +
        `${esc(v.name)} · ${esc(v.quality)}${v.speakers > 1 ? ` · ${v.speakers} Sprecher` : ""}` +
        ` · ${v.size_mb} MB${v.installed ? " · installiert" : ""}</option>`).join("")
    : `<option value="">Katalog nicht erreichbar (offline?)</option>`;
}

function initVoice() {
  ["voice-length", "voice-pitch", "voice-echo"].forEach((id) => $(id).addEventListener("input", renderVoiceOutputs));
  $("voice-model").addEventListener("change", () => renderSpeakers());

  document.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = VOICE_PRESETS[btn.dataset.preset];
      $("voice-length").value = preset.length_scale;
      $("voice-pitch").value = preset.pitch_semitones;
      $("voice-echo").value = preset.echo;
      renderVoiceOutputs();
      note($("voice-note"), "Voreinstellung übernommen – Hörprobe anhören oder speichern.");
    });
  });

  let audio = null;
  $("voice-preview").addEventListener("click", async () => {
    const btn = $("voice-preview");
    const settings = voiceSettings();
    if (!settings.voice_model || !settings.text) {
      note($("voice-note"), "Stimme und Testsatz dürfen nicht leer sein.", "err");
      return;
    }
    btn.disabled = true;
    note($("voice-note"), "Wird gesprochen…");
    try {
      const res = await fetch("/api/voice/preview", { method: "POST", ...jsonBody(settings) });
      if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
      if (audio) audio.pause();
      audio = new Audio(URL.createObjectURL(await res.blob()));
      await audio.play();
      note($("voice-note"), "");
    } catch (e) {
      note($("voice-note"), "Hörprobe fehlgeschlagen (" + e.message + ").", "err");
    } finally {
      btn.disabled = false;
    }
  });

  $("save-voice").addEventListener("click", () => {
    if (!$("voice-model").value) {
      note($("voice-note"), "Keine Stimme ausgewählt.", "err");
      return;
    }
    patchConfig(voiceFields(), $("voice-note"));
  });

  $("voice-download").addEventListener("click", async () => {
    const id = $("voice-catalog").value;
    if (!id) return;
    const btn = $("voice-download");
    btn.disabled = true;
    note($("voice-note"), `Lade ${id} herunter – das kann eine Minute dauern…`);
    try {
      const { voice_model } = await api("/api/voice/download", { method: "POST", ...jsonBody({ id }) });
      await loadVoices();
      $("voice-model").value = voice_model;
      renderSpeakers();
      note($("voice-note"), "Heruntergeladen und ausgewählt – Hörprobe anhören, dann speichern.", "ok");
    } catch (e) {
      note($("voice-note"), "Download fehlgeschlagen (" + e.message + ").", "err");
    } finally {
      btn.disabled = false;
    }
  });
}

function renderPromptState(customized) {
  $("prompt-meta").textContent = customized ? "Eigener Prompt · data/prompts/music.md" : "Standard · config/desks/music.md";
  $("reset-prompt").hidden = !customized;
}

async function initStudio() {
  const [prompt, config] = await Promise.all([
    api("/api/config/system_prompt"),
    api("/api/config"),
  ]);
  currentConfig = config;
  $("system-prompt").value = prompt.text;
  renderPromptState(prompt.customized);
  fillConfigFields(config);
  renderDial(config);

  $("llm-provider").addEventListener("change", () => {
    $("llm-model").value = currentConfig.llm?.[$("llm-provider").value]?.model || "";
  });

  $("save-prompt").addEventListener("click", async () => {
    try {
      const { customized } = await api("/api/config/system_prompt", {
        method: "PUT",
        ...jsonBody({ text: $("system-prompt").value }),
      });
      renderPromptState(customized);
      note($("prompt-note"), "Gespeichert.", "ok");
    } catch (e) {
      note($("prompt-note"), "Speichern fehlgeschlagen (" + e.message + ").", "err");
    }
  });

  $("reset-prompt").addEventListener("click", async () => {
    if (!confirm("Eigenen Prompt verwerfen und den Standard-Prompt wiederherstellen?")) return;
    try {
      const { text, customized } = await api("/api/config/system_prompt/reset", { method: "POST" });
      $("system-prompt").value = text;
      renderPromptState(customized);
      note($("prompt-note"), "Standard wiederhergestellt.", "ok");
    } catch (e) {
      note($("prompt-note"), "Zurücksetzen fehlgeschlagen (" + e.message + ").", "err");
    }
  });

  $("save-schedule").addEventListener("click", () => patchConfig(scheduleFields(), $("schedule-note")));
  $("save-config").addEventListener("click", () => patchConfig(agentFields(), $("config-note")));
  $("save-raw").addEventListener("click", () => saveRawConfig($("raw-note")));
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
      const res = await api("/api/desks/music/run", { method: "POST" });
      if (res.status === "skipped" || res.status === "backoff" || res.status === "disabled") {
        btn.disabled = false;
        note($("trigger-note"), res.reason || "Gerade nicht möglich.", "err");
        return;
      }
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

  initVoice();
  await Promise.all([refreshStatus(), loadPlugins(), loadHistory(), loadVoices().catch(() => {
    note($("voice-note"), "Stimmen konnten nicht geladen werden.", "err");
  })]);
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
