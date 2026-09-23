async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    throw new Error(`${path} -> ${res.status}: ${await res.text()}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res.text();
}

function showToast(el, message, isError) {
  el.textContent = message;
  el.style.color = isError ? "#ff5b7f" : "#4caf7d";
  setTimeout(() => { el.textContent = ""; }, 3000);
}

// ---------- Config page ----------

async function initConfigPage() {
  const promptArea = document.getElementById("system-prompt");
  const savePromptBtn = document.getElementById("save-prompt");
  const promptToast = document.getElementById("prompt-toast");

  const { text } = await api("/api/config/system_prompt");
  promptArea.value = text;

  savePromptBtn.addEventListener("click", async () => {
    try {
      await api("/api/config/system_prompt", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: promptArea.value }),
      });
      showToast(promptToast, "Gespeichert.");
    } catch (e) {
      showToast(promptToast, e.message, true);
    }
  });

  const config = await api("/api/config");
  const llmProvider = document.getElementById("llm-provider");
  const llmModel = document.getElementById("llm-model");
  const musicProvider = document.getElementById("music-provider");
  const loopInterval = document.getElementById("loop-interval");
  const scheduleEnabled = document.getElementById("schedule-enabled");
  const scheduleStart = document.getElementById("schedule-start");
  const scheduleEnd = document.getElementById("schedule-end");
  const rawConfig = document.getElementById("raw-config");
  const configToast = document.getElementById("config-toast");

  function applyToFields(cfg) {
    llmProvider.value = cfg.llm?.provider || "ollama";
    llmModel.value = cfg.llm?.[llmProvider.value]?.model || "";
    musicProvider.value = cfg.music?.provider || "local";
    loopInterval.value = cfg.agent?.loop_interval_seconds ?? 300;
    scheduleEnabled.checked = !!cfg.schedule?.enabled;
    scheduleStart.value = cfg.schedule?.start_time ?? "06:00";
    scheduleEnd.value = cfg.schedule?.end_time ?? "23:00";
  }

  applyToFields(config);
  rawConfig.value = JSON.stringify(config, null, 2);

  document.getElementById("save-config").addEventListener("click", async () => {
    try {
      const updated = JSON.parse(rawConfig.value);
      updated.llm = updated.llm || {};
      updated.llm.provider = llmProvider.value;
      updated.llm[llmProvider.value] = updated.llm[llmProvider.value] || {};
      updated.llm[llmProvider.value].model = llmModel.value;
      updated.music = updated.music || {};
      updated.music.provider = musicProvider.value;
      updated.agent = updated.agent || {};
      updated.agent.loop_interval_seconds = parseInt(loopInterval.value, 10);
      updated.schedule = updated.schedule || {};
      updated.schedule.enabled = scheduleEnabled.checked;
      updated.schedule.start_time = scheduleStart.value || "06:00";
      updated.schedule.end_time = scheduleEnd.value || "23:00";

      await api("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updated),
      });
      rawConfig.value = JSON.stringify(updated, null, 2);
      showToast(configToast, "Gespeichert. Wirkt ab dem nächsten Durchlauf.");
    } catch (e) {
      showToast(configToast, e.message, true);
    }
  });

  document.getElementById("sync-raw").addEventListener("click", () => {
    try {
      applyToFields(JSON.parse(rawConfig.value));
    } catch (e) {
      showToast(configToast, "Ungültiges JSON: " + e.message, true);
    }
  });

  await loadPlugins();
  await loadHistory();
}

async function loadHistory() {
  const list = document.getElementById("history-list");
  if (!list) return;
  const { transcripts } = await api("/api/transcripts");

  if (!transcripts.length) {
    list.innerHTML = '<p class="status-line">Noch keine Läufe.</p>';
    return;
  }

  list.innerHTML = "";
  for (const t of transcripts) {
    const item = document.createElement("div");
    item.className = "history-item";
    item.innerHTML = `
      <div class="history-header">
        <strong>${new Date(t.created_at).toLocaleString("de-DE")}</strong>
        <span class="status-line">${t.segment_count} Segmente · ${t.message_count} Nachrichten</span>
      </div>
      <div>${t.final_message || "<em>(keine Ansage)</em>"}</div>
      ${t.error ? `<div style="color:#ff5b7f;">Fehler: ${t.error}</div>` : ""}
      <button class="secondary toggle-raw">Rohdaten anzeigen</button>
      <pre class="raw-transcript" style="display:none;"></pre>`;

    const btn = item.querySelector(".toggle-raw");
    const pre = item.querySelector(".raw-transcript");
    btn.addEventListener("click", async () => {
      const hidden = pre.style.display === "none";
      if (hidden && !pre.textContent) {
        const full = await api(`/api/transcripts/${t.id}`);
        pre.textContent = JSON.stringify(full.messages, null, 2);
      }
      pre.style.display = hidden ? "block" : "none";
      btn.textContent = hidden ? "Rohdaten ausblenden" : "Rohdaten anzeigen";
    });

    list.appendChild(item);
  }
}

async function loadPlugins() {
  const list = document.getElementById("plugin-list");
  const { plugins } = await api("/api/plugins");
  list.innerHTML = "";
  for (const p of plugins) {
    const row = document.createElement("div");
    row.className = "plugin-row";
    row.innerHTML = `
      <div>
        <div class="name">${p.name}</div>
        <div class="desc">${p.description || ""}</div>
      </div>
      <label class="switch">
        <input type="checkbox" ${p.enabled ? "checked" : ""} data-name="${p.name}">
        <span class="slider"></span>
      </label>`;
    row.querySelector("input").addEventListener("change", async (ev) => {
      await api(`/api/plugins/${encodeURIComponent(p.name)}/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: ev.target.checked }),
      });
    });
    list.appendChild(row);
  }
}

