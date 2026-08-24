# 峰谷模型切换（MaiBot 插件）

根据**北京时间（UTC+8）**的峰谷时段，定时调整 MaiBot `model_config.toml` 中各任务的模型列表顺序：把当前时段（峰时 / 谷时）指定的模型提升到 `model_list` 的**首位**（优先使用），从而在不重启服务的前提下平衡模型成本与性能。

> 例如：工作日白天（峰时）优先使用 `deepseek-v4-pro-think` 保证回复质量；夜间与周末（谷时）自动切回 `[OCG]deepseek-v4-flash-opencode go` 降低成本。

## 功能特性

- **峰谷时段自定义**：峰时支持多个时间段（精确到分钟，支持跨天），谷时默认取峰时之外的所有时间，也可显式指定；支持按星期排除峰时（如排除周六周日）。
- **任务范围限定**：仅调整 `replyer`（回复）、`planner`（规划）、`memory`（记忆）、`mid_memory`（聊天回想）、`utils`（工具）、`learner`（学习）、`expression_use`（表达方式使用）、`emoji`（表情包）八个任务。
- **排除任务**：`vlm`（视觉）、`voice`（语音）、`embedding`（嵌入）任务保持不变。
- **优先切换**：把峰时/谷时指定模型移动到 `model_list` 索引 0，其余模型顺序不变（配合 `selection_strategy = "sequential"` 即优先使用）。
- **静默跳过**：某任务峰/谷模型任一未配置时，该任务全部切换操作静默跳过，不打印任何日志。
- **容错警告**：模型未在 `[[models]]` 中定义或不在任务 `model_list` 中时，输出警告日志并跳过该任务（格式如 `任务 replyer 的峰时模型 "xxx" 不在其 model_list 中，跳过切换。`）。
- **无需重启**：利用 MaiBot 的 `model_config.toml` 文件监听热重载（600ms 防抖自动生效），修改后自动生效。
- **TOML 格式保真**：使用 `tomlkit` 解析/写回，保留注释与空行、保持行尾风格（CRLF/LF）、**原地写入同一文件**（保持 inode，避免 FileWatcher 丢失监听）。
- **自动备份**：每次实际修改前自动备份原文件到插件数据目录（保留最近 N 份）。
- **容错**：`model_config.toml` 或插件配置格式错误时捕获异常并记录日志，不使主程序崩溃。
- **`/llmlist` 命令**：发送 `/llmlist` 以图片形式查看所有任务的模型列表（首模型带「优先」徽章，动态/固定任务分组展示）。

## 安装方式

1. 将本插件目录（含 `_manifest.json`、`plugin.py`、`switcher_core.py` 等文件）放入 MaiBot 的 `plugins/` 目录。
2. 重启 MaiBot，或在 WebUI 插件中心安装。
3. 插件依赖 `tomlkit`，已声明于 `_manifest.json`，Host 会自动安装。

> 兼容性声明：`host_application` `1.0.0 ~ 1.99.99`，`sdk` `2.0.0 ~ 2.99.99`（Manifest v2）。

## 配置说明

插件加载后由 Runner 在插件目录生成 `config.toml`，可在 WebUI 修改：

```toml
[plugin]
enabled = true                # 是否启用插件
config_version = "1.0.0"      # 配置版本（与插件版本同步，UI 中隐藏）

[schedule]
peak_periods = ["09:00-12:00", "14:00-18:00"]   # 峰时时段（北京时间 HH:MM-HH:MM，支持跨天如 "22:00-02:00"）
offpeak_periods = []                              # 谷时时段（可选；留空 = 峰时之外的所有时间）
exclude_weekdays = [6, 7]                         # 排除峰时的星期（1=周一 ... 7=周日；如 [6,7] 表示周六日不执行峰时）

[task_mapping]               # 每个任务的峰/谷模型为独立配置项（16 个）
replyer_peak_model = "deepseek-v4-pro-think"             # 回复任务·峰时模型
replyer_offpeak_model = "[OCG]deepseek-v4-flash-opencode go"   # 回复任务·谷时模型
planner_peak_model = "deepseek-v4-pro-think"
planner_offpeak_model = "[OCG]deepseek-v4-flash-opencode go"
# memory / mid_memory / utils / learner / expression_use / emoji 任务的
# <task>_peak_model / <task>_offpeak_model 同理...
# 全部留空 → 该任务不参与切换

[model_file]
model_config_path = ""        # 留空 = 自动定位 <MaiBot根目录>/config/model_config.toml；可填绝对路径覆盖
backup = true                 # 修改前是否备份
backup_keep = 10              # 备份保留份数
```

