# VISTA 用户手册

面向使用者的完整文档。想了解**为什么这么设计**，请看仓库根目录的 `DESIGN.md`。

---

## 目录

1. [安装](#1-安装)
2. [第一次运行](#2-第一次运行)
3. [配置模型](#3-配置模型)
4. [两种工作方式](#4-两种工作方式)
5. [交互模式的斜杠命令](#5-交互模式的斜杠命令)
6. [理解 VISTA 的输出](#6-理解-vista-的输出)
7. [权限与安全](#7-权限与安全)
8. [记忆系统的日常维护](#8-记忆系统的日常维护)
9. [会话与报告](#9-会话与报告)
10. [配置项完整参考](#10-配置项完整参考)
11. [跑评测与消融实验](#11-跑评测与消融实验)
12. [常见问题排查](#12-常见问题排查)
13. [把 VISTA 用在你自己的项目上](#13-把-vista-用在你自己的项目上)

---

## 1. 安装

**要求**：Python 3.11 或更高版本。**没有其它必需依赖。**

```bash
git clone <仓库地址>
cd vista
pip install -e .
```

`pip install -e .` 只做一件事：把 `vista` 命令装进 PATH。它不会拉取任何第三方包。

验证：

```bash
vista --version
vista doctor
```

### 可选增强

```bash
pip install -e ".[enhanced]"     # tiktoken + tree-sitter + PyYAML
pip install -e ".[sdk]"          # openai 官方 SDK
```

| 装了它 | 会变好 | 不装的话 |
|---|---|---|
| `tiktoken` | token 计数精确 | 中英混合启发式估算，偏差 ±15% |
| `tree_sitter_language_pack` | 符号抽取用语法树 | 正则抽取器，覆盖 12 种语言 |
| `PyYAML` | 技能卡解析更宽容 | 内置 YAML 子集解析器 |
| `openai` | 可用 `--provider openai` | 默认 `http` provider 只用标准库 |
| `ripgrep`（系统命令） | grep 更快 | Python 正则遍历 |

`vista doctor` 会逐项打印当前状态。**所有"不可用"都有降级路径，不影响使用。**

---

## 2. 第一次运行

不需要 API key，直接看系统在做什么：

```bash
vista demo
```

这会在临时目录生成一个带 bug 的小项目，然后用预录脚本跑完整个流程。你会看到：

```
  · 正在采集验收基线…
  · 基线：退出码 1，已有 4 个失败用例（unittest）：...
· 第 1 步   [2,996 tok]
  ▸ todo_write(items=[...])
· 第 2 步   [3,123 tok]
  ▸ repo_map(focus=['src/auth.py'], budget=400)
· 第 3 步   [3,473 tok]
  ▸ grep(pattern=utcnow|now\(, path=src, context=1)
· 第 4 步   [3,701 tok]
  ▸ read_file(path=src/auth.py)
  ⟐ 上下文压缩：0.4k → 0.2k tokens（丢弃 1 条可重取内容，保留 1 个锚点）
· 第 5 步   [3,993 tok]
  ▸ edit_file(path=src/auth.py, ...)
...
  ⊙ 正在执行 Verify-Gate 验收…
  ✓ 验收通过（test）
  ◆ 项目记忆已更新：.../project.md
  ◆ 已蒸馏技能卡：修复时区不一致导致的比较报错 → .../fix-naive-aware-datetime.yaml
```

结束后产物会被复制到当前目录的 `.vista/demo/`，包括一份 HTML 报告——
用浏览器打开就能看到上下文 token 的波形图。

---

## 3. 配置模型

### 环境变量（推荐）

```bash
export VISTA_API_KEY=sk-...
export VISTA_BASE_URL=https://api.openai.com/v1
export VISTA_MODEL=gpt-4o-mini
export VISTA_WEAK_MODEL=gpt-4o-mini
```

任何 OpenAI 兼容的网关都可以，只要它支持 `/chat/completions` 与原生 tool calling。
把 `VISTA_BASE_URL` 指过去即可。

> **凭据只从环境变量读取。** 如果你在配置文件里写了 `api_key`，
> VISTA 启动时会直接报错退出。这是为了保证凭据不会被误提交进版本库。

也可以用 `.env` 文件（仓库里有 `.env.example`），但需要你自己 source 它：

```bash
cp .env.example .env
# 编辑 .env 填入凭据
set -a && source .env && set +a
```

### 两个模型角色

| 角色 | 干什么 | 配什么 |
|---|---|---|
| `main` | 主循环的推理与决策 | 强模型，tool calling 要稳 |
| `weak` | 上下文压缩、技能卡蒸馏 | 便宜模型即可 |

`weak` 只做"把已有信息重新组织"，不做决策。留空时自动复用 `main`。

### 配置文件

```bash
vista init      # 在当前项目生成 .vista/config.toml 模板
```

优先级（低 → 高）：内置默认 → `~/.vista/config.toml` →
`<项目>/.vista/config.toml` → 环境变量 → 命令行参数。

### 自检

```bash
vista doctor
```

会打印 Python 版本、各可选依赖状态、模型配置、API key 是否设置、
上下文预算与压缩阈值、以及对当前项目探测到的测试与静态检查命令。

---

## 4. 两种工作方式

### 一次性任务

```bash
vista run "给 /api/todos 接口加上分页，参数 page 和 per_page，默认每页 20 条"
vista run -f task.md                    # 任务写在文件里
echo "修复登录超时" | vista run          # 从标准输入读
```

常用参数：

```bash
vista --max-steps 60 --budget 2.0 run "重构用户模块"
vista --yolo run "..."                  # 跳过所有权限确认（危险）
vista run "..." --report                # 结束后生成 HTML 报告
vista run "..." --json                  # 机器可读输出（评测用）
```

**注意参数顺序**：`--max-steps`、`--budget`、`--yolo`、消融开关都是**全局参数**，
必须写在 `run` 之前；`--json`、`--report`、`-f` 是 `run` 的子命令参数，写在后面。

### 交互模式

```bash
vista
```

一个会话里可以连续下达多个任务。文件指纹账本、项目记忆、仓库索引缓存、
快照栈都是跨任务保持的，所以第二个任务不需要重新读一遍文件。

---

## 5. 交互模式的斜杠命令

| 命令 | 作用 |
|---|---|
| `/help` | 显示帮助 |
| `/context` | **打印上下文的分层构成与 token 分布** |
| `/cost` | token 与成本统计，按模型角色分列 |
| `/todo` | 当前任务清单 |
| `/undo [snap-id]` | 回滚最近一次（或指定的）文件快照 |
| `/snapshots` | 列出本会话全部快照 |
| `/skills` | 列出技能库（L3） |
| `/memory` | 打印项目记忆（L2） |
| `/map [文件...]` | 打印仓库索引（L1），可指定聚焦文件 |
| `/verify` | 立即执行一次 Verify-Gate（不改变循环状态） |
| `/report` | 为最近一次任务生成 HTML 报告 |
| `/model` | 当前模型与 provider 配置 |
| `/clear` | 清空对话历史（记忆层与账本保留） |
| `/quit` | 退出 |

`/context` 的输出最值得关注：

```
上下文构成（共 18,204 / 100,000 tokens，18%）
  ├ 系统提示与安全约束     1,490
  ├ L2 项目记忆              384
  ├ L1 仓库索引            1,012
  ├ L3 技能卡                421
  ├ 工具 schema            1,860
  ├ 任务（pinned）            42
  ├ 任务清单（pinned）        118
  ├ 压缩标记 x2            2,240
  └ 近期事件              10,637
  压缩阈值 60,000 tokens（θ=0.6）；已压缩 2 次；历史事件 47 条（存活 12 条）
```

「历史事件 47 条（存活 12 条）」这一行体现的是：历史只追加不删除，
压缩是通过标记 + 计算视图实现的。

---

## 6. 理解 VISTA 的输出

### 步骤行

```
· 第 7 步   [12,483 tok]
  ▸ read_file(path=src/auth.py)
  ✗ edit_file → STALE_CONTEXT
  ⟐ 上下文压缩：22.4k → 2.1k tokens（丢弃 11 条可重取内容，保留 8 个锚点）
  ⊙ 正在执行 Verify-Gate 验收…
  ✓ 验收通过（test）
  ◆ 已蒸馏技能卡：...
```

| 符号 | 含义 |
|---|---|
| `·` | 步骤开始，方括号里是当前上下文 token |
| `▸` | 工具调用 |
| `✗` | 工具失败（后面是错误码） |
| `⟐` | 上下文压缩 |
| `⊙` | Verify-Gate 开始 |
| `✓` / `✗` | 验收通过 / 未通过 |
| `◆` | 记忆层写入 |
| `!` | 系统干预（如无进展检测） |

### 常见错误码

| 错误码 | 含义 | 这是正常的吗 |
|---|---|---|
| `STALE_CONTEXT` | 文件在读取后被改动，编辑被拒绝 | **是**，这是指纹守卫在保护你 |
| `NOT_READ` | 没读过就想编辑 | 是，agent 会自己去读 |
| `NO_MATCH` | `old_str` 没匹配上 | 常见，返回里会附近似位置 |
| `AMBIGUOUS` | `old_str` 匹配到多处 | 常见，agent 会扩大范围重试 |
| `BLOCKED_COMMAND` | 命中危险命令拦截 | 需要注意，看看 agent 想干什么 |
| `TIMEOUT` | 命令超时被杀 | 可能需要提高 `--verify-cmd` 的超时 |
| `NO_INTERACTIVE` | 非交互模式下 agent 想提问 | 换成交互模式重跑 |

### 终止状态

| 状态 | 含义 |
|---|---|
| `success` | 完成且 Verify-Gate 通过 |
| `success` + `verified=false` | 完成，但**没能真正验证**（项目没有测试） |
| `answered` | 纯问答，没有改动文件 |
| `steps_exhausted` | 步数用完 |
| `budget_exhausted` | 成本用完 |
| `stuck` | 连续无进展被终止 |
| `verify_exhausted` | Verify-Gate 连续失败达上限 |

看到 `verified=false` 时要留心：改动可能是对的，但系统没有能力证明它是对的。

---

## 7. 权限与安全

### 三态策略

| 操作 | 交互模式 | `run` 模式 |
|---|---|---|
| 读文件 / 检索 / 仓库索引 | 允许 | 允许 |
| 写文件 / 编辑文件 | **询问** | 允许 |
| 只读命令（`ls` `cat` `git status` …） | 允许 | 允许 |
| 其它 bash 命令 | **询问** | 允许 |
| 危险命令 | **拒绝** | 拒绝 |
| 工作区外的文件操作 | 拒绝 | 拒绝 |

询问时可以选 `y`（本次允许）、`a`（始终允许这类操作）、`n`（拒绝）。

```bash
vista --yolo run "..."     # 全部转为允许，启动时会有红色警告
```

### 被拒绝的命令

危险模式表包括：递归删除根目录或家目录、fork bomb、直接写裸设备、格式化磁盘、
`curl | sh`、对根目录 `chmod 777`、关机重启，以及——

**改写 git 历史的命令**：`git push --force`、`git rebase`、`filter-branch`、
`reset --hard origin/`。

### 回滚

任何写操作之前都有快照。

```
/undo              回滚最近一次
/undo snap-003     回滚到指定快照
/snapshots         列出全部
```

回滚之后文件指纹账本会整体作废，agent 下次编辑前会重新读取——这是自动的。

### 工作区边界

所有文件操作都限制在工作区内。`.git`、`.env`、`.ssh`、`.aws` 这些目录
禁止直接的文件级操作（但允许通过 bash 执行 git 命令）。

---

## 8. 记忆系统的日常维护

### L2 项目记忆

存在 `.vista/project.md`，是纯 Markdown，可以直接编辑。

```bash
vista memory show      # 查看
vista memory edit      # 用 $EDITOR 打开
vista memory detect    # 看自动探测到了什么
vista memory clear     # 清空
```

内容按五段组织：`build`（构建与依赖）、`verify`（验收方式）、
`conventions`（代码约定）、`layout`（目录职责）、`notes`（其它）。

**建议提交进 git**——它是项目资产，团队成员共享。

手写一段 `conventions` 通常收益很大，例如：

```markdown
## conventions
- 路由集中在 src/api/routes/，需在 src/api/__init__.py 注册
- 全部使用 Pydantic v2，禁止裸 dict 作为 response_model
- 数据库访问一律走 repository 层，不在 service 里直接写 SQL
```

### L3 技能卡

存在 `.vista/skills/*.yaml`，每张卡是一个独立文件。

```bash
vista skills list
vista skills show fix-naive-aware-datetime
vista skills disable <name>      # 停用但保留文件
vista skills enable <name>
vista skills rm <name>           # 删除
```

**蒸馏什么时候发生**：Verify-Gate 通过、步数 ≥ 6、本次有过文件改动、
且未命中已有技能卡——四个条件全部满足才触发。

**卡片会不会积累错误经验**：会，这是记忆系统的固有风险。三层缓解：
只蒸馏验证通过的轨迹、连续失败两次自动停用、**你随时可以打开 YAML 看见并删掉**。
第三层是最实在的。

如果发现某张卡在误导 agent，直接 `vista skills rm` 或者手工编辑它的
`triggers` 缩小触发范围。

### L1 仓库索引

不需要维护，自动构建并按文件 mtime 缓存。

```bash
vista map                        # 全局
vista map src/auth.py            # 聚焦某个文件
vista map --top-files 8          # 附带打印高分文件
vista map --budget 2000          # 调大预算
```

仓库源文件少于 15 个时会自动关闭——固定成本超过收益。

---

## 9. 会话与报告

### 会话列表

```bash
vista sessions
```

```
20260827-063750  success           8 步  $0.0022  修复 verify() 的时区 bug
20260827-061204  verify_exhausted 12 步  $0.0180  给订单加取消功能
```

### 续跑

```bash
vista resume                     # 续跑最近一次
vista resume 20260827-063750
vista resume 20260827-063750 "改用另一种方案"     # 顺便调整任务
```

续跑不会重放全部历史，而是取最后一条压缩摘要 + 最近若干条事件重新组装。
用的正是压缩机制本身。

### HTML 报告

```bash
vista report                     # 最近一次
vista report 20260827-063750
vista report <id> -o /tmp/r.html
```

报告是**单文件、无外部依赖、可离线打开**的。包含五块：

1. **指标条**：状态、步数、token、成本、耗时、模型
2. **Context Pressure**：上下文 token 波形图。橙色虚线是压缩阈值 θ，
   纵向虚线是每次压缩，曲线在那里切出的凹口就是被释放的可重取内容；
   底部圆点是每次 Verify-Gate（绿=通过，红=未通过）
3. **Signals**：压缩次数、快照次数、**指纹拦截次数**、`old_str` 未命中/歧义次数、
   权限拒绝、危险命令拦截、命令超时、验收尝试
4. **Run Facts / Tool Usage / Model Routing**：运行事实、工具调用分布、两个模型角色的成本
5. **Trajectory**：完整时间线，每行可展开看参数与结果

### 清理

```bash
vista clean                      # 删除会话与快照
vista clean --all                # 连同项目记忆与技能卡一起删
vista clean --yes                # 不询问
```

`.gitignore` 已经排除了 `.vista/sessions/`，但保留了 `.vista/project.md`
和 `.vista/skills/`。

---

## 10. 配置项完整参考

`.vista/config.toml`：

```toml
[model]
provider    = "http"      # http（标准库直连）| openai（官方 SDK）| mock（离线回放）
base_url    = "https://api.openai.com/v1"
main        = "gpt-4o-mini"
weak        = ""          # 留空则复用 main
temperature = 0.2
max_tokens  = 4096
timeout     = 180         # 单次请求超时（秒）
max_retries = 3           # 指数退避次数
context_window = 128000
price_in    = 0.0         # 每百万输入 token 的美元价格，仅用于成本估算
price_out   = 0.0
stream      = true

[limits]
max_steps    = 40
max_cost     = 1.0        # 美元
stuck_window = 4          # 无进展检测的窗口
max_parse_failures = 3

[context]
enabled     = true        # false 等价于 --no-compact
budget      = 0           # 0 表示按 context_window * 0.7 自动推导
theta       = 0.6         # 压缩触发阈值比例
gamma       = 0.35        # 压缩目标比例
recent_keep = 6           # 保护最近 N 条事件不被压缩
min_span    = 4           # 少于 N 条不值得压缩
max_overdue = 10          # 超过 N 步未压缩则忽略 TODO 边界强制压缩
anchor_cap  = 30          # 单次锚点块条数上限
probe       = false       # 压缩验证探针（会多一次弱模型调用）

[repomap]
enabled      = true
budget       = 1024       # 索引的 token 预算
min_files    = 15         # 源文件少于这个数就自动关闭
focus_weight = 20.0       # 焦点文件的个性化权重
damping      = 0.85       # PageRank 阻尼系数
per_file_cap = 10         # 每个文件最多保留几个符号

[skills]
enabled         = true
min_steps       = 6       # 步数少于这个数不蒸馏
min_score       = 0.5     # 检索命中阈值
top_k           = 2       # 最多注入几张卡
max_fail_streak = 2       # 连续失败几次自动停用

[verify]
enabled          = true
baseline         = true   # 任务开始时采集基线
timeout          = 300
baseline_timeout = 120
max_attempts     = 3      # Verify-Gate 连续失败几次后放弃
command          = ""     # 手动指定验收命令，等价于 --verify-cmd

[permission]
mode = "ask"              # ask | allow | deny
yolo = false
allow_bash_in_run_mode = true

[tools]
max_file_bytes    = 524288
read_limit        = 400   # read_file 默认行数
bash_timeout      = 120
bash_output_bytes = 4096  # bash 输出截断阈值
grep_max_results  = 40
tool_result_bytes = 12000 # 单个工具结果的体积上限

[memory]
project_budget = 400      # L2 注入上下文的 token 预算
skill_budget   = 500      # L3 注入上下文的 token 预算
```

---

## 11. 跑评测与消融实验

**需要真实 API key。**

```bash
export VISTA_API_KEY=sk-...

python evals/run_eval.py --list                  # 12 道题目
python evals/run_eval.py --config full           # 完整配置跑一遍
python evals/run_eval.py --ablation --repeat 1   # 六组消融
python evals/run_eval.py --summarize evals/results/
```

只跑某几题：

```bash
python evals/run_eval.py --tasks fix-timezone-bug,order-cancel
```

输出是一张 Markdown 表：

```
| 配置 | n | pass@1 | 谎报率 | 平均步数 | 平均输入 tok | 平均成本 | 平均耗时 | 平均压缩次数 |
```

**谎报率**是 agent 声称完成、但独立验收命令不通过的比例——
`--no-verify` 那一行的这个数字最值得看。

运行器会自动做长/短任务的子集切分。压缩与索引都有固定成本，
任务太短时摊不薄，完整配置未必在总体上赢。

### 加自己的题目

在 `evals/tasks/` 下新建一个 YAML：

```yaml
id: my-task
fixture: pyauth                  # evals/fixtures/ 下的目录名
tags: [feature, python]
prompt: 任务描述，会原样作为 agent 的输入
verify: python3 -c 'assert ...' && python3 -m unittest tests.test_x -q
max_steps: 30
timeout: 900
pair: 用来"跑热"技能库的配对任务描述（可选）
```

**验收命令必须独立于 agent 生成的测试**，否则判分是循环的。

---

## 12. 常见问题排查

### `vista: error: unrecognized arguments: --max-steps`

全局参数要写在子命令之前：

```bash
vista --max-steps 60 run "..."      # 对
vista run "..." --max-steps 60      # 错
```

### 报错说配置文件里有凭据

```
配置错误：.vista/config.toml 中出现了疑似凭据字段 'model.api_key'
```

这是防呆检查。把该字段从配置文件里删掉，改用 `export VISTA_API_KEY=...`。

### `verified=false`

项目里没有可执行的测试与静态检查，或者验收命令跑不起来（比如声明了 pytest 但没装）。
VISTA 降级为语法/导入检查，并诚实标注。

解决办法：`vista --verify-cmd "你的测试命令" run "..."`，
或者写进 `.vista/project.md` 的 `verify` 段。

### agent 一直被 `STALE_CONTEXT` 拦住

说明有东西在持续改动文件。常见原因是编辑器的保存时格式化、
文件监听工具（watch/nodemon）、或者 agent 自己跑了 `black`/`prettier`。
这是保护机制在正常工作，agent 会自己重读后继续。

### 上下文一直不压缩

压缩需要同时满足：超过阈值 θ、且处在 TODO 项边界。如果 agent 一直没更新 TODO，
要等到 `max_overdue`（默认 10）步之后才会强制压缩。用 `/context` 查看当前状态。

### 任务总是 `stuck`

无进展检测触发了两次：连续若干步没有新的工具调用签名，且工作区没有变化。
通常说明任务描述太模糊，agent 找不到下手的地方。试试把任务拆细，
或者在任务里直接指出相关文件。

### 想看 agent 到底做了什么

```bash
vista report                                     # HTML 报告
cat .vista/sessions/<id>/trajectory.jsonl        # 原始轨迹
```

### 成本超预算

```bash
vista --budget 0.5 run "..."
```

或者给 `weak` 角色配一个更便宜的模型——压缩与蒸馏都走它。

---

## 13. 把 VISTA 用在你自己的项目上

推荐的接入步骤：

**第一步**，在项目根目录跑自检：

```bash
cd /path/to/your/project
vista doctor
```

重点看最后的「验收探测」部分——如果 `test` 是空的，Verify-Gate 会降级，
后续所有任务都会标 `verified=false`。

**第二步**，如果探测不准，手工写一次项目记忆：

```bash
vista memory edit
```

```markdown
## verify
- 测试命令：pytest tests/ -q
- 静态检查：ruff check . && mypy src

## conventions
- 新增接口必须在 src/api/__init__.py 注册
- 禁止在 service 层直接写 SQL

## layout
- src/api/ HTTP 层 · src/core/ 领域逻辑 · src/db/ 持久化
```

这一步的收益通常最大——它同时改善了 Verify-Gate 的准确性和 agent 的行为一致性。

**第三步**，先用交互模式跑一个小任务，观察它的行为：

```bash
vista
vista ▸ 给 UserRepo 加一个 by_role 方法
```

期间用 `/context` 看上下文构成，用 `/verify` 手动验收，用 `/undo` 回滚不满意的改动。

**第四步**，确认行为符合预期后，再用 `vista run` 做无人值守的任务。

**第五步**，把 `.vista/project.md` 和 `.vista/skills/` 提交进 git——
它们是团队共享的项目资产。会话轨迹（`.vista/sessions/`）不要提交，
`.gitignore` 里已经排除了。
