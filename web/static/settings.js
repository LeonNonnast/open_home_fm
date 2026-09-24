// "Technik" (settings.html): Sendezeiten, LLM & Musikquelle, Stimme, Plugins, Erweitert.
// Each card sends only its own fields (PATCH, deep-merged on the server), so saving one card never
// overwrites what another card - or another browser tab - changed in the meantime.

(() => {
  let config = {};

  function setConfig(next) {
    config = next;
    $("raw-config").value = JSON.stringify(config, null, 2);
  }

  async function patchConfig(partial, noteEl, message = "Gespeichert.") {
    try {
      setConfig(await api("/api/config", { method: "PATCH", ...jsonBody(partial) }));
      note(noteEl, message, "ok");
      Status.refresh();
      return true;
    } catch (e) {
      note(noteEl, "Speichern fehlgeschlagen (" + e.message + ").", "err");
      return false;
    }
  }

  // ---------- Sendezeiten ----------

  function fillSchedule() {
    $("schedule-enabled").checked = !!config.schedule?.enabled;
    $("schedule-start").value = config.schedule?.start_time ?? "06:00";
    $("schedule-end").value = config.schedule?.end_time ?? "23:00";
  }

  const scheduleFields = () => ({
    schedule: {
      enabled: $("schedule-enabled").checked,
      start_time: $("schedule-start").value || "06:00",
      end_time: $("schedule-end").value || "23:00",
    },
  });

  // ---------- LLM & Musikquelle ----------

  let playlists = { provider: null, playlists: [], error: null };

  function fillSource() {
    $("llm-provider").value = config.llm?.provider || "ollama";
    $("llm-model").value = config.llm?.[$("llm-provider").value]?.model || "";
    $("music-provider").value = config.music?.provider || "local";
    const volume = config.music?.spotify?.volume_percent;
    $("spotify-volume").value = volume ?? "";
    renderVolumeField();
    renderPlaylists();
  }

  function renderVolumeField() {
    $("volume-field").hidden = $("music-provider").value !== "spotify";
  }

  function renderPlaylists() {
    const selected = new Set(config.music?.favorite_playlists || []);
    const known = playlists.playlists || [];
    const rows = known.map((p) => ({ ...p, missing: false }));
    // Favorites the source doesn't list (other source, deleted playlist) stay selectable - and kept.
    for (const id of selected) if (!known.some((p) => p.id === id)) rows.push({ id, name: id, missing: true });

    const saved = config.music?.provider || "local";
    const label = saved === "spotify" ? "Spotify" : "lokalen Bibliothek";
    $("playlist-source").textContent = playlists.error
      ? `Playlists der ${label} nicht abrufbar: ${playlists.error}`
      : $("music-provider").value !== saved
        ? `Die Liste zeigt die gespeicherte Quelle (${label}) – nach dem Speichern neu laden.`
        : saved === "local" ? "Ordner unter der lokalen Bibliothek." : "";

    const list = $("playlist-list");
    if (!rows.length) {
      list.innerHTML = `<li class="hint">${playlists.error ? "–" : "Keine Playlists gefunden."}</li>`;
      return;
    }
    list.innerHTML = rows.map((p, i) => `
      <li>
        <label for="pl-${i}">
          <input type="checkbox" id="pl-${i}" data-id="${esc(p.id)}" ${selected.has(p.id) ? "checked" : ""}>
          <span class="${p.missing ? "missing" : ""}">${esc(p.name)}${p.missing ? " (nicht gefunden)" : ""}</span>
          ${p.track_count != null ? `<span class="count">${p.track_count}</span>` : ""}
        </label>
      </li>`).join("");
  }

  async function loadPlaylists() {
    try {
      playlists = await api("/api/music/playlists");
    } catch (e) {
      playlists = { playlists: [], error: e.message };
    }
    renderPlaylists();
  }

  function sourceFields() {
    const provider = $("llm-provider").value;
    const volumeRaw = $("spotify-volume").value.trim();
    const music = {
      provider: $("music-provider").value,
      favorite_playlists: [...document.querySelectorAll("#playlist-list input:checked")].map((i) => i.dataset.id),
    };
    if (music.provider === "spotify") {
      const volume = volumeRaw === "" ? null : Math.round(Number(volumeRaw));
      if (volume !== null && !(volume >= 0 && volume <= 100)) throw new Error("Lautstärke muss zwischen 0 und 100 liegen");
      music.spotify = { volume_percent: volume };
    }
    return { llm: { provider, [provider]: { model: $("llm-model").value.trim() } }, music };
  }

  async function saveSource() {
    let partial;
    try {
      partial = sourceFields();
    } catch (e) {
      note($("source-note"), e.message + ".", "err");
      return;
    }
    const sourceChanged = partial.music.provider !== (config.music?.provider || "local");
    if (await patchConfig(partial, $("source-note"), "Gespeichert – gilt ab dem nächsten Lauf.")) {
      if (sourceChanged) loadPlaylists();
      else renderPlaylists();
    }
  }

  // ---------- Stimme ----------

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
    const piper = config.tts?.piper || {};
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
      const { text, ...piper } = voiceSettings();
      patchConfig({ tts: { piper } }, $("voice-note"), "Gespeichert – gilt für die nächsten Ansagen.");
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

  // ---------- Plugins ----------

  async function loadPlugins() {
    const list = $("plugin-list");
    let plugins;
    try {
      ({ plugins } = await api("/api/plugins"));
    } catch (e) {
      list.innerHTML = `<p class="note err">Plugins konnten nicht geladen werden (${esc(e.message)}).</p>`;
      return;
    }
    if (!plugins.length) {
      list.innerHTML = `<p class="hint">Keine Plugins gefunden. Lege einen Ordner mit manifest.yaml und plugin.py unter ./plugins an.</p>`;
      return;
    }
    list.innerHTML = plugins.map((p, i) => `
      <div class="plugin">
        <div>
          <div class="plugin-name">${esc(p.name)}${p.context ? `<span class="chip" title="Kann Redaktionen automatisch als Kontext mitgegeben werden">Kontext</span>` : ""}</div>
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
          setConfig(await api("/api/config"));
        } catch {
          input.checked = !input.checked;
        }
      });
    });
  }

  // ---------- Erweitert ----------

  // Replaces the whole config with the JSON as shown (PUT) - keys removed there fall back to
  // their defaults.
  async function saveRawConfig() {
    const noteEl = $("raw-note");
    let updated;
    try {
      updated = JSON.parse($("raw-config").value);
    } catch (e) {
      note(noteEl, "Ungültiges JSON: " + e.message, "err");
      return;
    }
    try {
      await api("/api/config", { method: "PUT", ...jsonBody(updated) });
      setConfig(await api("/api/config"));
      fillAll();
      note(noteEl, "Gespeichert.", "ok");
      Status.refresh();
    } catch (e) {
      note(noteEl, "Speichern fehlgeschlagen (" + e.message + ").", "err");
    }
  }

  function fillAll() {
    fillSchedule();
    fillSource();
  }

  // ---------- boot ----------

  Pages.settings = async () => {
    $("save-schedule").addEventListener("click", () =>
      patchConfig(scheduleFields(), $("schedule-note"), "Gespeichert – gilt sofort."));
    $("llm-provider").addEventListener("change", () => {
      $("llm-model").value = config.llm?.[$("llm-provider").value]?.model || "";
    });
    $("music-provider").addEventListener("change", () => {
      renderVolumeField();
      renderPlaylists();
    });
    $("save-source").addEventListener("click", saveSource);
    $("save-raw").addEventListener("click", saveRawConfig);
    $("reload-raw").addEventListener("click", async () => {
      try {
        setConfig(await api("/api/config"));
        note($("raw-note"), "Neu geladen.", "ok");
      } catch (e) {
        note($("raw-note"), "Laden fehlgeschlagen (" + e.message + ").", "err");
      }
    });
    initVoice();

    try {
      setConfig(await api("/api/config"));
    } catch (e) {
      note($("schedule-note"), "Konfiguration konnte nicht geladen werden (" + e.message + ").", "err");
      return;
    }
    fillAll();
    await Promise.all([
      loadPlaylists(),
      loadPlugins(),
      loadVoices().catch(() => note($("voice-note"), "Stimmen konnten nicht geladen werden.", "err")),
    ]);
  };
})();
