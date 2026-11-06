"use strict";

/* Agent 足迹 i18n：zh-CN / en 双语字典 + 文案应用。
 * 语言解析顺序：URL ?lang= → localStorage("agent-trail.lang")
 *   → navigator.language（zh* → zh-CN，其余 → en）。
 * 本文件在 app.js 之前同步加载；后端消息键（launcher/server 返回的
 * message 键）以 "backend." 前缀收录，未知键原样返回。 */

const I18N_DICT = {
  "zh-CN": {
    "app.title": "Agent 足迹 · 找回工作现场",
    "header.local_badge": "本地运行 · 元数据索引",
    "header.refresh": "刷新足迹",
    "skip.timeline": "跳至时间线",
    "brand.aria": "Agent 足迹",
    "brand.name_html": "AGENT <strong>足迹</strong>",
    "hero.title_html": "找回<br>工作现场。",
    "hero.intro_html": "你记得做过什么。<br>足迹帮你想起在哪里、用什么、何时完成。",
    "filter.heading": "缩小范围",
    "filter.reset": "重置",
    "filter.period_aria": "日期快捷筛选",
    "period.all": "全部",
    "period.today": "今天",
    "period.yesterday": "昨天",
    "period.week": "最近 7 天",
    "filter.agent": "Agent 工具",
    "filter.all_tools": "全部工具",
    "filter.project": "项目目录",
    "filter.all_projects": "全部项目",
    "filter.text_label": "标题或目录",
    "filter.text_placeholder": "输入记忆中的线索…",
    "filter.note": "只搜索原生标题和目录，不搜索对话正文。",
    "segment.toggle": "折叠工作段",
    "segment.toggle_hint": "同工具、同项目、相邻 ≤ 45 分钟",
    "sources.heading": "来源状态",
    "sources.waiting": "等待扫描",
    "timeline.heading": "时间线",
    "timeline.count_suffix": "条本地会话",
    "loading.text": "正在拾起工作足迹…",
    "empty.title": "这段时间没有足迹",
    "empty.text": "换一个日期或工具试试。记录会在后台自动更新。",
    "privacy.note": "数据不离开本机。不保存消息、命令、代码或差异正文。",
    "dialog.close": "关闭",
    "session.unnamed": "未命名会话",
    "detail.id": "会话 ID",
    "detail.created": "首次活动",
    "detail.updated": "最后活动",
    "detail.stats": "改动统计",
    "detail.locator": "源记录位置",
    "resume.button": "回到原会话继续",
    "srcstatus.ok": "正常",
    "srcstatus.unavailable": "未发现",
    "srcstatus.experimental": "实验性",
    "srcstatus.error": "暂时失败",
    "srcstatus.incompatible": "格式不兼容",
    "common.unknown": "未知",
    "date.today": "今天",
    "date.yesterday": "昨天",
    "stats.unknown": "改动统计未知",
    "stats.files": "{n} 个文件",
    "error.load_failed": "时间线加载失败。请确认本地服务仍在运行，然后重试。",
    "status.scanned_at": "{time} 已扫描",
    "status.not_scanned": "尚未扫描",
    "sources.available": "{n} 个可用",
    "sources.skipped": "{n} 条不完整记录已跳过",
    "card.aria_view": "查看 {source} 在 {workdir} 的会话详情",
    "badge.missing": "源记录已缺失",
    "card.open_hint": "查看详情 →",
    "segment.badge": "工作段 · {n} 个会话",
    "segment.expand": "展开工作段 ↓",
    "resume.warning_missing": "原会话记录已缺失。这里保留的是最后一次本地元数据，不能恢复原会话。",
    "resume.reason_view_only": "目前仅支持查看；此工具的会话恢复路径尚未验证。",
    "resume.reason_missing": "源记录已缺失，恢复已停用。",
    "resume.reason_noworkdir": "原工作目录不存在，恢复已停用；不会改到其他目录。",
    "resume.reason_no_cli": "未在 PATH 中找到原工具",
    "resume.reason_ok": "将在原目录发起一个新终端。请在原工具中确认会话是否打开。",
    "toast.refreshed": "足迹已刷新。",
    "toast.refresh_failed": "刷新失败，请检查本地服务状态。",
    "toast.resume_sent": "已发起终端，请在原工具中确认会话。",
    "toast.resume_failed": "无法发起恢复；目录或原工具可能不可用。请查看本地服务状态。",
    "error.no_key": "此标签页没有访问密钥。请从终端启动 agent-trail serve，并打开它输出的完整链接。",
    "error.init_failed": "控制台初始化失败。请确认本地服务和访问链接有效。",
    "backend.session_not_found": "找不到对应的本地会话",
    "backend.session_source_missing": "源记录已缺失，无法恢复",
    "backend.session_view_only": "目前仅支持查看此会话",
    "backend.resume.no_verified_path": "此会话没有已验证的恢复方式",
    "backend.resume.workdir_missing": "原工作目录不存在，未启动终端",
    "backend.resume.cli_not_found": "未在 PATH 中找到原工具，未启动终端",
    "backend.resume.invalid_args": "恢复参数无效，未启动终端",
    "backend.resume.terminal_config_invalid": "终端参数配置无效",
    "backend.resume.no_terminal": "未找到可用终端，请配置 AGENT_TRAIL_TERMINAL",
    "backend.resume.launch_failed": "终端启动失败，请检查终端配置",
    "backend.resume.launch_ok": "已发起，请在原工具中确认恢复结果",
  },
  en: {
    "app.title": "Agent Trail · Find your way back to work",
    "header.local_badge": "Local · metadata index",
    "header.refresh": "Refresh trail",
    "skip.timeline": "Skip to timeline",
    "brand.aria": "Agent Trail",
    "brand.name_html": "AGENT <strong>TRAIL</strong>",
    "hero.title_html": "Find your way<br>back to work.",
    "hero.intro_html": "You remember doing it.<br>Trail reminds you where, with what, and when it was finished.",
    "filter.heading": "Narrow down",
    "filter.reset": "Reset",
    "filter.period_aria": "Date quick filters",
    "period.all": "All",
    "period.today": "Today",
    "period.yesterday": "Yesterday",
    "period.week": "Last 7 days",
    "filter.agent": "Agent tool",
    "filter.all_tools": "All tools",
    "filter.project": "Project directory",
    "filter.all_projects": "All projects",
    "filter.text_label": "Title or directory",
    "filter.text_placeholder": "Type a clue from memory…",
    "filter.note": "Searches native titles and directories only — never conversation bodies.",
    "segment.toggle": "Fold work segments",
    "segment.toggle_hint": "Same tool, same project, within 45 min",
    "sources.heading": "Sources",
    "sources.waiting": "Waiting for scan",
    "timeline.heading": "Timeline",
    "timeline.count_suffix": "local sessions",
    "loading.text": "Picking up your footprints…",
    "empty.title": "No footprints in this range",
    "empty.text": "Try another date or tool. Records refresh in the background.",
    "privacy.note": "Data never leaves this machine. No message, command, code or diff bodies are stored.",
    "dialog.close": "Close",
    "session.unnamed": "Unnamed session",
    "detail.id": "Session ID",
    "detail.created": "First activity",
    "detail.updated": "Last activity",
    "detail.stats": "Changes",
    "detail.locator": "Source record",
    "resume.button": "Resume original session",
    "srcstatus.ok": "OK",
    "srcstatus.unavailable": "Not found",
    "srcstatus.experimental": "Experimental",
    "srcstatus.error": "Temporary failure",
    "srcstatus.incompatible": "Incompatible format",
    "common.unknown": "Unknown",
    "date.today": "Today",
    "date.yesterday": "Yesterday",
    "stats.unknown": "Change stats unknown",
    "stats.files": "{n} files",
    "error.load_failed": "Failed to load the timeline. Make sure the local service is still running, then retry.",
    "status.scanned_at": "Scanned at {time}",
    "status.not_scanned": "Not scanned yet",
    "sources.available": "{n} available",
    "sources.skipped": "{n} incomplete records skipped",
    "card.aria_view": "View {source} session details in {workdir}",
    "badge.missing": "Source record missing",
    "card.open_hint": "View details →",
    "segment.badge": "Work segment · {n} sessions",
    "segment.expand": "Expand work segment ↓",
    "resume.warning_missing": "The original record is gone. Only the last local metadata is kept; the session cannot be resumed.",
    "resume.reason_view_only": "View only; the resume path for this tool has not been verified.",
    "resume.reason_missing": "Source record missing; resume is disabled.",
    "resume.reason_noworkdir": "The original working directory no longer exists; resume is disabled. It will not switch to another directory.",
    "resume.reason_no_cli": "Original tool not found in PATH",
    "resume.reason_ok": "A new terminal will open in the original directory. Confirm the session inside the original tool.",
    "toast.refreshed": "Trail refreshed.",
    "toast.refresh_failed": "Refresh failed. Check the local service status.",
    "toast.resume_sent": "Terminal launched. Confirm the session in the original tool.",
    "toast.resume_failed": "Could not launch resume; the directory or original tool may be unavailable. Check the local service status.",
    "error.no_key": "This tab has no access key. Start agent-trail serve from a terminal and open the full URL it prints.",
    "error.init_failed": "Console initialization failed. Make sure the local service and access link are valid.",
    "backend.session_not_found": "No matching local session",
    "backend.session_source_missing": "Source record missing; cannot resume",
    "backend.session_view_only": "This session is view-only",
    "backend.resume.no_verified_path": "No verified resume path for this session",
    "backend.resume.workdir_missing": "Original working directory does not exist; terminal not launched",
    "backend.resume.cli_not_found": "Original tool not found in PATH; terminal not launched",
    "backend.resume.invalid_args": "Invalid resume arguments; terminal not launched",
    "backend.resume.terminal_config_invalid": "Invalid terminal argument configuration",
    "backend.resume.no_terminal": "No usable terminal found; configure AGENT_TRAIL_TERMINAL",
    "backend.resume.launch_failed": "Terminal launch failed. Check the terminal configuration",
    "backend.resume.launch_ok": "Launched. Confirm the resume result in the original tool",
  },
};

