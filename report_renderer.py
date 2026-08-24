"""峰谷模型切换插件 — 任务模型列表 HTML 报表渲染（不依赖 MaiBot SDK，便于离线测试）。

生成参考样式：浅灰背景 + 白色圆角卡片 + 深绿强调色（对应 36116a27f2c579fc3a85d297e5ba5495.png 的报表风格）。
数据来自 model_config.toml 的 [model_task_config]（动态调整 8 任务 + 固定 3 任务）。
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

# 动态调整任务（峰谷切换插件管理）：(配置键, 中文名)
DYNAMIC_TASKS: Tuple[Tuple[str, str], ...] = (
    ("replyer", "回复"),
    ("planner", "规划"),
    ("memory", "记忆"),
    ("mid_memory", "聊天回想"),
    ("utils", "工具"),
    ("learner", "学习"),
    ("expression_use", "表达方式使用"),
    ("emoji", "表情包"),
)

# 固定任务（不参与动态调整）
FIXED_TASKS: Tuple[Tuple[str, str], ...] = (
    ("vlm", "视觉"),
    ("voice", "语音"),
    ("embedding", "嵌入"),
)


def build_report_html(doc: Any, now: datetime | None = None) -> str:
    """根据 model_config.toml 的 tomlkit 文档生成 HTML 报表。

    doc: tomlkit 解析后的文档对象（含 model_task_config）。
    now: 可选，用于标题日期（默认当前时间）。
    """
    now = now or datetime.now()
    task_config = doc.get("model_task_config")
    if not isinstance(task_config, dict):
        task_config = {}

    dynamic_rows = _render_task_rows(doc, DYNAMIC_TASKS, show_badge=True)
    fixed_rows = _render_task_rows(doc, FIXED_TASKS, show_badge=False)

    date_str = now.strftime("%Y年%m月%d日")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html {{ width: 360px; }}
  body {{
    font-family: "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
    background: #f5f7fa;
    width: 360px;
    padding: 14px 10px 10px;
  }}
  .title-bar {{ display: flex; align-items: stretch; margin-bottom: 12px; }}
  .title-bar .bar {{ width: 4px; background: #1d7a5f; border-radius: 2px; margin-right: 10px; }}
  .title-bar h1 {{ font-size: 22px; color: #1a1a1a; font-weight: 700; line-height: 1.3; }}
  .title-bar .date {{ font-size: 13px; color: #888; margin-top: 2px; }}

  .stat-cards {{ display: flex; gap: 8px; margin-bottom: 12px; }}
  .stat-card {{
    flex: 1; background: #fff; border: 1px solid #eceff3; border-radius: 10px;
    padding: 10px 12px;
  }}
  .stat-card .label {{ font-size: 13px; color: #8a8f98; }}
  .stat-card .value {{ font-size: 22px; font-weight: 700; color: #1a1a1a; margin-top: 4px; }}
  .stat-card .value.green {{ color: #1d7a5f; }}

  .section-title {{
    font-size: 15px; font-weight: 600; color: #333; margin: 12px 0 8px;
    padding-left: 8px; border-left: 3px solid #1d7a5f;
  }}

  .task-card {{
    background: #fff; border: 1px solid #eceff3; border-radius: 10px;
    padding: 10px 12px; margin-bottom: 8px;
  }}
  .task-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .task-name {{ font-size: 15px; font-weight: 600; color: #1a1a1a; }}
  .task-key {{ font-size: 12px; color: #a0a6af; font-weight: 400; margin-left: 4px; }}
  .task-count {{ font-size: 13px; color: #8a8f98; }}
  .task-count.empty {{ color: #b0b6bf; }}

  .first-line {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
  .badge {{
    font-size: 11px; color: #fff; background: #1d7a5f; border-radius: 4px;
    padding: 2px 7px; flex-shrink: 0;
  }}
  .badge.gray {{ background: #9aa3ad; }}
  .model-first {{ font-size: 14px; font-weight: 600; color: #1a1a1a; word-break: break-word; }}

  .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .model-chip {{
    font-size: 12px; color: #55606b; background: #f0f3f7;
    border-radius: 5px; padding: 3px 8px;
  }}

  .task-note {{
    display: flex; align-items: flex-start; gap: 6px;
    margin-top: 5px; padding-top: 8px;
    font-size: 12px; color: #8a8f98; line-height: 1.5;
  }}
  .note-text {{ word-break: break-word; }}

  .empty-note {{ font-size: 13px; color: #a0a6af; }}

  .footer {{ font-size: 12px; color: #a0a6af; margin-top: -2px; line-height: 1; text-align: center; }}
</style>
</head>
<body>
  <div class="title-bar">
    <div class="bar"></div>
    <div>
      <h1>任务模型列表</h1>
      <div class="date">{date_str} · MaiBot 模型配置概览</div>
    </div>
  </div>

  <div class="stat-cards">
    <div class="stat-card">
      <div class="label">任务总数</div>
      <div class="value">{len(DYNAMIC_TASKS) + len(FIXED_TASKS)}</div>
    </div>
    <div class="stat-card">
      <div class="label">动态调整</div>
      <div class="value green">{len(DYNAMIC_TASKS)}</div>
    </div>
    <div class="stat-card">
      <div class="label">固定任务</div>
      <div class="value">{len(FIXED_TASKS)}</div>
    </div>
  </div>

  <div class="section-title">动态调整任务</div>
  {dynamic_rows}

  <div class="section-title">固定任务</div>
  {fixed_rows}

  <div class="footer">数据来自 config/model_config.toml · 峰谷模型切换插件</div>
</body>
</html>"""


