<div align="center">

# VISTA Agent

**Verified · Indexed · Self-evolving · Tiered-memory · Anchored-context**

一个从零实现的本地编程智能体：读取仓库、修改代码、执行命令，并通过环境验证完成结果。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.0.0-5E81AC)](https://github.com/ErosiveSquare/VISTA_Agent)
[![License](https://img.shields.io/badge/license-MIT-6FCF97)](LICENSE)
[![Agent Framework](https://img.shields.io/badge/agent_framework-none-E8A33D)](DESIGN.md)

[设计说明](DESIGN.md) · [用户手册](docs/USER_MANUAL.md) 

</div>

## 项目简介

VISTA 接收自然语言编程任务，通过大语言模型的原生 tool calling 自主完成“理解仓库 → 定位代码 → 修改文件 → 执行测试 → 验收结果”的闭环。项目不依赖任何 Agent 框架；对话历史、上下文压缩、工具定义与本地执行、模型输出解析、循环终止及错误处理均由项目自行实现。

主循环保持为可审计的单线程流程：

```text
ASSEMBLE → INFER → DECIDE → DISPATCH → OBSERVE
    ↑                                      │
    └──────── 继续修复 / 验收反馈 ──────────┘
```

## 核心设计

| 机制 | 作用 |
|---|---|
| **Verified** | `finish` 只是退出申请；Verify-Gate 会执行测试、静态检查或降级语法检查，并与任务开始时的失败基线比较 |
| **Indexed** | RepoMap 抽取仓库符号、构建引用图，并用个性化 PageRank 在固定 token 预算内呈现关键结构 |
| **Self-evolving** | 验收成功且满足条件的轨迹可蒸馏为 YAML 技能卡；后续任务按关键词、IDF、语言和历史表现检索 |
| **Tiered-memory** | L1 仓库索引、L2 项目约定、L3 技能卡与 L4 会话轨迹按不同时间尺度保存和注入 |
| **Anchored-context** | 将历史分为 pinned、reclaimable、derived 三类：可重取正文释放为带路径、行号和哈希的锚点，其余信息压成固定结构摘要 |

此外，VISTA 提供文件指纹校验、工作区路径边界、危险命令拦截、权限策略、文件快照、会话续跑与单文件 HTML 报告。

## 快速开始

要求 Python 3.11 或更高版本。运行所需代码只使用 Python 标准库；第三方依赖均为可选增强。

```bash
git clone https://github.com/ErosiveSquare/VISTA_Agent.git
cd VISTA_Agent
python -m pip install -e .
```

## 配置并运行真实任务

VISTA 默认通过标准库 `urllib` 调用 OpenAI 兼容的 `/chat/completions` 接口。凭据只从环境变量读取，不应写入仓库、配置文件或演示视频。

```bash
export VISTA_API_KEY="your-api-key"
export VISTA_BASE_URL="https://api.openai.com/v1"
export VISTA_MODEL="模型名称"

vista run "修复当前项目中失败的单元测试，并说明根因"
```

PowerShell 中可使用：

```powershell
$env:VISTA_API_KEY = "your-api-key"
$env:VISTA_MODEL = "模型名称"
vista run "修复当前项目中失败的单元测试，并说明根因"
```

常用命令：

```bash
vista                    # 交互模式
vista doctor             # 环境与配置自检
vista map                # 查看 RepoMap 仓库索引
vista resume             # 续跑最近一次会话
vista report             # 生成最近一次会话的 HTML 报告
```

完整参数、配置文件与权限说明见[用户手册](docs/USER_MANUAL.md)。

## 实现边界

- 不使用 LangChain、LlamaIndex、AutoGen、CrewAI、OpenAI Agents SDK 等 Agent 框架。
- 不依赖 Code Interpreter、Files API 等服务端托管的代码执行或文件工具。
- 文件读写与命令执行均发生在本地工作区；模型只负责推理和选择工具。

## 许可证

本项目采用 [MIT License](LICENSE)。
