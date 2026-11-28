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
  workbench: { content_enabled: true, chat_enabled: true },
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
  state.workbench = {
    content_enabled: Boolean(status.content_enabled),
    chat_enabled: Boolean(status.chat_enabled),
  };
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

function showDetails(session, options = {}) {
  state.activeSession = session;
  session.__autoLoadMessages = Boolean(options.autoLoadMessages) || Boolean(session.__autoLoadMessages);
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
  resetDetailMessages(session);
  dialog.showModal();
  if (session.__autoLoadMessages) loadSessionMessages(session);
}

// ---- Markdown 迷你渲染器：先整体转义，再生成受控标签；正文不经过 innerHTML 原样注入 ----

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function markdownInlineHtml(escaped) {
  return escaped
    .split(/\n{2,}/)
    .map((paragraph) => {
      const body = paragraph
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replaceAll("\n", "<br>");
      return `<p>${body}</p>`;
    })
    .join("");
}

function markdownToHtml(raw) {
  const text = String(raw ?? "");
  const parts = text.split("```");
  let html = "";
  for (let index = 0; index < parts.length; index++) {
    const segment = parts[index];
    if (index % 2 === 1) {
      const newline = segment.indexOf("\n");
      const lang = newline >= 0 ? segment.slice(0, newline).trim() : "";
      const code = (newline >= 0 ? segment.slice(newline + 1) : segment).replace(/\n$/, "");
      html += `<pre><code>${escapeHtml(code)}</code></pre>`;
    } else if (segment) {
      html += markdownInlineHtml(escapeHtml(segment));
    }
  }
  return html;
}

function snippetToHtml(snippet) {
  // 后端 snippet() 的 <mark> 包住的是原文；先转义再还原受控的 mark 标签。
  return escapeHtml(snippet).replaceAll("&lt;mark&gt;", "<mark>").replaceAll("&lt;/mark&gt;", "</mark>");
}

function makeMessageNode(role, html, { streaming = false } = {}) {
  const node = element("div", `chat-msg role-${role}${streaming ? " is-streaming" : ""}`);
  const meta = element("div", "chat-msg-meta");
  meta.append(element("span", "chat-msg-role", t(`chat.role_${role}`) || role));
  node.append(meta, element("div", "md-body"));
  node.querySelector(".md-body").innerHTML = html;
  return node;
}

// ---- 会话消息查看器（详情弹窗内） ----

function resetDetailMessages(session) {
  const button = document.getElementById("load-messages");
  const status = document.getElementById("detail-messages-status");
  const container = document.getElementById("detail-messages");
  container.replaceChildren();
  status.hidden = false;
  status.textContent = "";
  if (!state.workbench.content_enabled) {
    button.hidden = true;
    status.textContent = t("messages.status_disabled");
    return;
  }
  button.hidden = false;
  status.hidden = true;
  button.onclick = () => loadSessionMessages(session);
}

async function loadSessionMessages(session) {
  const button = document.getElementById("load-messages");
  const status = document.getElementById("detail-messages-status");
  const container = document.getElementById("detail-messages");
  button.hidden = true;
  status.hidden = false;
  status.textContent = t("messages.loading");
  let result;
  try {
    result = await api(
      `/api/session/messages?source=${encodeURIComponent(session.source)}&session_id=${encodeURIComponent(session.session_id)}`,
    );
  } catch (error) {
    status.textContent = t("messages.load_failed");
    return;
  }
  if (result.status === "ok") {
    status.hidden = true;
    container.replaceChildren();
    for (const message of result.messages) {
      container.append(makeMessageNode(message.role, markdownToHtml(message.text)));
    }
    if (!result.messages.length) {
      status.hidden = false;
      status.textContent = t("messages.empty");
    } else if (result.truncated) {
      status.hidden = false;
      status.textContent = t("messages.truncated");
    }
    container.scrollTop = 0;
    return;
  }
  status.textContent = t(`messages.status_${result.status}`) || result.status;
}

// ---- 全局全文搜索 ----

function bindGlobalSearch() {
  const input = document.getElementById("global-search");
  const panel = document.getElementById("search-results");
  const timer = { id: null };
  const close = () => { panel.hidden = true; };
  input.addEventListener("input", () => {
    clearTimeout(timer.id);
    const query = input.value.trim();
    if (!query) { close(); panel.replaceChildren(); return; }
    timer.id = setTimeout(() => runGlobalSearch(query).catch(() => {}), 250);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".global-search")) close();
  });
}

