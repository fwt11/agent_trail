# Agent 足迹 Implementation Plan


**Goal:** 构建一个自动发现多种编码 Agent 本地会话、以跨工具时间线展示元数据并能按能力恢复原会话的本地网页工具。

**Architecture:** Python 扫描器通过独立适配器把 JSON、JSONL 和 SQLite 来源规范化成 `ActivitySession`，写入只保存元数据的本地 SQLite 索引。回环地址 HTTP 服务提供带随机访问令牌的 API，原生网页展示时间线；恢复操作由独立终端启动层执行适配器生成的结构化计划。

**Tech Stack:** Python 3 标准库、SQLite、原生 HTML/CSS/JavaScript、本机 Agent CLI。

**Spec:** `docs/design.md`

## Global Constraints

- 只读源数据；SQLite 源库使用 `mode=ro`，禁止 checkpoint、vacuum、迁移和写事务。
- 本地索引和日志不得保存消息正文、代码、命令、diff、附件、token 或凭据；不得导入 Kimi `lastPrompt`、Codex `first_user_message`/`preview`。
- 首版无全文搜索、AI 摘要、云同步、统一启动器；无原生标题时使用 `fallback`，不得读取首条用户消息补标题。
- 默认每 30 秒增量扫描；工作段只合并同一 Agent、同一规范化目录且相邻间隔不超过 45 分钟的会话，原会话必须可展开。
- HTTP 仅绑定 `127.0.0.1`；每次启动生成高熵令牌；所有 API 校验令牌，刷新和恢复使用 POST 并校验 Origin。
- 恢复使用适配器白名单与参数数组，不拼接 shell 命令；目录缺失或 CLI 不可用时不得启动。
- ZCode 深链未经真实验证前 `resume_kind="none"`；Claude Code 只标实验，未经真实验证不得显示可恢复。
- 当前目录不是 Git 仓库；除非用户另行明确要求，不执行 `git init`、`git add` 或 `git commit`。
- 当前空项目没有测试体系；不新增 `tests/` 或测试脚手架，以行内 Python 断言、真实 CLI 扫描和浏览器端到端操作验证。

## Review Focus

- 损坏或半写入的 JSONL：单条坏记录不得让整个工具或其他适配器失效；Task 3 用坏行与有效行混合的临时来源验证。
- 活跃或 Schema 不兼容的 SQLite：查询失败不得写源库或阻塞其他适配器；Task 5 用只读连接和缺失表数据库验证。
- 重复、删除和迁移记录：OpenCode 双存储不得重复，源记录删除后必须保留元数据但禁用恢复；Task 2 和 Task 6 分别验证。
- 恶意 HTTP 输入和路径：调用者不能通过会话 ID、目录或请求体执行任意命令；Task 9 验证无令牌、错误 Origin、注入字符串和伪造恢复计划。
- 缺少标题、CLI、目录及时区边界：界面应显示未知而非猜测，恢复应明确失败，日期按本地时间分组；Task 8 和 Task 9 验证。

---

## 文件地图

- `pyproject.toml`：无运行时第三方依赖的包与命令入口。
- `src/agent_trail/models.py`：通用会话、扫描状态、恢复计划和筛选类型。
- `src/agent_trail/store.py`：本地元数据索引、去重、查询和源缺失标记。
- `src/agent_trail/scanner.py`：适配器调度、指纹复用、错误隔离和周期扫描。
- `src/agent_trail/adapters/`：每种 Agent 的发现、解析和恢复计划；`base.py` 定义契约。
- `src/agent_trail/cli.py`、`__main__.py`：`scan`、`list`、`serve` 命令。
- `src/agent_trail/server.py`：回环 HTTP、静态资源、API、令牌和 Origin 防护。
- `src/agent_trail/launcher.py`：目录/CLI 校验、终端检测和结构化恢复计划执行。
- `src/agent_trail/static/`：时间线、筛选、详情、适配器状态及恢复按钮。

### Task 1: 通用模型与本地元数据索引

**Files:**
- Create: `pyproject.toml`
- Create: `src/agent_trail/__init__.py`
- Create: `src/agent_trail/models.py`
- Create: `src/agent_trail/store.py`

