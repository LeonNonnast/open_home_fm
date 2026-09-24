// "Sendung" (index.html): on-air display, "Als Nächstes" from the queue, 24h dial, skip/remove.

(() => {
  const VISIBLE_SEGMENTS = 10;
  const UNDO_SECONDS = 10;

  let config = {};
  let lastQueue = null;
  let lastNowKey = null;

  // ---------- 24h dial ----------

  function minutesOf(value) {
    const [h, m] = (value || "00:00").split(":").map(Number);
    return h * 60 + m;
  }

  function renderDial() {
    const track = $("dial-track");
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

    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
    const status = Status.data;
    // Off air the program expires at the end of the current segment - nothing to show.
    const remaining = status?.on_air ? status.remaining_program_seconds || 0 : 0;
    if (remaining > 0) {
      // Planned program as a band from now, wrapping past midnight.
      const endMin = nowMin + remaining / 60;
      parts.push(`<div class="dial-program" style="left:${pct(nowMin)};width:${pct(Math.min(endMin, day) - nowMin)}"></div>`);
      if (endMin > day) parts.push(`<div class="dial-program" style="left:0;width:${pct(Math.min(endMin - day, day))}"></div>`);
    }

    for (let h = 1; h < 24; h++) {
      parts.push(`<div class="dial-tick${h % 6 === 0 ? " major" : ""}" style="left:${pct(h * 60)}"></div>`);
    }
    for (const h of [0, 6, 12, 18, 24]) {
      const cls = h === 0 ? " first" : h === 24 ? " last" : "";
      parts.push(`<span class="dial-label${cls}" style="left:${pct(h * 60)}">${pad(h)}:00</span>`);
    }
    parts.push(`<div class="dial-needle" style="left:${pct(nowMin)}" title="Jetzt ${hhmm(now)}"></div>`);
    track.innerHTML = parts.join("");

    $("dial-window").textContent = schedule.enabled
      ? `Sendefenster ${schedule.start_time} – ${schedule.end_time} Uhr`
      : "Rund um die Uhr auf Sendung";
    $("dial-program").textContent = !status
      ? ""
      : remaining > 0
        ? `Programm bis ~${hhmm(new Date(Date.now() + remaining * 1000))}`
        : status.on_air ? "Kein Programm geplant" : status.next_on_air_at ? `Sendebeginn ${fmtWhen(status.next_on_air_at)}` : "";
  }

  // ---------- on air ----------

  function progressTick() {
    const np = Status.data?.now_playing;
    if (!np) return;
    const position = (np.position || 0) + (Date.now() - Status.receivedAt) / 1000;
    const duration = np.duration;
    if (duration) {
      const clamped = Math.min(position, duration);
      $("progress-bar").style.transform = `scaleX(${Math.min(1, clamped / duration)})`;
      $("progress-pos").textContent = fmtDuration(clamped);
      $("progress-dur").textContent = `noch ${fmtDuration(duration - clamped)}`;
    } else {
      $("progress-bar").style.transform = "scaleX(0)";
      $("progress-pos").textContent = fmtDuration(position);
      $("progress-dur").textContent = "";
    }
  }

  function setStateLine(text, kind) {
    const el = $("state-line");
    el.hidden = !text;
    el.textContent = text || "";
    if (kind) el.dataset.kind = kind; else delete el.dataset.kind;
  }

  function renderOnAir(status) {
    const np = status.now_playing;
    const player = status.player || {};
    const desk = status.desks?.music;
    const planning = desk?.state === "running";
    const display = $("onair");

    // Mode text in the head only while something plays - otherwise the state line says it.
    $("onair-mode").textContent = np ? player.mode_text || "" : "";
    display.dataset.lane = np?.lane || "program";

    if (!status.on_air && !np) {
      display.dataset.state = "off";
      $("now-chips").innerHTML = "";
      $("now-title").textContent = "Sendepause";
      $("now-text").hidden = true;
      $("progress-wrap").hidden = true;
      setStateLine(status.next_on_air_at ? `Sender ruht bis ${fmtWhen(status.next_on_air_at)}` : "Sender ruht");
    } else if (!np) {
      display.dataset.state = "idle";
      $("now-chips").innerHTML = "";
      $("now-title").textContent = planning ? "Musikredaktion plant…" : "Gleich geht’s weiter";
      $("now-text").hidden = true;
      $("progress-wrap").hidden = true;
      if (player.mode === "paused") setStateLine(player.mode_text, "crit");
      else setStateLine(player.mode_text && player.mode_text !== "spielt" ? player.mode_text : "");
    } else {
      display.dataset.state = "on";
      const chips = [laneChip(np.lane), segChip(np.type)];
      if (np.segment_count > 1) chips.push(`<span class="chip">${np.segment_index + 1} / ${np.segment_count}</span>`);
      const chipHtml = chips.join("");
      if ($("now-chips").innerHTML !== chipHtml) $("now-chips").innerHTML = chipHtml;
      const isJingle = np.type === "jingle";
      $("now-title").textContent = isJingle ? "Moderation" : np.title;
      $("now-text").hidden = !(isJingle && np.text);
      $("now-text").textContent = isJingle ? plain(np.text) : "";
      $("progress-wrap").hidden = false;
      if (!status.on_air) setStateLine("Sendeschluss – dieser Beitrag läuft noch aus.", "warn");
      else if (player.mode === "paused") setStateLine(player.mode_text, "crit");
      else if (np.lane === "filler") {
        setStateLine(`${player.mode_text || "Füllprogramm"} · die Musikredaktion ${planning ? "plant gerade" : "plant nach"}`, "warn");
      } else setStateLine("");
      progressTick();
    }

    const skip = $("skip");
    skip.disabled = !np;
    skip.textContent = np?.type === "jingle" ? "Ansage überspringen" : "Song überspringen";

    // A new segment on air shifts the "Als Nächstes" list.
    const nowKey = np ? `${np.item_id}:${np.segment_index}` : "";
    if (nowKey !== lastNowKey) {
      lastNowKey = nowKey;
      refreshQueue();
    } else if (lastQueue) {
      renderQueue(lastQueue);
    }
  }

  function renderDesk(status) {
    const summary = deskSummary(status.desks?.music);
    const lamp = $("desk-lamp");
    lamp.dataset.state = summary.state;
    $("desk-lamp-label").textContent = summary.label;
    $("desk-lines").innerHTML = summary.lines
      .map((l) => `<p class="desk-line${l.kind === "err" ? " err" : ""}">${esc(l.text)}</p>`)
      .join("") + (summary.state === "error" ? `<p class="desk-line"><a class="link" href="/desks.html#music">Details in der Redaktion</a></p>` : "");
  }

  function renderLog(status) {
    const lines = [...(status.player?.log || [])].reverse().slice(0, 12);
    $("player-log").innerHTML = lines.length
      ? lines.map((l) => `<li>${esc(l)}</li>`).join("")
      : `<li>Noch nichts gesendet.</li>`;
  }

  // ---------- Als Nächstes ----------

  const trackCount = (segs) => segs.filter((s) => s.type === "track").length;

  function removeLabel(segs) {
    const n = trackCount(segs);
    return n ? `Block entfernen (${n} Titel)` : "Beitrag entfernen";
  }

  // Start of an item's remaining segments: the server's estimate, or - for the block on air -
  // the end of the current segment.
  function itemStart(item) {
    if (item.starts_at) return new Date(item.starts_at).getTime();
    const np = Status.data?.now_playing;
    if (np?.duration) {
      const position = (np.position || 0) + (Date.now() - Status.receivedAt) / 1000;
      return Date.now() + Math.max(0, np.duration - position) * 1000;
    }
    return Date.now();
  }

  function renderQueue(data) {
    lastQueue = data;
    const status = Status.data;
    let budget = VISIBLE_SEGMENTS;
    const rows = [];
    for (const item of data.items) {
      const segs = item.segments.slice(item.next_segment || 0);
      if (!segs.length) continue;
      const shown = Math.max(0, Math.min(segs.length, budget));
      budget -= shown;
      rows.push({ item, segs, shown });
    }

    const planning = status?.desks?.music?.state === "running";
    const threshold = status?.desks?.music?.fill?.threshold_minutes;
    let empty;
    if (status && !status.on_air) empty = "Sendepause – geplant wird wieder ab Sendebeginn.";
    else if (planning) empty = "Musikredaktion plant…";
    else if (status?.desks?.music?.state === "error") {
      const retry = status.desks.music.backoff_until;
      empty = `Noch nichts eingeplant – die Musikredaktion hatte einen Fehler${retry ? `, nächster Versuch ${fmtWhen(retry)}` : ""}. Bis dahin läuft Füllprogramm, falls vorhanden.`;
    }
    else empty = `Noch nichts eingeplant – die Musikredaktion plant, sobald weniger als ${threshold ?? 10} min Programm übrig sind.`;

    syncList($("next-list"), rows, {
      className: "block",
      key: (r) => r.item.id,
      sig: (r) => `${r.item.status}|${r.item.next_segment}|${r.shown}|${r.segs.length}|${r.item.lane}`,
      empty,
      render: (r) => {
        const segItems = r.segs.slice(0, r.shown).map((s, i) => `
          <li class="${s.type === "jingle" ? "jingle" : "track"}">
            <span class="t" data-seg="${i}"></span>
            <span class="title">${esc(s.type === "jingle" ? plain(s.text || s.title) : s.title)}</span>
            <span class="dur">${s.type === "jingle" ? "" : fmtDuration(s.duration_seconds)}</span>
          </li>`).join("");
        const more = r.segs.length - r.shown;
        const total = r.segs.reduce((sum, s) => sum + segSeconds(s), 0);
        return `
          <div class="block-head">
            ${laneChip(r.item.lane)}
            <span class="when" data-when></span>
            <span class="meta">${trackCount(r.segs)} Titel · ${fmtMinutes(total)}</span>
            <button class="btn ghost small" type="button" data-remove="${esc(r.item.id)}"
              data-count="${trackCount(r.segs)}">${esc(removeLabel(r.segs))}</button>
          </div>
          ${r.shown ? `<ol class="segs">${segItems}${more ? `<li class="more">+ ${more} weitere</li>` : ""}</ol>` : ""}`;
      },
      update: (el, r) => {
        const start = itemStart(r.item);
        const whenEl = el.querySelector("[data-when]");
        whenEl.textContent = r.item.status === "playing" ? `läuft · weiter ab ~${hhmm(new Date(start))}` : `ab ~${hhmm(new Date(start))}`;
        let offset = 0;
        r.segs.slice(0, r.shown).forEach((s, i) => {
          const t = el.querySelector(`[data-seg="${i}"]`);
          if (t) t.textContent = hhmm(new Date(start + offset * 1000));
          offset += segSeconds(s);
        });
      },
    });

    const total = data.items.reduce((sum, i) => sum + (i.remaining_seconds || 0), 0);
    $("next-meta").textContent = rows.length ? `${rows.length} ${rows.length === 1 ? "Beitrag" : "Beiträge"} · ${fmtMinutes(total)}` : "";
  }

  async function refreshQueue() {
    try {
      renderQueue(await api("/api/queue"));
    } catch { /* next poll */ }
  }

  // ---------- actions ----------

  const undo = { id: null, timer: null, left: 0 };

  function hideUndo() {
    clearInterval(undo.timer);
    undo.id = null;
    $("undo").hidden = true;
  }

  function showUndo(id, text) {
    clearInterval(undo.timer);
    undo.id = id;
    undo.left = UNDO_SECONDS;
    $("undo-text").textContent = text;
    $("undo-btn").textContent = `Rückgängig (${undo.left})`;
    $("undo-btn").disabled = false;
    $("undo").hidden = false;
    undo.timer = setInterval(() => {
      undo.left -= 1;
      if (undo.left <= 0) hideUndo();
      else $("undo-btn").textContent = `Rückgängig (${undo.left})`;
    }, 1000);
  }

  function initActions() {
    $("skip").addEventListener("click", async () => {
      const np = Status.data?.now_playing;
      if (!np) return;
      const what = np.type === "jingle" ? "die laufende Ansage" : `„${np.title}“`;
      if (!confirm(`${what[0].toUpperCase()}${what.slice(1)} überspringen?`)) return;
      $("skip").disabled = true;
      try {
        const { skipped } = await api("/api/player/skip", { method: "POST" });
        note($("skip-note"), skipped ? "Übersprungen." : "Gerade läuft nichts.", skipped ? "ok" : "");
      } catch (e) {
        note($("skip-note"), "Überspringen fehlgeschlagen (" + e.message + ").", "err");
      }
      setTimeout(() => Status.refresh(), 800);
    });

    $("next-list").addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-remove]");
      if (!btn) return;
      const id = btn.dataset.remove;
      const count = Number(btn.dataset.count);
      btn.disabled = true;
      try {
        await api(`/api/queue/${encodeURIComponent(id)}`, { method: "DELETE" });
        btn.closest("li.block")?.remove();
        showUndo(id, count
          ? `Block mit ${count} Titeln entfernt – die Musikredaktion plant nach.`
          : "Beitrag entfernt.");
        refreshQueue();
      } catch (err) {
        btn.disabled = false;
        showUndo(null, "Entfernen fehlgeschlagen (" + err.message + ").");
        $("undo-btn").disabled = true;
      }
    });

    $("undo-btn").addEventListener("click", async () => {
      if (!undo.id) return;
      const id = undo.id;
      clearInterval(undo.timer);
      $("undo-btn").disabled = true;
      try {
        await api(`/api/queue/${encodeURIComponent(id)}/restore`, { method: "POST" });
        hideUndo();
        refreshQueue();
        Status.refresh();
      } catch (e) {
        $("undo-text").textContent = "Rückgängig nicht mehr möglich (" + e.message + ").";
        setTimeout(hideUndo, 4000);
      }
    });
  }

  // ---------- boot ----------

  async function loadConfig() {
    try {
      config = await api("/api/config");
    } catch { /* keep the last one */ }
    renderDial();
  }

  Pages.broadcast = () => {
    initActions();
    Status.on((status) => {
      renderOnAir(status);
      renderDesk(status);
      renderLog(status);
      renderDial();
    });
    loadConfig();
    setInterval(loadConfig, 5 * 60000);
    setInterval(() => { if (document.visibilityState !== "hidden") refreshQueue(); }, 15000);
    setInterval(progressTick, 500);
  };
})();
