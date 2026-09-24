// Shared by every page: helpers, header (lamp, clock, notices), status polling, page boot.
// Each page script registers its init function in `Pages` under the name used in
// <body data-page="…">; common.js calls it on DOMContentLoaded.

const $ = (id) => document.getElementById(id);
const Pages = {};

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
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("application/json") ? res.json() : res.text();
}

const jsonBody = (data) => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data),
});

function note(el, message, kind) {
  if (!el) return;
  el.textContent = message;
  el.className = "note" + (kind ? ` ${kind}` : "");
  clearTimeout(el._timer);
  if (kind === "ok") el._timer = setTimeout(() => { el.textContent = ""; }, 3500);
}

// ---------- time & text formatting ----------

const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
const pad = (n) => String(n).padStart(2, "0");
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const toDate = (iso) => (iso ? new Date(iso) : null);

function fmtDateTime(d) {
  return `${WEEKDAYS[d.getDay()]} ${pad(d.getDate())}.${pad(d.getMonth() + 1)}. · ${hhmm(d)}`;
}

// "07:43", "morgen 06:30" or "Mo 06:30" - for times up to a few days ahead/behind.
function fmtWhen(iso) {
  const d = toDate(iso);
  if (!d) return "";
  const today = new Date();
  const dayDiff = Math.round(
    (new Date(d.getFullYear(), d.getMonth(), d.getDate()) -
      new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000,
  );
  if (dayDiff === 0) return hhmm(d);
  if (dayDiff === 1) return `morgen ${hhmm(d)}`;
  if (dayDiff === -1) return `gestern ${hhmm(d)}`;
  return `${WEEKDAYS[d.getDay()]} ${hhmm(d)}`;
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return "–:––";
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

const fmtMinutes = (seconds) => `${Math.max(0, Math.round((seconds || 0) / 60))} min`;

// The model tends to answer in light Markdown; the UI shows plain text.
function plain(text) {
  return String(text ?? "")
    .replace(/\*\*|__/g, "")
    .replace(/(^|[\s(])[*_]([^*_\n]+)[*_]/g, "$1$2")
    .replace(/^\s*[-*]\s+/gm, "– ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ---------- chips ----------

const LANE_LABELS = {
  urgent: "Jetzt",
  reply: "Antwort",
  news: "Nachrichten",
  program: "Programm",
  filler: "Füllprogramm",
};

const laneChip = (lane) =>
  `<span class="chip" data-lane="${esc(lane)}">${esc(LANE_LABELS[lane] || lane)}</span>`;

const segChip = (type) =>
  `<span class="chip seg">${type === "jingle" ? "Ansage" : "Musik"}</span>`;

// Estimated length of a segment, same fallbacks as app/program/queue.py.
const segSeconds = (seg) => seg.duration_seconds || (seg.type === "track" ? 210 : 20);

// ---------- keyed list updates ----------

// Reconciles `container`'s children with `items` by key instead of rebuilding innerHTML, so focus,
// open <details> and scroll position survive polling. An element is re-rendered only when its
// signature changes; `update(el, item)` runs every time for cheap volatile bits (times).
function syncList(container, items, { key, sig = (item) => JSON.stringify(item), render, update, tag = "li", className = "", empty }) {
  const existing = new Map();
  for (const el of [...container.children]) existing.set(el.dataset.key, el);

  const rows = items.length ? items : empty ? [{ __empty: true }] : [];
  const wanted = new Set();
  rows.forEach((item, index) => {
    const k = item.__empty ? "__empty" : String(key(item));
    const s = item.__empty ? `__empty:${empty}` : sig(item);
    wanted.add(k);
    let el = existing.get(k);
    if (!el) {
      // A <details> placeholder would render a disclosure triangle - use a plain block instead.
      el = document.createElement(item.__empty && tag === "details" ? "div" : tag);
      el.dataset.key = k;
    }
    if (el.dataset.sig !== s) {
      // Re-rendering replaces the children: keep the focus on the control with the same
      // data-fkey (e.g. an "Rückgängig" button), if it still exists afterwards.
      const focused = el.contains(document.activeElement) ? document.activeElement.dataset.fkey : null;
      el.className = item.__empty ? "empty" : className;
      el.innerHTML = item.__empty ? empty : render(item);
      el.dataset.sig = s;
      if (focused) el.querySelector(`[data-fkey="${CSS.escape(focused)}"]`)?.focus();
    }
    if (!item.__empty && update) update(el, item);
    if (container.children[index] !== el) container.insertBefore(el, container.children[index] || null);
  });
  for (const [k, el] of existing) if (!wanted.has(k)) el.remove();
}

// ---------- desks (short status, shared by Sendung and Redaktion) ----------

const DESK_NAMES = { music: "Musik", dispatch: "Leitstelle" };

const lineHtml = (lines) =>
  lines.map((l) => `<p class="desk-line${l.kind === "err" ? " err" : ""}">${esc(l.text)}</p>`).join("");

// {state, label, lines[]} for a desk from /api/status (short) or /api/desks (full).
function deskSummary(desk, name = desk?.name) {
  if (!desk) return { state: "off", label: "unbekannt", lines: [] };
  if (name === "dispatch") return dispatchSummary(desk);
  const lines = [];
  let state = desk.state || "idle";
  let label = { idle: "bereit", running: "plant…", error: "Fehler" }[state] || state;
  if (desk.enabled === false) {
    state = "off";
    label = "aus";
  }
  const fill = desk.fill;
  if (fill && state !== "off") {
    lines.push({
      text: `Musik reicht noch ${fmtMinutes(fill.remaining_seconds)} · plant nach bei ${fill.threshold_minutes} min`,
    });
  } else if (state === "off") {
    lines.push({ text: "Aus – es wird kein neues Programm geplant, nur Füllprogramm." });
  }
  if (desk.last_error && desk.consecutive_failures > 0) {
    const retry = desk.backoff_until ? ` · nächster Versuch ${fmtWhen(desk.backoff_until)}` : "";
    lines.push({ text: `Fehler: ${desk.last_error}${retry}`, kind: "err" });
  } else if (desk.last_success_at) {
    lines.push({ text: `zuletzt geplant ${fmtWhen(desk.last_success_at)}` });
  }
  return { state, label, lines };
}

// The dispatch desk ("Leitstelle"): "wartet auf Zwischenrufe · zuletzt 07:14 · 0 offen".
function dispatchSummary(desk) {
  const lines = [];
  let state = desk.state || "idle";
  let label = { idle: "wartet", running: "sortiert…", error: "Fehler" }[state] || state;
  const open = desk.open_calls || 0;
  if (desk.enabled === false) {
    state = "off";
    label = "aus";
    lines.push({ text: `Aus – Zwischenrufe bleiben liegen, bis die Leitstelle wieder an ist${open ? ` · ${open} offen` : ""}.` });
  } else {
    const parts = [state === "running" ? "sortiert Zwischenrufe ein" : "wartet auf Zwischenrufe"];
    if (desk.last_run_at) parts.push(`zuletzt ${fmtWhen(desk.last_run_at)}`);
    parts.push(`${open} offen`);
    lines.push({ text: parts.join(" · ") });
  }
  if (desk.queued_calls) {
    lines.push({ text: `${desk.queued_calls} ${desk.queued_calls === 1 ? "Antwort" : "Antworten"} im Programm eingeplant` });
  }
  if (desk.last_error && desk.consecutive_failures > 0) {
    const retry = desk.backoff_until ? ` · nächster Versuch ${fmtWhen(desk.backoff_until)}` : "";
    lines.push({ text: `Fehler: ${desk.last_error}${retry}`, kind: "err" });
  }
  return { state, label, lines };
}

// ---------- local storage (per device; may be unavailable) ----------

const Store = {
  get(key, fallback = null) {
    try {
      const raw = localStorage.getItem(`ohfm.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`ohfm.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

// ---------- header: lamp, clock, notices ----------

function tickClock() {
  const el = $("clock");
  if (el) el.textContent = fmtDateTime(new Date());
}

function renderLamp(onAir) {
  const lamp = $("lamp");
  if (!lamp) return;
  const state = onAir ? "on" : "off";
  if (lamp.dataset.state === state) return; // don't re-announce an unchanged status
  lamp.dataset.state = state;
  $("lamp-label").textContent = onAir ? "On Air" : "Sendepause";
}

const dismissedNotices = new Set();

function renderNotices(notices) {
  const box = $("notices");
  if (!box) return;
  const visible = (notices || []).filter((n) => !dismissedNotices.has(n.id));
  syncList(box, visible, {
    tag: "div",
    className: "notice",
    key: (n) => n.id,
    sig: (n) => `${n.text}|${n.dismissible}`,
    render: (n) => `
      <span class="text">${esc(n.text)}</span>
      ${n.dismissible === false ? "" : `<button class="btn ghost small" type="button" data-dismiss="${esc(n.id)}">Ausblenden</button>`}`,
    update: (el, n) => { el.dataset.kind = n.id === "player-breaker" ? "crit" : "info"; },
  });
}

function initNotices() {
  const box = $("notices");
  if (!box) return;
  box.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-dismiss]");
    if (!btn) return;
    const id = btn.dataset.dismiss;
    dismissedNotices.add(id);
    btn.closest(".notice").remove();
    try {
      await api(`/api/notices/${encodeURIComponent(id)}/dismiss`, { method: "POST" });
    } catch { /* already gone on the server - stays hidden here */ }
  });
}

// ---------- status polling ----------

const Status = {
  data: null,
  receivedAt: 0, // Date.now() when `data` arrived, for client-side progress
  interval: 15000,
  listeners: [],
  timer: null,

  on(fn) {
    this.listeners.push(fn);
    if (this.data) fn(this.data);
  },

  async refresh() {
    let data;
    try {
      data = await api("/api/status");
    } catch {
      return null;
    }
    this.data = data;
    this.receivedAt = Date.now();
    renderLamp(data.on_air);
    renderNotices(data.notices);
    for (const fn of this.listeners) {
      try { fn(data); } catch (e) { console.error(e); }
    }
    return data;
  },

  start() {
    this.refresh();
    this.timer = setInterval(() => {
      if (document.visibilityState !== "hidden") this.refresh();
    }, this.interval);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") this.refresh();
    });
  },
};

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 15000);
  initNotices();
  const page = document.body.dataset.page;
  if (page === "broadcast" || page === "calls") Status.interval = 5000;
  try {
    Pages[page]?.();
  } finally {
    Status.start();
  }
});
