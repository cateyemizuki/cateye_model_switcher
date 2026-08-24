# 梁文峰&梁文谷模型切换（MaiBot 插件）

> ## ⚠️ 非规范声明
>
> 本插件**不符合 MaiBot 插件规范**：
> - MaiBot 的插件架构**不允许插件直接修改框架自身的配置文件**（如 `model_config.toml`、`bot_config.toml`）；
> - 本插件使用了**直接的 IO 操作**读写 `model_config.toml` 文件，这同样超出插件架构的允许范围；
> - 因此本插件**仅作者自己使用**，**不会上传至插件市场**（MaiBot 插件中心）；
> - 使用本插件造成的一切后果（配置损坏、热重载异常等）由使用者自行承担。

按**北京时间（UTC+8）**的峰谷时段，定时调整 MaiBot `model_config.toml` 中各任务的模型列表顺序：把当前时段（峰时 / 谷时）指定的模型提升到 `model_list` **首位**（优先使用），在不重启服务的前提下平衡模型成本与性能。

> 示例：工作日（峰时）优先用其他低成本模型；夜间与周末（谷时）自动切回deepseek系列模型。

## 功能

- **峰谷时段自定义**：峰时支持多个时间段（精确到分钟、支持跨天）；谷时默认取峰时之外的所有时间，也可显式指定；支持按星期排除峰时（如周六日）。
- **任务范围限定**：仅调整 8 个任务（replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji）；vlm / voice / embedding 不受影响。
- **优先切换**：把指定模型移动到 `model_list` 索引 0，其余模型顺序不变（配合 `selection_strategy = "sequential"` 即优先使用）。
- **静默跳过**：某任务峰/谷模型任一未配置时，该任务全部切换静默跳过，不打印日志。
- **容错警告**：模型未在 `[[models]]` 中定义、或不在任务 `model_list` 中时，输出警告并跳过该任务。
- **无需重启**：修改后由 MaiBot 文件监听自动热重载生效。
- **TOML 格式保真**：用 `tomlkit` 解析/写回，保留注释与空行、保持行尾风格（CRLF/LF）、**原地写入同一文件**（保持 inode，避免 FileWatcher 丢失监听）。
- **自动备份**：每次实际修改前备份原文件到插件数据目录（保留最近 N 份）。
- **`/llmlist` 命令**：以图片形式查看所有任务的模型列表（首位模型带「优先」徽章，动态/固定任务分组展示）。默认所有人可用，可配置为仅管理员可用。
- **`/switcher debug` 命令**：**仅管理员可用**，强制翻转当前峰谷状态并立即应用；调用后自动检测（安全网）暂停 `debug_pause_minutes` 分钟（默认 5，可配置；静默期内再次调用重新计时、不叠加），用于测试切换代码是否正常。
- **版本自动兼容**：旧版配置文件自动补齐新增字段（`admin_users` / `llmlist_admin_only` / `debug_pause_minutes`），无需手动迁移。

## 安装

1. 将插件目录（含 `_manifest.json`、`plugin.py` 等）放入 MaiBot 的 `plugins/` 目录。
2. 重启 MaiBot，或在 WebUI 插件中心安装。
3. 插件为标准 SDK 插件（基于 `maibot-plugin-sdk`），SDK 由 MaiBot Runner 内置提供，**无需用户手动安装**；`tomlkit` 依赖已声明于 `_manifest.json`，Host 会自动安装。

> 兼容：`host_application` `1.0.0 ~ 1.99.99`，`sdk` `2.0.0 ~ 2.99.99`（Manifest v2）。

## 配置

插件加载后由 Runner 在插件目录生成 `config.toml`，可在 WebUI 修改：

```toml
[plugin]
enabled = true                # 是否启用插件
admin_users = []              # 管理员列表（用户ID 或 平台:用户ID，如 "123456789" 或 "qq:123456789"；/switcher debug 强制仅管理员可用）
llmlist_admin_only = false    # 是否限制 /llmlist 仅管理员可用（默认关，所有人可用）
debug_pause_minutes = 5       # /switcher debug 后暂停自动检测（安全网）的分钟数（默认 5）
config_version = "1.1.1"      # 配置版本（与插件版本同步，UI 中隐藏）

[schedule]
peak_periods = ["09:00-12:00", "14:00-18:00"]   # 峰时时段（北京时间 HH:MM-HH:MM，支持跨天如 "22:00-02:00"）
offpeak_periods = []                              # 谷时时段（可选；留空 = 峰时之外的所有时间）
exclude_weekdays = [6, 7]                         # 排除峰时的星期（1=周一 ... 7=周日）

[task_mapping]               # 每个任务的峰/谷模型为独立配置项（16 个）
replyer_peak_model = "deepseek-v4-pro-think"             # 回复任务·峰时模型
replyer_offpeak_model = "[OCG]deepseek-v4-flash-opencode go"   # 回复任务·谷时模型
planner_peak_model = "deepseek-v4-pro-think"
planner_offpeak_model = "[OCG]deepseek-v4-flash-opencode go"
# memory / mid_memory / utils / learner / expression_use / emoji 同理
# 全部留空 → 该任务不参与切换

[model_file]
model_config_path = ""        # 留空 = 自动定位 <MaiBot根目录>/config/model_config.toml；可填绝对路径覆盖
backup = true                 # 修改前是否备份
backup_keep = 10              # 备份保留份数
```

