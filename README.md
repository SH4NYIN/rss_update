# RSS Update

Python RSS 更新提醒器：**原始标题弹窗 → 原文保存为 Markdown → 后台生成短文件名并归档**。默认订阅 DeepSeek 服务状态，提供轻量 TUI 阅读与订阅管理。

当前版本：**0.1.2**。源码分发包用户请先看包内 `START_HERE.md`，首次双击 `install.cmd`，之后双击 `rss.cmd`。查看版本可执行 `py rss_update.py --version`。分发包默认关闭可选的 AI 命名，不需要 WSL 或 Codex。

## 启动

Windows 安装 Python 3.10+，在项目目录打开 PowerShell：

```powershell
py -m pip install -r requirements.txt
py rss_update.py test-notification
py rss_update.py start
```

`test-notification` 显示明确标记的测试弹窗；Windows 下点击通知会打开源站。`start` 启动无窗口后台，关闭终端后继续运行，重复执行不会启动第二份。也可双击 `rss_background.pyw`，前提是 `.pyw` 关联的 Python 已安装依赖。

```powershell
py rss_update.py feeds       # 查看订阅列表与归档位置
py rss_update.py status      # 查看后台、各源抓取/通知/命名状态
py rss_update.py stop        # 请求停止；等待当前请求结束后退出
py rss_update.py run         # 前台运行，Ctrl+C 停止
py rss_update.py once        # 所有已启用源检查一次并退出，不等待 LLM
```

开始阅读：**[data/index.md](data/index.md)**。后台运行时请用 `status` 查看状态；`once` 与后台共用单实例锁，不能同时写同一份数据。

## 在任意目录使用 rss 命令

在 Windows 项目目录双击 **`add_to_path.cmd`**，或在 PowerShell 执行：

```powershell
.\add_to_path.cmd
```

脚本只把当前程序目录加入**当前用户的 PATH**，保留其它项，无需管理员权限。安装依赖后再使用；命令优先使用本目录的 `.venv`，没有时使用系统的 `py -3` 或 `python`。

完成后**完全关闭并重新打开 Windows Terminal / PowerShell / CMD**，即可在任意目录执行（只开新标签页可能仍沿用旧 PATH）：

```powershell
rss -tui         # 打开阅读界面
rss-tui          # 同上
rss tui          # 同上；直接输入 rss 也可以
rss status       # 查看后台状态
rss stop         # 停止后台
```

命令始终使用安装目录的默认配置和归档；不会在终端当前目录新建资料库。自定义配置可以用 `rss --config "D:\RSS设置\config.json" tui`。

移除入口请运行 **`remove_from_path.cmd`**，不会删除程序或归档。移动目录前先移除旧入口，移动后重新添加。如重开终端后命令仍找不到，可注销后重新登录；如打开了其它同名程序，用 `where.exe rss` 查看命令所在位置。

## TUI 阅读与管理

在 Windows Terminal / PowerShell 的项目目录运行：

```powershell
py -m pip install -r requirements.txt
py rss_update.py tui
```

也可执行 `py rss_tui.py`。已有后台会直接复用；没有后台时自动静默启动。**退出 TUI 只关闭阅读界面，后台继续监测、弹窗和归档。** 多个 TUI 可以同时打开，不会分别抓取或发送通知。原来的 `rss_background.pyw` 继续只负责静默启动后台。

左侧显示全部来源及各订阅源，右侧显示标题列表。列表每 2 秒读取本地记录；更新时保持选中的文章与阅读位置。回车进入详情，支持键盘和滚轮阅读，Esc 返回。暂停的源仍可查看历史。

| 按键 | 操作 |
| --- | --- |
| `Tab` / `Shift+Tab` | 切换来源列表与标题列表 |
| `↑` / `↓`、翻页键、滚轮 | 选择或滚动 |
| `Enter` | 左侧确认来源并进入列表；右侧打开正文 |
| `Esc` | 关闭详情或取消表单 |
| `A` | 添加 RSS：ID、名称、地址和检查间隔 |
| `E` | 编辑左侧选中来源的名称、检查间隔 |
| `P` | 暂停 / 启用选中来源 |
| `Delete` | 移除选中订阅，确认后生效；已有归档保留 |
| `O` | 用浏览器打开选中条目的原文 |
| `R` | 刷新本地列表；后台未运行时启动后台 |
| `Q` | 退出 TUI |