> **峰谷独立配置**：`task_mapping` 下每个任务的峰时模型与谷时模型是**两个独立配置项**
> （`<task>_peak_model` 与 `<task>_offpeak_model`），WebUI 中每个字段单独显示、分别填写，
> 不会合并显示。例如表达方式使用任务的完整配置项为
> `task_mapping.expression_use_peak_model` 与 `task_mapping.expression_use_offpeak_model`。

### 配置说明

| 配置项 | 说明 |
|--------|------|
| `plugin.enabled` | 是否启用插件（默认开）。关闭后停止峰谷切换调度 |
| `schedule.peak_periods` | 峰时时段列表，格式 `HH:MM-HH:MM`（开始-结束，精确到分钟；半开区间 [start, end)）。支持跨天（如 `"22:00-02:00"`） |
| `schedule.offpeak_periods` | 谷时时段列表（可选）。留空时谷时 = 峰时之外的所有时间（取反）；填写后峰时 = 非排除日且不在谷时段内 |
| `schedule.exclude_weekdays` | 排除峰时的星期数组，`1`=周一 … `7`=周日。默认 `[6, 7]`（周六日不执行峰时切换，即整天保持谷时） |
| `task_mapping.<任务>_peak_model` | 对应任务峰时使用的模型名，必须与 `model_config.toml` 中 `[[models]]` 的 `name` 完全一致。`<任务>` 为 replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji |
| `task_mapping.<任务>_offpeak_model` | 对应任务谷时使用的模型名，同上 |
| `model_file.model_config_path` | `model_config.toml` 路径。默认自动推导为插件目录上两级 `config/model_config.toml`；如插件不在标准位置，可填绝对路径 |
| `model_file.backup` | 修改前备份到 `data/plugins/cateye_model_switcher/backup/`（默认开） |
| `model_file.backup_keep` | 备份保留份数（默认 10） |

## 使用说明

### ⚠️ 重要：模型选择策略需改为「按顺序优先」

本插件通过把峰时/谷时模型**提升到 `model_list` 首位**来实现优先使用，因此被调整任务的 `selection_strategy` 必须为 **`"sequential"`（按配置顺序优先选择）**，否则提升顺序不会生效（如 `random` 会随机选择、`balance` 会负载均衡）。

请在 `model_config.toml` 中为参与动态调整的任务设置：

```toml
[model_task_config.replyer]
model_list = [ ... ]
selection_strategy = "sequential"   # 必须：按配置顺序优先选择，首位模型优先使用
# planner / memory / mid_memory / utils / learner / expression_use / emoji 同理
```

> 插件不会自动改写 `selection_strategy`（避免干扰其他手动配置），请确保已按上述说明设置。

### 自动化切换

1. **启动时**：立即检查一次当前时段并应用（峰时 → 提升峰时模型，谷时 → 提升谷时模型）。
2. **运行中**：每 60 秒检查一次当前时段；**仅当时段发生变化（峰↔谷）** 时修改 `model_config.toml`。
3. **热重载**：修改后 MaiBot 的 FileWatcher 自动热重载 `model_config.toml`，无需重启服务（插件订阅 `model` 配置热重载，可感知外部改动并刷新内部状态）。

### 防刷屏设计（单次修改）

为避免"插件写文件 → 触发 MaiBot 热重载 → 收到回执后误判为外部改动 → 再次写文件"的死循环导致热重载日志刷屏，插件做了三重保护：

