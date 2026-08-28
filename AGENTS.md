# AGENTS.md

给在本仓库里工作的编程智能体（包括 VISTA 自己）的项目约定。

## 项目是什么

VISTA 是一个从零实现的编程智能体。**零必需依赖**：只用 Python 3.11+ 标准库。
所有第三方库都是可选增强，且必须有降级路径。

## 构建与验收

```bash
pip install -e .                                    # 装 vista 命令
python -m unittest discover -s tests -t . -q        # 全部单元测试
vista demo                                          # 端到端冒烟测试（离线）
```

改动任何代码之后，这两条都必须通过。

## 架构的硬约束

依赖严格单向向下，**禁止反向 import**：

```
L5 cli/ · __main__.py
L4 loop.py
L3 context/ · memory/ · verify.py · report.py
L2 tools/ · safety/
L1 llm/ · types.py · config.py · errors.py · util/
```

`types.py` 被所有层依赖，自身不 import 任何内部模块。
`tools/` 需要上层能力时通过 `tools/context.py` 的 `ToolContext` 窄接口拿，
不要直接 import `memory/` 或 `context/`。

## 六条不可违背的不变式

| 编号 | 不变式 |
|---|---|
| I1 | history 只追加，永不删除；压缩靠插入标记 + 计算视图 |
| I2 | 每个 tool_call 有且仅有一个 tool_result |
| I3 | pinned 事件永远出现在视图中，且保持相对位置 |
| I4 | 任何工具异常一律转为 ToolResult(ok=False)，不向上抛 |
| I5 | 任何写操作之前必有快照 |
| I6 | 任何编辑之前必有指纹校验 |

`tests/` 里每一条都有对应用例。改动破坏了它们，先想清楚是不是真的应该改。

## 代码风格

- 行宽 110，`ruff check .` 应当干净
- 全部函数带类型标注
- **注释写"为什么"，不写"做了什么"**。代码本身已经说明了做什么
- 中文注释与文档使用中文标点
- 不引入新的必需依赖。确实需要某个库时，写成可选 + 降级路径

## 提交约定

Conventional Commits，body 写取舍理由而不是改动清单：

```
feat(context): Anchor Compression 的三分类压缩策略

按内容可重取性分三类：可重取内容正文整段丢弃只留锚点；
不可重取内容交弱模型压成固定 schema；pinned 事件不参与压缩。

考虑过对全部内容做统一 LLM 摘要，但对可重取内容做有损摘要是
没必要的信息损失——编程域的文件随时可以重读，这是通用域没有的杠杆。
代价是模型需要偶尔重读文件。

不变式 I1：压缩通过插入标记实现，原事件永不删除。
```

## 不要做的事

- 不要 force push、rebase 或以任何方式改写已推送的历史
- 不要把凭据写进任何文件。凭据只从环境变量读
- 不要提交 `.vista/sessions/`（已在 .gitignore 中）
- **要**提交 `.vista/project.md` 与 `.vista/skills/`，它们是项目资产
