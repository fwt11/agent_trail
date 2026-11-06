"use strict";

const SOURCE_LABELS = Object.freeze({
  "kimi-code": "Kimi Code",
  codebuddy: "CodeBuddy",
  "minimax-code": "MiniMax Code",
  zcode: "ZCode",
  "deepseek-harness": "DeepSeek Harness",
  pi: "Pi",
  opencode: "OpenCode",
  codex: "Codex",
  "claude-code": "Claude Code",
});
const t = (key, params) => window.I18N.t(key, params);

function tb(key) {
  const full = `backend.${key}`;
  const text = t(full);
  return text === full ? key : text;
}

const SOURCE_STATUS_KINDS = new Set(["ok", "unavailable", "experimental", "error", "incompatible"]);

function statusLabel(kind) {
  return SOURCE_STATUS_KINDS.has(kind) ? t(`srcstatus.${kind}`) : kind;
}
const state = {
  period: "week",
  source: "",
  workdir: "",
  query: "",
  collapse: false,
  sessions: [],
  statuses: [],
  activeSession: null,
};
let apiKey = "";
let requestSequence = 0;
let searchTimer = null;
let toastTimer = null;

function localDay(value) {
  const date = new Date(value);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function groupWorkSegments(sessions, gapMinutes = 45) {
  const sorted = [...sessions].sort(
    (left, right) => new Date(right.updated_at) - new Date(left.updated_at),
  );
  const groups = [];
  for (const session of sorted) {
    const timestamp = new Date(session.updated_at).getTime();
    const day = localDay(session.updated_at);
    const previous = groups.at(-1);
    const previousSession = previous?.sessions.at(-1);
    const previousTimestamp = previousSession
      ? new Date(previousSession.updated_at).getTime()
      : NaN;
    const gap = previousTimestamp - timestamp;
    if (
      previous &&
      previous.day === day &&
      previous.source === session.source &&
      previous.workdir === session.workdir_normalized &&
      Number.isFinite(timestamp) &&
      gap >= 0 &&
      gap <= gapMinutes * 60_000
    ) {
      previous.sessions.push(session);
    } else {
      groups.push({
        key: `${session.source}:${session.session_id}:${day}`,
        day,
        source: session.source,
        workdir: session.workdir_normalized,
        sessions: [session],
      });
    }
  }
  return groups;
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatTime(value) {
  return new Intl.DateTimeFormat(I18N.locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatFullTime(value) {
  if (!value) return t("common.unknown");
  return new Intl.DateTimeFormat(I18N.locale, {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function dateHeading(day) {
  const today = localDay(new Date());
  const yesterdayDate = new Date();
  yesterdayDate.setDate(yesterdayDate.getDate() - 1);
  const yesterday = localDay(yesterdayDate);
  const date = new Date(`${day}T00:00:00`);
  const formatted = new Intl.DateTimeFormat(I18N.locale, {
    month: "long",
    day: "numeric",
  }).format(date);
  const weekday = new Intl.DateTimeFormat(I18N.locale, { weekday: "long" }).format(date);
  const prefix = day === today ? t("date.today") : day === yesterday ? t("date.yesterday") : formatted;
  return { title: prefix, subtitle: day === today || day === yesterday ? `${formatted} · ${weekday}` : weekday };
}

function sessionTitle(session) {
  return session.title || t("session.unnamed");
}

function appendStats(parent, session) {
  const stats = element("span", "change-stats");
  if (session.additions === null && session.deletions === null && session.files_changed === null) {
    stats.textContent = t("stats.unknown");
  } else {
    if (session.additions !== null) {
      stats.append(element("span", "additions", `+${session.additions} `));
    }
    if (session.deletions !== null) {
      stats.append(element("span", "deletions", `−${session.deletions} `));
    }
    if (session.files_changed !== null) {
      stats.append(document.createTextNode(t("stats.files", { n: session.files_changed })));
    }
  }
  parent.append(stats);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Agent-Trail-Key": apiKey,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    try {
      error.payload = await response.json();
    } catch {
      error.payload = null;
    }
    throw error;
  }
  return response.json();
}

function periodRange(period) {
  if (period === "all") return {};
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  if (period === "yesterday") {
    start.setDate(start.getDate() - 1);
    end.setDate(end.getDate() - 1);
  } else if (period === "week") {
    start.setDate(start.getDate() - 6);
  }
  return { since: start.toISOString(), until: end.toISOString() };
}

async function loadTimeline({ quiet = false } = {}) {
  const sequence = ++requestSequence;
  if (!quiet) {
    document.getElementById("loading").hidden = false;
    document.getElementById("empty").hidden = true;
  }
  const params = new URLSearchParams({ limit: "1000", ...periodRange(state.period) });
  if (state.source) params.set("source", state.source);
  if (state.workdir) params.set("workdir", state.workdir);
  if (state.query) params.set("query", state.query);
  try {
    const result = await api(`/api/sessions?${params}`);
    if (sequence !== requestSequence) return;
    state.sessions = result.sessions;
    document.getElementById("error-banner").hidden = true;
    renderTimeline();
  } catch (error) {
    if (sequence !== requestSequence) return;
    showError(t("error.load_failed"));
  } finally {
    if (sequence === requestSequence) document.getElementById("loading").hidden = true;
  }
}

async function loadFilterOptions() {
  const result = await api("/api/sessions?limit=1000");
  const sourceSelect = document.getElementById("source-filter");
  const projectSelect = document.getElementById("project-filter");
  const currentProject = projectSelect.value;
  const sources = new Set(result.sessions.map((session) => session.source));
  for (const status of state.statuses) sources.add(status.source);
  sourceSelect.replaceChildren(new Option(t("filter.all_tools"), ""));
  for (const source of [...sources].sort()) {
    sourceSelect.add(new Option(sourceLabel(source), source));
  }
  sourceSelect.value = state.source;
  const projects = new Map();
  for (const session of result.sessions) {
    if (!projects.has(session.workdir_normalized)) {
      projects.set(session.workdir_normalized, session.workdir);
    }
  }
  projectSelect.replaceChildren(new Option(t("filter.all_projects"), ""));
  for (const [normalized, workdir] of [...projects].sort((left, right) => left[1].localeCompare(right[1]))) {
    const option = new Option(workdir, normalized);
    option.title = workdir;
    projectSelect.add(option);
  }
  projectSelect.value = currentProject;
}

async function loadStatus() {
  const status = await api("/api/status");
  state.statuses = status.sources;
  document.getElementById("last-scan").textContent = status.last_scanned_at
    ? t("status.scanned_at", { time: formatTime(status.last_scanned_at) })
    : t("status.not_scanned");
  const available = status.sources.filter((source) => source.kind === "ok").length;
  document.getElementById("source-summary").textContent = t("sources.available", { n: available });
  const list = document.getElementById("source-list");
  list.replaceChildren();
  for (const source of status.sources) {
    const row = element("div", "source-status");
    row.append(
      element("span", "", sourceLabel(source.source)),
      element("span", "source-state", statusLabel(source.kind)),
    );
    if (source.error_count) {
      row.append(element("span", "source-error", t("sources.skipped", { n: source.error_count })));
    }
    list.append(row);
  }
}

function makeSourceBadge(source) {
  const badge = element("span", "source-badge", sourceLabel(source));
  badge.dataset.source = source;
  return badge;
}

function makeSessionButton(session, child = false) {
  const button = element("button", "session-main");
  button.type = "button";
  button.setAttribute("aria-label", t("card.aria_view", { source: sourceLabel(session.source), workdir: session.workdir }));
  const meta = element("span", "session-meta");
  if (child) {
    meta.append(element("span", "child-time", formatTime(session.updated_at)));
  } else {
    meta.append(makeSourceBadge(session.source));
  }
  if (session.source_missing) meta.append(element("span", "missing-badge", t("badge.missing")));
  button.append(
    meta,
    element("span", "session-title", sessionTitle(session)),
    element("span", "session-path", session.workdir),
  );
  const footer = element("span", "session-footer");
  appendStats(footer, session);
  if (!child) footer.append(element("span", "open-hint", t("card.open_hint")));
  button.append(footer);
  button.addEventListener("click", () => showDetails(session));
  return button;
}

function makeSegment(group) {
  const latest = group.sessions[0];
  const entry = element("article", "session-entry");
  entry.dataset.source = group.source;
  const time = element("time", "time-rail", formatTime(latest.updated_at));
  time.dateTime = latest.updated_at;
  const card = element("div", "session-card");
  entry.append(time, card);
  if (group.sessions.length === 1) {
    card.append(makeSessionButton(latest));
    return entry;
  }
  const toggle = element("button", "session-main segment-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  const meta = element("span", "session-meta");
  meta.append(makeSourceBadge(group.source), element("span", "segment-badge", t("segment.badge", { n: group.sessions.length })));
  const footer = element("span", "session-footer");
  footer.append(
    element("span", "", `${formatTime(group.sessions.at(-1).updated_at)} — ${formatTime(latest.updated_at)}`),
    element("span", "open-hint", t("segment.expand")),
  );
  toggle.append(
    meta,
    element("span", "session-title", sessionTitle(latest)),
    element("span", "session-path", latest.workdir),
    footer,
  );
  const children = element("div", "segment-children");
  children.hidden = true;
  for (const session of group.sessions) {
    children.append(makeSessionButton(session, true));
  }
  toggle.addEventListener("click", () => {
    children.hidden = !children.hidden;
    toggle.setAttribute("aria-expanded", String(!children.hidden));
  });
  card.append(toggle, children);
  return entry;
}

function renderTimeline() {
  const timeline = document.getElementById("timeline");
  timeline.replaceChildren();
  const groups = state.collapse
    ? groupWorkSegments(state.sessions)
    : state.sessions.map((session) => ({ day: localDay(session.updated_at), sessions: [session], source: session.source }));
  document.getElementById("result-count").textContent = state.sessions.length;
  document.getElementById("empty").hidden = state.sessions.length !== 0;
  const days = new Map();
  for (const group of groups) {
    if (!days.has(group.day)) days.set(group.day, []);
    days.get(group.day).push(group);
  }
  for (const [day, dayGroups] of days) {
    const block = element("section", "date-block");
    const heading = element("header", "date-heading");
    const text = dateHeading(day);
    const time = element("time", "", text.title);
    time.dateTime = day;
    heading.append(time, element("span", "", text.subtitle));
    const items = element("div", "date-items");
    for (const group of dayGroups) items.append(makeSegment(group));
    block.append(heading, items);
    timeline.append(block);
  }
}

function showDetails(session) {
  state.activeSession = session;
  const dialog = document.getElementById("session-dialog");
  document.getElementById("detail-source").replaceChildren(makeSourceBadge(session.source));
  document.getElementById("detail-title").textContent = sessionTitle(session);
  document.getElementById("detail-workdir").textContent = session.workdir;
  document.getElementById("detail-id").textContent = session.session_id;
  document.getElementById("detail-created").textContent = formatFullTime(session.created_at);
  document.getElementById("detail-updated").textContent = formatFullTime(session.updated_at);
  const stats = document.getElementById("detail-stats");
  stats.replaceChildren();
  appendStats(stats, session);
  document.getElementById("detail-locator").textContent = session.source_locator || t("common.unknown");
  const warning = document.getElementById("detail-warning");
  warning.hidden = !session.source_missing;
  warning.textContent = session.source_missing ? t("resume.warning_missing") : "";
  const resume = document.getElementById("resume-button");
  const supported = session.resume_kind === "cli";
  resume.hidden = !supported;
  resume.disabled = !supported || session.source_missing || !session.workdir_exists || session.cli_available === false;
  const reason = document.getElementById("resume-reason");
  if (!supported) {
    reason.textContent = t("resume.reason_view_only");
  } else if (session.source_missing) {
    reason.textContent = t("resume.reason_missing");
  } else if (!session.workdir_exists) {
    reason.textContent = t("resume.reason_noworkdir");
  } else if (session.cli_available === false) {
    reason.textContent = t("resume.reason_no_cli");
  } else {
    reason.textContent = t("resume.reason_ok");
  }
  dialog.showModal();
}

function showError(message) {
  const banner = document.getElementById("error-banner");
  banner.textContent = message;
  banner.hidden = false;
}

function showToast(message) {
  const toast = document.getElementById("toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
}

function bindControls() {
  document.querySelectorAll("[data-period]").forEach((button) => {
    button.addEventListener("click", () => {
      state.period = button.dataset.period;
      document.querySelectorAll("[data-period]").forEach((item) => {
        item.setAttribute("aria-pressed", String(item === button));
      });
      loadTimeline();
    });
  });
  document.getElementById("source-filter").addEventListener("change", (event) => {
    state.source = event.target.value;
    loadTimeline();
  });
  document.getElementById("project-filter").addEventListener("change", (event) => {
    state.workdir = event.target.value;
    loadTimeline();
  });
  document.getElementById("text-filter").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = event.target.value.trim();
      loadTimeline();
    }, 250);
  });
  document.getElementById("segment-toggle").addEventListener("change", (event) => {
    state.collapse = event.target.checked;
    renderTimeline();
  });
  document.getElementById("clear-filters").addEventListener("click", () => {
    state.period = "all";
    state.source = "";
    state.workdir = "";
    state.query = "";
    document.getElementById("source-filter").value = "";
    document.getElementById("project-filter").value = "";
    document.getElementById("text-filter").value = "";
    document.querySelectorAll("[data-period]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.period === "all"));
    });
    loadTimeline();
  });
  document.getElementById("refresh-button").addEventListener("click", async () => {
    const button = document.getElementById("refresh-button");
    button.disabled = true;
    button.classList.add("is-refreshing");
    try {
      await api("/api/refresh", { method: "POST" });
      await Promise.all([loadStatus(), loadFilterOptions(), loadTimeline({ quiet: true })]);
      showToast(t("toast.refreshed"));
    } catch (error) {
      showToast(t("toast.refresh_failed"));
    } finally {
      button.disabled = false;
      button.classList.remove("is-refreshing");
    }
  });
  document.getElementById("close-dialog").addEventListener("click", () => {
    document.getElementById("session-dialog").close();
  });
  document.getElementById("session-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  document.getElementById("resume-button").addEventListener("click", async () => {
    const session = state.activeSession;
    if (!session) return;
    const button = document.getElementById("resume-button");
    button.disabled = true;
    try {
      const payload = await api("/api/resume", {
        method: "POST",
        body: JSON.stringify({ source: session.source, session_id: session.session_id }),
      });
      showToast(payload.message ? tb(payload.message) : t("toast.resume_sent"));
    } catch (error) {
      const raw = error.payload?.message;
      showToast(raw ? tb(raw) : t("toast.resume_failed"));
    } finally {
      button.disabled = session.resume_kind !== "cli" || session.source_missing || !session.workdir_exists || session.cli_available === false;
    }
  });
}

async function initialize() {
  const params = new URLSearchParams(window.location.search);
  const urlKey = params.get("key");
  if (urlKey) sessionStorage.setItem("agent-trail-key", urlKey);
  apiKey = sessionStorage.getItem("agent-trail-key") || "";
  if (!apiKey) {
    document.getElementById("loading").hidden = true;
    document.getElementById("refresh-button").disabled = true;
    showError(t("error.no_key"));
    return;
  }
  if (urlKey) history.replaceState({}, "", window.location.pathname);
  bindControls();
  await loadStatus();
  await Promise.all([loadFilterOptions(), loadTimeline()]);
  window.setInterval(() => {
    if (!document.hidden) {
      Promise.all([loadStatus(), loadTimeline({ quiet: true })]).catch(() => {});
    }
  }, 30_000);
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    initialize().catch(() => {
      document.getElementById("loading").hidden = true;
      showError(t("error.init_failed"));
    });
  });
}
