"""原子文件操作:写入与移动均通过临时文件 + rename 完成。

跨平台可靠,不依赖 flock。append 操作借助 OS 对 O_APPEND 的原子性保证,
适用于多 session 并发追加 jsonl。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """将 content 原子写入 path,失败时不留下半截文件。

    Args:
        path: 目标文件路径。父目录会被自动创建。
        content: 要写入的完整文本。
        encoding: 文本编码,默认 utf-8。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def append_jsonl_line(path: str | Path, line: str, encoding: str = "utf-8") -> None:
    """以 append-only 模式追加一行 JSON 到 jsonl 文件。

    依赖操作系统对 ``O_APPEND`` 的原子性保证(POSIX 和 Windows 均成立,只要
    单行长度不超过 PIPE_BUF 的等价限制),适用于多进程并发场景。

    Args:
        path: 目标 jsonl 文件路径。父目录会被自动创建。
        line: 单行 JSON 字符串,不含末尾换行。
        encoding: 文本编码,默认 utf-8。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding=encoding) as f:
        f.write(line)
        if not line.endswith("\n"):
            f.write("\n")


def atomic_move(src: str | Path, dst: str | Path) -> None:
    """将 src 原子移动到 dst,会覆盖已存在的 dst。

    Args:
        src: 源文件路径。
        dst: 目标文件路径。父目录会被自动创建。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def claim_file(src: str | Path, dst: str | Path) -> bool:
    """原子领取 src 为 dst:成功返回 True,源不存在返回 False。

    借助 ``os.replace`` 的原子 rename 语义实现多进程互斥 —— 多个进程同时领取
    同一个 src 时,只有一个能成功把它移走,其余因源已不存在而返回 False。等于把
    "先 ``exists`` 再 ``move``" 的 TOCTOU 收敛成一次内核调用。

    Args:
        src: 待领取的源文件路径。
        dst: 领取后的目标路径;应为调用方进程唯一,避免并发互相覆盖。父目录会被
            自动创建。

    Returns:
        成功领取返回 True;源不存在(已被他人领取或本就不存在)返回 False。
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        return False
    return True


def merge_jsonl_into(src: str | Path, dst: str | Path, encoding: str = "utf-8") -> int:
    """把 src 的每一非空行 append 进 dst,成功后删除 src;返回合并行数。

    用 :func:`append_jsonl_line`(``O_APPEND``)逐行追加,因此不会覆盖 dst 中
    "合并期间新到" 的行。用于把领取的候选还回 ``candidates.jsonl``(LLM 失败回退),
    以及回收崩溃残留的孤儿领取文件。

    Args:
        src: 源 jsonl 文件路径;不存在时视为零行,直接返回 0。
        dst: 目标 jsonl 文件路径。
        encoding: 文本编码,默认 utf-8。

    Returns:
        实际合并(追加)的非空行数。
    """
    src = Path(src)
    if not src.exists():
        return 0
    merged = 0
    with open(src, encoding=encoding) as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            append_jsonl_line(dst, line, encoding=encoding)
            merged += 1
    src.unlink(missing_ok=True)
    return merged
