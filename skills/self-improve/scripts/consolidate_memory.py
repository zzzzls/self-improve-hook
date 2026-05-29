#!/usr/bin/env python3
"""合并 candidates.jsonl → memory.generated.md。

**纯合并工具**,不是任何 hook 的直接入口。被 ``session_start.py`` 调用,
不读写 state 文件 — state 维护由 ``learn_on_stop.py`` 与 ``session_start.py``
分别负责。

设计原则
--------
- **静默失败**:任何异常都吞掉并写日志。
- **全量重写**:把候选与现有 markdown 一并送进 LLM,直接产出新 markdown。
- **原子写**:tmp 文件 + ``os.replace`` 替换 ``memory.generated.md``。
- **归档候选**:成功后把 ``candidates.jsonl`` 移到 ``archive/``。

输入(stdin JSON)
------------------
- ``session_id``: str(仅用于归档文件命名)
- ``cwd``: str
- ``reason``: str(可选,日志展示用)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from lib import atomic, claude_cli, runlog  # noqa: E402

MODEL = "claude-haiku-4-5"
PROMPT_PATH = HOOK_DIR / "prompts" / "consolidate.md"
MEMORY_DIRNAME = ".claude/memory"
# 最终产出落在项目根(cwd),与候选/归档/state 等内部文件(均在 MEMORY_DIRNAME 下)分开
MEMORY_FILENAME = "memory.generated.md"
EMPTY_TEMPLATE = """# 项目记忆(自动生成)

> 由经验提取流水线维护。最后更新:__UPDATED_AT__

## 用户长期指令
- (暂无)

## 项目约定
- (暂无)

## 错误经验
- (暂无)

## 工具使用偏好
- (暂无)

## 工作流约定
- (暂无)

## 问题修正
- (暂无)
"""


def main() -> int:
    """读取 hook 输入,执行合并流水线。无论成败均返回 0。"""
    # 强制 UTF-8 读 stdin(见 learn_on_stop 的说明):无论来自 Claude Code 还是
    # manager/session_start 的子进程 input,写入端均已钉成 UTF-8,两端一致。
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0

    cwd = payload.get("cwd")
    session_id = payload.get("session_id") or "unknown"
    if not cwd:
        return 0

    memory_dir = Path(cwd) / MEMORY_DIRNAME
    _setup_logging(memory_dir)
    log = logging.getLogger("consolidate_memory")
    log.info(
        "consolidate fired session=%s reason=%s",
        session_id,
        payload.get("reason"),
    )

    try:
        _process(cwd=Path(cwd), session_id=session_id)
    except Exception:
        log.exception("consolidate_memory failed")
    return 0


def _process(*, cwd: Path, session_id: str) -> None:
    log = logging.getLogger("consolidate_memory")
    memory_dir = cwd / MEMORY_DIRNAME
    candidates_file = memory_dir / "candidates.jsonl"
    memory_file = cwd / MEMORY_FILENAME
    archive_dir = memory_dir / "archive"

    # 原子领取:用进程唯一名把 candidates.jsonl 抢走。并发的多个 consolidate
    # 进程里只有一个能领取成功,其余因源已不存在而立即退出 —— 这就是天然的
    # N 进程互斥点(见 lib/atomic.claim_file)。领取之后 Stop hook 追加的新候选
    # 会落到新建的 candidates.jsonl,不会被本轮归档,故不丢候选。
    claimed = memory_dir / f"candidates.claiming-{os.getpid()}-{secrets.token_hex(4)}.jsonl"
    if not atomic.claim_file(candidates_file, claimed):
        log.info("nothing to claim (empty or claimed by a peer); skip")
        return

    try:
        _consolidate_claimed(
            cwd=cwd,
            session_id=session_id,
            claimed=claimed,
            candidates_file=candidates_file,
            memory_file=memory_file,
            archive_dir=archive_dir,
            log=log,
        )
    except Exception:
        # 兜底:任何意外都把领取的候选还回去,绝不让它们随崩溃凭空消失。
        restored = atomic.merge_jsonl_into(claimed, candidates_file)
        log.warning("consolidate crashed; restored %d candidates", restored)
        raise


def _consolidate_claimed(
    *,
    cwd: Path,
    session_id: str,
    claimed: Path,
    candidates_file: Path,
    memory_file: Path,
    archive_dir: Path,
    log: logging.Logger,
) -> None:
    """处理已领取的候选快照:合并进 memory 并归档,失败则把候选 merge 回去。"""
    candidates = _read_candidates(claimed)
    log.info("claimed %d candidates", len(candidates))
    if not candidates:
        claimed.unlink(missing_ok=True)
        log.info("claimed snapshot empty, skip")
        return

    if not claude_cli.is_available():
        restored = atomic.merge_jsonl_into(claimed, candidates_file)
        log.warning("claude CLI not available; restored %d candidates for retry", restored)
        return

    current_md = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    log.info("current memory.generated.md is %d chars", len(current_md))
    prompt = _render_prompt(current_md=current_md, candidates=candidates)
    log.info("calling LLM model=%s prompt_chars=%d", MODEL, len(prompt))

    new_md_raw: str | None = None
    with runlog.record_run(
        cwd=cwd,
        hook_event="SessionEnd",
        session_id=session_id,
        model=MODEL,
        prompt=prompt,
    ) as rec:
        rec.extras["candidates_count"] = len(candidates)
        rec.extras["current_md_chars"] = len(current_md)
        try:
            raw = claude_cli.run(prompt, model=MODEL, output_format="text", timeout=180)
            rec.response = raw
            new_md_raw = raw
            rec.ok = True
        except claude_cli.ClaudeCliError as e:
            rec.error = str(e)

    if new_md_raw is None:
        restored = atomic.merge_jsonl_into(claimed, candidates_file)
        log.warning(
            "consolidate LLM failed; restored %d candidates (run=%s)",
            restored,
            rec.run_dir.name,
        )
        return

    new_md = _post_process(new_md_raw)
    if not new_md.strip():
        restored = atomic.merge_jsonl_into(claimed, candidates_file)
        log.warning(
            "LLM returned empty markdown; restored %d candidates (run=%s)",
            restored,
            rec.run_dir.name,
        )
        return

    atomic.atomic_write_text(memory_file, new_md)
    _archive_claimed(claimed, archive_dir, session_id)
    log.info(
        "consolidated %d candidates into %s (md_chars=%d, run=%s)",
        len(candidates),
        memory_file,
        len(new_md),
        rec.run_dir.name,
    )


def _read_candidates(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                items.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return items


def _render_prompt(*, current_md: str, candidates: list[dict]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    current = current_md if current_md.strip() else EMPTY_TEMPLATE
    return template.replace("<<<CURRENT_MEMORY>>>", current).replace(
        "<<<CANDIDATES>>>",
        json.dumps(candidates, ensure_ascii=False, indent=2),
    )


def _post_process(markdown: str) -> str:
    """剥离可能的 markdown 围栏,并替换时间戳占位符。"""
    text = markdown.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return text.replace("__UPDATED_AT__", now_iso) + "\n"


def _archive_claimed(claimed: Path, archive_dir: Path, session_id: str) -> None:
    """把已成功合并的领取快照归档到 ``archive/``。

    ``claimed`` 由本进程领取且尚未还回,必定存在,故无需 TOCTOU 守卫。
    """
    date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    dst = archive_dir / f"candidates-{date}-{session_id[:8]}.jsonl"
    atomic.atomic_move(claimed, dst)


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
