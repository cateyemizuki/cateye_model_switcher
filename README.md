# 峰谷模型切换（MaiBot 插件）

按北京时间（UTC+8）的峰时与谷时，自动调整 MaiBot `model_config.toml` 中各任务的模型顺序：把当前时段指定的模型提升到 `model_list` 首位。任务的 `selection_strategy` 为 `"sequential"`（按列表顺序选择）时，首位即优先使用的模型；高峰时段可用性能更好的模型，低谷时段切回成本更低的模型，切换过程不需要重启 MaiBot。

插件只处理 8 个任务：`replyer`、`planner`、`memory`、`mid_memory`、`utils`、`learner`、`expression_use`、`emoji`。`vlm`、`voice`、`embedding` 不会被改动。

## 使用前须知

- **直接读写配置文件**：本插件直接读写 `<MaiBot根目录>/config/model_config.toml`，不经过宿主配置接口。运行 MaiBot 的进程必须对该文件有读写权限。
- **依赖宿主热重载**：文件写入后，由宿主对 `model_config.toml` 的热重载使新顺序生效。宿主未启用该热重载时，磁盘内容会更新，但运行中的模型选择不会改变。
- **修改前备份，但不自动回滚**：`model_file.backup` 默认开启。备份成功时，原文件会在写入前复制到插件数据目录下的 `backup/` 子目录，日志打印完整路径；备份失败时不会中止写入，也没有单独提示。
- **写入失败可能损坏文件**：文件缺失或解析失败只记录日志并跳过本次切换；写入失败同样只记录错误并返回，不会用备份恢复。写回时先以 `open(..., "w")` 截断再写入，写入或关闭过程中出错可能留下不完整的 `model_config.toml`；若已有备份，可用 `backup/` 中的文件手动恢复，没有备份时只能手工修复或重建配置。
- **停止方式**：在 WebUI 关闭 `plugin.enabled` 或停用插件，即可停止自动改写。已经写入的模型顺序会保持不变。

## 安装

1. 把插件目录放到 `<MaiBot根目录>/plugins/<插件目录>/`，在 WebUI 插件中心安装，或重启 MaiBot。
2. 确认 MaiBot 进程能读写 `config/model_config.toml`，并能创建插件数据目录。
3. 在 `model_config.toml` 中确认参与切换的任务使用 `selection_strategy = "sequential"`。插件只调整 `model_list` 顺序，其他策略下首位模型不保证生效。
4. 在插件配置中为需要的任务填写峰时/谷时模型，保存后插件会按当前时段应用。

依赖与兼容：

- 清单声明 Python 包依赖 `tomlkit >= 0.13.3`，用于保真读写 TOML。代码另外导入 `maibot_sdk` 与 `pydantic`，清单未声明为依赖，需由宿主运行环境提供。
- Manifest v2；清单声明 `host_application` 1.0.0 ~ 1.99.99、`sdk` 2.8.0 ~ 2.99.99。
- 声明能力：`send.text`、`send.image`、`send.forward`、`render.html2png`。

`model_config.toml` 位置：

- `model_file.model_config_path` 留空时，从插件文件所在目录向上两级推导：`<插件目录>/../../config/model_config.toml`，这要求插件目录位于 `<MaiBot根目录>/plugins/` 下。
- 目录布局不同（例如 WebUI 安装到其他位置）时，请把 `model_file.model_config_path` 填为实际文件的绝对路径。

## 工作原理

### 时段判定

所有判断使用北京时间（UTC+8），与宿主系统时区无关。

1. 当天星期命中 `schedule.exclude_weekdays` → 谷时。
2. `schedule.offpeak_periods` 非空 → 峰时 = 不在任何谷时段内，此时 `peak_periods` 不参与判断。
3. `schedule.offpeak_periods` 为空 → 峰时 = 落在任一 `peak_periods` 时段内，其余时间为谷时。

时段格式为 `HH:MM-HH:MM`，左闭右开：`09:00-12:00` 包含 09:00、不包含 12:00。开始时间晚于结束时间视为跨天，例如 `22:00-02:00`。

默认配置下，周一至周五 09:00-12:00、14:00-18:00 为峰时，其余时间（含周末）为谷时。

### 切换规则

- 只处理上文 8 个任务。每个任务的峰时模型与谷时模型都必须在配置中填写，任一为空则该任务整体跳过，不产生日志。
- 模型名必须与 `[[models]]` 的 `name` 完全一致，并且已存在于该任务的 `model_list` 中，否则记录 WARN 并跳过该任务；插件不会把模型加入列表。
- 模型已在首位时不改动；否则从原位置删除并插入到索引 0，其余模型的相对顺序不变。
- 插件只调整 `model_list` 顺序，不修改任务的 `selection_strategy`。要让调整后的首位模型真正被优先使用，对应任务必须设为 `selection_strategy = "sequential"`（按列表顺序选择）；其他策略下顺序调整不保证影响实际选型。