编辑表单中 `Ctrl+S` 保存。源 ID 和 URL 创建后固定，换地址请添加新源；同一份 `feeds.json` 可以继续手工编辑。若其它窗口或编辑器已修改列表，旧表单会提示刷新后重试，避免覆盖新配置。

详情显示 **RSS 实际提供的完整正文**，不额外抓取网页；源只提供摘要时，可按 `O` 查看网站原文。列表每源显示最近 1000 条，完整历史仍在 Markdown 归档中。窗口较窄时会隐藏来源列、缩短时间列，为标题留出空间。

新 TUI 可直接连接正在运行的现有后台，无需为这次界面升级重启后台。推荐在 Windows 使用 Windows Python，以继续接收 Windows 原生通知。

## 添加 RSS

直接编辑 **[feeds.json](feeds.json)**，在数组中添加一项：

```json
[
  {
    "id": "deepseek",
    "name": "DeepSeek 服务状态",
    "url": "https://status.deepseek.com/feed.rss",
    "enabled": true
  },
  {
    "id": "another-source",
    "name": "另一个订阅源",
    "url": "https://example.com/feed.rss",
    "enabled": true,
    "poll_interval_seconds": 300
  }
]
```

示例的第二个 URL 是占位地址，请换成实际 RSS。`id` 是固定目录名，只允许小写英文字母、数字、短杠和下划线，不能重复；`name` 是可随时更改的显示名称。

运行中的程序每 5 秒重读列表，当前网络请求可能稍微延后重读：

- **添加**：自动开始监测，新源首次抓取静默建立基线。
- **暂停**：改为 `enabled: false`；记录保留。
- **移除**：删除配置项即可停止监测，已有目录保留。
- **编辑时 JSON 暂时不完整**：保留上一份有效列表继续运行，并在日志和 `status` 中提示。
- **改成另一网站的 URL**：使用新的 `id`，避免不同来源混入同一个历史库。

每个来源有独立的去重状态、检查间隔、失败退避和归档目录。TUI 与手工编辑共用这份订阅列表，后台会自动加载变更。

## 归档

```text
data/
  index.md                         全部订阅的入口
  deepseek/
    index.md                       此来源的事件目录
    archive/2026/11/
      2026-11-03_简短事件名_事件ID.md
    state.sqlite3                  原文版本、去重、通知队列、命名缓存
    .runtime/archives.json         文件位置清单
  another-source/                  另一个来源的独立存档
  logs/monitor.log                 轮转日志
  .runtime/                       后台锁、状态及停止请求
```

每个事件一个 Markdown 文件；同一 GUID 的状态/正文变化及发布时间更新写入同一文件，按本地观察时间倒序保留版本。保留原始标题、链接、源站时间和正文；正文只去除 HTML 标签并进行 Markdown 转义，不用模型改写。时间显示为北京时间 UTC+08:00。

只有 RSS 整体时间变化时，会单独保存一条「来源时间更新」记录，注明这是程序提示及前后来源时间，不归到某个具体事件。后续时间变化继续追加到这份记录；TUI 中也可以打开查看。这类提示不调用模型命名。

先使用原标题生成可读文件名，模型返回后再换成短名称。日期与事件 ID 保持稳定，避免同名覆盖；自动更新目录链接，并移除旧名称的生成副本。数据库是完整记录来源，Markdown 可以重新生成，不适合手工编辑后期待修改长期保留。

## 命名与 fallback

[config.json](config.json) 的 `naming` 控制可选命名。源码分发包默认关闭；已有开发目录保持原配置。需要启用时改为：

```json
"naming": {
  "enabled": true,
  "timeout_seconds": 90,
  "wsl_distribution": ""
}
```

Windows 默认通过 `wsl.exe` 调用默认发行版里的 `codex exec`；`wsl_distribution` 可填安装 Codex 的发行版名称。Linux 直接调用 PATH 中的 `codex`。请先确保该 WSL 用户能正常使用 Codex，且已登录。

