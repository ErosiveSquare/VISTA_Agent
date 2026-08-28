"""Unified Agent Loop —— VISTA 的骨干。

这不是本项目的"特色"，而是刻意保持简单的部分：一个单线程 while 循环，
没有多智能体、没有图编排、没有异步队列。理由是有源码级研究指出，
Claude Code 里只有约 1.6% 的代码是 AI 决策逻辑，其余 98.4% 是 loop 之外的
确定性基础设施；mini-swe-agent 用 100 行、只有一个 bash 工具，
在 SWE-bench Verified 上也能超过 74%。所以工程力量应该投在 loop 的外围。

五步：
    ① ASSEMBLE  组装上下文（必要时先压缩）
    ② INFER     调用模型
    ③ DECIDE    解析输出，判断是工具调用、最终回答，还是 finish
    ④ DISPATCH  权限校验 → 快照 → 执行 → 结构化结果
    ⑤ OBSERVE   落盘、无进展检测、预算检查

五条终止条件：
    T1a  模型返回纯文本、无工具调用          → answered
    T1b  模型 finish 且 Verify-Gate 通过     → success
    T2   步数耗尽                            → steps_exhausted
    T3   成本耗尽                            → budget_exhausted
    T4   连续无进展（先干预一次，再退出）    → stuck
    T5   用户 Ctrl-C                         → interrupted
补充：verify_exhausted / parse_failure / api_failure
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .context.assembler import Assembled, assemble
from .context.budget import Budget
from .context.compactor import compact
from .context.history import History
from .errors import ContextOverflow, FatalLLMError, RetryableError, UserAbort
from .llm.client import LLM
from .llm.parser import PARSE_FAILED, parse_tool_calls
from .memory.archive import Archive
from .memory.project import ProjectMemory, detect_commands, merge_detected
from .memory.repomap import RepoMap
from .memory.skills import SkillIndex
from .prompts import (
    CONTEXT_OVERFLOW_HINT,
    FORMAT_ERROR_HINT,
    RESUME_NOTE,
    STUCK_HINT,
)
from .safety.permission import PermissionPolicy
from .safety.snapshot import SnapshotStore
from .tools import registry
from .tools.context import NullUI, ToolContext, ToolStats
from .tools.files import FileLedger
from .types import Call, RunResult, ToolResult, Usage, VerifyReport
from .util.paths import workspace_fingerprint
from .util.text import one_line
from .verify import VerifyGate


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class StepRecord:
    step: int
    signatures: set[str] = field(default_factory=set)
    fingerprint: str = ""
    tokens: int = 0


class Agent:
    """一次会话的编排者。"""

    def __init__(
        self,
        cfg: Config,
        llm: LLM | None = None,
        ui=None,
        session_id: str | None = None,
        on_event=None,
    ):
        self.cfg = cfg
        self.ui = ui or NullUI()
        self.llm = llm or LLM(cfg)
        self.on_event = on_event
        cfg.ensure_dirs()

        self.session_id = session_id or new_session_id()
        self.session_dir = cfg.sessions_dir / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.archive = Archive(self.session_dir, self.session_id)

        self.history = History(model=cfg.model.main)
        self.budget = Budget(
            max_cost=cfg.limits.max_cost,
            context_budget=cfg.context_budget,
            theta=cfg.context.theta,
        )

        # ---- 记忆层 ----
        self.project = ProjectMemory.load(cfg.project_file)
        self.detected = detect_commands(cfg.cwd)
        merge_detected(self.project, self.detected)
        self.repomap = RepoMap(cfg.cwd, cfg.repomap, model=cfg.model.main)
        self.skills = SkillIndex.load(cfg.skills_dir, cfg.skills, model=cfg.model.main)
        self.verifier = VerifyGate(cfg, detected=self.detected)

        # ---- 工具上下文 ----
        self.ledger = FileLedger()
        self.permission = PermissionPolicy(
            mode=cfg.permission.mode,
            interactive=cfg.interactive,
            allow_bash_in_run_mode=cfg.permission.allow_bash_in_run_mode,
        )
        self.snapshots = SnapshotStore(cfg.cwd, self.session_dir)
        self.ctx = ToolContext(
            cfg=cfg, root=Path(cfg.cwd), ledger=self.ledger, permission=self.permission,
            ui=self.ui, snapshots=self.snapshots, repomap=self.repomap,
            project=self.project, skills=self.skills, stats=ToolStats(),
            on_todo_change=self._on_todo_change,
        )

        # ---- 运行时状态 ----
        self.step = 0
        self.task = ""
        self.skill_cards: list = []
        self.parse_failures = 0
        self.stuck_warned = False
        self.verify_attempts = 0
        self.last_verify: VerifyReport | None = None
        self._records: list[StepRecord] = []
        self._seen_sigs: set[str] = set()
        self._todo_touched = False
        self._focus: list[str] = []
        self._t0 = time.time()
        self._forced_compaction = False

    # ==================================================================
    # 对外
    # ==================================================================
    def run(self, task: str, resume: dict | None = None) -> RunResult:
        self.task = task
        try:
            self._init_history(task, resume)
            return self._loop()
        except UserAbort:
            return self._finish_run("interrupted", "用户中断。")
        except KeyboardInterrupt:
            return self._finish_run("interrupted", "用户中断。")
        except FatalLLMError as e:
            return self._finish_run("api_failure", f"模型接口错误：{e}", error=str(e))
        except Exception as e:  # 兜底，保证轨迹一定被写完
            import traceback

            self.archive.write("error", message=str(e), traceback=traceback.format_exc()[:4000])
            return self._finish_run("error", f"内部错误：{type(e).__name__}: {e}", error=str(e))

    # ==================================================================
    # ① INIT
    # ==================================================================
    def _init_history(self, task: str, resume: dict | None) -> None:
        cfg = self.cfg
        self.repomap.build()
        scope = {"language": self.detected.get("language"), "framework": self.detected.get("framework")}
        if cfg.skills.enabled and not cfg.baseline_mode:
            self.skill_cards = self.skills.retrieve(task, scope=scope)
        self.verifier.make_plan()

        self.archive.meta(
            task=task, model=cfg.model.main, weak_model=cfg.model.weak_model,
            provider=self.llm.provider.name, cwd=str(cfg.cwd),
            vista_version=_version(), baseline_mode=cfg.baseline_mode,
            config=cfg.to_dict(), detected=self.detected,
            repomap=self.repomap.stats.to_dict(),
            skills=[c.name for c in self.skill_cards],
            verify_plan={"mode": self.verifier.plan.mode, "test": self.verifier.plan.test_cmd,
                         "lint": self.verifier.plan.lint_cmd},
        )

        task_ev = self.history.append_task(task)
        self.archive.event(task_ev)
        if resume:
            if resume.get("summary"):
                self.history.append_note(resume["summary"])
            self.history.append_note(RESUME_NOTE.format(steps=resume.get("steps", 0)))
            if resume.get("todo"):
                self.history.append_todo(resume["todo"])

        if cfg.verify.enabled and self.verifier.plan.mode == "test" and cfg.verify.baseline:
            self._emit("baseline", "正在采集验收基线…")
            note = self.verifier.run_baseline()
            self._emit("baseline_done", note)
            self.archive.write("baseline", note=note,
                               failures=sorted(self.verifier.baseline_failures))

    # ==================================================================
    # 主循环
    # ==================================================================
    def _loop(self) -> RunResult:
        cfg = self.cfg
        for step in range(1, cfg.limits.max_steps + 1):
            self.step = step
            self.ctx.step = step

            # ---- T3 成本 ----
            if self.budget.exceeded():
                return self._finish_run(
                    "budget_exhausted",
                    f"成本达到上限 ${cfg.limits.max_cost:.2f}（已用 ${self.budget.cost:.4f}）。",
                )

            # ---- ① ASSEMBLE（含压缩判定）----
            packed = self._assemble()
            if self._should_compact(packed.breakdown.total):
                pre_total = packed.breakdown.total
                stats = self._do_compact(force=False)
                packed = self._assemble()
                if stats is not None:
                    # 曲线上的前后两个数字必须是同一种口径（完整上下文），
                    # 否则图表会显示出并不存在的"断崖"。
                    self.archive.compaction(
                        {**stats.to_dict(),
                         "context_before": pre_total,
                         "context_after": packed.breakdown.total},
                        step,
                    )

            self.budget.record_context(step, packed.breakdown.total)
            self.archive.context(step, packed.breakdown.total,
                                 dict(packed.breakdown.parts))
            self._emit("step", f"第 {step} 步", tokens=packed.breakdown.total)

            # ---- ② INFER ----
            try:
                resp = self._infer(packed)
            except ContextOverflow:
                self._emit("warn", "上下文超出模型窗口，执行强制压缩后重试。")
                if self._forced_compaction:
                    return self._finish_run("api_failure", "强制压缩后上下文仍然超限。")
                self._forced_compaction = True
                forced = self._do_compact(force=True)
                if forced is not None:
                    self.archive.compaction({**forced.to_dict(), "forced": True}, self.step)
                self.history.append_note(CONTEXT_OVERFLOW_HINT)
                continue
            except RetryableError as e:
                return self._finish_run("api_failure", f"模型接口重试耗尽：{e}", error=str(e))
            self._forced_compaction = False

            # ---- ③ DECIDE ----
            calls = parse_tool_calls(resp)
            if calls is PARSE_FAILED:
                self.parse_failures += 1
                self._emit("warn", f"模型输出无法解析（第 {self.parse_failures} 次）。")
                self.archive.write("parse_failure", step=step, text=(resp.text or "")[:1500])
                if self.parse_failures >= cfg.limits.max_parse_failures:
                    return self._finish_run("parse_failure", "模型输出连续无法解析。")
                self.history.append_assistant(resp.text or "")
                self.history.append_note(FORMAT_ERROR_HINT)
                continue
            self.parse_failures = 0

            ev = self.history.append_assistant(resp.text or "", calls)
            self.archive.event(ev)

            # ---- T1a 纯文本回答 ----
            if not calls:
                return self._finish_run("answered", resp.text or "")

            finish_call = next((c for c in calls if c.name == "finish"), None)
            other_calls = [c for c in calls if c.name != "finish"]

            # ---- ④ DISPATCH ----
            for call in other_calls:
                result = self._dispatch(call)
                rev = self.history.append_tool_result(call, result)
                self.archive.event(rev)
                if call.name == "read_file":
                    path = str(call.arguments.get("path", ""))
                    if path and path not in self._focus:
                        self._focus = ([path] + self._focus)[:5]
                        self.repomap.invalidate([])
                if result.mutated:
                    self.repomap.invalidate([m for m in result.mutated if m != "<workspace>"])

            # ---- ⑤ finish → Verify-Gate ----
            if finish_call is not None:
                fr = self._dispatch(finish_call)
                self.history.append_tool_result(finish_call, fr)
                outcome = self._verify_gate()
                if outcome is not None:
                    return outcome
                continue

            # ---- ⑤ OBSERVE ----
            self._observe(calls)
            if self._no_progress():
                if self.stuck_warned:
                    return self._finish_run("stuck", "连续多步没有进展，已终止。")
                self.stuck_warned = True
                self._emit("warn", "检测到重复动作且无文件变更，注入干预提示。")
                self.history.append_note(STUCK_HINT)
                self.archive.write("intervention", step=self.step, kind="stuck")

        return self._finish_run("steps_exhausted", f"已达到步数上限 {cfg.limits.max_steps}。")

    # ==================================================================
    # ① ASSEMBLE
    # ==================================================================
    def _assemble(self) -> Assembled:
        cfg = self.cfg
        repo_text = ""
        if cfg.repomap.enabled and not cfg.baseline_mode and self.repomap.available:
            repo_text, _ = self.repomap.render(self._focus, cfg.repomap.budget)
        project_text = "" if cfg.baseline_mode else self.project.render(cfg.memory.project_budget, cfg.model.main)
        skills_text = "" if cfg.baseline_mode else self.skills.render(self.skill_cards, cfg.memory.skill_budget)
        verify_hint = "" if cfg.baseline_mode else self.verifier.plan.describe()
        if self.verifier.baseline_note:
            verify_hint += f"\n{self.verifier.baseline_note}"

        return assemble(
            cfg, self.history,
            repo_map_text=repo_text, project_text=project_text,
            skills_text=skills_text, verify_hint=verify_hint,
            tool_schemas=self._schemas(),
        )

    def _schemas(self) -> list[dict]:
        return registry.schemas(registry.tool_names(self.cfg.baseline_mode))

    # ---- 压缩判定 ----
    def _should_compact(self, tokens: int) -> bool:
        cfg = self.cfg
        if not cfg.context.enabled or cfg.baseline_mode:
            return False
        if not self.budget.should_compact(tokens):
            return False
        overdue = self.history.steps_since_compaction() >= cfg.context.max_overdue
        boundary = (not self.ctx.todos) or self._todo_touched
        return boundary or overdue

    def _do_compact(self, force: bool):
        """执行一次压缩。归档由调用方完成，以便记录同口径的上下文前后值。"""
        stats = compact(self.cfg, self.history, llm=self.llm, goal=self.task, force=force)
        if stats is None:
            return None
        self._todo_touched = False
        self._emit(
            "compaction",
            f"上下文压缩：{stats.before_tokens / 1000:.1f}k → {stats.after_tokens / 1000:.1f}k tokens"
            f"（丢弃 {stats.n_reclaimable} 条可重取内容，保留 {stats.n_anchors} 个锚点）",
        )
        return stats

    # ==================================================================
    # ② INFER
    # ==================================================================
    def _infer(self, packed: Assembled):
        on_delta = getattr(self.ui, "stream", None)
        resp = self.llm.call(packed.messages, tools=self._schemas(), role="main", on_delta=on_delta)
        cost = (
            resp.usage.in_tokens * self.cfg.model.price_in
            + resp.usage.out_tokens * self.cfg.model.price_out
        ) / 1_000_000
        self.budget.add(resp.usage, cost)
        self.archive.llm("main", resp.model, resp.usage, cost, resp.latency_ms, self.step)
        return resp

    # ==================================================================
    # ④ DISPATCH
    # ==================================================================
    def _dispatch(self, call: Call) -> ToolResult:
        self._emit("tool", f"{call.name}({one_line(_args_preview(call.arguments), 70)})")
        result = registry.dispatch(call, self.ctx)
        if call.name == "todo_write" and result.ok:
            self._todo_touched = True
        if not result.ok:
            self._emit("tool_error", f"{call.name} → {result.code}")
        return result

    def _on_todo_change(self, todos) -> None:
        rendered = "\n".join(t.render() for t in todos)
        ev = self.history.append_todo(rendered)
        self.archive.event(ev)

    # ==================================================================
    # ⑤ Verify-Gate
    # ==================================================================
    def _verify_gate(self) -> RunResult | None:
        cfg = self.cfg
        self.verify_attempts += 1
        self._emit("verify", "正在执行 Verify-Gate 验收…")

        if self.verifier.plan.mode == "syntax":
            report = self.verifier._run_syntax(sorted(self.ctx.mutated_files))
            report.duration_ms = 0
            self.verifier.attempts += 1
            self.verifier.history.append(report)
        else:
            report = self.verifier.run(sorted(self.ctx.mutated_files))

        self.last_verify = report
        self.archive.verify(report, self.verify_attempts, self.step)
        ev = self.history.append_verify(report.render(), report.passed)
        self.archive.event(ev)

        if report.passed:
            self._emit("verify_pass",
                       f"验收通过（{report.mode}）" + ("" if report.verified else "，但未经真实验证"))
            self._post_success()
            return self._finish_run("success", self.ctx.finish_summary or "任务完成。", verify=report)

        self._emit("verify_fail", f"验收未通过：新增 {len(report.new_failures)} 个失败")
        if self.verify_attempts >= cfg.verify.max_attempts:
            return self._finish_run(
                "verify_exhausted",
                f"Verify-Gate 连续 {self.verify_attempts} 次未通过。", verify=report,
            )
        self.history.append_note(self.verifier.render_failure(report))
        self.ctx.finish_summary = None
        return None

    # ---- 成功之后：写 L2、蒸馏 L3 ----
    def _post_success(self) -> None:
        cfg = self.cfg
        if self.project.save():
            self._emit("memory", f"项目记忆已更新：{cfg.project_file}")

        if self.skill_cards:
            self.skills.record_outcome(self.skill_cards, success=True)

        ok, why = self.skills.should_distill(
            success=True,
            steps=self.step,
            mutated=len(self.ctx.mutated_files),
            hit_existing=bool(self.skill_cards),
        )
        if not ok:
            self.archive.write("distill", skipped=True, reason=why)
            return
        summary = self._distill_summary()
        scope = {"language": self.detected.get("language"), "framework": self.detected.get("framework")}
        card = self.skills.distill(self.llm, summary, self.session_id, self.step, scope=scope)
        if card:
            self.archive.write("distill", skipped=False, name=card.name, path=card.path)
            self._emit("skill", f"已蒸馏技能卡：{card.title or card.name} → {card.path}")
        else:
            self.archive.write("distill", skipped=True, reason="模型判定无复用价值或输出不合法")

    def _distill_summary(self) -> str:
        """给蒸馏用的结构化摘要（不喂完整轨迹：太长且噪声大）。"""
        lines = [f"任务：{self.task}", ""]
        files = sorted(self.ctx.mutated_files)
        lines.append(f"改动的文件（{len(files)} 个）：{', '.join(files[:12]) or '无'}")
        lines.append("")
        lines.append("关键步骤序列：")
        i = 0
        for e in self.history.events:
            if e.kind != "assistant" or not e.calls:
                continue
            i += 1
            for c in e.calls:
                lines.append(f"  {i}. {c.name}({one_line(_args_preview(c.arguments), 60)})")
        lines.append("")
        errs = [
            f"  - {e.tool_name} 报 {e.code}：{one_line(e.content, 90)}"
            for e in self.history.events
            if e.kind == "tool_result" and not e.meta.get("ok", True)
        ]
        if errs:
            lines.append("过程中遇到的错误：")
            lines += errs[:8]
            lines.append("")
        if self.last_verify:
            lines.append(f"验收结果：{self.last_verify.command or self.last_verify.mode} 通过")
        conventions = self.project.get("conventions")
        if conventions:
            lines.append("项目约定：" + "；".join(conventions[:4]))
        if self.ctx.finish_summary:
            lines.append("")
            lines.append(f"智能体的自述：{one_line(self.ctx.finish_summary, 300)}")
        return "\n".join(lines)

    # ==================================================================
    # ⑤ OBSERVE
    # ==================================================================
    def _observe(self, calls: list[Call]) -> None:
        sigs = {c.signature() for c in calls}
        fp = workspace_fingerprint(self.cfg.cwd)
        self._records.append(StepRecord(self.step, sigs, fp, self.history.total_tokens()))
        self._seen_sigs |= sigs

    def _no_progress(self) -> bool:
        """T4 无进展检测。

        失败轨迹的典型特征是重复的、非自适应的动作循环。判据取两个条件的合取：
        最近 K 步没有出现过新的工具调用签名，且工作区没有任何文件变更。
        """
        k = self.cfg.limits.stuck_window
        if len(self._records) < k + 1:
            return False
        window = self._records[-k:]
        prior: set[str] = set()
        for r in self._records[: -k]:
            prior |= r.signatures
        novel = set().union(*(r.signatures for r in window)) - prior
        fingerprints = {r.fingerprint for r in self._records[-(k + 1) :]}
        return not novel and len(fingerprints) == 1

    # ==================================================================
    # 收尾
    # ==================================================================
    def _finish_run(self, status: str, summary: str, verify: VerifyReport | None = None,
                    error: str | None = None) -> RunResult:
        wall = int((time.time() - self._t0) * 1000)
        if status not in ("success",) and self.skill_cards and status in (
            "verify_exhausted", "stuck", "steps_exhausted"
        ):
            # 注入过的技能卡没能帮上忙 → 失败降权
            self.skills.record_outcome(self.skill_cards, success=False)

        result = RunResult(
            status=status,  # type: ignore[arg-type]
            summary=summary,
            steps=self.step,
            usage=Usage(self.budget.usage.in_tokens, self.budget.usage.out_tokens),
            cost=self.budget.cost,
            wall_ms=wall,
            session_id=self.session_id,
            session_dir=str(self.session_dir),
            verified=bool(verify and verify.passed and verify.verified),
            verify=verify or self.last_verify,
            mutated=sorted(self.ctx.mutated_files),
            error=error,
        )
        self.archive.end(
            status=status, steps=self.step, summary=one_line(summary, 400),
            total_in=result.usage.in_tokens, total_out=result.usage.out_tokens,
            cost=round(result.cost, 6), wall_ms=wall, verified=result.verified,
            mutated=result.mutated, llm=self.llm.stats(),
            tool_stats=self.ctx.stats.to_dict(),
            compactions=self.history.n_compactions,
            snapshots=self.snapshots.count,
            context_series=self.budget.context_series,
            reason=result.reason,
        )
        self._emit("end", f"{status}：{one_line(summary, 120)}")
        return result

    # ------------------------------------------------------------------
    def _emit(self, kind: str, text: str, **extra) -> None:
        if self.on_event:
            try:
                self.on_event(kind, text, extra)
            except Exception:
                pass


def _args_preview(args: dict) -> str:
    parts = []
    for k, v in list(args.items())[:3]:
        s = v if isinstance(v, str) else repr(v)
        parts.append(f"{k}={one_line(str(s), 40)}")
    return ", ".join(parts)


def _version() -> str:
    from . import __version__

    return __version__