def _get_task_comment(doc: Any, key: str) -> str:
    """从 model_config.toml 提取 [model_task_config.<key>] 表头注释（去掉 # 前缀）。

    例如表头 `[model_task_config.replyer] # 回复模型，影响麦麦的回复表现`
    返回 `回复模型，影响麦麦的回复表现`；无注释返回空字符串。
    """
    try:
        task_config = doc.get("model_task_config")
        if not isinstance(task_config, dict):
            return ""
        item = task_config.get(key)
        trivia = getattr(item, "trivia", None)
        comment = getattr(trivia, "comment", None) if trivia is not None else None
        if not comment:
            return ""
        text = str(comment)
        text = text.strip()
        if text.startswith("#"):
            text = text[1:].strip()
        return text
    except Exception:
        return ""


def _render_task_rows(
    doc: Any,
    tasks: Sequence[Tuple[str, str]],
    show_badge: bool,
) -> str:
    """渲染一组任务卡片（每个任务一个白色圆角卡片）。

    doc: tomlkit 文档（用于读取任务表头注释作为作用说明）。
    """
    task_config = doc.get("model_task_config")
    if not isinstance(task_config, dict):
        task_config = {}
    rows: List[str] = []
    for key, label in tasks:
        tc = task_config.get(key)
        if not isinstance(tc, dict):
            continue
        note = _get_task_comment(doc, key)
        note_html = (
            f'<div class="task-note"><span class="note-text">{html.escape(note)}</span></div>'
        ) if note else ""
        ml = tc.get("model_list") or []
        items = [str(x) for x in ml]
        if not items:
            rows.append(f"""
        <div class="task-card">
          <div class="task-head">
            <span class="task-name">{html.escape(label)} <span class="task-key">{html.escape(key)}</span></span>
            <span class="task-count empty">未配置</span>
          </div>
          {note_html}
        </div>""")
            continue
        first = items[0]
        rest = items[1:]
        rest_html = "".join(
            f'<span class="model-chip">{html.escape(m)}</span>' for m in rest
        ) if rest else ""
        badge = '<span class="badge">优先</span>' if show_badge else '<span class="badge gray">固定</span>'
        rows.append(f"""
        <div class="task-card">
          <div class="task-head">
            <span class="task-name">{html.escape(label)} <span class="task-key">{html.escape(key)}</span></span>
            <span class="task-count">{len(items)} 个模型</span>
          </div>
          <div class="first-line">
            {badge}
            <span class="model-first">{html.escape(first)}</span>
          </div>
          {f'<div class="chips">{rest_html}</div>' if rest_html else ''}
          {note_html}
        </div>""")
    return "\n".join(rows)


def collect_task_stats(doc: Any) -> Dict[str, int]:
    """统计信息：{dynamic_configured, fixed_configured, ...}（供日志/扩展使用）。"""
    task_config = doc.get("model_task_config")
    if not isinstance(task_config, dict):
        task_config = {}
    stats = {
        "dynamic_configured": 0,
        "fixed_configured": 0,
    }
    for key, _ in DYNAMIC_TASKS:
        tc = task_config.get(key)
        if isinstance(tc, dict) and tc.get("model_list"):
            stats["dynamic_configured"] += 1
    for key, _ in FIXED_TASKS:
        tc = task_config.get(key)
        if isinstance(tc, dict) and tc.get("model_list"):
            stats["fixed_configured"] += 1
    return stats