async function runGlobalSearch(query) {
  const panel = document.getElementById("search-results");
  let result;
  try {
    result = await api(`/api/search?q=${encodeURIComponent(query)}`);
  } catch (error) {
    return;
  }
  panel.replaceChildren();
  if (!result.available) {
    panel.append(element("p", "search-note", t("search.disabled")));
  } else if (!result.hits.length) {
    panel.append(element("p", "search-note", t("search.no_results")));
  } else {
    for (const hit of result.hits.slice(0, 8)) {
      const item = element("button", "search-hit");
      item.type = "button";
      const head = element("span", "search-hit-head");
      head.append(
        makeSourceBadge(hit.source),
        element("span", "search-hit-title", hit.session_title || t("session.unnamed")),
      );
      const snippet = element("span", "search-hit-snippet");
      snippet.innerHTML = snippetToHtml(hit.snippet);
      item.append(head, snippet);
      item.addEventListener("click", () => {
        panel.hidden = true;
        openSessionFromSearch(hit);
      });
      panel.append(item);
    }
    const footer = element("p", "search-note", t("search.result_count", { n: result.hits.length }));
    panel.append(footer);
  }
  panel.hidden = false;
}

async function openSessionFromSearch(hit) {
  const known = state.sessions.find(
    (session) => session.source === hit.source && session.session_id === hit.session_id,
  );
  if (known) {
    showDetails(known, { autoLoadMessages: true });
    return;
  }
  const partial = {
    source: hit.source,
    session_id: hit.session_id,
    title: hit.session_title,
    workdir: hit.workdir || "",
    updated_at: hit.updated_at,
    __autoLoadMessages: true,
  };
  showDetails(partial, { autoLoadMessages: true });
}

// ---- 视图切换 ----