### 触发与写入

- `plugin.enabled` 为开时，插件加载后立即应用一次当前时段，之后每 60 秒检查一次；只有判定时段与当前记录的时段不同才继续。
- 有任务需要调整时：读取 TOML → 调整 `model_list` → 备份 → 与磁盘内容做字节级比较 → 原地写入。写入保留原文件的行尾风格（CRLF/LF）、注释与空行，并直接覆盖同一文件而不替换 inode，避免宿主的文件监听失效。
- 写入成功后记录日志：`已切换为峰时：3 个任务调整了 model_list`。
- 插件写入后 10 秒内收到的 `model_config.toml` 热重载视为自身写入的回执，不再重复处理；其他外部改动会清空内部时段状态，下一次检查按当前时段重新应用。因此手动调整这几个任务的 `model_list` 顺序，可能在下一轮被插件覆盖。

## 配置

配置在 WebUI 插件配置面板中修改。保存后插件会重建时段与任务映射、清除 debug 静默窗口，并按 `enabled` 决定是否启动调度器、立即应用当前时段。

### plugin

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 关闭后不启动自动切换调度器，不会自动改写配置 |
| `admin_users` | `[]` | 管理员列表，支持 `"123456789"` 或 `"qq:123456789"` 两种写法。匹配时只比较冒号后的 ID，平台前缀不参与校验；列表为空表示没有管理员 |
| `llmlist_admin_only` | `false` | 为 `true` 时 `/llmlist` 仅管理员可用 |
| `debug_pause_minutes` | `5` | `/switcher debug` 后暂停自动检测的分钟数。填 `0` 会按 5 处理，负数等于不暂停 |
| `config_version` | `"1.2.0"` | WebUI 中隐藏且不可编辑；与插件内置版本不一致时只打印 INFO 提示，不做字段迁移 |

### schedule

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `peak_periods` | `["09:00-12:00", "14:00-18:00"]` | 峰时时段列表，北京时间。格式非法的项会被忽略并记录 WARN |
| `offpeak_periods` | `[]` | 谷时时段列表。留空时谷时 = 峰时之外的所有时间；填写后峰时完全由谷时段决定，`peak_periods` 不再参与判断 |
| `exclude_weekdays` | `[6, 7]` | 排除峰时的星期，`1`=周一 … `7`=周日。命中的星期整天判为谷时；非法值会被忽略并记录 WARN |

### task_mapping

每个任务有两个独立字段：`<task>_peak_model` 与 `<task>_offpeak_model`，默认均为空字符串。

| 任务键 | 中文名 |
|--------|--------|
| `replyer` | 回复 |
| `planner` | 规划 |
| `memory` | 记忆 |
| `mid_memory` | 聊天回想 |
| `utils` | 工具 |
| `learner` | 学习 |
| `expression_use` | 表达方式使用 |
| `emoji` | 表情包 |

```toml
[task_mapping]
replyer_peak_model = "model-peak"
replyer_offpeak_model = "model-offpeak"
planner_peak_model = "model-peak"
planner_offpeak_model = "model-offpeak"
# 其余任务同理；峰/谷任一留空的任务不参与切换
```

字段值必须与 `model_config.toml` 的 `[[models]].name` 完全一致。未填写的任务不产生任何日志；填了但模型未定义或不在 `model_list` 中会记录 WARN。

### model_file

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `model_config_path` | `""` | 留空时按插件位置推导 `<插件目录>/../../config/model_config.toml`；填写时按填写的值使用，相对路径以进程当前工作目录为基准，建议填绝对路径 |
| `backup` | `true` | 仅在本次确有任务变更时备份。文件名为 `model_config_YYYYmmdd_HHMMSS.toml`（北京时间），按修改时间倒序保留最近 `backup_keep` 份 |
| `backup_keep` | `10` | 备份保留份数。填 `0` 会按 10 处理 |

## 命令

### /llmlist

输入 `/llmlist`（注册名 `llmlist`）查看当前 `model_config.toml` 中各任务的模型列表。

- 读取 `[model_task_config]`，分「动态调整任务」（8 个）与「固定任务」（`vlm` / `voice` / `embedding`）两组展示。
- 每个任务显示模型数量，首位模型带「优先」徽章（固定任务显示「固定」），其余模型以标签列出；`model_list` 为空的显示「未配置」，任务表头注释会作为说明显示。
- 生成 360px 宽的 PNG，优先作为单条合并转发消息发送；合并转发失败时回退为普通图片；解析或渲染失败时发送纯文本提示，渲染异常会记录日志。
- 默认所有人可用；`plugin.llmlist_admin_only = true` 时仅 `admin_users` 可用，拒绝时提示「权限不足：/llmlist 已设为仅管理员可用。」

