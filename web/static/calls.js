// "Rufen" (inbox.html): send a wish as text or voice, see what's still open.
// Phase 1 keeps the old wish inbox (/api/inbox); the calls with the dispatch desk come in Phase 2.

(() => {
  // Inbox filenames are UTC stamps like 20260923T071915043891.txt
  function stampToDate(filename) {
    const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/.exec(filename || "");
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
  }

  async function refreshQueue() {
    let items;
    try {
      ({ items } = await api("/api/inbox"));
    } catch {
      return;
    }
    $("queue-meta").textContent = items.length ? `${items.length} offen · kommt ins nächste Programm` : "";
    syncList($("inbox-list"), items, {
      key: (item) => item.filename,
      sig: (item) => item.text,
      empty: `<p class="queue-empty">Gerade ist nichts offen – der nächste Wunsch könnte deiner sein.</p>`,
      render: (item) => {
        const when = stampToDate(item.filename);
        return `<span class="text">${esc(item.text)}</span><span class="when">${when ? hhmm(when) : ""}</span>`;
      },
    });
  }

  function initText() {
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
        note(noteEl, "Angekommen! Kommt ins nächste Programm.", "ok");
        refreshQueue();
      } catch (e) {
        note(noteEl, "Senden fehlgeschlagen (" + e.message + ").", "err");
      }
    });
  }

  function initRecorder() {
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
      } catch {
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
  }

  Pages.calls = () => {
    initText();
    initRecorder();
    refreshQueue();
    setInterval(() => { if (document.visibilityState !== "hidden") refreshQueue(); }, 15000);
  };
})();
