# Agent 足迹（agent-trail）

中文 | [English](README.en.md)

跨 Agent 的本地工作记忆：自动发现你机器上各种编码 Agent 留下的会话记录，汇成一条按日期分组的时间线，帮你回答"昨天那个东西我到底是用哪个工具、在哪个目录做的"，并尽量一键回到原会话继续工作。

![时间线截图](docs/screenshot.png)

如果你在多个编码 Agent（Kimi Code、CodeBuddy、ZCode、OpenCode……）之间来回切换，大概也经历过这种时刻：只记得"昨天做过某个东西"，却想不起用了哪个工具、在哪个目录。这个工具做的就是缩短"模糊回忆 → 找到工作现场 → 继续工作"的路径。

## 特性

- **自动发现**：只读扫描各 Agent 已保存的本机会话（JSONL / JSON / SQLite），不改变任何现有使用习惯
- **统一时间线**：按本地日期分组、按最后活动时间倒序；支持今天/昨天/最近 7 天、Agent、项目目录和标题/目录文本筛选
- **实时更新**：Linux 下用 inotify 监控源文件变化，秒级反映新会话（不可用时自动回退到定时轮询）
- **工作段折叠**：同一 Agent、同一目录、相邻间隔 ≤45 分钟的会话可折叠为一段，展开仍能定位每个原会话
- **回到原会话**：对真实验证过恢复路径的工具，在原工作目录起一个新终端执行对应恢复命令
- **隐私优先**：本地索引只存元数据（标题、目录、时间、统计），不存任何消息正文、代码、diff 或凭据；HTTP 服务只绑定 127.0.0.1，每次启动生成高熵访问令牌
- **诚实降级**：没有原生标题就显示"未命名会话"，恢复路径没验证过就不显示恢复按钮，绝不伪造

## 平台支持

| 能力 | Linux | Windows | macOS |
| --- | --- | --- | --- |
| scan / list / serve 网页控制台 | ✅ | ✅ | ✅ |
| 实时变更监控 | inotify 秒级 | 轮询 ≈5s | 轮询 ≈5s |
| 系统托盘 + 内嵌窗口 | ✅（原生 SNI + WebKit2GTK） | 规划中 | 规划中 |
| 回到原会话（已验证工具） | ✅ | 取决于该 Agent 的 CLI 是否存在 | 同左 |

说明：各 Agent 的会话数据路径按 Linux 布局实现（`~/.kimi-code` 等，`Path.home()` 在全平台生效）；部分工具在其他平台的实际目录（如 opencode 的 XDG 变体）尚待真机验证，未发现数据源的适配器会诚实显示"未发现"。

## 支持的 Agent

| Agent | 时间线 | 回到原会话 | 说明 |
| --- | :-: | :-: | --- |
| Kimi Code | ✅ | ✅ | `kimi --session <id>`，真机验证 |
| CodeBuddy | ✅ | ✅ | `codebuddy --resume <id>`，真机验证 |
| MiniMax Code | ✅ | ✅ | `mcode --session <id>`，主会话真机验证；子代理会话只读 |
| OpenCode | ✅ | ✅ | `opencode --session <id>`，真机验证 |
| ZCode | ✅ | ➖ | 深链路由未验证，只读展示 |
| DeepSeek Harness | ✅ | ➖ | 恢复未通过真机验证，只读展示 |
| Pi | ✅ | ➖ | 恢复未通过真机验证，只读展示 |
| Codex | ✅ | ➖ | 恢复需要目录信任决策，未授权验证，只读展示 |
| Claude Code | ⚠️ | ➖ | 实验性检测，本机无数据时不产生虚假记录 |

## 安装

要求：Python ≥ 3.10，Linux（inotify 实时监控为 Linux 特性，其余平台自动退化为轮询）。**零第三方运行时依赖**，纯标准库。

```bash
pip install session-trail
```

或从源码安装：

```bash
git clone https://github.com/fwt11/agent_trail.git
cd agent_trail
pip install .
```

## 使用

```bash
agent-trail scan      # 扫描一次，输出各来源状态与记录数
agent-trail list      # 以 JSON Lines 输出全部会话元数据
agent-trail serve     # 启动本地控制台（默认自动打开浏览器）
agent-trail tray      # 系统托盘形态（Linux，见下）
```

`serve` 常用选项：

```bash
agent-trail serve --port 8731        # 固定端口（默认随机）
agent-trail serve --no-browser       # 不自动打开浏览器
agent-trail serve --no-watch         # 禁用 inotify，只用定时轮询
agent-trail serve --interval 30      # 轮询间隔（watch 健康时自动降为 5 分钟安全网）
```

启动后控制台 URL 带一次性高熵访问令牌（如 `http://127.0.0.1:8731/?key=…`）；令牌保存在浏览器 sessionStorage，后续请求通过请求头携带，所有 API 都会校验。手动刷新按钮立即触发一次扫描。