> `task_mapping` 下每个任务的峰时与谷时模型是**两个独立配置项**（`<task>_peak_model` 与 `<task>_offpeak_model`），WebUI 中分别显示、分别填写。`<任务>` 为 replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji。

### 配置项一览

| 配置项 | 说明 |
|--------|------|
| `plugin.enabled` | 是否启用插件（默认开）。关闭后停止峰谷切换调度 |
| `plugin.admin_users` | 管理员列表（用户ID 或 `平台:用户ID`）。`/switcher debug` **强制仅管理员可用**；`/llmlist` 是否仅管理员由 `llmlist_admin_only` 决定 |
| `plugin.llmlist_admin_only` | 是否限制 `/llmlist` 仅管理员可用（默认 `false`，所有人可用） |
| `plugin.debug_pause_minutes` | 调用 `/switcher debug` 后暂停自动检测（安全网）的分钟数（默认 `5`）。静默期内再次 debug 重新计时、不叠加 |
| `schedule.peak_periods` | 峰时时段列表，格式 `HH:MM-HH:MM`（半开区间 `[start, end)`），支持跨天（如 `"22:00-02:00"`） |
| `schedule.offpeak_periods` | 谷时时段列表（可选）。留空时谷时 = 峰时之外的所有时间；填写后峰时 = 非排除日且不在谷时段内 |
| `schedule.exclude_weekdays` | 排除峰时的星期数组，`1`=周一 … `7`=周日。默认 `[6, 7]`（周六日整天保持谷时） |
| `task_mapping.<任务>_peak_model` | 对应任务峰时使用的模型名，必须与 `[[models]]` 的 `name` 完全一致；留空则跳过该任务 |
| `task_mapping.<任务>_offpeak_model` | 对应任务谷时使用的模型名，同上 |
| `model_file.model_config_path` | `model_config.toml` 路径。默认自动推导为插件目录上两级 `config/model_config.toml`；可填绝对路径 |
| `model_file.backup` | 修改前备份到 `data/plugins/cateye_model_switcher/backup/`（默认开） |
| `model_file.backup_keep` | 备份保留份数（默认 10） |

## 使用

### ⚠️ 前置条件：选择策略须为「按顺序优先」

本插件通过**提升到 `model_list` 首位**实现优先使用，因此被调整任务的 `selection_strategy` 必须为 **`"sequential"`**，否则提升顺序不会生效（如 `random` 会随机选择、`balance` 会负载均衡）。请在 `model_config.toml` 中设置：

```toml
[model_task_config.replyer]
model_list = [ ... ]
selection_strategy = "sequential"   # 必须：按配置顺序优先选择，首位模型优先使用
```

> 插件不会自动改写 `selection_strategy`（避免干扰其他手动配置），请确保已按上述说明设置。

### 自动化切换

1. **启动时**：立即检查一次当前时段并应用。
2. **运行中**：每 60 秒检查一次；**仅当时段发生变化（峰↔谷）** 才修改 `model_config.toml`。
3. **热重载**：修改后 MaiBot FileWatcher 自动热重载，无需重启（插件订阅 `model` 配置热重载，可感知外部改动并刷新内部状态）。

### 防刷屏设计

为避免「插件写文件 → 触发热重载 → 误判为外部改动 → 再写文件」的死循环导致日志刷屏，做了三重保护：

- **仅时段变化才写**：同一时段内绝不重复写文件；
- **字节级对比**：写入前将序列化结果与磁盘内容比对，内容一致时跳过写入；
- **自己写入回执识别**：插件写入后记录标记；收到 `model` 热重载回调时，若在 **10 秒窗口内**则视为自己触发的回执，**静默处理、不重写**；窗口外或无标记的热重载才视为外部改动。

因此每次时段切换**只写一次**文件、只触发一次热重载。插件**不打印任何热重载相关日志**——由框架原生处理。

### 命令：`/llmlist`

发送 `/llmlist` 查看所有任务的模型列表，以**图片**形式输出：

- 读取 `[model_task_config]`，按**动态调整任务**（8 个）与**固定任务**（vlm / voice / embedding）分组展示；
- 每个任务卡片标注模型数量，**首位模型带「优先」徽章**，其余模型以标签形式排列；
- 使用 `ctx.render.html2png()` 渲染为 PNG，作为**单条消息用合并转发**发出（收敛为一条转发气泡）；合并转发不可用时回退为普通图片发送；
- 渲染失败时回退为纯文本提示并记录日志；
- **权限**：默认所有人可用；若配置 `plugin.llmlist_admin_only = true`，则仅 `plugin.admin_users` 中的管理员可用。

### 命令：`/switcher debug`

**仅管理员可用**（`plugin.admin_users`），用于测试峰谷切换代码是否正常：

