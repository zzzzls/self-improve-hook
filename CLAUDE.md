# 项目说明 (self-improve-hook)

本项目是一个**实验性的 Claude Code skill —— 经验提取流水线**:在任意启用它的项目里,自动从会话对话中提取"可执行的长期规则",去重合并后沉淀到该项目根的 `memory.generated.md`,再通过该项目 `CLAUDE.md` 的 `@` 引用回注到后续每个会话,让 Claude 在那个项目里持续自我改进。

技术栈:纯 Python 3 标准库(无第三方依赖),唯一外部依赖是 `claude` CLI(提取/合并使用 `claude-haiku-4-5` 模型)。

> 本仓库是 skill **本体**。通过 `/self-improve init` 安装到**目标项目**后,hook 在目标项目的 `.claude/` 下运行,并在其项目根生成 `memory.generated.md`。下文凡提到 `.claude/memory/`、(目标)项目根 `memory.generated.md` 等运行时路径,均指**被安装的目标项目**,而非本仓库自身。

## 架构与数据流

三个脚本驱动整条流水线(hook 均为 async、静默失败,不阻塞用户会话):

```
[Stop]         learn_on_stop.py
               增量读取 transcript 新增行 → 调 LLM(extract.md)提取候选
               → 追加 candidates.jsonl → 更新 state/<sid>.json

[SessionStart] session_start.py
               初始化当前会话 state → rename-claim 抢占遗留 active 会话补提取(异常恢复)
               → 回收崩溃残留的孤儿领取文件 → 若有候选,子进程触发 consolidate_memory.py

(子进程)       consolidate_memory.py
               原子领取 candidates.jsonl(rename 到进程唯一名,实现多进程互斥)
               → 读领取快照 + 现有 memory.generated.md → 调 LLM(consolidate.md)
               语义去重/冲突合并 → 原子重写目标项目根 memory.generated.md → 领取快照归档到 archive/
               (LLM 失败则把候选 merge 回 candidates.jsonl 重试)

目标项目根 memory.generated.md ──@引用──> 回注到后续会话(目标项目的 CLAUDE.md)
```

要点:**提取**发生在每次 `Stop`(增量、append-only);**合并**发生在下个会话的 `SessionStart`(没有独立的 SessionEnd hook)。也可随时用 `/self-improve update` 手动触发一次合并。

## 目录与关键文件(本仓库)

```
CLAUDE.md                     # 本说明
skills/self-improve/          # 流水线 skill —— 斜杠命令 /self-improve(init/update/destroy)
  SKILL.md                    # skill 入口:把动作分发给 manager.py
  manager.py                  # 编排器:init 启用 / update 仅合并 / destroy 仅拆 hook
  scripts/                    # 流水线脚本
    learn_on_stop.py          # Stop:提取候选
    session_start.py          # SessionStart:状态恢复 + 触发合并
    consolidate_memory.py     # 合并候选 → memory.generated.md(子进程调用)
    prompts/
      extract.md              # 提取 prompt(占位符 <<<CONVERSATION>>>)
      consolidate.md          # 合并 prompt(占位符 <<<CURRENT_MEMORY>>> / <<<CANDIDATES>>>)
    lib/
      state.py                # SessionState 读写/扫描(state/<sid>.json)
      transcript.py           # transcript.jsonl 切片 + slim_events 紧凑序列化
      claude_cli.py           # claude CLI 封装 + JSON 提取
      atomic.py               # 原子写 / append / move / claim 领取 / merge 合并
      runlog.py               # 记录每次 LLM 调用(prompt/response/meta)
```

安装到目标项目后,运行时在该项目下产生以下文件(均在 `.claude/memory/`,不属于本仓库):
`candidates.jsonl`(待合并候选)、`state/<sid>.json` + `hook.log`、`runs/<ts>-<event>-<sid8>/`(LLM 调用记录)、`archive/`(已合并候选备份);最终产出 `memory.generated.md` 落在**目标项目根**。

## 开发与调试

- 手动运行 Python 用 `python3`(或 `python`,见下方解释器探测);hook 由 Claude Code 自动触发,正常无需手动跑。
- 手动触发一次合并:优先用 `/self-improve update`;调试也可向 `consolidate_memory.py` 的 stdin 喂 JSON,如 `{"cwd": "<目标项目根>", "session_id": "manual", "reason": "debug"}`。
- 查看 LLM 实际调用:目标项目 `.claude/memory/runs/<时间戳>-*/` 下的 `prompt.txt` / `response.txt` / `meta.json`。
- 查看运行日志:目标项目 `.claude/memory/state/hook.log`。
- 改提取/合并行为:编辑 `skills/self-improve/scripts/prompts/extract.md` 与 `consolidate.md`(务必保留 `<<<...>>>` 占位符);候选类型与校验见 `scripts/learn_on_stop.py` 的 `_VALID_TYPES` 与 `_enrich()`。

## 关键约定与 Gotcha

