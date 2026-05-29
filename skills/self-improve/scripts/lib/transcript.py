"""transcript.jsonl 切片工具。

Claude Code 将一次会话的所有事件(user / assistant / tool_use / tool_result)
按行追加到 transcript_path 指向的 jsonl 文件。Stop hook 每次只关心"上一轮
处理之后的新增行",故需要记录上次读到第几行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Claude Code 在一轮对话结束后会向 transcript 追加一批非对话元数据行
# (system / last-prompt / ai-title / mode)。它们落盘时机晚于 Stop hook 读取,
# 故下个会话的恢复路径会把这批尾部噪声当成"新对话"切出来,塌成几个光秃秃的
# [type] 标签——非空白但无正文,绕过空内容护栏白烧一次 LLM 调用。这里按 type
# 黑名单直接丢弃。
_META_TYPES = {"system", "last-prompt", "ai-title", "mode"}


def count_lines(path: str | Path) -> int:
    """返回 jsonl 文件的总行数;文件不存在时返回 0。"""
    p = Path(path)
    if not p.exists():
        return 0
    with open(p, "rb") as f:
        return sum(1 for _ in f)


def read_slice(path: str | Path, start_line: int) -> list[dict[str, Any]]:
    """读取 ``start_line``(含)开始到 EOF 的所有行,解析为 JSON 对象列表。

    Args:
        path: transcript.jsonl 路径。
        start_line: 起始行号,从 1 开始计数。``start_line<=0`` 视为从头开始。

    Returns:
        解析后的事件对象列表。无法解析的行被跳过。
    """
    p = Path(path)
    if not p.exists():
        return []
    start_line = max(1, start_line)
    events: list[dict[str, Any]] = []
    with open(p, encoding="utf-8") as f:
        for idx, raw in enumerate(f, start=1):
            if idx < start_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return events


def slim_events(events: list[dict[str, Any]], max_chars: int = 60_000) -> str:
    """将事件列表序列化成 prompt 友好的紧凑字符串。

    只保留对经验提取有意义的字段(role / type / content / tool_name 等),
    并对总长度做截断,避免把超长工具输出灌进 LLM。

    Args:
        events: 已解析的事件对象列表。
        max_chars: 序列化结果的最大字符数,超过则尾部截断。

    Returns:
        紧凑的多行字符串,每行一个事件。
    """
    lines: list[str] = []
    for ev in events:
        kind = ev.get("type") or ev.get("role") or "event"
        if kind in _META_TYPES:  # 已知元数据噪声直接丢
            continue
        msg = ev.get("message") or {}
        role = msg.get("role") or ev.get("role")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _extract_text(content) if content is not None else _extract_text(ev.get("content"))
        tool = ev.get("tool_name") or (msg.get("tool_use", {}) or {}).get("name")
        if not text and not tool:  # 兜底:无正文也无工具的事件不构成对话,跳过
            continue
        parts = [f"[{kind}]"]
        if role:
            parts.append(f"role={role}")
        if tool:
            parts.append(f"tool={tool}")
        if text:
            parts.append(text)
        lines.append(" ".join(parts))
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n...[truncated]"
    return joined


def _extract_text(content: Any) -> str:
    """从 Claude transcript 中各种 content 形态里抠出文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    chunks.append(str(item["text"]))
                elif item.get("type") == "tool_use":
                    name = item.get("name", "")
                    chunks.append(f"<tool_use name={name}>")
                elif item.get("type") == "tool_result":
                    chunks.append("<tool_result/>")
        return " ".join(c.strip() for c in chunks if c).strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"]).strip()
    return ""
