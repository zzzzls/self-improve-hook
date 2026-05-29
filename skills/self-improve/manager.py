#!/usr/bin/env python3
"""self-improve skill 的编排入口:init / update / destroy。

由 ``SKILL.md`` 通过单条 Bash 调用::

    python3 manager.py --action <init|update|destroy> [--project <项目根>]

设计要点
--------
- **自包含**:被管理的流水线脚本位于本文件同级的 ``scripts/``,靠 ``SCRIPTS_DIR``
  解析为**绝对路径**;skill 整体迁到别处后自动重解析,无需改代码。
- **纯标准库**:与被管理的脚本保持一致,不引第三方依赖。
- **原子写**:所有落盘走临时文件 + ``os.replace``。
- ``init`` 写个人 ``.claude/settings.local.json``(不入库),hook command 烘焙脚本绝对路径。
- ``update`` 仅合并已排队候选(跑 ``consolidate_memory.py``),**不**提取 transcript。
- ``destroy`` 仅移除本工具写入的 hook 条目,保留记忆与 CLAUDE.md 引用。

stdout 即给用户看的结果报告;参数错误或异常时报错到 stderr 并返回非 0。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
# 运行时配置:记录探测到的解释器/CLI 绝对路径,供 hook 与脚本读取(用户可手改)
CONFIG_PATH = SCRIPTS_DIR / "config.json"

MEMORY_DIRNAME = ".claude/memory"
# 最终产出落在项目根,内部文件(候选/归档/state)仍在 MEMORY_DIRNAME 下
MEMORY_FILENAME = "memory.generated.md"
SETTINGS_LOCAL_REL = ".claude/settings.local.json"
CLAUDE_MD_NAME = "CLAUDE.md"
MEMORY_REF = f"@{MEMORY_FILENAME}"
MANAGED_BEGIN = "<!-- self-improve:begin -->"
MANAGED_END = "<!-- self-improve:end -->"

# (hook 事件, 脚本文件名, 超时秒数) —— 与原 settings.json1 保持一致
HOOK_SPECS = (
    ("SessionStart", "session_start.py", 300),
    ("Stop", "learn_on_stop.py", 120),
)
# 识别"本工具写入的 hook":command 含这些脚本名即视为本工具所有
OUR_SCRIPT_NAMES = ("session_start.py", "learn_on_stop.py")
CONSOLIDATE_SCRIPT = "consolidate_memory.py"

# init 烘焙 hook command 前,按此顺序探测解释器,选用第一个确为 Python 3 的;
# 烘焙/记录的是其**绝对路径**(hook 在 Windows 的 Git Bash/PowerShell 里跑,裸名可能解析不到)
PYTHON_CANDIDATES = ("python3", "python")

ACTIONS = ("init", "update", "destroy")
# 合并子进程超时:略大于 consolidate_memory.py 内部 LLM 调用的 180s
CONSOLIDATE_TIMEOUT = 210

SEED_MEMORY = """# 项目记忆(自动生成)

> 由经验提取流水线维护。最后更新:{updated_at}

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


