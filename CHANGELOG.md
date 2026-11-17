# 更新日志

## 1.2.0

适配 MaiBot 1.3.0 / 新版插件市场规范（面向上架发布）：

- 全部配置项（含 16 个任务映射字段）补充英文 i18n（`json_schema_extra.i18n["en"]`），全部配置分组补充 `__ui_i18n__`；WebUI 界面语言为英文时显示英文标签，中文行为不变；
- `_manifest.json`：版本升至 1.2.0；`description` 移除"不上传插件市场"声明并保留直接读写 `model_config.toml` 的风险提示；`sdk.min_version` 修正为 `2.8.0`（实际最低，兼容 1.2.x 宿主自带的 2.8.1）；`i18n.supported_locales` 增加 `en`；新增 `changelog` 字段指向 `CHANGELOG.md`；
- README 移除"非规范声明"，改为工作原理与风险提示，同步兼容区间与版本历史（补记 1.1.3）。

## 1.1.3

为全部配置项补充/完善了用户友好的中文注释与说明（悬停提示），完善配置节说明；插件功能与行为不变。

## 1.1.2

- `model_file.model_config_path` 默认改为空，不再把绝对路径固化进配置；
- 路径解析改为从插件所在目录出发的相对推导（`<插件目录>/../../config/model_config.toml`），修复 MaiBot 目录迁移后旧路径失效的问题；
- 升级后请在 WebUI 将 `model_config_path` 清空以启用相对解析。

## 1.1.1

- `/switcher debug` 新增静默窗口：调用后自动检测（安全网）暂停 `debug_pause_minutes` 分钟（默认 5，可配置），静默期内再次调用重新计时、不叠加。

## 1.1.0

- 新增 `/switcher debug` 命令（仅管理员）；
- `admin_users` 插件自管管理员；
- `llmlist_admin_only` 开关；
- 旧配置自动兼容。

## 1.0.0

- 初始版本：峰谷定时切换、`/llmlist` 图片报表、TOML 格式保真、自动备份。