### /switcher debug

输入 `/switcher debug`（注册名 `switcher_debug`），仅 `admin_users` 中的管理员可用；列表为空时无人可用。

- 以插件记录的当前时段为准（尚未记录时按真实北京时间判断），取相反时段并立即应用；有任务变更时写入 `model_config.toml`。
- 回复当前已切换为峰时还是谷时，并说明是否实际修改了文件。没有任务需要修改时（例如未配置该时段模型，或模型已在首位），回复「已强制切换为谷时，但 model_config.toml 无需修改（可能未配置该时段模型）」。
- 调用后自动检测暂停 `plugin.debug_pause_minutes` 分钟，期间调度器不会按真实时段纠正。静默期内再次执行会重新计时、不叠加；保存插件配置会清除静默窗口；窗口结束后下一轮检查恢复按真实时段切换。
- 非管理员调用时提示「权限不足：/switcher debug 仅管理员可用。」，不会翻转状态。

## 日志示例

```
[INFO] 峰谷模型切换插件已加载：模型配置文件 D:\MaiBot\config\model_config.toml，当前为谷时
[INFO] 已备份原配置到 <插件数据目录>\backup\model_config_20260101_090000.toml
[INFO] 已切换为峰时：3 个任务调整了 model_list
[WARN] 任务 replyer 的峰时模型 "model-peak" 不在其 model_list 中，跳过切换。
[WARN] model_config.toml 含 TOML 非法空值行（如 api_key = ），已临时补为 "" 后解析；建议在 WebUI 补全密钥
```

## 常见问题

- **到点没有切换**：先确认 `plugin.enabled` 为开；再看日志里是否有对应任务的 WARN。某任务峰/谷模型只填了一个、模型名与 `[[models]].name` 不一致、模型不在该任务 `model_list` 中，都会导致该任务被跳过。
- **切换了但模型没变**：确认对应任务的 `selection_strategy` 为 `"sequential"`。插件只调整 `model_list` 顺序、不改该字段，其他策略下首位模型不保证被使用。
- **填写了 `offpeak_periods` 后峰时时段失效**：这是既定行为。谷时列表非空时，峰时 = 非排除日且不在任一谷时段内，`peak_periods` 不再参与判断。
- **周末一直是谷时**：`exclude_weekdays` 默认为 `[6, 7]`，命中时整天判为谷时。需要周末也执行峰时，把它改为空列表或只保留需要的星期。
- **日志提示找不到 `model_config.toml`**：`model_file.model_config_path` 留空时的推导路径是 `<插件目录>/../../config/model_config.toml`。插件不在 `<MaiBot根目录>/plugins/` 下时，请填写绝对路径；`/llmlist` 此时也会提示解析失败。
- **手动改的顺序被改回**：插件在外部热重载后会按当前时段重新应用；需要临时手动调整时，先关闭 `plugin.enabled`。
- **如何恢复备份**：先关闭 `plugin.enabled`，把日志中打印的备份文件复制回 `model_config.toml`，再按需重新启用。
- **debug 后一直没自动纠正**：静默窗口内本就不会自动纠正；等待窗口结束、再执行一次 `/switcher debug` 翻转回来，或在 WebUI 保存一次插件配置以清除静默窗口。
- **命令提示权限不足**：`/switcher debug` 始终要求管理员；`/llmlist` 仅在 `llmlist_admin_only = true` 时要求管理员。确认 `admin_users` 非空，且填写的 ID 与调用者 ID 一致（`平台:ID` 写法只比较冒号后的 ID）。
- **TOML 空值警告**：配置文件里存在 `api_key = ` 这类没有值的行。插件会临时补成 `""` 以保证解析通过，写回时保留该形态；请尽快在 WebUI 补全对应密钥。
- **报表日期与时段状态不在同一天**：`/llmlist` 标题日期取宿主系统本地时间，峰谷判断固定用 UTC+8；服务器时区不是东八区时可能出现差异。

## 文件结构

```
cateye_model_switcher/
├── _manifest.json      # Manifest v2 清单
├── plugin.py           # 插件入口：配置模型、调度器、命令
├── switcher_core.py    # 时段判定、TOML 读写、任务切换、备份
├── report_renderer.py  # /llmlist 报表 HTML
├── __init__.py
├── CHANGELOG.md
├── README.md
└── LICENSE             # MIT
```

## 许可证与仓库

MIT License，作者 cateye。

- 仓库：<https://github.com/cateyemizuki/cateye_model_switcher>
- 问题反馈：<https://github.com/cateyemizuki/cateye_model_switcher/issues>
- 更新记录：[CHANGELOG.md](CHANGELOG.md)