- 发送 `/switcher debug` 会**强制翻转**当前峰谷状态（峰 ↔ 谷）并立即应用（写入 `model_config.toml`）；
- 返回提示当前已切换为**峰时**还是**谷时**，以及是否实际修改了配置；
- **自动检测暂停**：每次调用后，调度器的自动检测（安全网）暂停 `plugin.debug_pause_minutes` 分钟（默认 5），期间**不会**按真实时段自动纠正；**静默期内再次执行 `/switcher debug` 会重新计时（不叠加）**；
- 静默窗口结束后自动恢复自动检测，按真实北京时间纠正到正确状态（这也是一种「切回来」的方式）；
- 若未配置任何管理员，或调用者不在 `admin_users` 中，命令被拒绝。

### 日志示例

```
[INFO] 峰谷模型切换插件已加载：模型配置文件 D:\MaiBot\config\model_config.toml，当前为谷时
[INFO] 已切换为峰时：3 个任务调整了 model_list
[WARN] 任务 replyer 的峰时模型 "xxx" 不在其 model_list 中，跳过切换。
[WARN] model_config.toml 含 TOML 非法空值行（如 api_key = ），已临时补为 "" 后解析；建议在 WebUI 补全密钥
[INFO] switcher debug：已强制切换为峰时（debug 测试）。自动检测已暂停 5 分钟，期间不会按真实时段纠正；再次执行 /switcher debug 可重新计时。
```

## 常见问题

- **模型不在 `model_list` 中**：插件只做「提升到首位」，**不会**把模型添加进列表。请先在对应任务 `model_list` 中加入该模型。
- **模型名不匹配**：`peak_model` / `offpeak_model` 必须与 `[[models]]` 下 `name` 字段**完全一致**（含空格、括号、大小写）。
- **修改不生效**：检查 `model_file.model_config_path` 指向是否正确；确认 `[[models]]` 与 `[model_task_config]` 非空；确认 `selection_strategy` 为 `"sequential"`。
- **想临时停用**：WebUI 关闭 `plugin.enabled`，或把某任务 `peak_model`/`offpeak_model` 都留空。
- **debug 后想立即切回真实状态**：再执行一次 `/switcher debug`（翻转回原状态），或等静默窗口结束后调度器自动按真实时段纠正；也可在 WebUI 保存一次插件配置触发热重载（会清除静默窗口）。
- **静默窗口时长如何调整**：改 `plugin.debug_pause_minutes`（分钟），保存配置后热重载生效；静默期内再次 debug 会按新值重新计时。

## 版本历史

| 版本 | 变更 |
|------|------|
| 1.0.0 | 初始版本：峰谷定时切换、`/llmlist` 图片报表、TOML 格式保真、自动备份 |
| 1.1.0 | 新增 `/switcher debug` 命令（仅管理员）；`admin_users` 插件自管管理员；`llmlist_admin_only` 开关；旧配置自动兼容 |
| 1.1.1 | `/switcher debug` 新增静默窗口：调用后自动检测（安全网）暂停 `debug_pause_minutes` 分钟（默认 5，可配置），静默期内再次调用重新计时、不叠加 |

## 文件结构

```
cateye_model_switcher/
├── _manifest.json      # 插件清单（Manifest v2）
├── plugin.py           # SDK 插件入口（生命周期、调度器、配置模型、/llmlist 与 /switcher debug 命令；依赖 maibot_sdk）
├── switcher_core.py    # 核心逻辑（时段解析、TOML 读写、任务切换、任务清单定义；纯 Python，不依赖 SDK，可独立测试）
├── report_renderer.py  # 任务模型列表 HTML 报表渲染（/llmlist 用；不依赖 SDK）
├── __init__.py
├── README.md
└── LICENSE             # MIT
```

## 开发与测试

本插件为标准 **MaiBot SDK 插件**（基于 `maibot-plugin-sdk`，使用 `MaiBotPlugin` / `PluginConfigBase` / `Field` / `Command` 等 SDK 组件；配置模型、生命周期、命令注册均由 SDK 提供）。

- **`switcher_core.py`**：核心逻辑（时段解析、TOML 读写、任务切换、任务清单定义）**不依赖 SDK**，是纯 Python 模块，便于离线单元测试；
- **`report_renderer.py`**：HTML 报表渲染，同样不依赖 SDK；
- **`plugin.py`**：SDK 插件入口，依赖 `maibot_sdk`（由 MaiBot Runner 提供），本地开发需先安装 SDK 才能导入。

```bash
pip install maibot-plugin-sdk tomlkit   # 本地开发依赖

python test/test_model_switcher.py        # 核心逻辑单元测试（时段/切换/TOML 往返/空值容错，不依赖 SDK）
python test/test_model_switcher_e2e.py    # 端到端模拟（峰→谷→峰，inode 检查，不依赖 SDK）
python test/test_report_renderer.py       # 报表渲染单元测试（不依赖 SDK）
python test/test_plugin_integration.py    # 插件类集成（stub SDK，无需真实 SDK 环境）
```