**Interfaces:**
- Produces: `ActivitySession`、`AdapterStatus`、`ScanResult`、`ResumePlan`、`SessionFilter` 数据类型。
- Produces: `LocalStore.replace_source(source, sessions, fingerprint, scanned_at) -> None`、`LocalStore.mark_unchanged(source, fingerprint, scanned_at) -> None`、`LocalStore.list_sessions(filters: SessionFilter) -> list[ActivitySession]`、`LocalStore.get_session(source: str, session_id: str) -> ActivitySession | None`。
- `ActivitySession` 字段严格采用设计文档第 7 节；时间统一为 UTC ISO 8601 字符串，UI 再转换为本地时间。

- [ ] **Step 1: 先运行行内模型验收，确认失败**

Run: `PYTHONPATH=src python3 -c "from agent_trail.store import LocalStore"`
Expected: FAIL，`ModuleNotFoundError`。

- [ ] **Step 2: 建立包入口和精确数据类型**

在 `pyproject.toml` 声明 `agent-trail = agent_trail.cli:main`，运行时依赖为空。在 `models.py` 定义上述数据类；`title_source` 只允许 `native|fallback|missing`，`resume_kind` 只允许 `cli|deep_link|none`，适配器状态至少区分 `ok|unavailable|experimental|error|incompatible`。

- [ ] **Step 3: 实现 SQLite 元数据存储**

`LocalStore` 接收调用方给定的索引路径；以 `source + session_id` 为唯一键。`replace_source` 对本次快照 upsert，并将该来源中未出现的旧记录标为 `source_missing=1`、`resume_kind="none"`；它不能物理删除旧元数据。`list_sessions` 支持时间范围、来源、目录、标题/目录文本、limit/offset。

- [ ] **Step 4: 用行内断言验证 round-trip、筛选和墓碑记录**

在一条 `python3 - <<'PY'` 命令中使用 `tempfile.TemporaryDirectory()`：保存两条同来源记录，筛选确认只能得到预期记录；再次以只剩一条的快照调用 `replace_source`，断言另一条仍存在但 `source_missing=True` 且 `resume_kind="none"`。最后断言数据库表不存在 `message`、`content`、`diff`、`prompt`、`token` 列。

Expected: 所有断言通过，临时目录自动清理。

- [ ] **Step 5: 验证语法**

Run: `python3 -m compileall -q src`
Expected: 退出码 0。

### Task 2: 适配器契约、扫描调度与基础 CLI

**Files:**
- Create: `src/agent_trail/adapters/base.py`
- Create: `src/agent_trail/adapters/__init__.py`
- Create: `src/agent_trail/scanner.py`
- Create: `src/agent_trail/cli.py`
- Create: `src/agent_trail/__main__.py`

**Interfaces:**
- Consumes: Task 1 的模型与 `LocalStore`。
- Produces: `BaseAdapter.source`、`BaseAdapter.adapter_version`、`BaseAdapter.discover() -> AdapterStatus`、`BaseAdapter.status() -> AdapterStatus`、`BaseAdapter.scan(previous_fingerprint: str | None, now: datetime) -> ScanResult`、`BaseAdapter.resume_plan(session: ActivitySession) -> ResumePlan`。
- Produces: `ScanService.scan_all() -> list[AdapterStatus]`、`ScanService.run_forever(interval_seconds: int = 30) -> None`。
- Produces: `default_adapters(home: Path) -> list[BaseAdapter]` 和 CLI `agent-trail [--home PATH] [--data-dir PATH] scan|list|serve`；本任务只要求 `scan`、`list` 可运行，后续任务补充适配器和 `serve`。

- [ ] **Step 1: 验证 CLI 尚不存在**

Run: `PYTHONPATH=src python3 -m agent_trail scan`
Expected: FAIL，模块或入口不存在。

- [ ] **Step 2: 实现扫描事务边界**

相同指纹返回 `changed=False` 时调用 `mark_unchanged`；变化且扫描成功时调用一次 `replace_source`。`unavailable` 且原先有数据时视为来源缺失；`error|incompatible` 保留旧快照。单个适配器抛出异常时转换成脱敏状态并继续其他适配器。

- [ ] **Step 3: 实现 CLI 与空注册表**

`scan` 输出每个来源的状态、记录数和耗时；`list` 输出 JSON Lines，字段仅限通用元数据。`--home` 默认 `Path.home()`，`--data-dir` 默认 `~/.agent-trail`，索引文件名为 `index.sqlite`。