def _now_iso() -> str:
    """返回当前本地时间的 ISO8601(秒级)字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    """用临时文件 + ``os.replace`` 原子写入文本。

    Args:
        path: 目标文件路径,父目录不存在会自动创建。
        text: 待写入的完整文本。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json_obj(path: Path) -> dict:
    """读取 JSON 对象;不存在返回空 dict,格式非法则抛错以避免覆盖。

    Args:
        path: JSON 文件路径。

    Returns:
        解析得到的 dict;文件不存在时为空 dict。

    Raises:
        ValueError: 文件存在但不是合法 JSON 对象。
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} 不是合法 JSON,已中止以避免覆盖: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层不是 JSON 对象,已中止")
    return data


def _command_is_ours(command: str) -> bool:
    """判断一条 hook command 是否由本工具写入(按脚本文件名匹配)。"""
    return any(name in command for name in OUR_SCRIPT_NAMES)


def _event_has_script(groups: list, script: str) -> bool:
    """检查某事件的 matcher 组里是否已存在引用 ``script`` 的 hook。"""
    return any(
        script in hook.get("command", "")
        for group in groups
        for hook in group.get("hooks", [])
    )


def _detect_python() -> str | None:
    """探测一个可用的 Python 3 解释器,返回其**绝对路径**(供烘焙 hook command 用)。

    按 :data:`PYTHON_CANDIDATES` 顺序(``python3`` → ``python``)逐个尝试:
    用 ``shutil.which`` 把命令名解析为绝对路径,再实际执行确认主版本号为 3。
    返回绝对路径而非裸名,是因为 hook 在 Windows 上由 Git Bash/PowerShell 执行,
    其 PATH 可能与安装环境不同,裸名 ``python`` 常解析不到(或落到商店占位)。

    Returns:
        第一个确为 Python 3.x 的解释器**绝对路径**;都不可用时返回 None。
    """
    for name in PYTHON_CANDIDATES:
        exe = shutil.which(name)
        if exe is None:
            continue
        try:
            proc = subprocess.run(
                [exe, "-c", "import sys; print(sys.version_info[0])"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == "3":
            return exe
    return None


def _detect_claude() -> str | None:
    """把 ``claude`` CLI 解析为绝对路径(``shutil.which``);找不到返回 None。

    与 Python 同理:运行时 hook 所在 shell 的 PATH 可能找不到裸名 ``claude``,
    故烘焙绝对路径写入 config.json,由 ``lib/claude_cli.py`` 读取。
    """
    return shutil.which("claude")


def _read_config() -> dict:
    """读取 skill 下的运行时 config.json;不存在或损坏一律视为空(由 init 重建)。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_choose(existing: object, detected: str | None) -> str | None:
    """优先沿用 config.json 里仍有效(文件存在)的手填/旧值,否则用新探测值。

    这样用户对 config.json 的手动修改不会被后续 ``init`` 覆盖。
    """
    if isinstance(existing, str) and existing.strip() and Path(existing.strip()).exists():
        return existing.strip()
    return detected


def cmd_init(project: Path) -> int:
    """在 ``project`` 启用 self-improve:探测解释器/CLI 绝对路径并写 config.json,
    再接 CLAUDE.md、写 hook、建骨架。"""
    lines = [f"self-improve init @ {project}"]
    existing = _read_config()
    python_path = _config_choose(existing.get("python"), _detect_python())
    if python_path is None:
        lines.append(
            "• 未检测到可用的 Python 3 解释器(已尝试 "
            + " / ".join(PYTHON_CANDIDATES)
            + ")"
        )
        lines.append(
            "  请先安装 Python 3 并确保其在 PATH 中(命令名为 python3 或 python),"
            "再重新运行 /self-improve init。"
        )
        lines.append("• 已中止:未创建任何 hook,也未改动 CLAUDE.md / memory。")
        print("\n".join(lines))
        return 1
    lines.append(f"• Python 3 解释器(绝对路径):{python_path}")
    claude_path = _config_choose(existing.get("claude"), _detect_claude())
    if claude_path:
        lines.append(f"• claude CLI(绝对路径):{claude_path}")
    else:
        lines.append(
            "• 未在 PATH 中找到 claude CLI:config.json 的 claude 暂留空,"
            "请手动填入其绝对路径(否则提取/合并会被静默跳过)"
        )
    _init_config(python_path, claude_path, lines)
    _init_claude_md(project, lines)
    _init_settings(project, python_path, lines)
    _init_memory_skeleton(project, lines)
    lines.append(
        "提示:自动 hook 通常下次会话才加载(可能需经 /hooks 审核);update 立即可用。"
    )
    lines.append(
        f"提示:解释器/CLI 路径记录在 {CONFIG_PATH},可手动修改(后续 init 不会覆盖仍有效的值)。"
    )
    print("\n".join(lines))
    return 0