- **静默失败**:所有 hook 捕获异常只写 `hook.log` 并返回 0,绝不影响主会话。
- **原子写**:文件写入全部走临时文件 + `os.replace`(`lib/atomic.py`);`candidates.jsonl` 依赖 `O_APPEND` 原子性以支持多会话并发追加。
- **增量提取**:`state/<sid>.json` 的 `last_processed_line` 记录已处理行号,每次 Stop 只处理新增行。
- **防循环**:Stop hook 收到 `stop_hook_active=true` 时立即返回。
- **6 种候选类型**(其它一律拒绝):`user_instruction` / `project_convention` / `error_lesson` / `tool_preference` / `workflow_convention` / `issue_fix`。
- **多窗口并发安全(原子领取)**:同时打开多个窗口会触发多个 `consolidate`。consolidate 进来先用 `atomic.claim_file` 把 `candidates.jsonl` 经 `os.replace` 原子改名成进程唯一名(`candidates.claiming-<pid>-<token>.jsonl`),只有抢到的进程跑 LLM,其余秒退 —— 这是天然的 N 进程互斥点。领取后 Stop hook 的新候选落到新建的 `candidates.jsonl`,不会被本轮归档(零丢失)。恢复路径对遗留 `<sid>.json` 同样做 rename-claim,避免并发重复提取。**刻意不用 flock**:项目在 9p/DrvFs 挂载上 flock 不可靠,互斥一律走 `os.replace`。
- **崩溃自愈**:consolidate 崩溃残留的领取文件(`candidates.claiming-*`)、恢复崩溃残留的 `<sid>.json.recovering-*`,都由下次 `SessionStart` 扫描回收(靠 `os.kill(pid,0)` 区分孤儿与在跑进程,跳过存活 owner)。
- **合并仍非幂等(语义)**:consolidate 领取后即把候选移走 —— LLM 成功则归档,失败则 `merge_jsonl_into` 把候选追加回 `candidates.jsonl` 重试。手动调试时第二次调用会发现没东西可领(`claim_file` 返回 False)直接跳过。
- **启用/禁用走 `/self-improve` skill**:推荐 `/self-improve init` 在目标项目启用(把 hook 写进该项目个人 `.claude/settings.local.json`,不入库)、`/self-improve update` 手动合并、`/self-improve destroy` 移除(仅拆 hook 配置,保留 `.claude/memory/` 与 `CLAUDE.md` 引用)。
- **Python 解释器自适应探测(两层,不硬编码 `python3`)**:启用路径不写死 `python3`,而是按 `python3 → python` 顺序探测,并**实际执行**校验主版本号为 3(拒绝 Python 2),选用第一个可用者。两层各管一处:① **`SKILL.md` bootstrap** 决定用哪个解释器去启动 `manager.py` 自身(解决"机器只有 `python`、没有 `python3`"时连管理器都起不来);② **`manager.py` 的 `_detect_python()`** 决定烘焙进 `settings.local.json` 的 hook command 用哪个解释器(`init` 流程,见 `PYTHON_CANDIDATES` 常量)。两层用同一套规则、文案统一。**探测不到 Python 3 则提醒用户并停止**:SKILL 层 `echo` 提示后 `exit 1`、不调 manager;`init` 层打印提示后 `return 1`、不写 hook、也不动目标项目的 `CLAUDE.md` / `memory`。运行时无需再探测——`session_start.py` 用 `sys.executable` spawn `consolidate`,自动跟随启动该 hook 的解释器。
- **烘焙绝对路径,不用裸名(Windows 必需)**:hook 的 `command` 由 Claude Code 用**另一个 shell** 执行——macOS/Linux 是 `sh -c`,**Windows 是 Git Bash(没装则 PowerShell)**,均**不是**启动 claude 的那个环境,PATH 也不同。所以裸名 `python` / `claude` 在 Windows 的 hook shell 里常解析不到(或落到商店占位 `python.exe`),表现为"hook 静默不执行、连 `hook.log` 都没有"。修法:`_detect_python()` / `_detect_claude()` 用 `shutil.which` 解析成**绝对路径**;`init` 把它们写进 skill 下 `scripts/config.json`(`{"python","claude"}`,本机相关、`.gitignore` 忽略、用户可手改),并把 python 绝对路径(引号包裹,容空格)烘焙进 hook command。运行时 `lib/claude_cli.py` 优先从 `config.json` 读 claude 绝对路径(回退 `which`→裸名),`is_available()` 同源;Windows 上若 claude 解析为 `.cmd`/`.bat`,`_build_argv` 经 `cmd /c` 拉起(`CreateProcess` 不能直接跑 `.cmd`)。**重跑 `init` 不覆盖手改值**:`_config_choose` 优先沿用 `config.json` 里仍指向存在文件的旧值。python 的运行时真值在 hook command 里(settings 无法间接读 config),`config.json` 同时记一份便于查看/重写。
