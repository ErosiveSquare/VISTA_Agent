"""交互式 REPL。

一个会话内可以连续下达多个任务，Agent 实例被复用，因此 FileLedger、
项目记忆、RepoMap 缓存、快照栈都是跨任务保持的。

斜杠命令的价值不只是易用性 —— /context 把上下文的分层构成直接打印出来，
这是向别人解释 Anchor Compression 与 Constraint Pinning 最快的方式。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..config import Config
from ..llm.client import LLM
from ..loop import Agent
from ..report import write_report
from ..types import RunResult
from .render import Console, TerminalUI

HELP = """\
可用命令：
  /help              显示这份帮助
  /context           打印当前上下文的分层构成与 token 分布
  /cost              打印本会话的 token 与成本统计
  /todo              打印当前任务清单
  /undo [snap-id]    回滚最近一次（或指定的）文件快照
  /snapshots         列出本会话的全部快照
  /skills            列出技能库（L3）
  /memory            打印项目记忆（L2）
  /map [文件...]     打印仓库索引（L1），可指定聚焦文件
  /verify            立即执行一次 Verify-Gate（不改变循环状态）
  /report            为最近一次任务生成 HTML 报告
  /model             显示当前模型与 provider 配置
  /clear             清空对话历史（记忆层与账本保留）
  /quit              退出