def _init_config(python_path: str, claude_path: str | None, lines: list[str]) -> None:
    """把探测到的解释器/CLI 绝对路径写入 skill 下的 config.json(运行时读取、用户可手改)。"""
    config = {"python": python_path, "claude": claude_path}
    _atomic_write_text(
        CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    )
    lines.append(f"• 写入运行时配置:{CONFIG_PATH}")


def _init_claude_md(project: Path, lines: list[str]) -> None:
    """确保 CLAUDE.md 含 memory 引用;先检测存在性,已有则不重复追加。"""
    path = project / CLAUDE_MD_NAME
    existed = path.exists()
    text = path.read_text(encoding="utf-8") if existed else ""
    if MEMORY_REF in text:
        lines.append(f"• CLAUDE.md 已含 {MEMORY_REF},跳过追加")
        return
    if text and not text.endswith("\n"):
        text += "\n"
    block = f"{MANAGED_BEGIN}\n{MEMORY_REF}\n{MANAGED_END}\n"
    separator = "\n" if text else ""
    _atomic_write_text(path, text + separator + block)
    lines.append(
        "• 创建 CLAUDE.md 并写入托管块" if not existed else "• CLAUDE.md 追加托管块"
    )


def _init_settings(project: Path, python_path: str, lines: list[str]) -> None:
    """把 SessionStart/Stop hook 合并进 ``.claude/settings.local.json``(不覆盖已有配置)。

    Args:
        project: 目标项目根。
        python_path: 已确认为 Python 3 的解释器**绝对路径**,烘焙进 hook command
            (绝对路径 + 引号包裹,兼容含空格的安装目录与 Windows 的 Git Bash/PowerShell)。
        lines: 累加输出报告行的列表。
    """
    path = project / SETTINGS_LOCAL_REL
    config = _read_json_obj(path)
    hooks = config.setdefault("hooks", {})
    changed = False
    for event, script, timeout in HOOK_SPECS:
        groups = hooks.setdefault(event, [])
        if _event_has_script(groups, script):
            lines.append(f"• settings.local.json {event} 已有本工具 hook,跳过")
            continue
        command = f'"{python_path}" "{SCRIPTS_DIR / script}"'
        groups.append(
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "async": True,
                        "timeout": timeout,
                    }
                ],
            }
        )
        changed = True
        lines.append(f"• settings.local.json 添加 {event} hook → {script}")
    if changed:
        _atomic_write_text(
            path, json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        )
    else:
        lines.append("• settings.local.json 无需改动")


def _init_memory_skeleton(project: Path, lines: list[str]) -> None:
    """确保 ``.claude/memory/state/`` 存在,并在缺失时写入种子 memory.generated.md。"""
    memory_dir = project / MEMORY_DIRNAME
    (memory_dir / "state").mkdir(parents=True, exist_ok=True)
    memory_file = project / MEMORY_FILENAME
    if memory_file.exists():
        lines.append("• memory.generated.md 已存在,保留")
    else:
        _atomic_write_text(memory_file, SEED_MEMORY.format(updated_at=_now_iso()))
        lines.append("• 创建种子 memory.generated.md")


