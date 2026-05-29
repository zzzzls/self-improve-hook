# self-improve-hook

> 实验性的 Claude Code skill:让 Claude 在你的项目里**从对话中持续自我改进**。

`self-improve-hook` 是一条**经验提取流水线**。它借助 Claude Code 的 hook 机制,在每次会话里自动从对话中提取"可执行的长期规则"(用户指令、项目约定、错误教训、工具偏好等),去重合并后沉淀进项目根的 `memory.generated.md`,再经 `CLAUDE.md` 的 `@` 引用回注到后续每个会话 —— 于是 Claude 在这个项目里越用越"懂行"。

- **纯标准库**:只依赖 Python 3 与 `claude` CLI(提取/合并用 `claude-haiku-4-5`),无第三方包。
- **静默、异步、不阻塞**:所有 hook 捕获异常只记日志并返回 0,绝不打断你的会话。
- **并发安全**:多窗口并发靠 `os.replace` 原子领取实现 N 进程互斥,并具备崩溃自愈(刻意不用 flock,9p/DrvFs 友好)。

## 工作原理

```
[Stop]          learn_on_stop.py       增量读取 transcript → LLM 提取候选 → 追加 candidates.jsonl
[SessionStart]  session_start.py       恢复遗留会话 + 回收孤儿领取 → 触发合并
(子进程)         consolidate_memory.py   原子领取候选 + 现有 memory → LLM 去重合并 → 重写 memory.generated.md

memory.generated.md ──@引用──> 回注到后续每个会话
```

- **提取**发生在每次 `Stop`(增量、append-only)。
- **合并**发生在下个会话的 `SessionStart`(无独立的 SessionEnd hook),也可随时手动触发。

## 安装与使用

1. 把 `skills/self-improve/` 放到你的 Claude Code skills 目录(个人 `~/.claude/skills/` 或项目 `.claude/skills/`)。
2. 在目标项目里启用:
   ```
   /self-improve init
   ```
   这会把 SessionStart/Stop hook 写入该项目个人 `.claude/settings.local.json`(不入库),在 `CLAUDE.md` 接好 `@memory.generated.md` 引用,并建好 `.claude/memory/` 骨架。
3. 正常使用 Claude Code 即可 —— 经验会自动积累。

### 斜杠命令

| 命令 | 作用 |
|------|------|
| `/self-improve init` | 在当前项目启用(写 hook 配置 + 接好 memory 引用 + 建骨架) |
| `/self-improve update` | 刷新记忆文档:把已排队候选合并进 `memory.generated.md`(不提取当前会话) |
| `/self-improve destroy` | 移除:仅拆 hook 配置,保留记忆与 `CLAUDE.md` 引用 |

> hook 通常在**下次会话**才被 Claude Code 加载(可能需经 `/hooks` 审核);`update` 当下即可用。

## 要求

- Python 3(命令名 `python3` 或 `python` 均可,skill 会自适应探测并校验主版本为 3)。
- 已登录的 `claude` CLI。

## 仓库结构

完整说明见 [`CLAUDE.md`](./CLAUDE.md)。核心是 `skills/self-improve/`:`SKILL.md`(入口)、`manager.py`(init/update/destroy 编排器)、`scripts/`(hook 脚本 + `lib/` + `prompts/`)。

## 候选类型

提取只接受以下 6 类规则,其余一律拒绝:`user_instruction`、`project_convention`、`error_lesson`、`tool_preference`、`workflow_convention`、`issue_fix`。

## 说明

实验性项目,主要在 Linux / WSL2 环境验证。所有自动维护的记忆(`memory.generated.md`)均可人工编辑或删除;如发现错误规则,删除对应行,或调整 `skills/self-improve/scripts/prompts/extract.md` 后让其在下个 session 重新生成。
