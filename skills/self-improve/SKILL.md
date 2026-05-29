---
name: self-improve
description: 管理 self-improve 经验提取流水线 —— 在当前项目启用(init)、刷新记忆文档(update)、或移除(destroy)。当用户输入 /self-improve init|update|destroy，或要求"启用/刷新/移除 self-improve、刷新项目记忆、把候选合并进 memory.generated.md"时使用。
disable-model-invocation: true
allowed-tools: Bash
argument-hint: [init | update | destroy]
---

# self-improve 流水线管理器

用户通过 `/self-improve <动作>` 触发，`<动作>` 为 `init` / `update` / `destroy` 之一。

执行下面这**一段**命令（它会自适应选用 Python 3 解释器，并在当前项目根运行 `manager.py`），然后把命令的标准输出**原样**转述给用户：

```bash
# 自适应选用 Python 3 解释器：优先 python3，回退 python；都不可用则提示后退出（不运行 manager）
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "未检测到可用的 Python 3 解释器（已尝试 python3 / python）。请先安装 Python 3 并确保其在 PATH 中（命令名为 python3 或 python），再运行 /self-improve。"
  exit 1
fi
"$PY" "${CLAUDE_SKILL_DIR}/manager.py" --action "$ARGUMENTS"
```

## 三个动作

- **`init`** — 在当前项目启用 self-improve：把 SessionStart/Stop hook 合并写入 `.claude/settings.local.json`（个人、不入库），并确保 `CLAUDE.md` 含 `@memory.generated.md` 引用，建好 `.claude/memory/` 骨架。
- **`update`** — 刷新记忆文档：调 `consolidate_memory.py` 把已排队候选（`candidates.jsonl`）合并进 `memory.generated.md`。**不**提取当前会话对话。
- **`destroy`** — 移除 self-improve：**仅**从 `.claude/settings.local.json` 移除本工具的 hook 条目；**保留** `.claude/memory/`（记忆）与 `CLAUDE.md` 引用。

## 注意

- 若 `$ARGUMENTS` 为空或不是上述三者，`manager.py` 会打印用法说明，照常转述即可。
- `init` 写入的 hook 通常在**下次会话**才被 Claude Code 加载（可能需经 `/hooks` 审核批准）；`update` 当下立即生效。
- `manager.py` 的标准输出就是要给用户看的结果，**直接转述、不要臆测或省略**；若它返回非 0 或打印到 stderr，如实告知用户。
