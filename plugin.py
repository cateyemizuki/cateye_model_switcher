"""峰谷模型切换插件 — MaiBot v2 插件入口。

功能：
- 根据北京时间（UTC+8）峰谷时段，定时调整 MaiBot `model_config.toml` 中
  replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji
  任务的 model_list 顺序，把当前时段指定的模型提升到列表首位（优先使用），
  从而在不重启服务的前提下平衡模型成本与性能。
- 启动时立即检查一次并应用；之后每 60 秒检查一次，时段变化（峰↔谷）才触发写文件。
- vlm / voice / embedding 任务不受影响。
- 修改使用 tomlkit 解析并原地写回，保留注释与空行、保持行尾风格、保持 inode
  （避免 MaiBot FileWatcher 因替换文件而丢失监听）。
- 容错：model_config.toml 或插件配置格式错误时捕获异常并记录日志，不使主程序崩溃。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, ClassVar, Dict, Iterable, Optional

from maibot_sdk import (
    Command,
    CONFIG_RELOAD_SCOPE_SELF,
    Field,
    MaiBotPlugin,
    ON_MODEL_CONFIG_RELOAD,
    PluginConfigBase,
)

from .report_renderer import build_report_html
from .switcher_core import (
    TZ,
    Schedule,
    TASK_KEYS,
    apply_phase_to_doc,
    backup_file,
    build_schedule,
    read_model_config,
    write_model_config,
)

# ==================== 常量 ====================

# 配置版本（config_version）：与 _manifest.json 的 version 保持同步。
SUPPORTED_CONFIG_VERSION = "1.0.0"

# 数据目录子文件夹名（data/plugins/cateye_model_switcher）。
DATA_DIR_NAME = "cateye_model_switcher"

# 插件默认模型配置文件路径：<MaiBot根目录>/config/model_config.toml
# 插件目录位于 <MaiBot根目录>/plugins/<plugin_id>/，故相对路径为 ../../config/model_config.toml
DEFAULT_MODEL_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "model_config.toml")
)

# 默认峰时时段（北京时间）：周一至周五 09:00-12:00、14:00-18:00
DEFAULT_PEAK_PERIODS = ["09:00-12:00", "14:00-18:00"]
# 默认排除星期：周六（6）、周日（7）
DEFAULT_EXCLUDE_WEEKDAYS = [6, 7]

# 检查周期（秒）
CHECK_INTERVAL = 60


# ==================== 配置模型 ====================


class ScheduleSectionConfig(PluginConfigBase):
    __ui_label__ = "峰谷时段"
    __ui_icon__ = "schedule"
    __ui_order__ = 1

    peak_periods: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PEAK_PERIODS),
        description="峰时时段列表（北京时间 HH:MM-HH:MM，支持跨天；例：09:00-12:00）",
    )
    offpeak_periods: list[str] = Field(
        default_factory=list,
        description="谷时时段列表（可选，留空则自动取峰时之外的所有时间；格式同峰时）",
    )
    exclude_weekdays: list[int] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_WEEKDAYS),
        description="排除峰时的星期（1=周一 ... 7=周日；如 [6,7] 表示周六日不执行峰时切换）",
    )


class TaskMappingSectionConfig(PluginConfigBase):
    """任务模型映射（每个任务的峰/谷模型为独立标量配置项）。

    注意：MaiBot WebUI 配置表单不支持嵌套 PluginConfigBase 对象（会显示 [object Object]），
    因此这里使用平铺的标量字段：<task>_peak_model / <task>_offpeak_model，
    每个字段在 WebUI 中都是独立的文本输入框。
    """

    __ui_label__ = "任务模型映射"
    __ui_icon__ = "swap_horiz"
    __ui_order__ = 1

    replyer_peak_model: str = Field(
        default="",
        description="回复任务：峰时使用的模型名（须与 model_config.toml 的 [[models]].name 完全一致；留空则跳过该任务）",
    )
    replyer_offpeak_model: str = Field(
        default="",
        description="回复任务：谷时使用的模型名（须与 model_config.toml 的 [[models]].name 完全一致；留空则跳过该任务）",
    )
    planner_peak_model: str = Field(
        default="",
        description="规划任务：峰时使用的模型名（留空则跳过该任务）",
    )
    planner_offpeak_model: str = Field(
        default="",
        description="规划任务：谷时使用的模型名（留空则跳过该任务）",
    )
    memory_peak_model: str = Field(
        default="",
        description="记忆任务：峰时使用的模型名（留空则跳过该任务）",
    )
    memory_offpeak_model: str = Field(
        default="",
        description="记忆任务：谷时使用的模型名（留空则跳过该任务）",
    )
    mid_memory_peak_model: str = Field(
        default="",
        description="聊天回想任务：峰时使用的模型名（留空则跳过该任务）",
    )
    mid_memory_offpeak_model: str = Field(
        default="",
        description="聊天回想任务：谷时使用的模型名（留空则跳过该任务）",
    )
    utils_peak_model: str = Field(
        default="",
        description="工具任务：峰时使用的模型名（留空则跳过该任务）",
    )
    utils_offpeak_model: str = Field(
        default="",
        description="工具任务：谷时使用的模型名（留空则跳过该任务）",
    )
    learner_peak_model: str = Field(
        default="",
        description="学习任务：峰时使用的模型名（留空则跳过该任务）",
    )
    learner_offpeak_model: str = Field(
        default="",
        description="学习任务：谷时使用的模型名（留空则跳过该任务）",
    )
    expression_use_peak_model: str = Field(
        default="",
        description="表达方式使用任务：峰时使用的模型名（留空则跳过该任务）",
    )
    expression_use_offpeak_model: str = Field(
        default="",
        description="表达方式使用任务：谷时使用的模型名（留空则跳过该任务）",
    )
    emoji_peak_model: str = Field(
        default="",
        description="表情包任务：峰时使用的模型名（留空则跳过该任务）",
    )
    emoji_offpeak_model: str = Field(
        default="",
        description="表情包任务：谷时使用的模型名（留空则跳过该任务）",
    )


class ModelFileSectionConfig(PluginConfigBase):
    __ui_label__ = "模型配置文件"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    model_config_path: str = Field(
        default=DEFAULT_MODEL_CONFIG_PATH,
        description="MaiBot model_config.toml 路径（默认 <MaiBot根目录>/config/model_config.toml，可覆盖为绝对路径）",
    )
    backup: bool = Field(
        default=True,
        description="修改前是否备份 model_config.toml 到 data/plugins/cateye_model_switcher/backup",
    )
    backup_keep: int = Field(
        default=10,
        description="备份保留份数",
    )


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步，用于检查配置文件是否需要更新）",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
        },
    )


class CateyeModelSwitcherConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    task_mapping: TaskMappingSectionConfig = Field(default_factory=TaskMappingSectionConfig)
    model_file: ModelFileSectionConfig = Field(default_factory=ModelFileSectionConfig)


# ==================== 插件主体 ====================


class CateyeModelSwitcherPlugin(MaiBotPlugin):
    """峰谷模型切换插件。"""

    config_model = CateyeModelSwitcherConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("model",)

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._schedule: Schedule = Schedule()
        self._schedule_bad: list[str] = []
        self._task_models: Dict[str, Dict[str, str]] = {}
        self._current_phase: Optional[str] = None  # "peak" / "offpeak"
        self._last_written_phase: Optional[str] = None  # 自己最近一次写入的时段
        self._last_written_at: Optional[float] = None  # 自己最近一次写入的时间戳
        self._scheduler_task: Optional[asyncio.Task] = None

    # ==================== 配置读取 ====================

    def _get_model_config_path(self) -> str:
        path = str(self.config.model_file.model_config_path or "").strip()
        if not path:
            path = DEFAULT_MODEL_CONFIG_PATH
        return os.path.normpath(path)

    def _get_data_dir(self) -> str:
        plugins_root = os.path.dirname(str(self.ctx.paths.data_dir))  # .../data/plugins
        return os.path.join(plugins_root, DATA_DIR_NAME)

    def _load_task_models(self) -> Dict[str, Dict[str, str]]:
        """把配置 [task_mapping] 转为 {task: {"peak": ..., "offpeak": ...}}。

        兼容三种形态：
        - 新版平铺：task_mapping.<task>_peak_model / <task>_offpeak_model（WebUI 独立配置项）；
        - 旧版嵌套：task_mapping.<task> 为含 peak_model/offpeak_model 属性的对象；
        - 旧版 dict/字符串：{"peak": ..., "offpeak": ...} 或单字符串（峰谷同模型）。
        """
        mapping = self.config.task_mapping
        result: Dict[str, Dict[str, str]] = {}
        for task in TASK_KEYS:
            entry: Dict[str, str] = {}
            # 新版：平铺标量字段 <task>_peak_model / <task>_offpeak_model
            has_peak = hasattr(mapping, f"{task}_peak_model")
            has_offpeak = hasattr(mapping, f"{task}_offpeak_model")
            if has_peak or has_offpeak:
                peak = getattr(mapping, f"{task}_peak_model", None)
                offpeak = getattr(mapping, f"{task}_offpeak_model", None)
                entry["peak"] = str(peak or "").strip()
                entry["offpeak"] = str(offpeak or "").strip()
                # 峰谷均留空 → 空映射（该任务静默跳过）
                if not entry["peak"] and not entry["offpeak"]:
                    entry = {}
            else:
                # 旧版：嵌套对象
                raw = getattr(mapping, task, None)
                peak2 = getattr(raw, "peak_model", None)
                offpeak2 = getattr(raw, "offpeak_model", None)
                if peak2 is not None or offpeak2 is not None:
                    entry["peak"] = str(peak2 or "").strip()
                    entry["offpeak"] = str(offpeak2 or "").strip()
                    if not entry["peak"] and not entry["offpeak"]:
                        entry = {}
                elif isinstance(raw, dict):
                    for key in ("peak", "offpeak"):
                        val = raw.get(key)
                        if isinstance(val, str):
                            entry[key] = val.strip()
                        elif val is not None:
                            entry[key] = str(val).strip()
                elif isinstance(raw, str) and raw.strip():
                    val = raw.strip()
                    entry = {"peak": val, "offpeak": val}
            result[task] = entry
        return result

    # ==================== 时段切换 ====================

    def _build_schedule_from_config(self) -> None:
        cfg = self.config.schedule
        schedule, bad = build_schedule(
            list(cfg.peak_periods or []),
            list(cfg.offpeak_periods or []),
            list(cfg.exclude_weekdays or []),
        )
        self._schedule = schedule
        self._schedule_bad = bad
        for msg in bad:
            self.ctx.logger.warning("峰谷时段配置无效：%s", msg)

    def _get_current_phase(self, dt=None) -> str:
        """返回当前时段："peak" / "offpeak"。"""
        now = dt or datetime.now(TZ)
        return "peak" if self._schedule.is_peak(now) else "offpeak"

    def _describe_phase(self, phase: str) -> str:
        return "峰时" if phase == "peak" else "谷时"

    # ==================== 核心执行 ====================

    async def _check_and_apply(self, force: bool = False) -> bool:
        """检查当前时段；若时段发生变化（或 force）则应用切换。全程容错。

        返回是否实际写入了 model_config.toml（True 表示发生了文件修改，
        可能触发 MaiBot 热重载）。
        """
        try:
            phase = self._get_current_phase()
        except Exception as e:
            self.ctx.logger.error("判断当前时段失败：%s", e)
            return False
        if not force and phase == self._current_phase:
            return False

        model_config_path = self._get_model_config_path()
        if not os.path.isfile(model_config_path):
            self.ctx.logger.warning(
                "model_config.toml 不存在（%s），本次跳过切换；请检查 model_file.model_config_path 配置",
                model_config_path,
            )
            return False

        doc, raw, repaired = read_model_config(model_config_path)
        if doc is None:
            self.ctx.logger.error(
                "model_config.toml 解析失败（含 TOML 非法空值行且修复无效），本次跳过切换：%s",
                model_config_path,
            )
            return False
        if repaired:
            self.ctx.logger.warning(
                "model_config.toml 含 TOML 非法空值行（如 api_key = ），已临时补为 \"\" 后解析；建议在 WebUI 补全密钥"
            )

        self._task_models = self._load_task_models()
        result = apply_phase_to_doc(doc, phase, self._task_models)

        for msg in result.warnings:
            self.ctx.logger.warning(msg)

        if result.changed == 0:
            # 无实际变更：仅更新状态，不写文件（也避免无谓触发热重载）。
            # 不打印 info（避免每次外部热重载后刷屏），仅 debug 级记录。
            self.ctx.logger.debug(
                "当前为%s，无任务需要调整（或指定模型均已就位/缺失），不修改 model_config.toml",
                self._describe_phase(phase),
            )
            self._current_phase = phase
            self._last_written_phase = None  # 未写文件，清除"自己写入"标记
            self._last_written_at = None
            return False

        for msg in result.infos:
            self.ctx.logger.info(msg)

        # 修改前备份（可选）
        if self.config.model_file.backup:
            data_dir = self._get_data_dir()
            backup_dir = os.path.join(data_dir, "backup")
            try:
                os.makedirs(backup_dir, exist_ok=True)
            except Exception as e:
                self.ctx.logger.warning("创建备份目录失败：%s", e)
            backup_path = backup_file(model_config_path, backup_dir, int(self.config.model_file.backup_keep or 10))
            if backup_path:
                self.ctx.logger.info("已备份原配置到 %s", backup_path)

        # 字节级对比：若序列化结果与磁盘一致则跳过写入（避免无谓热重载）
        if not write_model_config(model_config_path, doc, raw, skip_if_same=True):
            self.ctx.logger.error(
                "写入 model_config.toml 失败（%s），本次切换未生效；请检查文件权限",
                model_config_path,
            )
            return False

        self._current_phase = phase
        self._last_written_phase = phase  # 记录"自己写入的时段"，用于区分外部改动
        self._last_written_at = time.time()
        self.ctx.logger.info(
            "已切换为%s：%d 个任务调整了 model_list",
            self._describe_phase(phase),
            result.changed,
        )
        return True

    # ==================== 调度器 ====================

    def _start_scheduler(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("插件已禁用，不启动峰谷切换调度器")
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def _stop_scheduler(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        # 启动后立即检查一次并应用当前时段
        try:
            await self._check_and_apply(force=True)
        except Exception as e:
            self.ctx.logger.error("启动时峰谷切换检查失败：%s", e)
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            try:
                await self._check_and_apply()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ctx.logger.error("峰谷切换调度异常：%s", e)

    # ==================== 命令：/llmlist ====================

    @Command(
        "llmlist",
        description="查看所有任务的模型列表（以图片输出）",
        pattern=r"^/?llmlist\s*$",
    )
    async def cmd_llmlist(self, **kwargs: Any) -> tuple[bool, str, int]:
        """读取 model_config.toml，生成任务模型列表报表图片并发送。

        Command 返回值必须为三元组 (success, response, weight)。
        """
        stream_id = str(kwargs.get("stream_id") or "")
        try:
            model_config_path = self._get_model_config_path()
            doc, _, _ = read_model_config(model_config_path)
            if doc is None:
                await self._send_llmlist_fallback(
                    stream_id, "model_config.toml 解析失败，无法生成模型列表图片。"
                )
                return False, "model_config.toml 解析失败", 1

            html_content = build_report_html(doc)
            result = await self.ctx.render.html2png(
                html_content,
                viewport={"width": 360},  # 与 HTML 内容宽度一致，避免右侧裁切与空白
                full_page=True,
                omit_background=False,
            )
            image_base64 = result.get("image_base64") if isinstance(result, dict) else None
            if not image_base64:
                self.ctx.logger.error(
                    "html2png 未返回 image_base64：%s", result if isinstance(result, dict) else type(result)
                )
                await self._send_llmlist_fallback(stream_id, "图片渲染失败，请查看插件日志。")
                return False, "图片渲染失败", 1

            # 图片作为单条消息用合并转发发出（收敛为一条转发气泡）
            await self._send_llmlist_image(stream_id, image_base64)
            return True, "已发送任务模型列表图片", 2
        except Exception as e:
            self.ctx.logger.error("生成任务模型列表图片失败：%s", e)
            try:
                await self._send_llmlist_fallback(stream_id, f"生成失败：{e}")
            except Exception:
                pass
            return False, f"生成失败：{e}", 1

    async def _send_llmlist_image(self, stream_id: str, image_base64: str) -> None:
        """把报表图片作为单条消息用合并转发发出（收敛为一条转发气泡）。

        消息格式与社区已验证用法一致：单条消息 {"user_id": "0", "nickname": ...,
        "segments": [...]}；图片段为 {"type": "image", "content": base64}。
        合并转发失败（平台不支持等）时回退为普通图片发送。
        """
        if not stream_id:
            self.ctx.logger.warning("llmlist 无 stream_id，跳过发送")
            return
        messages = [
            {
                "user_id": "0",
                "nickname": "任务模型列表",
                "segments": [{"type": "image", "content": image_base64}],
            },
        ]
        try:
            await self.ctx.send.forward(messages, stream_id)
        except Exception as e:
            self.ctx.logger.warning("合并转发图片失败，回退为普通图片：%s", e)
            try:
                await self.ctx.send.image(image_base64, stream_id)
            except Exception as e2:
                self.ctx.logger.warning("发送图片失败：%s", e2)

    async def _send_llmlist_fallback(self, stream_id: str, text: str) -> None:
        """渲染失败时发送纯文本提示（尽力而为，不抛异常）。"""
        try:
            if stream_id:
                await self.ctx.send.text(text, stream_id)
            else:
                self.ctx.logger.warning("llmlist 失败且无 stream_id，未发送提示：%s", text)
        except Exception as e:
            self.ctx.logger.warning("发送 llmlist 失败提示出错：%s", e)

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        self._build_schedule_from_config()
        self._task_models = self._load_task_models()
        self._start_scheduler()
        self.ctx.logger.info(
            "峰谷模型切换插件已加载：模型配置文件 %s，当前为%s",
            self._get_model_config_path(),
            self._describe_phase(self._get_current_phase()),
        )

    async def on_unload(self) -> None:
        self._stop_scheduler()
        self.ctx.logger.info("峰谷模型切换插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            # 配置热重载：重建时段与任务映射，重启调度器并立即应用
            self._stop_scheduler()
            self._current_phase = None
            self._last_written_phase = None
            self._last_written_at = None
            self._build_schedule_from_config()
            self._task_models = self._load_task_models()
            self._start_scheduler()
            self.ctx.logger.info("峰谷模型切换插件配置已更新，已重启调度器并应用当前时段")
        elif scope == ON_MODEL_CONFIG_RELOAD:
            # model_config.toml 被热重载。若这是插件自己写入后（10 秒窗口内）触发的热重载回执，
            # 说明磁盘内容就是插件写出的状态，无需重新应用，避免"写→热重载→再写"死循环刷屏。
            # 只有外部改动（WebUI/手动编辑）导致的内容变化才需要重新评估。
            own_reload = (
                self._last_written_phase is not None
                and self._last_written_at is not None
                and (time.time() - self._last_written_at) < 10.0
            )
            if own_reload:
                # 自己触发的热重载：状态已是最新，仅清除写入标记，静默处理
                self._last_written_phase = None
                self._last_written_at = None
                return
            # 外部改动：刷新内部状态，下次调度按需重新应用。
            # 不打印日志：框架原生会在配置更新时自动重载并打印，插件不重复打印。
            self._last_written_phase = None
            self._last_written_at = None
            self._current_phase = None


def create_plugin() -> CateyeModelSwitcherPlugin:
    return CateyeModelSwitcherPlugin()
