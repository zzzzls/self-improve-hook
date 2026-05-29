"""LLM 调用记录器:每次 claude CLI 调用生成一份可回溯的 run 目录。

目录结构::

    <cwd>/.claude/memory/runs/<YYYYMMDD-HHMMSS>-<event>-<sid8>/
      prompt.txt        # 完整发送的 prompt
      response.txt      # 完整 LLM 返回(失败时为空)
      meta.json         # 元数据:时长、长度、状态、错误信息

设计要点
--------
- 即使写入失败也不抛异常,绝不影响主流程。
- meta.json 先写,prompt/response 后写;读取时以 meta 为权威。
- 仅按时间戳排序,无需索引文件。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_LOG = logging.getLogger("runlog")


@dataclass
class RunRecord:
    """LLM 调用过程中的可变记录,由 ``record_run`` 上下文管理器维护。"""

    hook_event: str
    session_id: str
    model: str
    prompt: str
    run_dir: Path
    response: str = ""
    duration_ms: int = 0
    ok: bool = False
    error: str | None = None
    extras: dict = field(default_factory=dict)


@contextmanager
def record_run(
    *,
    cwd: Path,
    hook_event: str,
    session_id: str,
    model: str,
    prompt: str,
) -> Iterator[RunRecord]:
    """以上下文管理器形态记录一次 LLM 调用。

    用法::

        with record_run(cwd=cwd, hook_event="Stop", ...) as rec:
            rec.response = claude_cli.run(prompt)
            rec.ok = True

    退出时无论成败都会落盘。捕获异常时把异常信息写入 ``rec.error``。

    Args:
        cwd: 当前项目根目录。
        hook_event: ``Stop`` / ``SessionEnd`` / ``SessionStart``。
        session_id: hook 输入里的 session id。
        model: 使用的模型 id。
        prompt: 完整 prompt 文本。

    Yields:
        RunRecord:调用方可在其上设置 ``response`` / ``ok`` / ``error`` / ``extras``。
    """
    ts = datetime.now(timezone.utc).astimezone()
    short_sid = (session_id or "unknown")[:8]
    run_dir = (
        cwd
        / ".claude"
        / "memory"
        / "runs"
        / f"{ts.strftime('%Y%m%d-%H%M%S')}-{hook_event.lower()}-{short_sid}"
    )
    rec = RunRecord(
        hook_event=hook_event,
        session_id=session_id,
        model=model,
        prompt=prompt,
        run_dir=run_dir,
    )
    start = time.perf_counter()
    try:
        yield rec
    except Exception as e:
        rec.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        rec.duration_ms = int((time.perf_counter() - start) * 1000)
        _persist(rec, ts)


def _persist(rec: RunRecord, ts: datetime) -> None:
    try:
        rec.run_dir.mkdir(parents=True, exist_ok=True)
        (rec.run_dir / "prompt.txt").write_text(rec.prompt, encoding="utf-8")
        if rec.response:
            (rec.run_dir / "response.txt").write_text(rec.response, encoding="utf-8")
        meta = {
            "ts": ts.isoformat(timespec="seconds"),
            "hook_event": rec.hook_event,
            "session_id": rec.session_id,
            "model": rec.model,
            "prompt_chars": len(rec.prompt),
            "response_chars": len(rec.response),
            "duration_ms": rec.duration_ms,
            "ok": rec.ok,
            "error": rec.error,
            **rec.extras,
        }
        (rec.run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _LOG.info(
            "LLM run %s %s %dms ok=%s prompt=%d resp=%d",
            rec.hook_event,
            rec.run_dir.name,
            rec.duration_ms,
            rec.ok,
            len(rec.prompt),
            len(rec.response),
        )
    except OSError as e:
        _LOG.warning("failed to persist run log: %s", e)
