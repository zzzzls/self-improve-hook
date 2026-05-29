#!/usr/bin/env python3
"""Claude Code Stop hook:从本轮对话中提取候选经验,追加到 candidates.jsonl。

设计原则
--------
- **静默失败**:任何异常都吞掉并写日志,确保 hook 不影响主流程。
- **增量提取**:用 ``state/<session_id>.json`` 记录已处理的 transcript 行号,
  每次 Stop 只把新增行喂给 LLM。
- **append-only**:候选直接追加 jsonl,合并去重留到 SessionEnd。

输入(stdin JSON)
------------------
- ``session_id``: str
- ``transcript_path``: str(绝对路径)
- ``cwd``: str
- ``stop_hook_active``: bool(true 表示已是 stop hook 强制继续状态)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from lib import atomic, claude_cli, runlog, state as state_mod, transcript  # noqa: E402

MODEL = "claude-haiku-4-5"

PROMPT_PATH = HOOK_DIR / "prompts" / "extract.md"
MEMORY_DIRNAME = ".claude/memory"


def main() -> int:
    """读取 hook 输入,执行提取流水线。无论成败均返回 0。"""
    # 防递归:若本 hook 由 claude_cli 派生的子 claude -p 会话触发,直接退出。
    if os.environ.get(claude_cli.HOOK_GUARD_ENV):
        return 0
    # Windows 中文区域下 sys.stdin 默认按 cp936 解码,会把 Claude Code 以 UTF-8
    # 传入的负载(含中文路径)解成乱码 → memory_dir 指向幽灵目录。强制按字节读再 UTF-8 解码。
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0

    if payload.get("stop_hook_active"):
        return 0

    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not (cwd and session_id and transcript_path):
        return 0

    memory_dir = Path(cwd) / MEMORY_DIRNAME
    _setup_logging(memory_dir)
    log = logging.getLogger("learn_on_stop")
    log.info("Stop hook fired session=%s", session_id)

    try:
        _process(
            cwd=Path(cwd),
            session_id=session_id,
            transcript_path=Path(transcript_path),
        )
    except Exception:
        log.exception("learn_on_stop failed")
    return 0


def _process(*, cwd: Path, session_id: str, transcript_path: Path) -> None:
    """每次 Stop 触发都重新读 state,提取新增行,写回 active 状态。"""
    log = logging.getLogger("learn_on_stop")
    memory_dir = cwd / MEMORY_DIRNAME
    state_dir = memory_dir / "state"
    state_file = state_mod.state_path(state_dir, session_id)
    candidates_file = memory_dir / "candidates.jsonl"

    state = state_mod.read_state(state_file) or state_mod.SessionState(
        session_id=session_id,
        transcript_path=str(transcript_path),
    )
    last_line = state.last_processed_line
    total_lines = transcript.count_lines(transcript_path)
    log.info("transcript lines total=%d, last=%d", total_lines, last_line)
    if total_lines <= last_line:
        log.info("no new lines, skip")
        return

    extract_result = run_extraction(
        cwd=cwd,
        session_id=session_id,
        transcript_path=transcript_path,
        last_line=last_line,
        total_lines=total_lines,
        candidates_file=candidates_file,
        log=log,
    )
    if not extract_result.advanced:
        return

    state.transcript_path = str(transcript_path)
    state.last_processed_line = total_lines
    state.status = "active"
    state_mod.write_state(state_file, state)


def run_extraction(
    *,
    cwd: Path,
    session_id: str,
    transcript_path: Path,
    last_line: int,
    total_lines: int,
    candidates_file: Path,
    log: logging.Logger,
) -> "_ExtractResult":
    """切片 transcript → 调 LLM → append 候选。被 Stop 与 SessionStart 共用。

    Returns:
        _ExtractResult,``advanced`` 表示是否成功推进 last_line。
    """
    events = transcript.read_slice(transcript_path, start_line=last_line + 1)
    log.info("sliced %d new events (lines %d..%d)", len(events), last_line + 1, total_lines)
    if not events:
        return _ExtractResult(advanced=True, appended=0, rejected=0)

    if not claude_cli.is_available():
        log.warning("claude CLI not available; skipping extraction")
        return _ExtractResult(advanced=False, appended=0, rejected=0)

    conversation = transcript.slim_events(events)
    if not conversation.strip():
        log.info("slim conversation is empty, skip LLM")
        return _ExtractResult(advanced=True, appended=0, rejected=0)

    prompt = _render_prompt(conversation)
    log.info("calling LLM model=%s prompt_chars=%d", MODEL, len(prompt))
    # 候选处理(enrich/append)与 extras 赋值都放进 with 块内:runlog 在 with 退出时即落盘,
    # 块外再改 rec.extras 不会写进 meta.json。这样统计与空数组原因都能进 meta.json,便于回溯。
    appended = 0
    rejected = 0
    advanced = False
    with runlog.record_run(
        cwd=cwd,
        hook_event="Stop",
        session_id=session_id,
        model=MODEL,
        prompt=prompt,
    ) as rec:
        try:
            raw_response = claude_cli.run(prompt, model=MODEL, output_format="text", timeout=90)
            rec.response = raw_response
            result = claude_cli.extract_json_object(raw_response)
        except claude_cli.ClaudeCliError as e:
            rec.error = str(e)
            result = None

        if result is None:
            rec.error = rec.error or "response is not valid JSON"
            log.warning("extraction LLM failed; see runs/%s", rec.run_dir.name)
        else:
            rec.ok = True
            advanced = True
            raw_candidates = result.get("candidates") or []
            rec.extras["candidates_raw"] = len(raw_candidates)
            if not raw_candidates:
                reason = (result.get("reason") or "").strip() or "(LLM 未给出原因)"
                rec.extras["empty_reason"] = reason
                log.info("LLM returned no candidates; reason=%s", reason)
            for item in raw_candidates:
                enriched = _enrich(
                    item=item,
                    session_id=session_id,
                    transcript_path=transcript_path,
                    start_line=last_line + 1,
                    end_line=total_lines,
                )
                if enriched is None:
                    rejected += 1
                    continue
                atomic.append_jsonl_line(
                    candidates_file, json.dumps(enriched, ensure_ascii=False)
                )
                appended += 1
            rec.extras["candidates_appended"] = appended
            rec.extras["candidates_rejected"] = rejected
            log.info(
                "appended %d / rejected %d candidates (lines %d..%d, run=%s)",
                appended,
                rejected,
                last_line + 1,
                total_lines,
                rec.run_dir.name,
            )

    if not advanced:
        return _ExtractResult(advanced=False, appended=0, rejected=0)
    return _ExtractResult(advanced=True, appended=appended, rejected=rejected)


class _ExtractResult:
    """提取结果。``advanced`` 表示是否应推进 ``last_processed_line``。"""

    __slots__ = ("advanced", "appended", "rejected")

    def __init__(self, *, advanced: bool, appended: int, rejected: int) -> None:
        self.advanced = advanced
        self.appended = appended
        self.rejected = rejected


def _render_prompt(conversation: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("<<<CONVERSATION>>>", conversation)


_VALID_TYPES = {
    "user_instruction",
    "project_convention",
    "error_lesson",
    "tool_preference",
    "workflow_convention",
    "issue_fix",
}


def _enrich(
    *,
    item: dict[str, Any],
    session_id: str,
    transcript_path: Path,
    start_line: int,
    end_line: int,
) -> dict[str, Any] | None:
    """补全候选条目的元信息;不合法条目返回 None。"""
    if not isinstance(item, dict):
        return None
    content = (item.get("content") or "").strip()
    type_ = item.get("type")
    if not content or type_ not in _VALID_TYPES:
        return None

    tags = item.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lower() for t in tags if str(t).strip()]

    now = datetime.now(timezone.utc).astimezone()
    return {
        "id": f"learn_{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}",
        "type": type_,
        "content": content,
        "scope": item.get("scope") or "project",
        "tags": tags,
        "source": {
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "start_line": start_line,
            "end_line": end_line,
            "hook_event": "Stop",
        },
        "dedupe_key": hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest(),
        "created_at": now.isoformat(timespec="seconds"),
    }


def _normalize(text: str) -> str:
    """语义无关地把文本压平,作为 dedupe_key 的输入。"""
    return " ".join(text.split()).lower()


def _setup_logging(memory_dir: Path) -> None:
    log_path = memory_dir / "state" / "hook.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    root = logging.getLogger()
    if any(getattr(h, "_self_improve", False) for h in root.handlers):
        return
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler._self_improve = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


if __name__ == "__main__":
    sys.exit(main())