function switchView(view) {
  document.getElementById("view-timeline").hidden = view !== "timeline";
  document.getElementById("view-chat").hidden = view !== "chat";
  for (const tab of document.querySelectorAll(".view-tab")) {
    const active = tab.dataset.view === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  if (view === "chat") refreshChat().catch(() => {});
}

// ---- 对话（应用内聊天） ----

const chatState = {
  agents: [],
  threads: [],
  activeThreadId: null,
  activeTaskId: null,
  streamingThreadId: null,
  streaming: false,
  streamAbort: null,
};

async function refreshChat() {
  await loadChatCapabilities();
  await loadChatThreads();
}

async function loadChatCapabilities() {
  let result = { agents: [] };
  try {
    result = await api("/api/chat/capabilities");
  } catch (error) {
    /* 状态栏已提示 */
  }
  chatState.agents = result.agents || [];
  const select = document.getElementById("chat-agent");
  select.replaceChildren();
  const available = chatState.agents.filter((agent) => agent.available);
  if (!available.length) {
    const option = new Option(t("chat.no_agent"), "");
    option.disabled = true;
    select.append(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const agent of available) {
    select.add(new Option(sourceLabel(agent.source), agent.source));
  }
}

async function loadChatThreads() {
  let result = { threads: [] };
  try {
    result = await api("/api/chat/threads");
  } catch (error) {
    /* 保留现有列表 */
  }
  chatState.threads = result.threads || [];
  const list = document.getElementById("thread-list");
  list.replaceChildren();
  document.getElementById("threads-empty").hidden = chatState.threads.length !== 0;
  for (const thread of chatState.threads) {
    const item = element("button", "thread-item");
    item.type = "button";
    item.dataset.threadId = thread.id;
    if (thread.id === chatState.activeThreadId) item.classList.add("is-active");
    item.append(
      element("span", "thread-title", thread.title || t("chat.title")),
      element("span", "thread-meta", `${sourceLabel(thread.source)} · ${formatTime(thread.updated_at)}`),
    );
    item.addEventListener("click", () => selectThread(thread.id).catch(() => {}));
    list.append(item);
  }
}

async function selectThread(threadId) {
  chatState.activeThreadId = threadId;
  let result;
  try {
    result = await api(`/api/chat/thread?id=${encodeURIComponent(threadId)}`);
  } catch (error) {
    showToast(t("chat.thread_load_failed"));
    return;
  }
  for (const item of document.querySelectorAll(".thread-item")) {
    item.classList.toggle("is-active", item.dataset.threadId === threadId);
  }
  const thread = result.thread;
  document.getElementById("chat-empty").hidden = true;
  document.getElementById("chat-title").textContent = thread.title || t("chat.title");
  document.getElementById("chat-context").textContent =
    `${sourceLabel(thread.source)} · ${thread.workdir}`;
  const container = document.getElementById("chat-messages");
  container.replaceChildren();
  for (const message of result.messages) {
    container.append(makeMessageNode(message.role, markdownToHtml(message.text)));
  }
  container.scrollTop = container.scrollHeight;
  document.getElementById("chat-send").disabled = false;
}

async function createThread() {
  const select = document.getElementById("chat-agent");
  const source = select.value;
  if (!source) {
    showToast(t("chat.no_agent"));
    return;
  }
  const workdir = document.getElementById("chat-workdir").value.trim();
  try {
    const result = await api("/api/chat/new", {
      method: "POST",
      body: JSON.stringify({ source, workdir: workdir || null }),
    });
    await loadChatThreads();
    await selectThread(result.thread.id);
    document.getElementById("chat-input").focus();
  } catch (error) {
    showToast(error.payload?.message ? tb(error.payload.message) : t("chat.create_failed"));
  }
}

function appendStreamingBubble() {
  const container = document.getElementById("chat-messages");
  const bubble = makeMessageNode("assistant", "", { streaming: true });
  container.append(bubble);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

function handleChatEvent(event) {
  const container = document.getElementById("chat-messages");
  if (event.type === "text" && event.text) {
    let bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
    if (!bubble) bubble = appendStreamingBubble();
    const body = bubble.querySelector(".md-body");
    const text = (body.dataset.raw || "") + (body.dataset.raw ? "\n\n" : "") + event.text;
    body.dataset.raw = text;
    body.innerHTML = markdownToHtml(text);
    container.scrollTop = container.scrollHeight;
    return;
  }
  if (event.type === "error") {
    const bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
    if (bubble) bubble.classList.remove("is-streaming");
    const note = element("div", "chat-error", event.text || t("chat.agent_error"));
    container.append(note);
    container.scrollTop = container.scrollHeight;
    return;
  }
  if (event.type === "done") {
    const bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
    if (bubble) bubble.classList.remove("is-streaming");
  }
}

function setStreaming(streaming) {
  chatState.streaming = streaming;
  document.getElementById("chat-send").disabled = streaming;
  document.getElementById("chat-stop").hidden = !streaming;
}

async function sendPrompt() {
  const input = document.getElementById("chat-input");
  const prompt = input.value.trim();
  if (!prompt || !chatState.activeThreadId || chatState.streaming) return;
  setStreaming(true);
  chatState.streamingThreadId = chatState.activeThreadId;
  const container = document.getElementById("chat-messages");
  container.append(makeMessageNode("user", markdownToHtml(prompt)));
  container.scrollTop = container.scrollHeight;
  input.value = "";
  let taskId = null;
  try {
    const result = await api("/api/chat/send", {
      method: "POST",
      body: JSON.stringify({ thread_id: chatState.activeThreadId, prompt }),
    });
    taskId = result.task_id;
    chatState.activeTaskId = taskId;
  } catch (error) {
    setStreaming(false);
    chatState.streamingThreadId = null;
    const message = error.payload?.message ? tb(error.payload.message) : t("chat.send_failed");
    container.append(element("div", "chat-error", message));
    return;
  }
  const abort = new AbortController();
  chatState.streamAbort = abort;
  try {
    const response = await fetch(`/api/chat/stream?task=${encodeURIComponent(taskId)}`, {
      headers: { "X-Agent-Trail-Key": apiKey },
      signal: abort.signal,
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop();
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        try {
          handleChatEvent(JSON.parse(line.slice(6)));
        } catch (error) { /* 事件行不完整时忽略 */ }
      }
    }
  } catch (error) {
    if (!abort.signal.aborted) {
      container.append(element("div", "chat-error", t("chat.stream_failed")));
    }
  } finally {
    setStreaming(false);
    chatState.streamAbort = null;
    chatState.streamingThreadId = null;
    // 服务端在回合结束时才落盘，流结束后拉一次持久化历史。
    if (chatState.activeThreadId) {
      selectThread(chatState.activeThreadId).catch(() => {});
    }
    loadChatThreads().catch(() => {});
  }
}

async function stopChat() {
  if (!chatState.activeTaskId) return;
  try {
    await api("/api/chat/cancel", { method: "POST", body: JSON.stringify({ task_id: chatState.activeTaskId }) });
    showToast(t("chat.stop_sent"));
  } catch (error) {
    showToast(t("chat.stop_failed"));
  }
  if (chatState.streamAbort) chatState.streamAbort.abort();
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
  for (const tab of document.querySelectorAll(".view-tab")) {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  }
  bindGlobalSearch();
  document.getElementById("chat-new").addEventListener("click", createThread);
  document.getElementById("chat-send").addEventListener("click", sendPrompt);
  document.getElementById("chat-stop").addEventListener("click", stopChat);
  document.getElementById("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  });
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
  // 工作台能力开关：内容索引/聊天被禁用时收起对应入口。
  const chatTab = document.querySelector('.view-tab[data-view="chat"]');
  if (chatTab) chatTab.hidden = !state.workbench.chat_enabled;
  const searchInput = document.getElementById("global-search");
  if (!state.workbench.content_enabled) {
    searchInput.disabled = true;
    searchInput.placeholder = t("search.disabled_placeholder");
  }
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
