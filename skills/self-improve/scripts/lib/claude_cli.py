"""claude CLI 的薄封装。

提取与合并阶段都通过 ``claude --model claude-haiku-4-5 -p`` 一次性
完成;不使用流式 / 多轮交互,简化错误处理。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

# 子进程会继承该环境变量;hook 入口检测到它即立即退出,
# 防止 claude -p 子会话递归触发 Stop/SessionStart hook。
HOOK_GUARD_ENV = "SELF_IMPROVE_HOOK"


class ClaudeCliError(RuntimeError):
    """claude CLI 调用失败。"""


def is_available() -> bool:
    """检测 claude CLI 是否在 PATH 中可用。"""
    return shutil.which("claude") is not None


def run(
    prompt: str,
    *,
    model: str = "claude-haiku-4-5",
    output_format: str = "text",
    timeout: int = 90,
) -> str:
    """以非交互方式调用 claude CLI 并返回文本输出。

    Args:
        prompt: 完整 prompt 字符串,通过 stdin 传入避免命令行长度限制。
        model: 模型 ID,默认 Haiku。
        output_format: ``text`` 或 ``json``;``json`` 时调用方负责解析。
        timeout: 超时秒数。

    Returns:
        CLI 的 stdout(已 strip)。

    Raises:
        ClaudeCliError: CLI 不可用、返回非 0 或超时。
    """
    if not is_available():
        raise ClaudeCliError("claude CLI not found in PATH")
    cmd = [
        "claude",
        # 注意:不能用 --bare —— 它在跳过 hooks 的同时会一并跳过 keychain reads,
        # 导致子进程读不到登录凭证而报 "Not logged in" 退出。防递归改为依赖
        # 下方注入的 HOOK_GUARD_ENV(两个 hook 入口检测到它即立即退出)。
        "--model",
        model,
        "--output-format",
        output_format,
        "-p",
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, HOOK_GUARD_ENV: "1"},
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeCliError(f"claude CLI timed out after {timeout}s") from e
    except OSError as e:
        raise ClaudeCliError(f"failed to spawn claude CLI: {e}") from e
    if result.returncode != 0:
        # "Not logged in" 等错误会写到 stdout 而非 stderr,两者都带上便于排查。
        detail = (result.stderr.strip() or result.stdout.strip())[:500]
        raise ClaudeCliError(f"claude CLI exited with {result.returncode}: {detail}")
    return result.stdout.strip()


def run_json(prompt: str, **kwargs: Any) -> dict[str, Any]:
    """``run`` 的便捷封装,要求模型输出 JSON 并解析。

    会从输出中尽可能提取首个 JSON 对象,容忍模型在 JSON 前后输出解释文本。

    Raises:
        ClaudeCliError: 输出无法解析为 JSON 对象。
    """
    raw = run(prompt, output_format="text", **kwargs)
    obj = extract_json_object(raw)
    if obj is None:
        raise ClaudeCliError(f"claude CLI output is not valid JSON: {raw[:200]}")
    return obj


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从可能带有 ``` fence 或前后解释的文本中提取首个 JSON 对象。"""
    if not text:
        return None
    candidates: list[str] = []
    fence_start = text.find("```")
    if fence_start != -1:
        rest = text[fence_start + 3 :]
        if rest.lstrip().lower().startswith("json"):
            rest = rest.lstrip()[4:]
        fence_end = rest.find("```")
        if fence_end != -1:
            candidates.append(rest[:fence_end])
    candidates.append(text)
    for cand in candidates:
        start = cand.find("{")
        end = cand.rfind("}")
        if start == -1 or end == -1 or end <= start:
            continue
        try:
            return json.loads(cand[start : end + 1])
        except json.JSONDecodeError:
            continue
    return None