- [ ] **Step 4: 验证空调度和重复扫描**

Run: `PYTHONPATH=src python3 -m agent_trail --data-dir /tmp/agent-trail-empty-check scan && PYTHONPATH=src python3 -m agent_trail --data-dir /tmp/agent-trail-empty-check list`
Expected: 第一条成功且适配器数为 0，第二条无会话输出、退出码 0。再次运行 `scan`，不得产生重复记录或错误。

- [ ] **Step 5: 验证适配器异常隔离**

用行内 Python 向 `ScanService` 注入一个抛出 `RuntimeError` 的适配器和一个返回单条有效记录的适配器；断言有效记录入库，错误状态不含异常消息中的模拟秘密字符串。

Expected: 所有断言通过。

### Task 3: Kimi Code 与 CodeBuddy 文件适配器

**Files:**
- Create: `src/agent_trail/adapters/jsonl_utils.py`
- Create: `src/agent_trail/adapters/kimi_code.py`
- Create: `src/agent_trail/adapters/codebuddy.py`
- Modify: `src/agent_trail/adapters/__init__.py`

**Interfaces:**
- Consumes: Task 2 的 `BaseAdapter`、扫描结果和注册表。
- Produces: `JsonlReadResult(records: list[dict], error_count: int)` 与 `read_jsonl_metadata(path: Path, allowed_fields: set[str]) -> JsonlReadResult`，只返回白名单字段，坏行计数但不抛正文。
- Produces: `kimi-code`、`codebuddy` 两个适配器及 `kimi --session <id>`、`codebuddy --resume <id>` 的结构化恢复计划。

- [ ] **Step 1: 在临时主目录建立混合好坏 JSONL 的行内验收**

断言 `read_jsonl_metadata` 返回有效记录和准确的 `error_count`，且返回字典不含白名单外的 `content`、`lastPrompt`。在实现前运行，预期导入失败。

- [ ] **Step 2: 实现共享 JSONL 元数据读取**

逐行解析，只提取允许字段；JSON 错误、非对象记录和缺字段记录局部跳过。默认错误状态只包含工具、行号、错误计数和错误类型，不包含原行或路径；路径只能在用户主动开启的调试模式中记录。

- [ ] **Step 3: 实现两个适配器并注册**

Kimi 联合 `session_index.jsonl` 与 `state.json`，只取 `title` 和时间，不取 `lastPrompt`。CodeBuddy 从项目 JSONL 聚合 `sessionId/cwd/timestamp`，忽略 `content`；无原生标题时 `title=None`、`title_source="fallback"`。两者以源文件路径、大小、mtime 和适配器版本生成整体指纹。

- [ ] **Step 4: 真实扫描并验证能力**

Run: `PYTHONPATH=src python3 -m agent_trail --home ~ --data-dir /tmp/agent-trail-file-check scan`
Expected: 两个适配器至少一个能导入真实记录；每条 Kimi/CodeBuddy 记录有稳定 ID、目录和时间，CodeBuddy 不伪造标题；重复扫描 `changed=False`，记录数不翻倍。

- [ ] **Step 5: 用行内检查验证无正文落库**

查询 `/tmp/agent-trail-file-check/index.sqlite` 的表结构和 `ActivitySession` 序列化字段，断言不存在正文相关字段；对 CodeBuddy 的 `resume_plan` 断言命令参数分别等于 `("codebuddy", "--resume", session_id)`，目录保持原值。

### Task 4: Pi 与 DeepSeek Harness 适配器

**Files:**
- Create: `src/agent_trail/adapters/pi.py`
- Create: `src/agent_trail/adapters/deepseek_harness.py`
- Modify: `src/agent_trail/adapters/__init__.py`

**Interfaces:**
- Consumes: Task 2 契约和 Task 3 JSONL 元数据工具。
- Produces: `pi`、`deepseek-harness` 适配器；恢复命令分别为 `pi --session <id>`、`dst --resume <id>`。

- [ ] **Step 1: 先验证注册表缺少新来源**

Run: `PYTHONPATH=src python3 -c "from pathlib import Path; from agent_trail.adapters import default_adapters; print([a.source for a in default_adapters(Path('~'))])"`
Expected: 不含 `pi`、`deepseek-harness`。

- [ ] **Step 2: 实现 Pi 元数据聚合**

