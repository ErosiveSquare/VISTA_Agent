# VISTA_Agent

2026.08.27开始

**V**erified · **I**ndexed · **S**elf-evolving · **T**iered-memory · **A**nchored-context
—— 一个从零实现的编程智能体（coding agent）。

方案重点：
1. 分层记忆（L1-L4），且RepoMap 索引（L1） + SOP 自演化（L3）

| 层 | 内容 | 存储 | 时间尺度 | 谁写入 |
|---|---|---|---|---|
| **L1** | RepoMap 仓库索引 | 内存 + mtime 缓存 | 随仓库变化 | scaffold 自动 |
| **L2** | 项目约定、构建与测试命令 | `.vista/project.md` | 跨会话长期 | agent 学习 + 自动探测 |
| **L3** | SOP 技能卡 | `.vista/skills/*.yaml` | 跨会话可复用 | 验证通过后蒸馏 |
| **L4** | 会话轨迹归档 | `.vista/sessions/` | 永久 | 每步自动 |

2. 证据锚定压缩 + 约束钉扎，我认为这个也属于“上下文密度管理”的其中机制
3. Verify-Gate 终止条件不由模型说了算
4. 统一 Agent Loop
