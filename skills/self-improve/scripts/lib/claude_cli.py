"""claude CLI 的薄封装。

提取与合并阶段都通过 ``claude --model claude-haiku-4-5 -p`` 一次性
完成;不使用流式 / 多轮交互,简化错误处理。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# 子进程会继承该环境变量;hook 入口检测到它即立即退出,
# 防止 claude -p 子会话递归触发 Stop/SessionStart hook。
HOOK_GUARD_ENV = "SELF_IMPROVE_HOOK"

# init 把 claude CLI 的绝对路径写在这里(scripts/config.json);运行时优先读它,
# 因为 hook 所在 shell(Windows 的 Git Bash/PowerShell)的 PATH 可能找不到裸名 claude。
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# 随包的设置文件,内容为 {"disableAllHooks": true}:调子 claude 时用 --settings 指向它,
# 从根上关掉子进程的所有 hook、杜绝递归(比仅靠 HOOK_GUARD_ENV 环境变量继承更可靠)。
_NO_HOOKS_SETTINGS = Path(__file__).resolve().parent.parent / "no_hooks.settings.json"


class ClaudeCliError(RuntimeError):
    """claude CLI 调用失败。"""


def _configured_claude() -> str | None:
    """从 scripts/config.json 读取 init 烘焙的 claude 绝对路径;无/损坏返回 None。"""
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    path = data.get("claude")
    return path.strip() if isinstance(path, str) and path.strip() else None


def _resolve_claude() -> str | None:
    """定位可用的 claude:优先 config.json 的绝对路径,回退到 PATH 查找。

    Returns:
        一个可执行的 claude 路径(绝对路径或 PATH 中找到的);都没有时返回 None。
    """
    configured = _configured_claude()
    if configured and Path(configured).exists():
        return configured
    return shutil.which("claude")


def _build_argv(claude: str, args: list[str]) -> list[str]:
    """构造 subprocess 参数;Windows 下 .cmd/.bat 需经 ``cmd /c`` 才能被拉起。"""
    if os.name == "nt" and claude.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", claude, *args]
    return [claude, *args]


def _disable_hooks_value() -> str:
    """``--settings`` 的取值:优先用随包设置文件的路径,缺失则回退为内联 JSON 字符串。

    用文件路径而非内联 JSON,是因为 Windows 上 claude 若为 ``.cmd`` 要经 ``cmd /c``,
    内联 JSON 里的引号极易被 cmd.exe 破坏;路径参数则不受影响。
    """
    if _NO_HOOKS_SETTINGS.exists():
        return str(_NO_HOOKS_SETTINGS)
    return '{"disableAllHooks":true}'


def is_available() -> bool:
    """检测 claude CLI 是否可用(config.json 绝对路径存在,或在 PATH 中)。"""
    return _resolve_claude() is not None


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
    claude = _resolve_claude()
    if claude is None:
        raise ClaudeCliError(
            "claude CLI not found(请检查 scripts/config.json 的 claude 绝对路径,"
            "或确保 claude 在 PATH 中)"
        )
    # 防递归:用 --settings 关掉子 claude 的所有 hook(主手段,见 _disable_hooks_value);
    # 同时保留下方注入的 HOOK_GUARD_ENV 作为兜底(双保险)。不能用 --bare —— 它在跳过
    # hooks 的同时会一并跳过 keychain reads,导致子进程读不到登录凭证而报 "Not logged in"。
    # prompt 走 stdin 而非命令行参数:避免 Windows 上经 cmd /c 时被 shell 的引号/编码
    # 处理弄坏或撞上命令行长度上限(表现为 claude 返回空);也契合 -p 从 stdin 读输入。
    # encoding 固定 UTF-8,确保中文 prompt/输出在 Windows(默认 cp936)下不乱码。
    cmd = _build_argv(
        claude,
        [
            "--model", model,
            "--output-format", output_format,
            "--settings", _disable_hooks_value(),
            "-p",
        ],
    )
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
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