def cmd_update(project: Path) -> int:
    """仅合并:跑 consolidate_memory.py 把已排队候选并入 memory.generated.md。"""
    consolidate = SCRIPTS_DIR / CONSOLIDATE_SCRIPT
    if not consolidate.exists():
        print(f"找不到合并脚本: {consolidate}", file=sys.stderr)
        return 1

    memory_dir = project / MEMORY_DIRNAME
    memory_file = project / MEMORY_FILENAME
    hook_log = memory_dir / "state" / "hook.log"

    log_before = hook_log.stat().st_size if hook_log.exists() else 0
    mem_before = (
        memory_file.read_text(encoding="utf-8") if memory_file.exists() else None
    )

    payload = {
        "cwd": str(project),
        "session_id": "manual-update",
        "reason": "manual-update",
    }
    try:
        subprocess.run(
            [sys.executable, str(consolidate)],
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",  # 与 consolidate 的 UTF-8 stdin 读取对齐(Windows 默认 cp936 会错配)
            timeout=CONSOLIDATE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"合并子进程超时(>{CONSOLIDATE_TIMEOUT}s)", file=sys.stderr)
        return 1

    appended = _read_text_tail(hook_log, log_before)
    mem_after = (
        memory_file.read_text(encoding="utf-8") if memory_file.exists() else None
    )

    lines = [f"self-improve update @ {project}"]
    if "nothing to claim" in appended:
        lines.append("• 无候选可合并(candidates.jsonl 为空或已被其它进程领取)")
    elif "consolidated" in appended:
        lines.append("• 已合并候选并更新 memory.generated.md")
    elif appended:
        lines.append("• 合并未完成(LLM 失败或不可用),候选已原样保留以便重试")
    else:
        lines.append("• 合并脚本无新日志输出,请查看 hook.log")
    lines.append(
        f"• memory.generated.md {'已更新' if mem_after != mem_before else '无变化'}"
    )
    lines.append(f"• 日志: {hook_log}")
    lines.append(f"• LLM 调用记录目录: {memory_dir / 'runs'}")
    if appended.strip():
        lines.append("--- 本次新增日志 ---")
        lines.append(appended.strip())
    print("\n".join(lines))
    return 0


def _read_text_tail(path: Path, offset: int) -> str:
    """读取文件从 ``offset`` 字节到结尾的内容;不存在或失败返回空串。"""
    if not path.exists():
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


def cmd_destroy(project: Path) -> int:
    """仅移除本工具写入的 hook 条目;保留 .claude/memory/ 与 CLAUDE.md 引用。"""
    path = project / SETTINGS_LOCAL_REL
    lines = [f"self-improve destroy @ {project}"]
    if not path.exists():
        lines.append("• 未发现 .claude/settings.local.json,无 hook 可移除")
        print("\n".join(lines))
        return 0

    config = _read_json_obj(path)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        lines.append("• settings.local.json 无 hooks 段,无操作")
        print("\n".join(lines))
        return 0

    removed = 0
    for event in ("SessionStart", "Stop"):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            entries = group.get("hooks", [])
            kept = [h for h in entries if not _command_is_ours(h.get("command", ""))]
            removed += len(entries) - len(kept)
            if kept:
                group["hooks"] = kept
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        config.pop("hooks", None)

    if removed:
        _atomic_write_text(
            path, json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        )
        lines.append(f"• 已从 settings.local.json 移除 {removed} 个本工具 hook 条目")
    else:
        lines.append("• 未发现本工具的 hook 条目,无操作")
    lines.append("• 已保留 .claude/memory/(记忆未删) 与 CLAUDE.md 引用")
    print("\n".join(lines))
    return 0


def _usage() -> str:
    return (
        "用法: /self-improve <init|update|destroy>\n"
        "  init    在当前项目启用 self-improve(写 hook 配置 + 接好 memory 引用)\n"
        "  update  刷新记忆文档(合并已排队候选 → memory.generated.md)\n"
        "  destroy 移除 self-improve(仅拆 hook 配置,保留记忆)"
    )


def main() -> int:
    """解析参数并分发到对应动作。"""
    parser = argparse.ArgumentParser(
        prog="self-improve", description="self-improve 流水线管理器"
    )
    parser.add_argument("--action", default="", help="init | update | destroy")
    parser.add_argument("--project", default="", help="目标项目根(默认当前工作目录)")
    args = parser.parse_args()

    tokens = args.action.strip().split()
    action = tokens[0].lower() if tokens else ""
    if action not in ACTIONS:
        print(_usage(), file=sys.stderr)
        return 2

    project = Path(args.project).resolve() if args.project.strip() else Path.cwd()
    if not project.is_dir():
        print(f"项目目录不存在: {project}", file=sys.stderr)
        return 1

    try:
        if action == "init":
            return cmd_init(project)
        if action == "update":
            return cmd_update(project)
        return cmd_destroy(project)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