- **仅时段变化才写**：`_current_phase` 缓存当前已应用的时段，`phase == _current_phase` 时直接跳过，同一时段内绝不重复写文件；
- **字节级对比**：写入前将序列化结果与磁盘内容比对（`skip_if_same`），内容一致时跳过写入，避免无谓触发热重载；
- **自己写入回执识别**：插件写入后记录 `_last_written_phase` + 时间戳；收到 `model` 配置热重载回调时，若在 **10 秒窗口内**则视为"自己触发的热重载回执"，**静默处理、不重写**；窗口外或没有写入标记的热重载才视为外部改动，刷新状态后按需重新应用。

因此每次时段切换**只写一次** `model_config.toml`、只触发一次热重载，不会反复写入刷屏日志。此外插件**不打印任何热重载相关日志**——MaiBot 框架原生会在配置更新时自动重载并打印，插件不重复打印。

### 命令：`/llmlist`（查看任务模型列表）

发送 `/llmlist` 即可查看当前所有任务的模型列表，以**图片**形式输出：

- 读取 `model_config.toml` 的 `[model_task_config]`，按 **动态调整任务**（replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji）与 **固定任务**（vlm / voice / embedding）分组展示；
- 每个任务卡片标注模型数量，**首位模型带「优先」徽章**（即当前优先使用的模型），其余模型以标签形式排列；
- 使用 `ctx.render.html2png()` 渲染为 PNG（viewport 宽度 380px，`full_page` 自动按内容高度裁切），作为**单条消息用合并转发**发出（收敛为一条转发气泡，避免刷屏）；合并转发不可用时回退为普通图片发送；
- 渲染失败时回退为纯文本提示并记录日志，不影响其他功能。

### 日志示例

```
[INFO] 峰谷模型切换插件已加载：模型配置文件 D:\MaiBot\config\model_config.toml，当前为谷时
[INFO] 已切换为峰时：3 个任务调整了 model_list
[WARN] 任务 replyer 的峰时模型 "xxx" 不在其 model_list 中，跳过切换。
[WARN] model_config.toml 含 TOML 非法空值行（如 api_key = ），已临时补为 "" 后解析；建议在 WebUI 补全密钥
```

> **关于热重载日志**：插件**不打印**任何与 `model_config.toml` 热重载相关的日志（如"配置已热重载""正在自动重载"等）。MaiBot 框架原生会在配置文件更新时自动重载并打印对应日志，插件不重复打印，避免日志刷屏。

## 常见问题

- **模型不在 `model_list` 中**：插件只做"提升到首位"，**不会**把模型添加进列表。请先在 `model_config.toml` 的对应任务 `model_list` 中加入该模型（或改用已在列表中的模型）。
- **模型名不匹配**：`peak_model` / `offpeak_model` 必须与 `[[models]]` 下 `name` 字段**完全一致**（含空格、括号、大小写）。
- **修改不生效**：检查 `model_file.model_config_path` 是否指向正确文件；确认 `[[models]]` 与 `[model_task_config]` 非空（MaiBot 启动要求非空）。
- **想临时停用**：WebUI 关闭 `plugin.enabled`，或把某任务 `peak_model`/`offpeak_model` 都留空。

## 文件结构

```
cateye_model_switcher/
├── _manifest.json      # 插件清单（Manifest v2）
├── plugin.py           # 插件入口（生命周期、调度器、配置、/llmlist 命令）
├── switcher_core.py    # 核心逻辑（时段解析、TOML 读写、任务切换，可独立测试）
├── report_renderer.py  # 任务模型列表 HTML 报表渲染（/llmlist 用）
├── __init__.py
├── README.md
└── LICENSE             # MIT
```

## 开发与测试

核心逻辑 `switcher_core.py` 不依赖 MaiBot SDK，可直接单元测试：

```bash
python test/test_model_switcher.py        # 单元测试（时段/切换/TOML 往返/空值容错）
python test/test_model_switcher_e2e.py    # 端到端模拟（峰→谷→峰，inode 检查）
python test/test_plugin_integration.py    # 插件类集成（stub SDK）
```