按项目目录发现会话 JSONL，只取会话 ID、时间、目录和原生命名记录；不读取消息 `content` 作为标题。无名称时使用 fallback；使用 `pi --session <id>`。

- [ ] **Step 3: 实现 DeepSeek Harness 元数据聚合**

从 `~/.dsh-tui/session-index.json` 的 `entries[*].derived` 取原生标题，以 `sessions.sqlite` 和 `~/.dsh/storages/session_projcache/` 的元数据补齐目录与时间；不解压 `session.v4.jsonl.zstd`。使用 `dst --resume <id>`。

- [ ] **Step 4: 真实扫描与脱敏验证**

Run: `PYTHONPATH=src python3 -m agent_trail --home ~ --data-dir /tmp/agent-trail-more-files-check scan`
Expected: 真实来源可读时导入记录；DeepSeek 标题来自 `derived.title`，Pi 无原生名称时为 fallback；输出和本地索引不含消息正文。

### Task 5: MiniMax Code 与 ZCode SQLite 适配器

**Files:**
- Create: `src/agent_trail/adapters/sqlite_utils.py`
- Create: `src/agent_trail/adapters/minimax_code.py`
- Create: `src/agent_trail/adapters/zcode.py`
- Modify: `src/agent_trail/adapters/__init__.py`

**Interfaces:**
- Consumes: Task 2 契约。
- Produces: `connect_readonly(path: Path) -> sqlite3.Connection`，使用 URI `mode=ro` 和短 busy timeout。
- Produces: `minimax-code`、`zcode` 适配器；MiniMax 使用 `mcode --session <id>`，ZCode 本任务固定 `resume_kind="none"`。

- [ ] **Step 1: 用临时 SQLite 验证只读与错误语义**

创建包含最小会话表的临时数据库和另一个缺少所需表的数据库；实现前运行只读连接与适配器验收，预期导入失败。

- [ ] **Step 2: 实现安全 SQLite 工具**

`connect_readonly` 不启用 immutable，以便读取 WAL 中已提交状态；只执行参数化 SELECT，查询结束关闭连接。缺表、缺列和数据库忙分别成为 `incompatible` 或可重试 `error`，不得写源库。

- [ ] **Step 3: 实现 MiniMax 与 ZCode 映射**

MiniMax 从 `local_runtime_sessions`、项目和已有 diff 表取标题、目录、时间及统计。ZCode 从 CLI `session` 与任务索引取相同元数据，跨库按会话/任务 ID 去重；不得连接 credentials 相关表。ZCode 不猜测 `zcode://` 路由。

- [ ] **Step 4: 真实扫描并验证源库只读与降级**

Run: `PYTHONPATH=src python3 -m agent_trail --home ~ --data-dir /tmp/agent-trail-sqlite-check scan`
Expected: 可读时导入 MiniMax/ZCode 元数据；ZCode 的 `resume_kind` 全为 `none`，MiniMax 恢复计划为 `("mcode", "--session", session_id)`。

- [ ] **Step 5: 验证坏 Schema 不阻塞其他适配器**

用行内临时主目录分别放置坏 MiniMax 数据库和有效 ZCode 数据库；断言 MiniMax 返回 `incompatible`，ZCode 仍入库，源数据库文件内容在扫描前后不变。

### Task 6: OpenCode、Codex 与实验性 Claude Code 适配器

**Files:**
- Create: `src/agent_trail/adapters/opencode.py`
- Create: `src/agent_trail/adapters/codex.py`
- Create: `src/agent_trail/adapters/claude_code.py`
- Modify: `src/agent_trail/adapters/__init__.py`

**Interfaces:**
- Consumes: Task 2 契约、Task 3 JSONL 工具、Task 5 SQLite 工具。
- Produces: `opencode`、`codex`、`claude-code` 适配器；前两者恢复计划分别为 `opencode --session <id>`、`codex resume <id>`，Claude 在真实验证前固定 `resume_kind="none"`。

- [ ] **Step 1: 实现 OpenCode 首选数据库与旧版回退**

优先读取 `opencode.db` 的 `session`；只有某会话不在数据库时才读取旧版 `storage/session/**/*.json`，按 `opencode + session_id` 去重。只取标题、目录、时间和原生统计。

- [ ] **Step 2: 实现 Codex 首选线程索引与元数据回退**

