"""Session 状态文件:`.claude/memory/state/<session_id>.json`。

状态机
------
- ``active``  :session 仍在用,或异常退出未关闭。Stop hook 持续更新
  ``last_processed_line``。
- ``closed``  :SessionEnd 成功合并后,或 SessionStart 扫描时手动关闭。

下次启动时 SessionStart 扫描所有 ``active`` 文件,若对应 transcript 行数
> ``last_processed_line``,说明上次 Stop 之后还产生了对话(可能是 Ctrl+C
中断),需要补一次提取。

字段
----
- ``session_id``           : 与文件名一致
- ``transcript_path``      : 该 session 的 transcript 绝对路径
- ``last_processed_line``  : Stop hook 已处理到的行号(含)
- ``status``               : ``active`` | ``closed``
- ``updated_at``           : 最后写入时间(ISO 8601 + tz)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from lib import atomic

Status = Literal["active", "closed"]


@dataclass
class SessionState:
    session_id: str
    transcript_path: str
    last_processed_line: int = 0
    status: Status = "active"
    updated_at: str = ""

    def touch(self) -> None:
        """把 ``updated_at`` 刷成当前时间(本地时区,秒精度)。"""
        self.updated_at = (
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        )


def state_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / f"{session_id}.json"


def read_state(path: Path) -> SessionState | None:
    """读取 state 文件;不存在或损坏返回 None。

    对历史字段名 ``last_line`` 做向后兼容映射。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status not in ("active", "closed"):
        status = "active"
    return SessionState(
        session_id=str(data.get("session_id") or path.stem),
        transcript_path=str(data.get("transcript_path") or ""),
        last_processed_line=int(
            data.get("last_processed_line") or data.get("last_line") or 0
        ),
        status=status,
        updated_at=str(data.get("updated_at") or ""),
    )


def write_state(path: Path, state: SessionState) -> None:
    """原子写入 state;会刷新 ``updated_at``。"""
    state.touch()
    atomic.atomic_write_text(
        path, json.dumps(asdict(state), ensure_ascii=False, indent=2)
    )


def list_active(state_dir: Path) -> list[SessionState]:
    """扫 ``state_dir`` 下所有 ``status=active`` 的 state。"""
    if not state_dir.exists():
        return []
    out: list[SessionState] = []
    for f in sorted(state_dir.glob("*.json")):
        s = read_state(f)
        if s is not None and s.status == "active":
            out.append(s)
    return out