命名任务只提交标题，每批最多 12 个不同标题，相同标题使用缓存；全程序最多同时执行一个命名请求。使用临时会话、只读沙箱和低推理量，单独配置运行，不读取日常 `config.toml`，登录态仍由 Codex 管理；使用 CLI 默认模型。需要自定义入口时，可增加 `naming.command` 字符串数组作为命令前缀。

模型只返回 ID 与短名称的 JSON，程序检查长度和非法字符，再执行重命名。**超时、调用失败、返回不完整或格式非法，都保留原标题文件名**，之后等待 30 分钟再尝试；不会阻止通知、保存或其它源的抓取。文件系统不允许的字符会替换为空格，过长原标题截至 40 字，完整原标题仍保留在正文中。

后台停止时，也可手动处理已保存文件：

```powershell
py rss_update.py rename
```

关闭 `naming.enabled` 后完全不调用模型。`once` 只抓取和保存；常驻后台负责异步命名。Codex 调用方式参考 [官方非交互模式文档](https://learn.chatgpt.com/docs/non-interactive-mode)。

## 提醒与配置

默认每 120 秒检查，单次网络超时 20 秒，在 `config.json` 修改。源配置可单独覆盖检查间隔。全局配置修改后停止并重新启动；订阅列表无需重启。

- 首次成功抓取只存档，不弹已有历史。后续新事件、相同事件的标题/链接/正文变化会提醒。
- **时间更新也提醒**：事件 `pubDate` 前进，或 RSS 整体 `lastBuildDate` / `pubDate` 前进但事件内容未变时，弹出「时间更新（正文未变）」。提示只表示来源有更新信号，不推断故障、恢复或具体受影响事件；可点击打开源站确认。
- 相同时间、等价时区表示、无效/无时区日期及时间回退不会单独重复提醒；同一批正文变化已有提醒时，不额外弹整体时间提示。某些源每次生成都会更新时间，因此可能更频繁提示。
- 从旧版升级后，首次完整抓取会静默建立整体时间基线，之后的新时间才提醒。升级需停止并重新启动后台，例如先运行 `rss stop`，用 `rss status` 确认停止后执行 `rss -tui`。
- 弹窗标题来自 RSS 原始标题，正文包含来源、状态及原始正文摘要截取；批量变化合并提醒，完整记录保存在 Markdown。
- 支持 HTTP `ETag` / `Last-Modified`。失败按源退避，最长增加到 30 分钟；如果配置间隔本来更长则保留原间隔。
- 通知发送失败保留待发送记录，之后重试。`notifications: false` 则只归档，关闭期间的变化不会在重新启用后补弹。
- 通知在独立进程中执行并限时；模型命名在后台线程中执行。系统已接受通知但尚未写入成功标记时若进程崩溃，重启可能重复提醒，不能保证严格“恰好一次”。

Windows 通知使用 `win11toast`；Linux 图形桌面需要 `notify-send`（Ubuntu/Debian 安装 `libnotify-bin`）。在 WSL 运行 Linux Python 不会自动桥接 Windows 通知，所以 Windows 用户请用 Windows 的 `py` 启动监测，命名环节再调用 WSL Codex。不要从 Windows 和 WSL 同时写同一个数据目录。

本版不自动配置登录自启；重启、注销或后台异常退出后需要重新启动。只能保存实际观察到的 RSS 内容，不能补回关闭期间或两次轮询间遗漏的变化。本版支持 RSS 2.0，尚不支持 Atom。

## 验证与离线试跑

```powershell
py -m unittest discover -s tests -v
py rss_update.py once --source deepseek --feed-file saved-feed.rss
```

离线快照走相同的去重、保存和提醒流程；模拟数据请使用独立配置和数据目录。`--config` 放在子命令前，所有相对目录均相对于配置文件所在位置。

测试覆盖通知与去重、版本保存、命名不阻塞/失败兜底、真实子进程协议（本地替身）、按源隔离、订阅列表热加载、后台生命周期，以及 TUI 的来源切换、正文滚动、订阅增改停删、更新时保持选择和读库不改通知状态。TUI 使用 Textual 的无窗口交互测试；Windows 终端外观仍需在实际桌面确认。此前开发沙箱中的 `codex exec` 受只读限制未能初始化，未把模拟结果当作实际模型调用成功。
