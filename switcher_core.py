"""峰谷模型切换插件 — 核心逻辑（不依赖 MaiBot SDK，便于单元测试）。

职责：
- 北京时间（UTC+8）峰/谷时段解析与判断；
- model_config.toml 的读取（含空值容错）、原地写回（保持 inode 与行尾风格）、修改前备份；
- 按任务把峰时/谷时指定的模型提升到 model_list 首位。

约定：
- 时段格式 "HH:MM-HH:MM"，支持跨天（如 "22:00-02:00"），开始时间 ≤ 结束时间视为同一自然日；
- 星期：1=周一 ... 7=周日；
- 峰/谷模型任一未配置的任务一律静默跳过；
- model_config.toml 若含 TOML 非法空值行（如 `api_key = `，常见于手动抹除密钥），
  读取时会将其临时补为 `api_key = ""` 再解析，保证切换照常工作（写回时保留该合法形态）。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import tomlkit

# 北京时间
TZ = timezone(timedelta(hours=8))

# 参与动态调整的任务（vlm / voice / embedding 保持不变）：
# (model_task_config 键, 中文名)，供切换逻辑与报表渲染共用。
TASKS: Tuple[Tuple[str, str], ...] = (
    ("replyer", "回复"),
    ("planner", "规划"),
    ("memory", "记忆"),
    ("mid_memory", "聊天回想"),
    ("utils", "工具"),
    ("learner", "学习"),
    ("expression_use", "表达方式使用"),
    ("emoji", "表情包"),
)

# 不参与动态调整的固定任务（仅报表展示用）
FIXED_TASKS: Tuple[Tuple[str, str], ...] = (
    ("vlm", "视觉"),
    ("voice", "语音"),
    ("embedding", "嵌入"),
)

# 任务键（供旧测试/兼容引用）
TASK_KEYS: Tuple[str, ...] = tuple(k for k, _ in TASKS)

# 匹配 "HH:MM"
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
# 匹配 TOML 空值行：`key = ` 后直接换行（值被手动抹除留下的非法形态）
# 注意：不能用 `\r?\n$`（re.M 下 $ 与换行消费冲突），用显式换行结尾。
_EMPTY_VALUE_LINE_RE = re.compile(r"^(\s*[A-Za-z0-9_.\-]+\s*=\s*)(?:\r\n|\n)", re.M)


@dataclass
class Period:
    """一个时间段（不跨年），start < end 为自然日区间，否则视为跨天区间。"""

    start: dtime
    end: dtime

    def contains(self, t: dtime) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        # 跨天（如 22:00-02:00）
        return t >= self.start or t < self.end


def parse_period(spec: Any) -> Optional[Period]:
    """解析 "HH:MM-HH:MM" → Period；格式非法返回 None。"""
    if not isinstance(spec, str):
        return None
    s, sep, e = spec.partition("-")
    if not sep:
        return None
    ms = _TIME_RE.match(s)
    me = _TIME_RE.match(e)
    if not ms or not me:
        return None
    sh, sm = int(ms.group(1)), int(ms.group(2))
    eh, em = int(me.group(1)), int(me.group(2))
    if sh > 23 or sm > 59 or eh > 23 or em > 59:
        return None
    return Period(dtime(sh, sm), dtime(eh, em))


@dataclass
class Schedule:
    """峰谷时段配置。

    offpeak_periods 为空时谷时 = 峰时之外的所有时间（取反）；
    否则按显式谷时段判断（此时峰时 = 非排除日且不在谷时段内）。
    """

    peak_periods: List[Period] = field(default_factory=list)
    offpeak_periods: List[Period] = field(default_factory=list)
    exclude_weekdays: List[int] = field(default_factory=list)  # 1=周一 ... 7=周日

    def is_peak(self, dt: datetime) -> bool:
        if dt.isoweekday() in self.exclude_weekdays:
            return False
        t = dt.time()
        if self.offpeak_periods:
            return not any(p.contains(t) for p in self.offpeak_periods)
        return any(p.contains(t) for p in self.peak_periods)


def build_schedule(
    peak_periods: Sequence[Any],
    offpeak_periods: Sequence[Any] = (),
    exclude_weekdays: Sequence[Any] = (),
) -> Tuple[Schedule, List[str]]:
    """从配置原始值构建 Schedule。返回 (schedule, 非法时段描述列表)。"""
    peaks: List[Period] = []
    offs: List[Period] = []
    bad: List[str] = []
    for spec in peak_periods or ():
        p = parse_period(spec)
        if p is not None:
            peaks.append(p)
        else:
            bad.append(f"峰时时段 {spec!r} 格式非法（应为 HH:MM-HH:MM），已忽略")
    for spec in offpeak_periods or ():
        p = parse_period(spec)
        if p is not None:
            offs.append(p)
        else:
            bad.append(f"谷时时段 {spec!r} 格式非法（应为 HH:MM-HH:MM），已忽略")
    weekdays: List[int] = []
    for w in exclude_weekdays or ():
        try:
            wd = int(w)
        except (TypeError, ValueError):
            bad.append(f"exclude_weekdays 中存在非法值 {w!r}，已忽略")
            continue
        if 1 <= wd <= 7:
            weekdays.append(wd)
        else:
            bad.append(f"exclude_weekdays 中存在越界值 {wd}（应为 1-7），已忽略")
    return Schedule(peaks, offs, weekdays), bad


# -------------------- model_config.toml 读写 --------------------


def read_model_config(path: str) -> Tuple[Optional[Any], str, bool]:
    """读取并解析 model_config.toml。

    容错：若因 `key = ` 空值行导致解析失败，则把这些行补为 `key = ""` 后重试。
    返回 (doc, 原始文本, 是否做过空值修复)；解析彻底失败时 doc 为 None。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return None, "", False
    try:
        return tomlkit.parse(raw), raw, False
    except Exception:
        fixed = _EMPTY_VALUE_LINE_RE.sub(r'\1""\n', raw)
        if fixed == raw:
            return None, raw, False
        try:
            return tomlkit.parse(fixed), raw, True
        except Exception:
            return None, raw, False


