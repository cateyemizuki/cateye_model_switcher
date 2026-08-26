"""峰谷模型切换插件 — MaiBot 插件入口。

按北京时间（UTC+8）峰谷时段，定时调整 MaiBot ``model_config.toml`` 中
replyer / planner / memory / mid_memory / utils / learner / expression_use / emoji
任务的 ``model_list`` 顺序：把当前时段指定的模型提升到首位（优先使用），
在不重启服务的前提下平衡模型成本与性能。vlm / voice / embedding 任务不受影响。

关键设计：
- 每 60 秒检查一次时段，仅峰↔谷变化（或启动/配置热重载时）才写文件；
- 用 tomlkit 原地写回，保留注释、空行、行尾风格与 inode（避免 FileWatcher 丢失监听）；
- 字节级对比 + 自己写入回执识别，避免「写文件 → 热重载 → 再写」死循环刷屏；
- 全程容错，配置文件缺失/损坏时仅记录日志，不影响主程序。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, ClassVar, Dict, Iterable, Optional

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    MaiBotPlugin,
    ON_MODEL_CONFIG_RELOAD,
    PluginConfigBase,
)
from pydantic import create_model

from .report_renderer import build_report_html
from .switcher_core import (
    TASKS,
    TZ,
    Schedule,
    apply_phase_to_doc,
    backup_file,
    build_schedule,
    read_model_config,
    write_model_config,
)

# ==================== 常量 ====================

# 配置版本：与 _manifest.json 的 version 保持同步。
# 1.0.0 → 1.1.0：新增 llmlist_admin_only 开关与 /switcher debug 命令。
# 1.1.0 → 1.1.1：/switcher debug 新增静默窗口（debug_pause_minutes，自动检测安全网暂停）。
# 1.1.1 → 1.1.2：model_config_path 默认改为空（不再把绝对路径固化进配置）；
#                路径解析改为从插件所在目录出发的相对推导，MaiBot 目录迁移后无需改配置。
# 旧版本配置文件在加载时自动补齐新字段，无需手动迁移。
SUPPORTED_CONFIG_VERSION = "1.1.2"

# 默认 model_config.toml 路径：<MaiBot根目录>/config/model_config.toml
# 插件目录位于 <MaiBot根目录>/plugins/<plugin_id>/，故相对插件目录向上两级。
# 注意：仅在 model_file.model_config_path 留空时使用；每次调用时动态计算，
# 避免插件目录在导入期之后被移动（如整目录迁移）导致路径失效。
def _default_model_config_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "model_config.toml")
    )


# 默认 bot_config.toml 路径：<MaiBot根目录>/config/bot_config.toml
DEFAULT_BOT_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "bot_config.toml")
)

# 默认峰时时段（北京时间）：周一至周五 09:00-12:00、14:00-18:00
DEFAULT_PEAK_PERIODS = ["09:00-12:00", "14:00-18:00"]
# 默认排除峰时的星期：周六（6）、周日（7）
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


def _build_task_mapping_config() -> type[PluginConfigBase]:
    """动态生成「任务模型映射」配置类。

    每个任务使用两个平铺标量字段 ``<task>_peak_model`` / ``<task>_offpeak_model``
    （WebUI 配置表单不支持嵌套 PluginConfigBase 对象，故用平铺字段）。
    用 create_model 生成以避免手写 16 个重复字段定义；
    WebUI 元数据（__ui_*）在类创建后以类属性方式设置，避免被当作字段。
    """
    fields: Dict[str, Any] = {}
    for task, label in TASKS:
        fields[f"{task}_peak_model"] = (
            str,
            Field(
                default="",
                description=f"{label}任务：峰时使用的模型名（须与 model_config.toml 的 [[models]].name 完全一致；留空则跳过该任务）",
            ),
        )
        fields[f"{task}_offpeak_model"] = (
            str,
            Field(
                default="",
                description=f"{label}任务：谷时使用的模型名（须与 model_config.toml 的 [[models]].name 完全一致；留空则跳过该任务）",
            ),
        )
    cls = create_model("TaskMappingConfig", __base__=PluginConfigBase, **fields)
    cls.__ui_label__ = "任务模型映射"
    cls.__ui_icon__ = "swap_horiz"
    cls.__ui_order__ = 1
    return cls


TaskMappingSectionConfig = _build_task_mapping_config()


class ModelFileSectionConfig(PluginConfigBase):
    __ui_label__ = "模型配置文件"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    model_config_path: str = Field(
        default="",
        description="MaiBot model_config.toml 路径（留空 = 自动从插件所在目录相对推导 <MaiBot根目录>/config/model_config.toml；填写 = 使用填写的路径，支持绝对或相对路径）",
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
    admin_users: list[str] = Field(
        default_factory=list,
        description="管理员列表（格式：用户ID 或 平台:用户ID，如 \"123456789\" 或 \"qq:123456789\"；留空则无管理员）",
    )
    llmlist_admin_only: bool = Field(
        default=False,
        description="是否限制 /llmlist 命令仅管理员可用（默认关，所有人可用）",
    )
    debug_pause_minutes: int = Field(
        default=5,
        description="调用 /switcher debug 后暂停自动检测（安全网）的分钟数（默认 5；静默期间再次 debug 会重新计时，不叠加）",
    )
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
    config_reload_subscriptions: ClassVar[Iterable[str]] = (ON_MODEL_CONFIG_RELOAD,)

    def __init__(self) -> None:
        super().__init__()
        self._schedule: Schedule = Schedule()
        self._schedule_bad: list[str] = []
        self._task_models: Dict[str, Dict[str, str]] = {}
        self._current_phase: Optional[str] = None  # "peak" / "offpeak"
        self._last_written_phase: Optional[str] = None  # 自己最近一次写入的时段
        self._last_written_at: Optional[float] = None  # 自己最近一次写入的时间戳
        self._scheduler_task: Optional[asyncio.Task] = None
        # debug 静默窗口：调用 /switcher debug 后在此时间戳之前暂停自动检测（安全网）
        self._debug_pause_until: Optional[float] = None

    # ==================== 配置读取 ====================

    def _get_model_config_path(self) -> str:
        """解析 model_config.toml 路径。

        - 配置 `model_file.model_config_path` 填写了路径 → 直接使用（绝对或相对均按填写值，
          相对路径以当前工作目录为基准，建议填写绝对路径）；
        - 留空 → 从插件所在目录出发相对推导：<插件目录>/../../config/model_config.toml
          （即 <MaiBot根目录>/config/model_config.toml，插件目录移动后依然正确）。
        """
        path = str(self.config.model_file.model_config_path or "").strip()
        return os.path.normpath(path) if path else _default_model_config_path()

    def _get_data_dir(self) -> str:
        # ctx.paths.data_dir = data/plugins/<plugin_id>；备份统一放其 backup 子目录
        return os.path.join(str(self.ctx.paths.data_dir), "backup")

    def _load_task_models(self) -> Dict[str, Dict[str, str]]:
        """把配置 [task_mapping] 转为 {task: {"peak": ..., "offpeak": ...}}。

        只读取平铺字段 <task>_peak_model / <task>_offpeak_model（配置模型的唯一形态）。
        """
        mapping = self.config.task_mapping
        result: Dict[str, Dict[str, str]] = {}
        for task, _ in TASKS:
            peak = str(getattr(mapping, f"{task}_peak_model", "") or "").strip()
            offpeak = str(getattr(mapping, f"{task}_offpeak_model", "") or "").strip()
            if peak or offpeak:
                result[task] = {"peak": peak, "offpeak": offpeak}
            else:
                result[task] = {}  # 峰谷均留空 → 空映射（该任务静默跳过）
        return result

    # ==================== 权限校验 ====================

    def _is_admin(self, user_id: str) -> bool:
        """判断 user_id 是否为插件配置的管理员。

        配置格式支持两种：
        - 纯用户 ID：如 "123456789"；
        - 平台前缀：如 "qq:123456789"（此时也校验纯 ID 匹配）。
        """
        uid = str(user_id or "").strip()
        if not uid:
            return False
        admins = [str(x).strip() for x in (self.config.plugin.admin_users or []) if str(x).strip()]
        if not admins:
            return False
        if uid in admins:
            return True
        # 兼容 "平台:ID" 形式：仅比较 ID 部分
        return any(uid == a.split(":", 1)[-1] for a in admins if ":" in a)

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

    def _get_current_phase(self, dt: datetime | None = None) -> str:
        """返回当前时段："peak" / "offpeak"。"""
        now = dt or datetime.now(TZ)
        return "peak" if self._schedule.is_peak(now) else "offpeak"

    @staticmethod
    def _describe_phase(phase: str) -> str:
        return "峰时" if phase == "peak" else "谷时"

    # ==================== 核心执行 ====================

    async def _check_and_apply(self, force: bool = False, phase_override: Optional[str] = None) -> bool:
        """检查当前时段；若时段发生变化（或 force）则应用切换。全程容错。

        参数：
        - force：忽略时段缓存，强制重新评估并应用；
        - phase_override：强制使用指定时段（"peak"/"offpeak"），用于 /switcher debug 测试，
          不走时间判断，直接应用该时段对应的模型（会同步更新内部缓存）。

        返回是否实际写入了 model_config.toml（True 表示发生了文件修改，
        可能触发 MaiBot 热重载）。
        """
        if phase_override is not None:
            phase = phase_override
        else:
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
            # 无实际变更：仅更新状态，不写文件（避免无谓触发热重载）。
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
            backup_path = backup_file(
                model_config_path,
                self._get_data_dir(),
                int(self.config.model_file.backup_keep or 10),
            )
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
            # debug 静默窗口内：暂停自动检测（安全网关闭），不按真实时间纠正
            if self._in_debug_pause():
                continue
            try:
                await self._check_and_apply()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ctx.logger.error("峰谷切换调度异常：%s", e)

    def _in_debug_pause(self) -> bool:
        """是否处于 debug 静默窗口（自动检测安全网已暂停）。"""
        return self._debug_pause_until is not None and time.time() < self._debug_pause_until

    def _reset_debug_pause(self) -> None:
        """调用 /switcher debug 后重设静默窗口（重新计时，不叠加）。"""
        minutes = int(self.config.plugin.debug_pause_minutes or 5)
        self._debug_pause_until = time.time() + max(minutes, 0) * 60

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
        # 开关：仅管理员可用
        if self.config.plugin.llmlist_admin_only and not self._is_admin(str(kwargs.get("user_id") or "")):
            await self._send_llmlist_fallback(stream_id, "权限不足：/llmlist 已设为仅管理员可用。")
            return False, "权限不足：仅管理员可用", 1
        try:
            model_config_path = self._get_model_config_path()
            doc, _, _ = read_model_config(model_config_path)
            if doc is None:
                await self._send_llmlist_fallback(stream_id, "model_config.toml 解析失败，无法生成模型列表图片。")
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

    # ==================== 命令：/switcher debug ====================

    @Command(
        "switcher_debug",
        description="强制切换当前峰谷状态（仅管理员，用于测试配置修改是否正常）",
        pattern=r"^/?switcher\s+debug\s*$",
    )
    async def cmd_switcher_debug(self, **kwargs: Any) -> tuple[bool, str, int]:
        """强制翻转当前峰谷状态并立即应用，用于测试 model_config.toml 修改代码。

        只做状态翻转与应用，不改变时间判断逻辑；切换后立即开启 debug 静默窗口
        （自动检测安全网暂停 debug_pause_minutes 分钟，期间调度器不按真实时间纠正），
        窗口结束后自动恢复自动检测。
        """
        stream_id = str(kwargs.get("stream_id") or "")
        # 强制仅管理员可用
        if not self._is_admin(str(kwargs.get("user_id") or "")):
            await self._send_llmlist_fallback(stream_id, "权限不足：/switcher debug 仅管理员可用。")
            return False, "权限不足：仅管理员可用", 1

        # 当前内部状态（未记录时按真实时间判断）
        current = self._current_phase or self._get_current_phase()
        # 强制翻转：峰 ↔ 谷
        target = "offpeak" if current == "peak" else "peak"
        try:
            wrote = await self._check_and_apply(force=True, phase_override=target)
        except Exception as e:
            self.ctx.logger.error("switcher debug 强制切换失败：%s", e)
            await self._send_llmlist_fallback(stream_id, f"强制切换失败：{e}")
            return False, f"强制切换失败：{e}", 1

        # 重设 debug 静默窗口（重新计时，不叠加）
        self._reset_debug_pause()
        minutes = int(self.config.plugin.debug_pause_minutes or 5)

        phase_desc = self._describe_phase(target)
        if wrote:
            msg = (
                f"已强制切换为{phase_desc}（debug 测试）。"
                f"自动检测已暂停 {minutes} 分钟，期间不会按真实时段纠正；"
                f"再次执行 /switcher debug 可重新计时。"
            )
        else:
            # 未写入：可能模型未配置/缺失，或目标时段本就与当前一致
            msg = (
                f"已强制切换为{phase_desc}，但 model_config.toml 无需修改（可能未配置该时段模型）。"
                f"自动检测已暂停 {minutes} 分钟。"
            )
        await self._send_llmlist_fallback(stream_id, msg)
        self.ctx.logger.info("switcher debug：%s", msg)
        return True, msg, 2

    # ==================== 版本兼容 ====================

    def _check_config_version(self) -> None:
        """检测配置版本并自动兼容旧版配置文件。

        当前版本：1.1.2。旧版本（1.0.0 / 1.1.0 / 1.1.1）的配置文件缺少
        admin_users / llmlist_admin_only / debug_pause_minutes 等字段，
        Runner 在配置注入时已按默认值自动补齐，这里仅做日志提示。
        另注意：1.1.1 及更早版本可能把 model_config_path 固化为绝对路径写入配置，
        若该路径已失效，请在 WebUI 将 model_file.model_config_path 清空以启用相对解析。
        """
        try:
            raw = self.get_plugin_config_data()
            current = str((raw.get("plugin") or {}).get("config_version") or "").strip()
        except Exception:
            return
        if current and current != SUPPORTED_CONFIG_VERSION:
            self.ctx.logger.info(
                "检测到旧版配置（config_version=%s，当前支持 %s），缺失字段已按默认值自动补齐",
                current,
                SUPPORTED_CONFIG_VERSION,
            )

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        self._check_config_version()
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
        self._debug_pause_until = None
        self.ctx.logger.info("峰谷模型切换插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            # 配置热重载：重建时段与任务映射，重启调度器并立即应用
            self._check_config_version()
            self._stop_scheduler()
            self._current_phase = None
            self._last_written_phase = None
            self._last_written_at = None
            self._debug_pause_until = None  # 配置更新后清除 debug 静默窗口
            self._build_schedule_from_config()
            self._task_models = self._load_task_models()
            self._start_scheduler()
            self.ctx.logger.info("峰谷模型切换插件配置已更新，已重启调度器并应用当前时段")
        elif scope == ON_MODEL_CONFIG_RELOAD:
            # model_config.toml 被热重载。若这是插件自己写入后（10 秒窗口内）触发的热重载回执，
            # 说明磁盘内容就是插件写出的状态，无需重新应用，避免「写→热重载→再写」死循环刷屏。
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
