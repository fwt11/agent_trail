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

// AionUI 式工具徽标：优先用各软件自己的 logo（内联 SVG，零外部资源）。
// openai / claude 路径来自 simple-icons（CC0）；zcode / kimi 等按其官方
// 图标形态定制（黑底圆角方块 + 粗白字母）；未收录的工具退回字母徽标。
const OPENAI_PATH = "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z";
const CLAUDE_PATH = "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z";

function svgGlyph(bg, path, scale) {
  const inner = path
    ? `<g transform="translate(4.56 4.56) scale(${scale ?? 0.62})" fill="#fff"><path d="${path}"/></g>`
    : "";
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5.5" fill="${bg}"/>${inner}</svg>`;
}

function svgLetter(bg, letter, options = {}) {
  const shape = options.circle
    ? `<circle cx="12" cy="12" r="12" fill="${bg}"/>`
    : `<rect width="24" height="24" rx="5.5" fill="${bg}"/>`;
  const size = options.size ?? 12.5;
  const mono = options.mono ?? false;
  const family = mono ? "JetBrains Mono,Cascadia Mono,monospace" : "Noto Sans CJK SC,PingFang SC,sans-serif";
  const color = options.color ?? "#fff";
  const weight = options.weight ?? 700;
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${shape}<text x="12" y="12.8" text-anchor="middle" dominant-baseline="central" font-family="${family}" font-size="${size}" font-weight="${weight}" fill="${color}">${letter}</text></svg>`;
}

const SOURCE_ICONS = Object.freeze({
  "kimi-code": svgLetter("#141414", "K", { circle: true }),
  zcode: svgLetter("#1c1c1e", "Z"),
  codex: svgGlyph("#0d0d0d", OPENAI_PATH),
  "claude-code": svgGlyph("#D97757", CLAUDE_PATH),
  "minimax-code": svgLetter("#2e7f74", "M"),
  codebuddy: svgLetter("#3f6fa5", "CB", { size: 10 }),
  opencode: svgLetter("#294c40", "&gt;_", { mono: true, size: 8 }),
  pi: svgLetter("#6d8a4e", "P"),
  "deepseek-harness": svgLetter("#4d6bfe", "DS", { size: 10 }),
});

const SOURCE_MARKS = Object.freeze({
  "kimi-code": { mark: "K", tone: "tone-plum" },
  zcode: { mark: "Z", tone: "tone-rust" },
  "minimax-code": { mark: "M", tone: "tone-teal" },
  codebuddy: { mark: "CB", tone: "tone-blue" },
  opencode: { mark: "OC", tone: "tone-forest" },
  codex: { mark: "CX", tone: "tone-slate" },
  "claude-code": { mark: "CC", tone: "tone-clay" },
  pi: { mark: "P", tone: "tone-moss" },
  "deepseek-harness": { mark: "DS", tone: "tone-ink" },
});

function makeToolMark(source) {
  const icon = SOURCE_ICONS[source];
  if (icon) {
    const node = element("span", "tool-mark tool-mark-svg");
    node.title = sourceLabel(source);
    node.innerHTML = icon;
    return node;
  }
  const conf = SOURCE_MARKS[source] || {
    mark: (sourceLabel(source)[0] || "?").toUpperCase(),
    tone: "tone-slate",
  };
  const node = element("span", `tool-mark ${conf.tone}`, conf.mark);
  node.title = sourceLabel(source);
  return node;
}

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

async function fetchUpdateBadge() {
  const badge = document.getElementById("update-badge");
  if (!badge) return;
  let result;
  try {
    result = await api("/api/update/status");
    // 缓存超过一天（托盘没跑或检查失败）：自动强制刷新一次。
    if ((result.cache_age ?? 0) > 86_400 || (!result.has_update && !result.checked_at)) {
      result = await api("/api/update/check", { method: "POST" });
    }
  } catch (error) {
    return;
  }
  if (result.has_update && result.latest) {
    badge.textContent = `↑ ${result.latest}`;
    badge.title = t("update.badge_hint", { current: result.current || "?" });
    badge.hidden = false;
  } else {
    badge.hidden = true;
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
  fetchUpdateBadge();
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
  try { window.localStorage.setItem("agent-trail.view", view); } catch (error) { /* 隐私模式 */ }
  document.getElementById("view-timeline").hidden = view !== "timeline";
  document.getElementById("view-chat").hidden = view !== "chat";
  for (const tab of document.querySelectorAll(".view-tab")) {
    const active = tab.dataset.view === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  if (view === "chat") {
    refreshChat()
      .then(() => {
        if (!chatState.activeThreadId) {
          setChatMode("start");
          loadStartConfig().catch(() => {});
        }
      })
      .catch(() => {});
  }
}

// ---- 对话（应用内聊天） ----

const chatState = {
  agents: [],
  threads: [],
  selectedAgent: null,
  workdirExpanded: false,
  configCache: {},
  startSettings: {},
  attachments: [],
  activeThreadId: null,
  activeThread: null,
  activeTaskId: null,
  streamingThreadId: null,
  streaming: false,
  streamAbort: null,
  approvalTimer: null,
  thoughtNode: null,
  stepsNode: null,
};

// 起始页（AionUI 式）：问候 + Agent 胶囊 + 卡片输入框 + 示例指令；
// 选中线程后切换到 thread 模式（头部 + 消息流）。
function setChatMode(mode) {
  const startMode = mode === "start";
  document.getElementById("chat-area").classList.toggle("is-start", startMode);
  document.getElementById("chat-header").hidden = startMode;
  document.getElementById("chat-start").hidden = !startMode;
  document.getElementById("chat-messages").hidden = startMode;
  document.getElementById("chat-suggestions").hidden = !startMode;
  document.getElementById("workdir-toggle").hidden = !startMode;
  if (startMode) {
    document.getElementById("workdir-row").hidden = !chatState.workdirExpanded;
    chatState.activeThread = null;
    chatState.activeThreadId = null;
    document.getElementById("chat-title").textContent = t("chat.title");
    document.getElementById("chat-context").textContent = "";
    document.getElementById("chat-messages").replaceChildren();
    for (const item of document.querySelectorAll(".thread-item")) item.classList.remove("is-active");
  } else {
    document.getElementById("workdir-row").hidden = true;
  }
  updateComposerPlaceholder();
  if (startMode) loadStartConfig().catch(() => {});
}

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
  const available = chatState.agents.filter((agent) => agent.available);
  const pills = document.getElementById("agent-pills");
  pills.replaceChildren();
  // 全量渲染（含未安装的）：置灰展示，用户能看清"有哪些、缺哪些"。
  if (!available.some((agent) => agent.source === chatState.selectedAgent)) {
    chatState.selectedAgent = available[0]?.source ?? null;
  }
  for (const agent of chatState.agents) {
    const pill = element("button", "agent-pill");
    pill.type = "button";
    pill.dataset.source = agent.source;
    pill.disabled = !agent.available;
    const isActive = agent.source === chatState.selectedAgent;
    pill.classList.toggle("is-active", isActive);
    if (!agent.available) pill.classList.add("is-unavailable");
    pill.title = agent.available ? "" : t("chat.agent_missing_hint");
    pill.append(makeToolMark(agent.source), element("span", "", sourceLabel(agent.source)));
    if (agent.available) {
      pill.addEventListener("click", () => {
        chatState.selectedAgent = agent.source;
        for (const item of pills.querySelectorAll(".agent-pill")) {
          item.classList.toggle("is-active", item.dataset.source === agent.source);
        }
        updateComposerPlaceholder();
        loadStartConfig().catch(() => {});
      });
    }
    pills.append(pill);
  }
  if (!available.length) {
    pills.append(element("span", "pill-empty", t("chat.no_agent")));
  }
  updateComposerPlaceholder();
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
    const body = element("span", "thread-body");
    body.append(
      element("span", "thread-title", thread.title || t("chat.title")),
      element("span", "thread-meta", `${sourceLabel(thread.source)} · ${formatTime(thread.updated_at)}`),
    );
    item.append(makeToolMark(thread.source), body);
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
  setChatMode("thread");
  for (const item of document.querySelectorAll(".thread-item")) {
    item.classList.toggle("is-active", item.dataset.threadId === threadId);
  }
  const thread = result.thread;
  chatState.activeThread = thread;
  const header = document.getElementById("chat-header");
  const existingMark = header.querySelector(".tool-mark");
  if (existingMark) existingMark.remove();
  header.prepend(makeToolMark(thread.source));
  document.getElementById("chat-title").textContent = thread.title || t("chat.title");
  document.getElementById("chat-context").textContent =
    `${sourceLabel(thread.source)} · ${thread.workdir}`;
  const container = document.getElementById("chat-messages");
  container.replaceChildren();
  chatState.thoughtNode = null;
  chatState.stepsNode = null;
  document.getElementById("chat-approvals").replaceChildren();
  for (const message of result.messages) {
    const node = makeMessageNode(message.role, markdownToHtml(message.text));
    container.append(node);
    if (message.attachments && message.attachments.length) {
      renderMessageAttachments(node, message.attachments, { previewByUrl: true });
    }
  }
  container.scrollTop = container.scrollHeight;
  document.getElementById("chat-send").disabled = false;
  updateComposerPlaceholder();
  loadThreadConfig(thread).catch(() => {});
}

async function createThread() {
  const source = chatState.selectedAgent;
  if (!source) {
    showToast(t("chat.no_agent"));
    return null;
  }
  const workdir = document.getElementById("chat-workdir").value.trim();
  try {
    const result = await api("/api/chat/new", {
      method: "POST",
      body: JSON.stringify({
        source,
        workdir: workdir || null,
        settings: chatState.startSettings,
      }),
    });
    await loadChatThreads();
    await selectThread(result.thread.id);
    return result.thread;
  } catch (error) {
    showToast(error.payload?.message ? tb(error.payload.message) : t("chat.create_failed"));
    return null;
  }
}

// ---- 附件（路径引用优先，内容上传兜底）----

function renderAttachList() {
  const wrap = document.getElementById("chat-attach-list");
  if (!wrap) return;
  wrap.replaceChildren();
  wrap.hidden = !chatState.attachments.length;
  chatState.attachments.forEach((att, index) => {
    const chip = element("span", "attach-chip");
    chip.append(
      element("span", "attach-icon", att.kind === "path" ? "🔗" : "📄"),
      element("span", "attach-name", att.name),
    );
    const remove = element("button", "attach-remove", "×");
    remove.type = "button";
    remove.title = t("chat.attach_remove");
    remove.addEventListener("click", () => {
      chatState.attachments.splice(index, 1);
      renderAttachList();
    });
    chip.append(remove);
    wrap.append(chip);
  });
}

function addPathAttachment(path, name) {
  if (chatState.attachments.length >= 8) {
    showToast(t("chat.attach_too_many"));
    return;
  }
  if (chatState.attachments.some((item) => item.path === path)) return;
  chatState.attachments.push({ kind: "path", name: name || path.split("/").pop() || path, path });
  renderAttachList();
}

function addContentAttachments(files) {
  for (const file of files) {
    if (chatState.attachments.length >= 8) {
      showToast(t("chat.attach_too_many"));
      break;
    }
    if (file.size > 6 * 1024 * 1024) {
      showToast(t("chat.attach_too_large"));
      continue;
    }
    chatState.attachments.push({ kind: "upload", name: file.name, mime: file.type || "", file });
  }
  renderAttachList();
}

function readUploadAttachment(att) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      resolve({ kind: "upload", name: att.name, mime: att.mime, data: dataUrl.split(",")[1] || "", preview: dataUrl });
    };
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(att.file);
  });
}

function renderMessageAttachments(parent, attachments, { previewByUrl = false } = {}) {
  if (!attachments || !attachments.length) return;
  const box = element("div", "chat-msg-attachments");
  for (const att of attachments) {
    if (att.kind === "upload" && att.mime && att.mime.startsWith("image/")) {
      const img = document.createElement("img");
      img.className = "chat-msg-image";
      img.alt = att.name;
      img.src = previewByUrl ? `/api/chat/file?name=${encodeURIComponent(att.rel || "")}` : att.preview || "";
      box.append(img);
    } else {
      box.append(element("span", "attach-chip", `${att.kind === "path" ? "🔗" : "📄"} ${att.name}`));
    }
  }
  parent.append(box);
}

function updateComposerPlaceholder() {
  const input = document.getElementById("chat-input");
  const thread = chatState.activeThread;
  const agent = thread ? thread.source : chatState.selectedAgent;
  input.placeholder = agent
    ? t("chat.input_placeholder_agent", { agent: sourceLabel(agent) })
    : t("chat.input_placeholder");
}

// ---- 文件夹选择器（服务端目录浏览；浏览器拿不到绝对路径，由本机服务代劳）----

const fsState = { path: null, parent: null, mode: "dir", onChoose: null };

async function fsNavigate(path) {
  let result;
  try {
    const fileParam = fsState.mode === "file" ? "&files=1" : "";
    const query = path ? `?path=${encodeURIComponent(path)}${fileParam}` : (fileParam ? `?files=1` : "");
    result = await api(`/api/fs/list${query}`);
  } catch (error) {
    showToast(t("fs.unreadable"));
    return;
  }
  fsState.path = result.path;
  fsState.parent = result.parent;
  document.getElementById("fs-current").textContent = result.path;
  document.getElementById("fs-up").disabled = !result.parent;
  const list = document.getElementById("fs-list");
  list.replaceChildren();
  if (!result.entries.length) {
    list.append(element("p", "fs-empty", t("fs.empty")));
  }
  for (const entry of result.entries) {
    const row = element("button", `fs-entry${entry.is_dir ? "" : " fs-entry-file"}`);
    row.type = "button";
    row.append(
      element("span", "fs-entry-icon", entry.is_dir ? "▸" : "📄"),
      element("span", "fs-entry-name", entry.name),
    );
    row.addEventListener("click", () => {
      if (!entry.is_dir) {
        // 文件模式：选中文件（零拷贝路径引用），目录则继续导航。
        if (fsState.mode === "file" && fsState.onChoose) {
          fsState.onChoose(entry);
          document.getElementById("fs-dialog").close();
        }
        return;
      }
      fsNavigate(entry.path).catch(() => {});
    });
    list.append(row);
  }
  const recents = document.getElementById("fs-recents");
  recents.replaceChildren();
  for (const recent of result.recents || []) {
    const chip = element("button", "chip-toggle fs-recent", recent.workdir);
    chip.type = "button";
    chip.title = recent.workdir;
    chip.addEventListener("click", () => fsNavigate(recent.workdir).catch(() => {}));
    recents.append(chip);
  }
}

function openFsDialog(mode, onChoose) {
  fsState.mode = mode;
  fsState.onChoose = onChoose || null;
  const title = document.getElementById("fs-title");
  if (title) title.textContent = mode === "file" ? t("fs.pick_file_title") : t("fs.pick_title");
  fsNavigate(null).catch(() => {});
  document.getElementById("fs-dialog").showModal();
}

function bindFsDialog() {
  const dialog = document.getElementById("fs-dialog");
  if (!dialog) return;
  document.getElementById("fs-browse").addEventListener("click", () => {
    const current = document.getElementById("chat-workdir").value.trim();
    fsState.mode = "dir";
    fsState.onChoose = null;
    fsNavigate(current || null).catch(() => {});
    document.getElementById("fs-title").textContent = t("fs.pick_title");
    dialog.showModal();
  });
  document.getElementById("fs-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) dialog.close();
  });
  document.getElementById("fs-home").addEventListener("click", () => fsNavigate(null).catch(() => {}));
  document.getElementById("fs-up").addEventListener("click", () => {
    if (fsState.parent) fsNavigate(fsState.parent).catch(() => {});
  });
  document.getElementById("fs-choose").addEventListener("click", () => {
    if (fsState.path) {
      document.getElementById("chat-workdir").value = fsState.path;
    }
    dialog.close();
  });
}

function appendStreamingBubble() {
  const container = document.getElementById("chat-messages");
  const bubble = makeMessageNode("assistant", "", { streaming: true });
  container.append(bubble);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

function showTypingIndicator() {
  const container = document.getElementById("chat-messages");
  removeTypingIndicator();
  const node = element("div", "chat-typing");
  const dots = element("span", "chat-typing-dots");
  dots.append(element("i"), element("i"), element("i"));
  node.append(dots, element("span", "", t("chat.thinking")));
  container.append(node);
  container.scrollTop = container.scrollHeight;
}

function removeTypingIndicator() {
  const old = document.querySelector(".chat-typing");
  if (old) old.remove();
}

// 打字机渐显：一次性到达的大段文本（如 kimi 整条消息）按帧推进渲染，
// token 级增量（claude/codex）则天然近实时。
function startReveal(body) {
  if (body.dataset.revealing === "1") return;
  body.dataset.revealing = "1";
  const container = document.getElementById("chat-messages");
  const step = () => {
    const current = body.dataset.raw || "";
    let shown = Number(body.dataset.shown || 0);
    shown = Math.min(shown + 14, current.length);
    body.dataset.shown = String(shown);
    body.innerHTML = markdownToHtml(current.slice(0, shown));
    container.scrollTop = container.scrollHeight;
    if (shown < current.length) {
      requestAnimationFrame(step);
    } else {
      body.dataset.revealing = "0";
    }
  };
  requestAnimationFrame(step);
}

const STEP_STATUS_ICONS = Object.freeze({
  pending: "○",
  in_progress: "◐",
  completed: "✓",
  failed: "✗",
});

function ensureThoughtNode(container) {
  if (chatState.thoughtNode && chatState.thoughtNode.isConnected) return chatState.thoughtNode;
  const details = document.createElement("details");
  details.className = "chat-thought";
  details.open = true;
  const summary = element("summary", "", t("chat.thought_running"));
  const body = element("div", "chat-thought-body");
  details.append(summary, body);
  const bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
  if (bubble) container.insertBefore(details, bubble);
  else container.append(details);
  chatState.thoughtNode = details;
  return details;
}

function appendThought(text) {
  const details = ensureThoughtNode(document.getElementById("chat-messages"));
  const body = details.querySelector(".chat-thought-body");
  body.dataset.raw = (body.dataset.raw || "") + text;
  body.textContent = body.dataset.raw;
  const container = document.getElementById("chat-messages");
  container.scrollTop = container.scrollHeight;
}

function settleThought() {
  const details = chatState.thoughtNode;
  if (details && details.isConnected) {
    details.open = false;
    const summary = details.querySelector("summary");
    if (summary) summary.textContent = t("chat.thought_done");
  }
  chatState.thoughtNode = null;
}

function ensureStepsNode(container) {
  if (chatState.stepsNode && chatState.stepsNode.isConnected) return chatState.stepsNode;
  const node = element("div", "chat-steps");
  const bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
  if (bubble) container.insertBefore(node, bubble);
  else container.append(node);
  chatState.stepsNode = node;
  return node;
}

function handleToolEvent(tool) {
  const container = document.getElementById("chat-messages");
  const steps = ensureStepsNode(container);
  const id = tool.toolCallId || `${tool.title}:${tool.status}`;
  let row = steps.querySelector(`[data-call-id="${CSS.escape(String(id))}"]`);
  if (!row) {
    row = element("div", "chat-step");
    row.dataset.callId = String(id);
    steps.append(row);
  }
  const icon = STEP_STATUS_ICONS[tool.status] || "·";
  row.replaceChildren(
    element("span", `chat-step-icon status-${tool.status || "pending"}`, icon),
    element("span", "chat-step-title", tool.title || tool.kind || t("chat.step_tool")),
  );
  container.scrollTop = container.scrollHeight;
}

function handleChatEvent(event) {
  const container = document.getElementById("chat-messages");
  removeTypingIndicator();
  if (event.type === "thought" && event.text) {
    appendThought(event.text);
    return;
  }
  if (event.type === "tool" && event.tool) {
    handleToolEvent(event.tool);
    return;
  }
  if (event.type === "title" && event.text) {
    document.getElementById("chat-title").textContent = event.text;
    loadChatThreads().catch(() => {});
    return;
  }
  if (event.type === "text" && event.text) {
    settleThought();
    let bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
    if (!bubble) bubble = appendStreamingBubble();
    const body = bubble.querySelector(".md-body");
    const previous = body.dataset.raw || "";
    const next = event.replace
      ? event.text
      : previous
        ? (event.delta ? previous + event.text : `${previous}\n\n${event.text}`)
        : event.text;
    body.dataset.raw = next;
    startReveal(body);
    container.scrollTop = container.scrollHeight;
    return;
  }
  if (event.type === "error") {
    settleThought();
    const bubble = container.querySelector(".chat-msg.is-streaming:last-of-type");
    if (bubble) bubble.classList.remove("is-streaming");
    const note = element("div", "chat-error", event.text || t("chat.agent_error"));
    container.append(note);
    container.scrollTop = container.scrollHeight;
    return;
  }
  if (event.type === "done") {
    settleThought();
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
  if ((!prompt && !chatState.attachments.length) || chatState.streaming) return;
  // AionUI 式直达：不先建线程，直接输入即开聊（按当前选中 Agent 创建）。
  if (!chatState.activeThreadId) {
    const thread = await createThread();
    if (!thread) return;
  }
  setStreaming(true);
  chatState.streamingThreadId = chatState.activeThreadId;
  chatState.thoughtNode = null;
  chatState.stepsNode = null;
  // 附件负载：路径引用直传；内容上传读为 base64。
  const attachmentsPayload = [];
  for (const att of chatState.attachments) {
    if (att.kind === "path") {
      attachmentsPayload.push({ kind: "path", name: att.name, path: att.path });
    } else {
      attachmentsPayload.push(await readUploadAttachment(att));
    }
  }
  const bubble = makeMessageNode("user", markdownToHtml(prompt));
  renderMessageAttachments(bubble, attachmentsPayload.map((item) => ({ ...item, kind: item.kind })), { previewByUrl: false });
  const container = document.getElementById("chat-messages");
  container.append(bubble);
  container.scrollTop = container.scrollHeight;
  input.value = "";
  chatState.attachments = [];
  renderAttachList();
  showTypingIndicator();
  startApprovalPolling(chatState.activeThreadId);
  let taskId = null;
  try {
    const result = await api("/api/chat/send", {
      method: "POST",
      body: JSON.stringify({
        thread_id: chatState.activeThreadId,
        prompt,
        attachments: attachmentsPayload.length ? attachmentsPayload : undefined,
      }),
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
    removeTypingIndicator();
    stopApprovalPolling();
    chatState.streamAbort = null;
    chatState.streamingThreadId = null;
    chatState.thoughtNode = null;
    chatState.stepsNode = null;
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

// ---- 工具审批（ACP request_permission）----

function startApprovalPolling(threadId) {
  stopApprovalPolling();
  const poll = async () => {
    let result;
    try {
      result = await api(`/api/chat/approvals?thread_id=${encodeURIComponent(threadId)}`);
    } catch (error) {
      return;
    }
    renderApprovals(result.approvals || []);
  };
  poll().catch(() => {});
  chatState.approvalTimer = setInterval(() => poll().catch(() => {}), 2000);
}

function stopApprovalPolling() {
  if (chatState.approvalTimer) {
    clearInterval(chatState.approvalTimer);
    chatState.approvalTimer = null;
  }
  renderApprovals([]);
}

function renderApprovals(approvals) {
  const container = document.getElementById("chat-approvals");
  if (!container) return;
  const existing = new Set(
    [...container.children].map((node) => node.dataset.approvalId),
  );
  for (const approval of approvals) {
    if (existing.has(approval.id)) {
      existing.delete(approval.id);
      continue;
    }
    const card = element("div", "chat-approval");
    card.dataset.approvalId = approval.id;
    const head = element("div", "chat-approval-head");
    head.append(
      element("span", "chat-approval-title", `${t("chat.approval_title")}: ${approval.title}`),
    );
    const actions = element("div", "chat-approval-actions");
    for (const option of approval.options || []) {
      const button = element(
        "button",
        `button ${option.kind && option.kind.startsWith("allow") ? "button-accent" : "button-danger"} chat-approval-button`,
        option.name || option.optionId,
      );
      button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api("/api/chat/approvals/decision", {
            method: "POST",
            body: JSON.stringify({ id: approval.id, option_id: option.optionId }),
          });
          card.remove();
        } catch (error) {
          button.disabled = false;
          showToast(t("chat.approval_failed"));
        }
      });
      actions.append(button);
    }
    card.append(head, actions);
    container.append(card);
    container.scrollIntoView({ block: "nearest" });
  }
  // 已消失（被处理/超时）的卡片移除
  for (const node of [...container.children]) {
    if (existing.has(node.dataset.approvalId)) node.remove();
  }
}

// ---- 线程/起始页配置选择器（完全通用：agent 返回什么 configOptions
// 就渲染什么，用 agent 自己的命名——对每个 agent 同等看待）----

function renderConfigChips(options, thread) {
  const wrapper = document.getElementById("chat-config");
  if (!wrapper) return;
  wrapper.replaceChildren();
  let visible = false;
  const threadValues = thread ? thread.config || {} : null;
  for (const option of options || []) {
    if (option.type && option.type !== "select") continue;
    const choices = option.options || [];
    const stored = threadValues ? threadValues[option.id] : chatState.startSettings[option.id];
    const current = option.currentValue || stored || "";
    if (!choices.length && !current) continue;
    visible = true;
    const select = document.createElement("select");
    select.className = "config-chip";
    select.title = option.name || option.id;
    if (choices.length) {
      // “自动”= 不覆盖，用 agent 自己的默认；未选过时自动选中。
      select.add(new Option(t("chat.config_auto"), ""));
      for (const choice of choices) {
        select.add(new Option(choice.name || choice.value, choice.value));
      }
      select.value = current;
      if (!select.value) select.value = "";
      select.disabled = false;
    } else {
      select.add(new Option(current, current));
      select.disabled = true;
    }
    select.addEventListener("change", async () => {
      if (thread) {
        try {
          await api("/api/chat/settings", {
            method: "POST",
            body: JSON.stringify({ thread_id: thread.id, settings: { [option.id]: select.value } }),
          });
          showToast(t("chat.config_applied"));
        } catch (error) {
          showToast(error.payload?.message ? tb(error.payload.message) : t("chat.config_failed"));
        }
      } else {
        chatState.startSettings[option.id] = select.value;
      }
    });
    wrapper.append(select);
  }
  wrapper.hidden = !visible;
}

async function loadThreadConfig(thread) {
  let options = [];
  try {
    const result = await api(`/api/chat/config?thread_id=${encodeURIComponent(thread.id)}`);
    options = result.configOptions || [];
  } catch (error) {
    options = [];
  }
  renderConfigChips(options, thread);
}

async function loadStartConfig() {
  const source = chatState.selectedAgent;
  if (!source) {
    renderConfigChips([], null);
    return;
  }
  let options = chatState.configCache[source];
  if (!options || !options.length) {
    // 首次选中：后端后台预建 ACP 会话（1~3 秒），轮询到配置就绪即渲染。
    const agent = chatState.agents.find((item) => item.source === source);
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        const result = await api(`/api/chat/config-defaults?source=${encodeURIComponent(source)}`);
        options = result.configOptions || [];
      } catch (error) {
        options = [];
      }
      if (options.length) {
        chatState.configCache[source] = options;
        break;
      }
      if (!(agent && agent.acp)) break;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    // 预建仍在进行：稍后补渲染一次（用户没切走才生效）。
    if (!options.length && agent && agent.acp) {
      setTimeout(() => {
        if (chatState.selectedAgent === source && !chatState.activeThreadId) {
          loadStartConfig().catch(() => {});
        }
      }, 2500);
    }
  }
  if (chatState.selectedAgent === source) {
    renderConfigChips(options || [], null);
  }
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
  document.getElementById("chat-new").addEventListener("click", () => {
    setChatMode("start");
    document.getElementById("chat-input").focus();
  });
  document.getElementById("chat-send").addEventListener("click", sendPrompt);
  document.getElementById("chat-stop").addEventListener("click", stopChat);
  document.getElementById("workdir-toggle").addEventListener("click", () => {
    chatState.workdirExpanded = !chatState.workdirExpanded;
    document.getElementById("workdir-row").hidden = !chatState.workdirExpanded;
    if (!chatState.workdirExpanded) document.getElementById("chat-workdir").value = "";
    else document.getElementById("chat-workdir").focus();
  });
  for (const button of document.querySelectorAll(".suggestion")) {
    button.textContent = t(button.dataset.promptKey);
    button.addEventListener("click", () => {
      const input = document.getElementById("chat-input");
      input.value = button.textContent;
      input.focus();
    });
  }
  document.getElementById("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  });
  // 附件：+ 按钮走服务端文件浏览器（零拷贝路径引用）；文件输入/粘贴/拖拽走内容上传。
  document.getElementById("chat-attach").addEventListener("click", () => {
    openFsDialog("file", (entry) => addPathAttachment(entry.path, entry.name));
  });
  const fileInput = document.getElementById("chat-file-input");
  fileInput.addEventListener("change", () => {
    addContentAttachments([...fileInput.files]);
    fileInput.value = "";
  });
  const input = document.getElementById("chat-input");
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) {
      event.preventDefault();
      addContentAttachments(files);
    }
  });
  const composer = input.closest(".composer");
  composer.addEventListener("dragover", (event) => {
    event.preventDefault();
    composer.classList.add("is-dragover");
  });
  composer.addEventListener("dragleave", () => composer.classList.remove("is-dragover"));
  composer.addEventListener("drop", (event) => {
    event.preventDefault();
    composer.classList.remove("is-dragover");
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length) addContentAttachments(files);
  });
  bindFsDialog();
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
  // 默认打开对话（AionUI 式工作台主视图）；记住用户上次的选择。
  let initialView = "chat";
  try { initialView = window.localStorage.getItem("agent-trail.view") || "chat"; } catch (error) { /* 隐私模式 */ }
  if (initialView === "chat" && !state.workbench.chat_enabled) initialView = "timeline";
  switchView(initialView);
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