## 桌面托盘（Linux）

`serve` 之外还有系统托盘形态，已整合为子命令：内嵌窗口展示控制台，托盘菜单提供打开控制台、立即刷新、启动/停止服务、开机自启和退出。

```bash
agent-trail tray             # 启动托盘（自动拉起 agent-trail serve）
agent-trail tray --show      # 启动同时打开控制台窗口
agent-trail tray --install   # 手动写入应用菜单入口；--uninstall 反向清理
```

pip 安装后首次运行 `agent-trail tray` 会自动把"Agent 足迹"注册进系统应用菜单（应用启动器可直接搜索到）。Deepin 的自启动加载器会劫持指向 Python 脚本的 Exec 强制用系统解释器执行，故自启条目以 `/bin/sh -c exec` 包装规避"Agent 足迹"注册进系统应用菜单（应用启动器可直接搜索到），之后升级换路径也会自动刷新；无桌面会话（SSH/服务器）不写入。

托盘壳本身不依赖任何第三方 pip 包，但需要 GTK 的 GI 绑定（系统 Python 通常自带）。`agent-trail tray` 在当前解释器缺 GI 绑定时会自动改用带绑定的系统 Python 重执行（例如 pip 装在 venv 里、托盘跑在系统 Python），无需任何手动操作；实在找不到时按提示安装：

```bash
# Debian/Ubuntu/deepin 系
sudo apt install python3-gi gir1.2-webkit2-4.1
```

`agent-trail tray` 默认脱离终端后台运行：命令立即返回，日志追加在 `~/.agent-trail/tray.log`，关闭终端不影响托盘；`--foreground` 可前台运行调试。托盘以子进程方式管理 `agent-trail serve`——**启动托盘即自动拉起服务**（菜单里可停止/启动，异常退出自动重启）。窗口直接关闭只是隐藏，退出走托盘菜单，或 `kill $(cat ~/.agent-trail/tray.lock)`；flock 单实例锁防止重复启动（重复执行会提示"已在运行"）。

托盘图标走原生 StatusNotifierItem/DBusMenu 协议（`agent_trail/tray/sni.py`，Gio 实现——libayatana 在 Deepin 23 的托盘守护上不工作，详见源码注释）。内嵌 WebView 默认使用软件合成（`WEBKIT_DISABLE_DMABUF_RENDERER` / `WEBKIT_DISABLE_COMPOSITING_MODE`，可用同名环境变量覆盖），避免在无 DRM 权限的会话里出现空白窗口。`serve` 的所有命令行形态保持不变，无托盘环境（SSH/无桌面）不受影响。

## 工作原理

```
各 Agent 本地数据                本工具                        呈现
─────────────────      ─────────────────────────      ─────────────────
JSONL / JSON / SQLite ─▶ 适配器（9 种，只读） ─▶ 元数据索引    ─▶ 本地网页时间线
  ~/.kimi-code/…          规范化为统一模型      (SQLite)      127.0.0.1 + 令牌
  ~/.zcode/…              指纹增量去重                        筛选 / 详情 / 恢复
  ~/.codex/…              inotify 事件触发重扫
```

- **适配器契约**：每个 Agent 一个适配器，实现发现（discover）、扫描（scan）、状态（status）、恢复计划（resume_plan）四个边界；数据源格式变化时报告不兼容而不是猜测
- **增量扫描**：以源路径、大小、mtime 和适配器版本计算指纹，未变化的来源毫秒级跳过
- **实时监控**：`watcher.py` 用 ctypes 直接调 libc inotify（保持零依赖），目录级 watch 覆盖原子替换与 WAL sidecar 变化；去抖 + 最小间隔合并高频写入
- **恢复安全**：恢复命令来自适配器白名单参数数组，不经过 shell；API 只接受会话标识，服务端重新查库生成计划，调用者无法注入命令或目录
- **源数据只读**：SQLite 源以 `mode=ro` 打开，禁止任何写事务；源记录删除后保留最后元数据并标记"源已缺失"、禁用恢复

## 隐私与安全

- 明确不读取各工具的 credentials、auth、token、密钥类文件
- 本地索引与日志不保存消息正文、代码、命令、diff、附件或凭据
- HTTP 仅绑定回环地址；刷新与恢复使用 POST 并校验 Origin，防止外部网页诱导本机服务执行动作
- 网页不加载任何第三方脚本、字体或统计

## 已知限制

- 元数据优先：部分会话只有"未命名会话"，以目录和时间辅助识别；不读取首条用户消息补标题
- 适配器基于当前各工具的本机数据格式，工具升级后可能需要更新
- "回到工作现场"指原 Agent 会话与目录，不保证恢复未保存的文件或桌面布局

## 开发文档

- [设计文档](docs/design.md)
- [实施计划](docs/implementation-plan.md)

## 许可证

[MIT](LICENSE)