function i18nResolveLocale() {
  const fromUrl = new URLSearchParams(window.location.search).get("lang");
  if (fromUrl && I18N_DICT[fromUrl]) {
    try { window.localStorage.setItem("agent-trail.lang", fromUrl); } catch (error) { /* 隐私模式 */ }
    return fromUrl;
  }
  try {
    const stored = window.localStorage.getItem("agent-trail.lang");
    if (stored && I18N_DICT[stored]) return stored;
  } catch (error) { /* 隐私模式 */ }
  const language = (navigator.languages && navigator.languages[0]) || navigator.language || "";
  return language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

let i18nLocale = i18nResolveLocale();

/* 注意：不要在全局作用域声明名为 t 的函数/变量——WebKit 的 JSC 会拒绝
 * app.js 的 const t 遮蔽声明（V8 容忍），导致整个 app.js 解析失败。
 * 翻译函数只挂在 I18N.t 上，app.js 自行声明 const t = window.I18N.t。 */

window.I18N = {
  t(key, params) {
    const dictionary = I18N_DICT[i18nLocale] || I18N_DICT.en;
    let text = dictionary[key] ?? I18N_DICT.en[key] ?? key;
    if (params) {
      for (const [name, value] of Object.entries(params)) {
        text = text.split(`{${name}}`).join(String(value));
      }
    }
    return text;
  },
  get locale() { return i18nLocale; },
};

function i18nApplyStatic() {
  document.documentElement.lang = i18nLocale;
  document.title = I18N.t("app.title");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = I18N.t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-html]")) {
    node.innerHTML = I18N.t(node.dataset.i18nHtml);
  }
  for (const node of document.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of node.dataset.i18nAttr.split(";")) {
      const [attr, key] = pair.split(":");
      if (attr && key) node.setAttribute(attr.trim(), I18N.t(key.trim()));
    }
  }
  for (const button of document.querySelectorAll("#lang-switch [data-lang]")) {
    const active = button.dataset.lang === i18nLocale;
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.classList.toggle("lang-active", active);
  }
}

function i18nWireSwitcher() {
  for (const button of document.querySelectorAll("#lang-switch [data-lang]")) {
    button.addEventListener("click", () => {
      try { window.localStorage.setItem("agent-trail.lang", button.dataset.lang); } catch (error) { /* 隐私模式 */ }
      window.location.reload();
    });
  }
}

i18nApplyStatic();
i18nWireSwitcher();