优先读取 `state_5.sqlite` 的 `threads`；只选 `id/title/cwd/created_at/updated_at/rollout_path/archived` 等元数据列，明确不选 `first_user_message`、`preview`。rollout 回退只取 `payload` 中的会话元数据，不取消息正文。

- [ ] **Step 3: 实现 Claude Code 的保守检测**

仅在 `~/.claude/projects/` 存在时尝试元数据适配；状态标为 `experimental`。没有真实样例验证的字段保持空，恢复为 `none`；缺少目录时报告 `unavailable`，不读取 `~/.claude.json` 或凭据。

- [ ] **Step 4: 验证双存储去重和真实扫描**

用行内临时 OpenCode 数据让同一 ID 同时出现在 DB 与 JSON，断言只产生一条且优先 DB。随后运行：

Run: `PYTHONPATH=src python3 -m agent_trail --home ~ --data-dir /tmp/agent-trail-all-check scan`
Expected: OpenCode/Codex 真实记录不重复，Codex 标题来自 `threads.title`；本机 Claude 无可验证数据时不产生虚假记录或恢复能力。

### Task 7: 带访问令牌的本地 HTTP API 与扫描调度

**Files:**
- Create: `src/agent_trail/server.py`
- Modify: `src/agent_trail/cli.py`
- Modify: `src/agent_trail/scanner.py`

**Interfaces:**
- Consumes: `LocalStore`、`ScanService`、`SessionFilter` 和全部适配器。
- Produces: `LocalServer.start() -> str`，返回带 `key` 的完整本机 URL；`LocalServer.stop() -> None`。
- Produces: `GET /api/sessions`、`GET /api/status`、`POST /api/refresh`；静态入口为 `GET /?key=<token>`。
- CLI `serve` 固定监听 `127.0.0.1`，增加 `--port`、`--no-browser`、`--interval 30`。

- [ ] **Step 1: 先定义 API 安全验收**

用行内 Python 启动 `LocalServer`：无 key 的 GET 必须返回 403；错误 key 必须返回 403；正确 key 的 GET 必须返回 200；`POST /api/refresh` 使用错误 Origin 必须返回 403，正确 key 与 Origin 才触发一次扫描。实现前运行预期导入失败。

- [ ] **Step 2: 实现回环服务与令牌校验**

用 `ThreadingHTTPServer` 和 `secrets.token_urlsafe(32)`；Host 只接受实际监听的本机地址，禁止 DNS rebinding。API 通过 `X-Agent-Trail-Key` 接收令牌，POST 校验 `Origin` 的 scheme/host/port；响应添加 `Cache-Control: no-store` 和基础安全头。

- [ ] **Step 3: 实现查询与刷新接口**

`GET /api/sessions` 将日期、来源、目录、query、limit/offset 映射为 `SessionFilter`；`GET /api/status` 返回适配器状态、最近扫描时间和记录数；`POST /api/refresh` 在同一扫描锁内执行，避免周期扫描与手动扫描并发写索引。

- [ ] **Step 4: 实现后台调度与 CLI 启动**

`serve` 先执行一次扫描，再启动每 30 秒调度；`--no-browser` 不打开浏览器，否则用 `webbrowser.open` 打开完整带 key URL。停止时先停调度，再停 HTTP 和关闭索引。

- [ ] **Step 5: 运行真实 API 验收**

在临时索引中启动服务，使用 `urllib.request` 验证 Step 1 全部状态码、刷新次数和响应字段；确认服务地址只能是 `127.0.0.1`，响应不包含任何源正文。

### Task 8: 跨 Agent 时间线网页

**Files:**
- Create: `src/agent_trail/static/index.html`
- Create: `src/agent_trail/static/styles.css`
- Create: `src/agent_trail/static/app.js`
- Modify: `src/agent_trail/server.py`

**Interfaces:**
- Consumes: Task 7 API；页面从首次完整 URL 的 `key` 取得令牌，后续只放 `sessionStorage` 和请求头。
- Produces: `groupWorkSegments(sessions, gapMinutes = 45)`、时间线筛选、会话详情和适配器状态视图。

- [ ] **Step 1: 实现语义化页面与本地时间转换**

页面包含日期快捷筛选、Agent/项目筛选、标题/目录文本输入、时间线、详情和扫描状态。所有日期在浏览器本地时区显示，分组键使用本地年月日；无标题显示“未命名会话”，缺失统计显示“未知”。

