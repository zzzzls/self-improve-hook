#!/usr/bin/env python3
"""Claude Code SessionStart hook:初始化本 session 的 state + 恢复遗留 active session。

为何需要
--------
SessionEnd 在 Ctrl+C 一次、SIGHUP、SIGKILL 等场景不触发,会留下一份
``status=active`` 的 state 文件。本 hook 在新 session 启动时:

1. 给当前 session 写一份 ``active``/``last_processed_line=0`` 的初始 state。
2. 扫描其他 ``status=active`` 的 state:若对应 transcript 比 last 还多,
   说明上次 Stop 之后又产生了对话,补一次提取。
3. 提取完毕(或本来就无需提取)后,把那些 state 标记为 ``closed``。
4. 如果合并候选还在 ``candidates.jsonl`` 中,调一次 ``consolidate_memory.py``
   把它们落进 ``memory.generated.md``。

整个流程靠 hook 的 ``"async": true`` 配置在后台跑,不阻塞用户启动 session。

输入(stdin JSON)
------------------
- ``session_id``: str(本 session 的 id)
- ``transcript_path``: str
- ``cwd``: str
- ``hook_event_name``: "SessionStart"
- ``source``: "startup" | "resume" | "clear" | "compact"
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from lib import atomic, claude_cli, state as state_mod, transcript  # noqa: E402

import learn_on_stop  # noqa: E402

CONSOLIDATE_SCRIPT = HOOK_DIR / "consolidate_memory.py"
MEMORY_DIRNAME = ".claude/memory"


def main() -> int:
    # 防递归:若本 hook 由 claude_cli 派生的子 claude -p 会话触发,直接退出。
    if os.environ.get(claude_cli.HOOK_GUARD_ENV):
        return 0
    # 强制 UTF-8 读 stdin(见 learn_on_stop 的说明):Windows 中文区域默认 cp936
    # 会把 UTF-8 负载里的中文路径解成乱码,导致 memory_dir 指向幽灵目录。
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0

    cwd = payload.get("cwd")
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path") or ""
    if not (cwd and session_id):
        return 0

    memory_dir = Path(cwd) / MEMORY_DIRNAME
    _setup_logging(memory_dir)
    log = logging.getLogger("session_start")
    log.info(
        "SessionStart fired session=%s source=%s", session_id, payload.get("source")
    )

    try:
        _process(
            cwd=Path(cwd),
            session_id=session_id,
            transcript_path=transcript_path,
        )
    except Exception:
        log.exception("session_start failed")
    return 0


def _process(*, cwd: Path, session_id: str, transcript_path: str) -> None:
    log = logging.getLogger("session_start")
    memory_dir = cwd / MEMORY_DIRNAME
    state_dir = memory_dir / "state"
    candidates_file = memory_dir / "candidates.jsonl"

    _init_current_state(state_dir, session_id, transcript_path, log)
    recovered = _recover_active_sessions(
        cwd=cwd,
        state_dir=state_dir,
        candidates_file=candidates_file,
        current_session_id=session_id,
        log=log,
    )
    reclaimed = _reclaim_orphan_candidates(memory_dir, candidates_file, log)
    if recovered or reclaimed or _has_pending_candidates(candidates_file):
        _trigger_consolidate(cwd=cwd, session_id=session_id, log=log)


def _init_current_state(
    state_dir: Path,
    session_id: str,
    transcript_path: str,
    log: logging.Logger,
) -> None:
    """为当前 session 写一份 active 初始 state(若已存在则不覆盖)。"""
    state_file = state_mod.state_path(state_dir, session_id)
    if state_mod.read_state(state_file) is not None:
        log.info("current session state already exists, keep")
        return
    state = state_mod.SessionState(
        session_id=session_id,
        transcript_path=transcript_path,
        last_processed_line=0,
        status="active",
    )
    state_mod.write_state(state_file, state)
    log.info("init current session state at %s", state_file)


def _recover_active_sessions(
    *,
    cwd: Path,
    state_dir: Path,
    candidates_file: Path,
    current_session_id: str,
    log: logging.Logger,
) -> bool:
    """扫描其他 active state,补处理 transcript 残留 + 标记 closed。

    多窗口并发启动时,每个遗留 active session 先用 :func:`atomic.claim_file`
    把 ``<sid>.json`` 原子改名成本进程独占的 ``.recovering-*``,只有抢到的窗口才
    补提取,避免对同一 transcript 重复提取(问题 4)。

    Returns:
        是否至少补处理了一个 session(用于决定是否触发 consolidate)。
    """
    _reclaim_stale_recoveries(state_dir, log)
    active_states = [
        s for s in state_mod.list_active(state_dir) if s.session_id != current_session_id
    ]
    log.info("found %d leftover active session(s)", len(active_states))
    recovered_any = False
    for s in active_states:
        state_file = state_mod.state_path(state_dir, s.session_id)
        recovering_file = state_file.with_name(
            f"{state_file.name}.recovering-{os.getpid()}-{current_session_id[:8]}"
        )
        if not atomic.claim_file(state_file, recovering_file):
            log.info("  session=%s claimed by a peer, skip", s.session_id)
            continue
        log.info(
            "claiming leftover session=%s last=%d transcript=%s",
            s.session_id,
            s.last_processed_line,
            s.transcript_path,
        )
        try:
            recovered_any |= _recover_one(
                cwd=cwd,
                state=s,
                state_file=state_file,
                recovering_file=recovering_file,
                candidates_file=candidates_file,
                log=log,
            )
        except Exception:
            # 出意外:把领取的 state 原样改回原路径(仍是 active),供下次重试。
            _restore_recovering(recovering_file, state_file, log)
            raise
    return recovered_any


def _recover_one(
    *,
    cwd: Path,
    state: state_mod.SessionState,
    state_file: Path,
    recovering_file: Path,
    candidates_file: Path,
    log: logging.Logger,
) -> bool:
    """处理一个已领取的遗留 session;返回是否补提取了候选。

    无需提取(transcript 缺失 / 无新行)或提取成功时,把 state 写成 ``closed``
    发布回原路径;claude 不可用或提取失败时,把领取文件原样改回原路径保留
    ``active``,供下次重试。
    """
    if not state.transcript_path or not Path(state.transcript_path).exists():
        log.info("  transcript missing, mark closed without extraction")
        _close_recovered(recovering_file, state_file, state, log)
        return False

    total = transcript.count_lines(Path(state.transcript_path))
    if total <= state.last_processed_line:
        log.info(
            "  no new lines (total=%d <= last=%d), mark closed",
            total,
            state.last_processed_line,
        )
        _close_recovered(recovering_file, state_file, state, log)
        return False

    if not claude_cli.is_available():
        log.warning("  claude CLI unavailable, keep active for next retry")
        _restore_recovering(recovering_file, state_file, log)
        return False

    result = learn_on_stop.run_extraction(
        cwd=cwd,
        session_id=state.session_id,
        transcript_path=Path(state.transcript_path),
        last_line=state.last_processed_line,
        total_lines=total,
        candidates_file=candidates_file,
        log=log,
        hook_event="SessionStart",
    )
    if not result.advanced:
        log.warning("  extraction failed for %s, keep active", state.session_id)
        _restore_recovering(recovering_file, state_file, log)
        return False

    state.last_processed_line = total
    _close_recovered(recovering_file, state_file, state, log)
    return True


def _close_recovered(
    recovering_file: Path,
    state_file: Path,
    state: state_mod.SessionState,
    log: logging.Logger,
) -> None:
    """把领取的遗留 session 写成 closed,并原子发布回原 ``<sid>.json``。"""
    state.status = "closed"
    state_mod.write_state(recovering_file, state)
    os.replace(recovering_file, state_file)
    log.info("  marked %s as closed", state.session_id)


def _restore_recovering(recovering_file: Path, state_file: Path, log: logging.Logger) -> None:
    """把领取文件原样改回原路径(内容仍是 active),供下次重试。"""
    try:
        os.replace(recovering_file, state_file)
    except FileNotFoundError:
        return
    log.info("  restored %s to active for retry", state_file.stem)


def _reclaim_stale_recoveries(state_dir: Path, log: logging.Logger) -> None:
    """把崩溃残留的 ``*.json.recovering-*`` 改回原 state 名,供后续重试。

    跳过 owner 进程仍存活的领取文件(说明另一个窗口正在恢复),避免打断它。
    """
    if not state_dir.exists():
        return
    for rec_file in sorted(state_dir.glob("*.json.recovering-*")):
        if _recovering_owner_alive(rec_file):
            continue
        original = rec_file.with_name(rec_file.name.split(".recovering-", 1)[0])
        try:
            os.replace(rec_file, original)
            log.info("reclaimed stale recovery %s", rec_file.name)
        except OSError as e:
            log.warning("failed to reclaim %s: %s", rec_file.name, e)


def _reclaim_orphan_candidates(
    memory_dir: Path, candidates_file: Path, log: logging.Logger
) -> bool:
    """回收 consolidate 崩溃残留的领取文件(``candidates.claiming-*.jsonl``)。

    consolidate 领取候选后若崩溃,会留下一个孤儿领取文件,里面的候选既没进 memory
    也不在 ``candidates.jsonl`` 里。这里把它们 merge 回 ``candidates.jsonl``,使其能在
    本次 SessionStart 触发的合并里重新处理。跳过 owner 进程仍存活的领取文件,避免
    打断正在运行的 consolidate。

    Returns:
        是否回收了至少一行候选(用于决定是否触发 consolidate)。
    """
    reclaimed = 0
    for orphan in sorted(memory_dir.glob("candidates.claiming-*.jsonl")):
        if _claiming_owner_alive(orphan):
            log.info("skip live claiming file %s", orphan.name)
            continue
        merged = atomic.merge_jsonl_into(orphan, candidates_file)
        if merged:
            log.info("reclaimed %d orphan candidates from %s", merged, orphan.name)
        reclaimed += merged
    return reclaimed > 0


def _claiming_owner_alive(claiming_file: Path) -> bool:
    """``candidates.claiming-<pid>-<token>.jsonl`` 的 owner 进程是否仍存活。"""
    parts = claiming_file.name.split("-")
    return len(parts) >= 2 and _pid_alive(parts[1])


def _recovering_owner_alive(recovering_file: Path) -> bool:
    """``<sid>.json.recovering-<pid>-<sid8>`` 的 owner 进程是否仍存活。"""
    suffix = recovering_file.name.split(".recovering-", 1)
    return len(suffix) == 2 and _pid_alive(suffix[1].split("-", 1)[0])


def _pid_alive(pid_text: str) -> bool:
    """判断字符串形式的 PID 对应进程是否存活;解析失败保守视为不存活。

    用于区分 "正在运行的 owner" 与 "崩溃残留的孤儿"。极少数 PID 复用场景下可能
    误判为存活,代价仅是把回收推迟到下次 SessionStart,不影响正确性。
    """
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _has_pending_candidates(candidates_file: Path) -> bool:
    return candidates_file.exists() and candidates_file.stat().st_size > 0


def _trigger_consolidate(*, cwd: Path, session_id: str, log: logging.Logger) -> None:
    """以子进程方式调 consolidate_memory.py;hook_event 用 SessionStart 防止它写 closed。"""
    payload = {
        "session_id": f"recover-{session_id[:8]}",
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "reason": "recovery",
    }
    log.info("triggering consolidate as subprocess")
    try:
        subprocess.run(
            [sys.executable, str(CONSOLIDATE_SCRIPT)],
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",  # 与 consolidate 的 UTF-8 stdin 读取对齐(Windows 默认 cp936 会错配)
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("consolidate subprocess timed out")
    except OSError as e:
        log.warning("failed to spawn consolidate subprocess: %s", e)


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