def write_model_config(path: str, doc: Any, raw: str, skip_if_same: bool = True) -> bool:
    """原地写回 model_config.toml。

    - 保持原 inode（直接 open 写入，不用 mv/替换文件，避免 FileWatcher 丢失监听）；
    - 保持原文件行尾风格（CRLF / LF）；
    - 使用 tomlkit 保留注释与空行；
    - skip_if_same=True 时，若序列化结果与磁盘当前内容字节级一致，则不写（返回 True，
      避免无谓触发热重载与日志刷屏）。

    返回是否"写入成功"（含跳过写入的情况）。
    """
    try:
        content = tomlkit.dumps(doc)
        if "\r\n" in raw:
            content = content.replace("\n", "\r\n")
        if skip_if_same and content == raw:
            return True
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return True
    except Exception:
        return False


def backup_file(path: str, backup_dir: str, keep: int = 10) -> Optional[str]:
    """修改前把原文件备份到 backup_dir，仅保留最近 keep 份。返回备份路径或 None。"""
    try:
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(backup_dir, f"model_config_{ts}.toml")
        shutil.copy2(path, dst)
        backups = sorted(
            glob.glob(os.path.join(backup_dir, "model_config_*.toml")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in backups[keep:]:
            try:
                os.remove(old)
            except Exception:
                pass
        return dst
    except Exception:
        return None


# -------------------- 任务切换 --------------------


@dataclass
class SwitchResult:
    """一次时段切换的结果与日志消息（由调用方输出日志）。"""

    changed: int = 0  # 实际修改了 model_list 的任务数
    skipped: int = 0  # 因模型缺失/格式问题跳过（含已就位）的任务数
    warnings: List[str] = field(default_factory=list)
    infos: List[str] = field(default_factory=list)


def apply_phase_to_doc(
    doc: Any,
    phase: str,
    task_models: Dict[str, Dict[str, str]],
) -> SwitchResult:
    """按 phase（"peak"/"offpeak"）把各任务指定模型提升到 model_list 首位。

    task_models: {task_key: {"peak": 模型名, "offpeak": 模型名}}。
    规则：
    - 峰/谷模型任一未配置（空字符串）的任务静默跳过该任务的全部切换，不产生任何日志；
    - 模型名未在 [[models]].name 中定义 → 警告并跳过该任务；
    - 模型不在该任务 model_list 中 → 警告并跳过（需求指定格式）；
    - 已在首位 → 跳过（不写文件）。
    """
    res = SwitchResult()
    phase_label_zh = "峰时" if phase == "peak" else "谷时"

    if doc is None:
        res.warnings.append("model_config.toml 解析结果为空，本次跳过全部任务切换")
        return res

    model_names: Set[str] = {
        str(m.get("name", "")).strip()
        for m in doc.get("models", [])
        if isinstance(m, dict)
    }

    task_config = doc.get("model_task_config")
    if not isinstance(task_config, dict):
        res.warnings.append("model_config.toml 中缺少 [model_task_config] 段，本次跳过全部任务切换")
        return res

    for task in TASK_KEYS:
        mapping = task_models.get(task) or {}
        peak_model = str(mapping.get("peak") or "").strip()
        offpeak_model = str(mapping.get("offpeak") or "").strip()
        if not peak_model or not offpeak_model:
            continue  # 峰/谷模型任一未配置 → 静默跳过该任务全部切换
        model = peak_model if phase == "peak" else offpeak_model
        if model not in model_names:
            res.warnings.append(
                f'任务 {task} 的{phase_label_zh}模型 "{model}" 未在 [[models]] 中定义，跳过切换。'
            )
            res.skipped += 1
            continue
        tc = task_config.get(task)
        if not isinstance(tc, dict) or "model_list" not in tc:
            res.warnings.append(f"任务 {task} 缺少 model_list 配置，跳过切换。")
            res.skipped += 1
            continue
        ml = tc["model_list"]
        if not isinstance(ml, list):
            res.warnings.append(f"任务 {task} 的 model_list 不是数组，跳过切换。")
            res.skipped += 1
            continue
        items = [str(x) for x in ml]
        if model not in items:
            res.warnings.append(
                f'任务 {task} 的{phase_label_zh}模型 "{model}" 不在其 model_list 中，跳过切换。'
            )
            res.skipped += 1
            continue
        if items and items[0] == model:
            continue  # 已就位，无需修改
        idx = items.index(model)
        del ml[idx]
        ml.insert(0, model)
        res.changed += 1
        res.infos.append(
            f'任务 {task} 的{phase_label_zh}模型 "{model}" 已提升到 model_list 首位'
        )
    return res
