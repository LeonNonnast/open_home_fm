// "Rufen" (calls.html): send a call ("Zwischenruf") as text or voice, follow what the dispatch
// desk ("Leitstelle") made of it, undo single actions. Phone-first.
//
// Per device (localStorage): the caller's name ("Wer ruft?"), the ids of calls sent from here
// (own calls are shown on the right) and an unconfirmed voice draft.

(() => {
  const QUICK_NAMES = ["Mama", "Papa", "Oma", "Opa"];
  const FAST_POLL = 3000;
  const SLOW_POLL = 15000;
  const SLOW_AFTER_SECONDS = 60; // "dauert länger als sonst"
  const MAX_OWN = 300;
  const OPEN = new Set(["transcribing", "awaiting_confirmation", "new", "processing", "retrying", "queued"]);
  const DRAFT = new Set(["transcribing", "awaiting_confirmation"]);
  const DOWNGRADED = /^Unterbrechen/;

  const calls = new Map();
  const feedback = new Map(); // call id -> {text, kind} shown under the call for a moment
  let own = new Set(Store.get("myCalls", []));
  let author = Store.get("author", "") || "";
  let draft = Store.get("draft", null); // {id}
  let serverOffset = 0; // server clock - local clock (ms)
  let lastServerTime = null;
  let listMeta = { dispatch: null, interrupt_available_at: null };
  let pollTimer = null;
  let loaded = false;
  let playing = { id: null, audio: null };

  const serverNow = () => Date.now() + serverOffset;

  // ---------- helpers ----------

  function rememberOwn(id) {
    own.add(id);
    const ids = [...own].slice(-MAX_OWN);
    own = new Set(ids);
    Store.set("myCalls", ids);
  }

  function merge(view) {
    if (view && view.id) calls.set(view.id, view);
  }

  function minutesUntil(iso) {
    const t = toDate(iso);
    return t ? Math.max(0, Math.round((t.getTime() - serverNow()) / 60000)) : null;
  }

  const etaText = (iso) => {
    const m = minutesUntil(iso);
    return m === null ? "" : m === 0 ? "gleich" : `ca. ${m} min`;
  };

  // ---------- conversation ----------

  // "→ …" text of an action, without its (volatile) state.
  function actionText(a) {
    const summary = plain(a.summary || "");
    const colon = summary.indexOf(": ");
    const rest = colon >= 0 ? summary.slice(colon + 2) : summary;
    const noteHead = (a.note || "").split(";")[0].trim();
    const downgraded = DOWNGRADED.test(noteHead);
    switch (a.type) {
      case "play_now":
        return `jetzt: ${rest} (${noteHead || "Unterbrechen kommt später"} – läuft als Nächstes)`;
      case "reply":
        return downgraded ? `Antwort jetzt (${noteHead} – läuft als Nächstes)` : "Antwort als Nächstes";
      case "breaking":
        return `Eilmeldung jetzt (${noteHead || "Unterbrechen kommt später"} – läuft als Nächstes), auch für die Nachrichten vorgemerkt`;
      default:
        return summary;
    }
  }

  // Extra line under an action: the note, minus what actionText already says.
  function actionNote(a) {
    const parts = (a.note || "").split(";").map((p) => p.trim()).filter(Boolean);
    const shown = ["play_now", "reply", "breaking"].includes(a.type) && DOWNGRADED.test(parts[0] || "");
    return (shown ? parts.slice(1) : parts).join(" · ");
  }

  const ACTION_STATE = {
    done: "✓",
    failed: "fehlgeschlagen",
    playing: "läuft jetzt",
    aired: "gesendet",
    expired: "verfallen",
    removed: "entfernt",
    used: "eingeplant",
  };

  function actionState(a) {
    if (a.status === "queued") return a.eta ? `(${etaText(a.eta)})` : "";
    if (a.status === "held") return a.eta ? `ab ${fmtWhen(a.eta)}` : "zum Sendebeginn";
    return ACTION_STATE[a.status] || "";
  }

  function canRetry(c) {
    return c.status === "expired" || (c.status === "failed" && !c.dispatched && !!c.text);
  }

  // The call's status line (German, volatile parts computed here so they stay current).
  function statusLine(c) {
    const dispatch = listMeta.dispatch || Status.data?.desks?.dispatch;
    switch (c.status) {
      case "new":
      case "processing": {
        if (dispatch && dispatch.enabled === false) {
          return { text: "Die Leitstelle ist ausgeschaltet – dein Zwischenruf wartet.", kind: "warn" };
        }
        const since = toDate(c.submitted_at || c.created_at);
        const slow = since && (serverNow() - since.getTime()) / 1000 > SLOW_AFTER_SECONDS;
        return slow ? { text: "Leitstelle sortiert noch ein – dauert länger als sonst", kind: "warn" } : { text: "Leitstelle sortiert ein…", kind: "busy" };
      }
      case "retrying":
        return { text: c.status_text, kind: "warn" };
      case "queued": {
        const states = new Set((c.actions || []).filter((a) => a.queue_item_id).map((a) => a.status));
        if (states.has("playing")) return { text: "läuft jetzt", kind: "live" };
        if (states.has("held")) return { text: c.status_text, kind: "held" };
        return { text: "eingeplant", kind: "busy" };
      }
      case "aired":
        return { text: c.status_text, kind: "ok" };
      case "routed":
        return { text: "erledigt", kind: "ok" };
      case "expired":
        return { text: "verfallen – die Leitstelle war zu lange nicht erreichbar", kind: "err" };
      case "failed":
        return { text: c.status_text, kind: "err" };
      case "removed":
        return { text: c.dispatched ? "vom Sender entfernt" : "zurückgezogen", kind: "muted" };
      default:
        return { text: c.status_text || c.status, kind: "" };
    }
  }

  function renderCall(c) {
    const mine = own.has(c.id);
    const who = c.author || (mine ? "Du" : "Jemand");
    const actions = (c.actions || []).map((a, i) => {
      const extra = actionNote(a);
      return `
        <li class="action" data-status="${esc(a.status)}">
          <span class="arrow" aria-hidden="true">→</span>
          <span class="a-body">
            <span class="a-text">${esc(actionText(a))}</span>
            <span class="a-state" data-astate="${i}"></span>
            ${extra ? `<span class="a-note">${esc(extra)}</span>` : ""}
          </span>
          ${a.undo_available ? `<button class="btn ghost small" type="button" data-undo="${i}" data-fkey="undo-${i}">Rückgängig</button>` : ""}
        </li>`;
    }).join("");
    const reply = c.reply_text ? `
      <div class="reply">
        <span class="reply-label">Antwort der Leitstelle</span>
        <p>${esc(plain(c.reply_text))}</p>
        ${c.reply_audio_url ? `<button class="btn ghost small" type="button" data-listen data-fkey="listen">Anhören</button>` : ""}
      </div>` : "";
    const final = !actions && !reply && c.final_message && ["routed", "aired"].includes(c.status)
      ? `<p class="call-final">${esc(plain(c.final_message))}</p>` : "";
    const fb = feedback.get(c.id);
    return `
      <div class="bubble">
        <div class="call-meta">
          <span class="who">${esc(who)}</span>
          <time datetime="${esc(c.created_at)}">${esc(fmtWhen(c.created_at))}</time>
          ${c.source === "voice" ? `<span>gesprochen</span>` : ""}
        </div>
        <p class="call-text">${esc(c.text)}</p>
      </div>
      <div class="result">
        <p class="call-status" data-status></p>
        ${actions ? `<ul class="actions" aria-label="Ergebnis der Leitstelle">${actions}</ul>` : ""}
        ${reply}${final}
        ${canRetry(c) ? `<button class="btn small" type="button" data-retry data-fkey="retry">Nochmal senden</button>` : ""}
        ${fb ? `<p class="note ${esc(fb.kind || "")}">${esc(fb.text)}</p>` : ""}
      </div>`;
  }

  function callSig(c) {
    const fb = feedback.get(c.id);
    return JSON.stringify([
      c.status, c.status_text, c.text, c.author, c.reply_text, c.reply_audio_url, c.final_message, c.dispatched,
      own.has(c.id), fb?.text,
      (c.actions || []).map((a) => [a.summary, a.status, a.note, a.undo_available]),
    ]);
  }

  function updateCall(el, c) {
    const line = statusLine(c);
    const status = el.querySelector("[data-status]");
    if (status.textContent !== line.text) status.textContent = line.text;
    status.dataset.kind = line.kind;
    (c.actions || []).forEach((a, i) => {
      const s = el.querySelector(`[data-astate="${i}"]`);
      const text = actionState(a);
      if (s && s.textContent !== text) s.textContent = text;
    });
    const listen = el.querySelector("[data-listen]");
    if (listen) {
      const label = playing.id === c.id ? "Stopp" : "Anhören";
      if (listen.textContent !== label) listen.textContent = label;
    }
  }

  function renderLog() {
    const items = [...calls.values()]
      .filter((c) => c.text && !DRAFT.has(c.status))
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
      .slice(0, 60);
    syncList($("call-log"), items, {
      key: (c) => c.id,
      sig: callSig,
      render: renderCall,
      update: updateCall,
      className: "",
      empty: loaded ? `<p class="log-empty">Noch keine Zwischenrufe. Der erste könnte deiner sein – Licht, Musikwunsch, Gruß oder Frage.</p>` : "",
    });
    // className depends on the item - set it after syncList (it resets className on re-render).
    for (const li of $("call-log").children) {
      const c = calls.get(li.dataset.key);
      if (!c) continue;
      const cls = `call ${own.has(c.id) ? "own" : "other"}`;
      if (li.className !== cls) li.className = cls;
      li.dataset.state = c.status;
      li.tabIndex = -1;
    }
    const openCount = items.filter((c) => OPEN.has(c.status) && c.status !== "queued").length;
    $("convo-meta").textContent = items.length ? `${items.length} ${items.length === 1 ? "Zwischenruf" : "Zwischenrufe"}${openCount ? ` · ${openCount} offen` : ""}` : "";
  }

  // ---------- polling ----------

  const hasOpen = () => [...calls.values()].some((c) => OPEN.has(c.status)) || !!draft;

  function schedule(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms ?? (hasOpen() ? FAST_POLL : SLOW_POLL));
  }

  async function load() {
    // A small overlap: a call changed while the previous answer was built is fetched again.
    const since = lastServerTime ? new Date(new Date(lastServerTime).getTime() - 3000).toISOString() : null;
    const data = await api(`/api/calls?limit=60${since ? `&since=${encodeURIComponent(since)}` : ""}`);
    serverOffset = new Date(data.server_time).getTime() - Date.now();
    lastServerTime = data.server_time;
    listMeta = { dispatch: data.dispatch, interrupt_available_at: data.interrupt_available_at };
    for (const c of data.calls) merge(c);
    loaded = true;
    renderLog();
    renderHint();
  }

  async function poll() {
    if (document.visibilityState !== "hidden") {
      try { await load(); } catch { /* next poll */ }
    }
    schedule();
  }

  // ---------- sending ----------

  function setBusy(busy) {
    $("send-call").disabled = busy;
  }

  async function sendText() {
    const area = $("call-text");
    const text = area.value.trim();
    const noteEl = $("composer-note");
    if (!text) {
      note(noteEl, draft ? "Der Text ist leer – eintippen oder verwerfen." : "Schreib erst deinen Zwischenruf ins Feld – oder sprich ihn ein.", "err");
      area.focus();
      return;
    }
    setBusy(true);
    try {
      let view;
      if (draft) {
        view = await api(`/api/calls/${encodeURIComponent(draft.id)}`, { method: "PATCH", ...jsonBody({ text }) });
        clearDraft();
      } else {
        view = await api("/api/calls/text", { method: "POST", ...jsonBody({ text, author: author || null }) });
      }
      rememberOwn(view.id);
      merge(view);
      area.value = "";
      note(noteEl, "Angekommen – die Leitstelle sortiert ein.", "ok");
      renderLog();
      schedule(1500);
    } catch (e) {
      note(noteEl, "Senden fehlgeschlagen (" + e.message + ").", "err");
    } finally {
      setBusy(false);
    }
  }

  // ---------- voice with check step ----------

  function setDraftUi(mode, message, kind) {
    const area = $("call-text");
    const banner = $("draft-banner");
    banner.hidden = mode === "none";
    banner.dataset.kind = kind || "";
    $("draft-text").textContent = message || "";
    $("discard-draft").hidden = mode === "none";
    area.disabled = mode === "transcribing";
    area.placeholder = mode === "transcribing" ? "Wird transkribiert…" : area.dataset.placeholder;
    $("send-call").textContent = mode === "confirm" ? "So senden" : "Senden";
    $("send-call").disabled = mode === "transcribing";
    $("mic-btn").disabled = mode === "transcribing";
  }

  function clearDraft() {
    draft = null;
    Store.set("draft", null);
    setDraftUi("none");
  }

  async function followDraft() {
    if (!draft) return;
    let view;
    try {
      view = await api(`/api/calls/${encodeURIComponent(draft.id)}`);
    } catch (e) {
      if (String(e.message).startsWith("404")) clearDraft();
      else setTimeout(followDraft, 2000);
      return;
    }
    merge(view);
    if (view.status === "transcribing") {
      setDraftUi("transcribing", "Wird transkribiert…", "busy");
      setTimeout(followDraft, 1200);
    } else if (view.status === "awaiting_confirmation") {
      const area = $("call-text");
      if (!area.value.trim()) area.value = view.text || "";
      setDraftUi("confirm", view.text
        ? "So haben wir dich verstanden – bei Bedarf korrigieren, dann senden."
        : view.status_text, view.text ? "" : "warn");
      area.focus();
    } else if (view.status === "failed" && !view.text) {
      clearDraft();
      setDraftUi("failed", `${view.error || "Transkription fehlgeschlagen"} – tipp deinen Zwischenruf einfach ein.`, "err");
      $("discard-draft").hidden = true;
    } else {
      clearDraft(); // confirmed or withdrawn elsewhere
    }
    renderLog();
  }

  function pickMime() {
    const types = [["audio/webm;codecs=opus", "webm"], ["audio/webm", "webm"], ["audio/mp4", "m4a"], ["audio/ogg", "ogg"]];
    for (const [type, ext] of types) {
      if (window.MediaRecorder?.isTypeSupported?.(type)) return { type, ext };
    }
    return { type: "", ext: "webm" };
  }

  function initRecorder() {
    const mic = $("mic-btn");
    const timer = $("rec-timer");
    let recorder = null;
    let chunks = [];
    let ticker = null;

    function idle() {
      mic.classList.remove("recording");
      mic.setAttribute("aria-label", "Einsprechen");
      clearInterval(ticker);
      timer.textContent = "";
    }

    mic.addEventListener("click", async () => {
      if (recorder && recorder.state === "recording") {
        recorder.stop();
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        setDraftUi("failed", "Einsprechen geht hier nicht (auf dem Handy nur über HTTPS) – tipp deinen Zwischenruf einfach ein.", "err");
        $("discard-draft").hidden = true;
        return;
      }
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        setDraftUi("failed", "Kein Mikrofonzugriff – im Browser erlauben oder einfach eintippen.", "err");
        $("discard-draft").hidden = true;
        return;
      }
      const { type, ext } = pickMime();
      chunks = [];
      recorder = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
      recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        idle();
        if (draft) { // a new recording replaces an unconfirmed one
          api(`/api/calls/${encodeURIComponent(draft.id)}`, { method: "DELETE" }).catch(() => {});
          clearDraft();
        }
        $("call-text").value = "";
        setDraftUi("transcribing", "Wird hochgeladen…", "busy");
        const form = new FormData();
        form.append("file", new Blob(chunks, { type: type || "audio/webm" }), `zwischenruf.${ext}`);
        if (author) form.append("author", author);
        try {
          const view = await api("/api/calls/voice", { method: "POST", body: form });
          rememberOwn(view.id);
          merge(view);
          draft = { id: view.id };
          Store.set("draft", draft);
          followDraft();
          schedule();
        } catch (e) {
          setDraftUi("failed", "Hochladen fehlgeschlagen (" + e.message + ") – nochmal versuchen oder eintippen.", "err");
          $("discard-draft").hidden = true;
        }
      };
      recorder.start();
      const started = Date.now();
      mic.classList.add("recording");
      mic.setAttribute("aria-label", "Aufnahme beenden");
      timer.textContent = "Aufnahme 0:00 – nochmal tippen beendet";
      ticker = setInterval(() => {
        timer.textContent = `Aufnahme ${fmtDuration((Date.now() - started) / 1000)} – nochmal tippen beendet`;
      }, 250);
    });

    $("discard-draft").addEventListener("click", async () => {
      if (!draft) { setDraftUi("none"); return; }
      const id = draft.id;
      clearDraft();
      $("call-text").value = "";
      try {
        merge(await api(`/api/calls/${encodeURIComponent(id)}`, { method: "DELETE" }));
      } catch { /* gone anyway */ }
      note($("composer-note"), "Aufnahme verworfen.", "ok");
      renderLog();
    });
  }

  // ---------- "Wer ruft?" ----------

  function knownNames() {
    const recent = [...calls.values()]
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
      .map((c) => c.author)
      .filter(Boolean);
    return [...new Set([...(author ? [author] : []), ...QUICK_NAMES, ...recent])].slice(0, 8);
  }

  function renderWho() {
    $("who-name").textContent = author || "bitte wählen";
    $("who-chip").dataset.empty = author ? "" : "1";
    $("who-picks").innerHTML = knownNames().map((n) => `
      <button class="pick" type="button" data-name="${esc(n)}" aria-pressed="${n === author}">${esc(n)}</button>`).join("");
  }

  function toggleWho(open) {
    const panel = $("who-panel");
    const show = open ?? panel.hidden;
    panel.hidden = !show;
    $("who-chip").setAttribute("aria-expanded", String(show));
    if (show) renderWho();
  }

  function setAuthor(name) {
    author = String(name || "").trim().slice(0, 40);
    Store.set("author", author);
    renderWho();
    toggleWho(false);
    $("who-chip").focus();
  }

  function initWho() {
    renderWho();
    $("who-chip").addEventListener("click", () => toggleWho());
    $("who-picks").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-name]");
      if (btn) setAuthor(btn.dataset.name);
    });
    const save = () => {
      const value = $("who-input").value.trim();
      if (!value) { $("who-input").focus(); return; }
      $("who-input").value = "";
      setAuthor(value);
    };
    $("who-save").addEventListener("click", save);
    $("who-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); save(); }
      if (e.key === "Escape") toggleWho(false);
    });
    if (!author) toggleWho(true);
  }

  // ---------- log actions: undo, retry, listen ----------

  function flash(id, text, kind) {
    feedback.set(id, { text, kind });
    renderLog();
    setTimeout(() => {
      if (feedback.get(id)?.text === text) { feedback.delete(id); renderLog(); }
    }, 6000);
  }

  function listen(c) {
    if (playing.audio) {
      playing.audio.pause();
      const wasSame = playing.id === c.id;
      playing = { id: null, audio: null };
      renderLog();
      if (wasSame) return;
    }
    const audio = new Audio(c.reply_audio_url);
    playing = { id: c.id, audio };
    const stop = () => { if (playing.audio === audio) { playing = { id: null, audio: null }; renderLog(); } };
    audio.addEventListener("ended", stop);
    audio.addEventListener("error", () => { stop(); flash(c.id, "Antwort konnte nicht abgespielt werden.", "err"); });
    audio.play().catch(() => stop());
    renderLog();
  }

  function initLog() {
    $("call-log").addEventListener("click", async (e) => {
      const li = e.target.closest("li[data-key]");
      const c = li && calls.get(li.dataset.key);
      if (!c) return;
      const undoBtn = e.target.closest("[data-undo]");
      if (undoBtn) {
        undoBtn.disabled = true;
        try {
          merge(await api(`/api/calls/${encodeURIComponent(c.id)}/actions/${undoBtn.dataset.undo}`, { method: "DELETE" }));
          flash(c.id, "Rückgängig gemacht.", "ok");
          // The button is gone now - keep the keyboard focus on this call instead of the page.
          if (!li.contains(document.activeElement)) li.focus();
        } catch (err) {
          undoBtn.disabled = false;
          flash(c.id, "Rückgängig nicht möglich (" + err.message.replace(/^\d+: /, "") + ").", "err");
        }
        return;
      }
      if (e.target.closest("[data-retry]")) {
        try {
          merge(await api(`/api/calls/${encodeURIComponent(c.id)}/retry`, { method: "POST" }));
          renderLog();
          schedule(1500);
        } catch (err) {
          flash(c.id, "Nochmal senden fehlgeschlagen (" + err.message.replace(/^\d+: /, "") + ").", "err");
        }
        return;
      }
      if (e.target.closest("[data-listen]")) listen(c);
    });
  }

  // ---------- "läuft gerade" strip, hint under the input ----------

  function renderNowbar(status) {
    const np = status.now_playing;
    const bar = $("nowbar");
    let label = "läuft gerade";
    let title;
    if (np) title = np.type === "jingle" ? `Moderation${np.text ? `: ${plain(np.text)}` : ""}` : np.title;
    else if (!status.on_air) {
      label = "";
      title = status.next_on_air_at ? `Sender ruht bis ${fmtWhen(status.next_on_air_at)}` : "Sender ruht";
    } else title = "Gleich geht’s weiter";
    bar.dataset.lane = np?.lane || "program";
    bar.dataset.state = np ? "on" : "off";
    const html = `${np && np.lane !== "program" ? laneChip(np.lane) : ""}${label ? `<span class="nowbar-label">${esc(label)}</span>` : ""}<span class="nowbar-title">${esc(title)}</span>`;
    if (bar.dataset.html !== html) {
      bar.innerHTML = html;
      bar.dataset.html = html;
    }
  }

  function renderHint() {
    const status = Status.data;
    const dispatch = listMeta.dispatch || status?.desks?.dispatch;
    const interrupt = listMeta.interrupt_available_at || status?.interrupt_available_at;
    const parts = [];
    if (dispatch && dispatch.enabled === false) parts.push("Die Leitstelle ist gerade ausgeschaltet – Zwischenrufe bleiben liegen, bis sie wieder an ist.");
    if (status && !status.on_air) {
      parts.push(`Sender ruht${status.next_on_air_at ? ` bis ${fmtWhen(status.next_on_air_at)}` : ""} – Licht & Co. gehen sofort, alles fürs Programm kommt dann dran.`);
    }
    if (interrupt && new Date(interrupt).getTime() > serverNow()) parts.push(`Unterbrechen erst wieder ab ${fmtWhen(interrupt)} möglich.`);
    const text = parts.join(" ");
    const el = $("composer-hint");
    if (el.textContent !== text) el.textContent = text;
    el.hidden = !text;
  }

  // ---------- boot ----------

  Pages.calls = () => {
    const area = $("call-text");
    area.dataset.placeholder = area.placeholder;
    initWho();
    initRecorder();
    initLog();
    $("send-call").addEventListener("click", sendText);
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendText(); }
    });
    Status.on((status) => {
      renderNowbar(status);
      renderHint();
      if (loaded) renderLog();
    });
    if (draft) followDraft();
    poll();
    // Volatile texts ("ca. 2 min", "dauert länger als sonst") between polls.
    setInterval(() => { if (loaded) renderLog(); }, 10000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") schedule(0);
    });
  };
})();