async function initStatusPanel() {
  const el = document.getElementById("status-panel");
  if (!el) return;

  async function refresh() {
    const { state, player_current_segment_index, on_air } = await api("/api/status");
    const onAirBadge = on_air
      ? '<span style="color:#4caf7d;">● On Air</span>'
      : '<span style="color:#ff5b7f;">● Außerhalb der Sendezeit</span>';

    if (!state) {
      el.innerHTML = `<p class="status-line">${onAirBadge}</p><p class="status-line">Noch kein Durchlauf.</p>`;
      return;
    }
    const segments = state.script?.segments || [];
    const segmentsHtml = segments
      .map((s, i) => `
        <div class="segment">
          <span class="idx">${i + 1}</span>
          <span class="type ${s.type}">${s.type}</span>
          <span>${s.title}${i === player_current_segment_index ? " ▶" : ""}</span>
        </div>`)
      .join("");
    el.innerHTML = `
      <p class="status-line">${onAirBadge}</p>
      <p class="status-line">Letzter Durchlauf: ${new Date(state.last_run).toLocaleString("de-DE")}</p>
      <p class="status-line">Verarbeitete Wünsche: ${state.inbox_items_processed}</p>
      ${state.final_message ? `<p>${state.final_message}</p>` : ""}
      ${segmentsHtml}`;
  }

  document.getElementById("trigger-now").addEventListener("click", async () => {
    await api("/api/status/trigger", { method: "POST" });
    setTimeout(refresh, 2000);
  });

  refresh();
  setInterval(refresh, 15000);
}

// ---------- Inbox page ----------

async function initInboxPage() {
  const textArea = document.getElementById("wish-text");
  const sendBtn = document.getElementById("send-text");
  const toast = document.getElementById("inbox-toast");
  const list = document.getElementById("inbox-list");

  async function refreshList() {
    const { items } = await api("/api/inbox");
    list.innerHTML = items.length
      ? items.map((i) => `<div class="inbox-item">${i.text}</div>`).join("")
      : '<p class="status-line">Keine offenen Wünsche.</p>';
  }

  sendBtn.addEventListener("click", async () => {
    if (!textArea.value.trim()) return;
    try {
      await api("/api/inbox/text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: textArea.value.trim() }),
      });
      textArea.value = "";
      showToast(toast, "Wunsch gesendet!");
      refreshList();
    } catch (e) {
      showToast(toast, e.message, true);
    }
  });

  const micBtn = document.getElementById("mic-btn");
  let mediaRecorder, chunks = [];

  micBtn.addEventListener("click", async () => {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => chunks.push(e.data);
      mediaRecorder.onstop = async () => {
        micBtn.classList.remove("recording");
        micBtn.textContent = "🎤";
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks, { type: "audio/webm" });
        const form = new FormData();
        form.append("file", blob, "wish.webm");
        showToast(toast, "Transkribiere...");
        try {
          const result = await api("/api/inbox/voice", { method: "POST", body: form });
          showToast(toast, `Erkannt: "${result.text}"`);
          refreshList();
        } catch (e) {
          showToast(toast, e.message, true);
        }
      };
      mediaRecorder.start();
      micBtn.classList.add("recording");
      micBtn.textContent = "■";
    } catch (e) {
      showToast(toast, "Mikrofonzugriff verweigert: " + e.message, true);
    }
  });

  refreshList();
  setInterval(refreshList, 15000);
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("system-prompt")) initConfigPage();
  if (document.getElementById("status-panel")) initStatusPanel();
  if (document.getElementById("wish-text")) initInboxPage();
});