- [ ] **Step 2: 实现工作段折叠与详情**

`groupWorkSegments` 只折叠 `source` 和规范化 `workdir` 相同、相邻 `updated_at` 间隔不超过 45 分钟的记录；跨午夜仍按各会话本地日期归属，不跨日期合并。展开工作段必须显示每个原会话及其独立详情。在浏览器控制台分别用间隔 45、46 分钟以及本地 23:50/次日 00:10 的记录调用该函数，断言 45 分钟可合并、46 分钟不合并、跨午夜不合并。

- [ ] **Step 3: 实现状态与恢复按钮的诚实呈现**

`resume_kind="cli"` 才显示“回到原会话继续”；`none` 显示“目前仅支持查看”。源缺失、目录缺失或适配器不可恢复时禁用按钮并给出原因。ZCode 和未验证 Claude 不得出现可恢复状态。

- [ ] **Step 4: 浏览器端到端验收**

启动真实服务并打开带 key URL，验证：真实扫描记录可见；昨天/最近 7 天、Agent、项目和文本筛选可用；工作段可展开；刷新后 UI 更新；新标签无 key 时不能读取 API。用浏览器控制台确认没有第三方网络请求。

Expected: 所有操作在真实页面完成，控制台无未处理异常。

### Task 9: 原会话恢复与整体验收

**Files:**
- Create: `src/agent_trail/launcher.py`
- Modify: `src/agent_trail/server.py`
- Modify: `src/agent_trail/static/app.js`
- Modify: `src/agent_trail/cli.py`

**Interfaces:**
- Consumes: 适配器 `resume_plan`、存储中的单个 `ActivitySession`、Task 7 POST 防护。
- Produces: `TerminalLauncher.launch(plan: ResumePlan) -> LaunchResult`。
- Produces: `POST /api/resume`，请求体只接受 `source` 与 `session_id`；服务端重新查记录并生成计划，拒绝调用者提供的命令、目录或恢复类型。

- [ ] **Step 1: 先运行启动器安全验收**

用行内断言覆盖：目录不存在、CLI 不在 PATH、`resume_kind="none"`、源记录缺失都返回失败且不创建进程；会话 ID `x; touch /tmp/should-not-exist` 不得创建该文件；请求体额外传入 `command/workdir/resume_kind` 时服务端忽略并重新生成计划。实现前运行预期导入失败。

- [ ] **Step 2: 实现结构化恢复计划与终端检测**

`ResumePlan` 包含 `kind`、`executable`、`args: tuple[str, ...]`、`workdir`、不可用原因。`TerminalLauncher` 按当前 Linux 环境检测终端，允许显式配置参数数组模板；最终参数为终端参数加白名单 CLI 参数，不经过 `shell=True`。

- [ ] **Step 3: 实现受保护的恢复 API 和 UI 反馈**

服务端只按 `source + session_id` 查记录并调用对应适配器；成功仅返回“已发起，请在原工具中确认恢复结果”，不得返回“已恢复”。失败返回脱敏、可操作的原因，不泄露令牌或正文。

- [ ] **Step 4: 用伪终端验证实际参数传递**

在 `TemporaryDirectory()` 中建立临时可执行终端脚本，将收到的 argv 写入临时文件；配置启动器使用该脚本，发起一次恢复并断言目录、CLI、会话 ID 均为独立 argv，注入字符串未被执行。临时文件自动清理。

- [ ] **Step 5: 真实恢复原会话**

对每个在 UI 标为可恢复的适配器，从真实时间线选择一条已有会话并点击“回到原会话继续”。Expected: 新终端在原工作目录启动，原工具加载预期会话；若未实际验证，则把该适配器降级为 `resume_kind="none"`，不得保留可恢复宣称。

- [ ] **Step 6: 执行完整交付验收**

依次运行 `python3 -m compileall -q src`、一次全量真实扫描和 Task 7–9 的 API/UI/恢复检查。检查本地索引与日志没有正文或凭据；检查 ZCode 只读降级、Claude 实验状态、缺失目录和损坏来源。删除本次验证创建的 `/tmp/agent-trail-*-check` 临时索引，保留用户正式数据。

Expected: 设计文档第 12 节可验证项目全部通过；未通过项明确降级或报告，不宣称完成。