直接输入文字即为下达一个任务。
"""


class Repl:
    def __init__(self, cfg: Config, console: Console | None = None):
        self.cfg = cfg
        self.cfg.interactive = True
        self.console = console or Console(color=cfg.color)
        self.ui = TerminalUI(self.console, interactive=True)
        self.llm = LLM(cfg)
        self.agent = Agent(cfg, llm=self.llm, ui=self.ui, on_event=self.console.event)
        self.last: RunResult | None = None

    # ------------------------------------------------------------------
    def run(self) -> int:
        from .. import __version__

        c = self.console
        c.banner(__version__,
                 f"{self.cfg.model.provider} · {self.cfg.model.main} · 工作区 {self.cfg.cwd}")
        c.write(c.style("  输入任务开始，或输入 /help 查看命令。", "grey"))
        c.write()

        while True:
            try:
                line = input(c.style("vista ▸ ", "bold", "brightcyan")).strip()
            except (EOFError, KeyboardInterrupt):
                c.write()
                break
            if not line:
                continue
            if line.startswith("/"):
                if self._command(line) is False:
                    break
                continue
            self._task(line)
        self._shutdown()
        return 0

    # ------------------------------------------------------------------
    def _task(self, text: str) -> None:
        c = self.console
        try:
            res = self.agent.run(text)
        except KeyboardInterrupt:
            c.write()
            c.warn("已中断当前任务。")
            return
        self.last = res
        c.result(res)

    # ------------------------------------------------------------------
    def _command(self, line: str) -> bool | None:
        c = self.console
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        ag = self.agent

        if cmd in ("/quit", "/exit", "/q"):
            return False

        if cmd in ("/help", "/h", "/?"):
            c.write(HELP)

        elif cmd == "/context":
            packed = ag._assemble()
            c.write()
            c.write(packed.breakdown.render(self.cfg.context_budget))
            c.write(c.style(
                f"  压缩阈值 {self.cfg.compact_threshold:,} tokens（θ={self.cfg.context.theta}）；"
                f"已压缩 {ag.history.n_compactions} 次；"
                f"历史事件 {len(ag.history.events)} 条（存活 {len(ag.history.live_events())} 条）",
                "grey"))
            c.write()

        elif cmd == "/cost":
            s = self.llm.stats()
            c.write()
            c.kv("provider", f'{s["provider"]}（main={s["model_main"]}，weak={s["model_weak"]}）')
            c.kv("调用次数", str(s["calls"]))
            c.kv("token", f'输入 {s["in_tokens"]:,} / 输出 {s["out_tokens"]:,}')
            c.kv("成本", f'${s["cost"]:.4f} / 上限 ${self.cfg.limits.max_cost:.2f}')
            for role, d in (s.get("by_role") or {}).items():
                c.kv(f"  {role}", f'{d["calls"]} 次 · 入 {d["in"]:,} · 出 {d["out"]:,} · ${d["cost"]:.4f}')
            c.write()

        elif cmd == "/todo":
            if not ag.ctx.todos:
                c.write(c.style("  当前没有任务清单。", "grey"))
            else:
                c.write()
                for t in ag.ctx.todos:
                    style = {"done": "green", "doing": "yellow"}.get(t.status, "grey")
                    c.write("  " + c.style(t.render(), style))
                c.write()

        elif cmd == "/undo":
            ok, msg = ag.snapshots.restore(args[0] if args else None)
            (c.ok if ok else c.warn)("  " + msg)
            # 回滚会让磁盘内容与账本记录的指纹不一致，因此整体作废，
            # 强制智能体在下次编辑前重新 read_file（不变式 I6 的自然结果）。
            ag.ledger = type(ag.ledger)()
            ag.ctx.ledger = ag.ledger
            c.write(c.style("  文件指纹账本已重置，智能体下次编辑前会重新读取。", "grey"))

        elif cmd == "/snapshots":
            snaps = ag.snapshots.list()
            if not snaps:
                c.write(c.style("  本会话还没有快照。", "grey"))
            else:
                c.write()
                for s in snaps:
                    files = ", ".join(f["path"] for f in s.files[:4])
                    c.write(f"  {c.style(s.id, 'cyan')}  第 {s.step} 步  {files}")
                c.write()

        elif cmd == "/skills":
            rows = ag.skills.summary()
            if not rows:
                c.write(c.style("  技能库为空。任务通过验收且步数足够时会自动蒸馏。", "grey"))
            else:
                c.write()
                for r in rows:
                    flag = c.style("启用", "green") if r["enabled"] else c.style("停用", "red")
                    c.write(f'  {c.style(r["name"], "cyan")}  {r["title"]}  [{flag}]  '
                            f'{r["success"]}/{r["usage"]} 成功')
                    c.write(c.style(f'    触发词：{", ".join(r["triggers"])}', "grey"))
                c.write()

        elif cmd == "/memory":
            text = ag.project.render(4000, self.cfg.model.main)
            c.write()
            c.write(text or c.style("  项目记忆为空。", "grey"))
            c.write(c.style(f"  文件：{self.cfg.project_file}", "grey"))
            c.write()

        elif cmd == "/map":
            ag.repomap.build()
            if not ag.repomap.available:
                c.warn(f"  仓库索引未启用：{ag.repomap.stats.reason}")
            else:
                text, n = ag.repomap.render(args or ag._focus, self.cfg.repomap.budget)
                c.write()
                c.write(text)
                c.write(c.style(f"  （{n} tokens）", "grey"))
                c.write()

        elif cmd == "/verify":
            c.write(c.style("  正在执行 Verify-Gate…", "grey"))
            report = ag.verifier.run(sorted(ag.ctx.mutated_files))
            c.write()
            c.write(report.render())
            c.write()

        elif cmd == "/report":
            path = write_report(ag.session_dir)
            c.ok(f"  报告已生成：{path}")

        elif cmd == "/model":
            m = self.cfg.model
            c.write()
            c.kv("provider", m.provider)
            c.kv("base_url", m.base_url)
            c.kv("main", m.main)
            c.kv("weak", m.weak_model)
            c.kv("温度", str(m.temperature))
            c.kv("上下文预算", f"{self.cfg.context_budget:,} tokens")
            c.write()

        elif cmd == "/clear":
            from ..context.history import History

            ag.history = History(model=self.cfg.model.main)
            ag.step = 0
            ag._records.clear()
            ag._seen_sigs.clear()
            ag.stuck_warned = False
            ag.verify_attempts = 0
            c.ok("  对话历史已清空（项目记忆、技能库、文件账本保留）。")

        else:
            c.warn(f"  未知命令 {cmd}，输入 /help 查看可用命令。")
        return None

    # ------------------------------------------------------------------
    def _shutdown(self) -> None:
        c = self.console
        try:
            self.agent.archive.close()
        except Exception:
            pass
        s = self.llm.stats()
        if s["calls"]:
            c.write()
            c.write(c.style(
                f"本次会话：{s['calls']} 次模型调用 · "
                f"输入 {s['in_tokens']:,} / 输出 {s['out_tokens']:,} tokens · "
                f"${s['cost']:.4f}", "grey"))
            c.write(c.style(f"轨迹已保存到 {self.agent.session_dir}", "grey"))
        c.write(c.style("再见。", "grey"))


def main(cfg: Config) -> int:
    if not sys.stdin.isatty():
        Console().error("交互模式需要终端。请改用：vista run \"任务描述\"")
        return 2
    return Repl(cfg).run()
