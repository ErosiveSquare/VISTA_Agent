"""Verify-Gate —— VISTA 名字里的 "V"。

核心主张：**模型不能决定任务是否完成**。finish 只是一个请求，
真正的裁定权交给环境信号——测试、编译器、静态检查。

判据（基线对比）：

    pass  ⟺  (F₁ \\ F₀ = ∅)  ∧  (lint 退出码 = 0)

其中 F₀ 是任务开始时就失败的用例集合，F₁ 是当前失败集合。
只要求"不引入新的失败"，不要求把项目里原本就坏的测试也修好——
否则在任何有历史遗留问题的项目上，这道门永远过不去。

没有测试的项目走降级模式（语法检查 / 导入检查），
并且诚实地把结果标记为 verified=false。
"这个项目我没能真正验证" 本身就是一种可靠性。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .types import VerifyReport
from .util.paths import rel_to, truncate_head_tail

# ---------------------------------------------------------------------------
# 失败用例解析
# ---------------------------------------------------------------------------
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+.*)?$", re.M)
_PYTEST_SHORT = re.compile(r"^(\S+::\S+)\s+(?:FAILED|ERROR)\b", re.M)
_UNITTEST = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)", re.M)
_JEST_X = re.compile(r"^\s*(?:✕|×|✗)\s+(.+?)(?:\s+\(\d+\s*ms\))?$", re.M)
_JEST_BULLET = re.compile(r"^\s*●\s+(?!Console)(.+?)$", re.M)
_GO_FAIL = re.compile(r"^\s*--- FAIL:\s+(\S+)", re.M)
_CARGO_FAIL = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", re.M)
_MOCHA = re.compile(r"^\s*\d+\)\s+(.+)$", re.M)


def parse_failures(output: str) -> tuple[set[str], str]:
    """从测试输出中抽取失败用例的标识集合。返回 (集合, 识别到的框架)。"""
    text = output or ""

    py = set(_PYTEST_FAILED.findall(text)) | set(_PYTEST_SHORT.findall(text))
    py = {x for x in py if "::" in x or x.endswith(".py")}
    if py:
        return py, "pytest"

    # unittest 的输出格式随版本变化：
    #   Python ≤3.10  FAIL: test_expiry (tests.test_auth.TestAuth)
    #   Python ≥3.11  FAIL: test_expiry (tests.test_auth.TestAuth.test_expiry)
    # 后者括号里已经带上了方法名，直接拼接会得到 ...test_expiry.test_expiry。
    ut: set[str] = set()
    for name, qual in _UNITTEST.findall(text):
        ut.add(qual if qual.split(".")[-1] == name else f"{qual}.{name}")
    if ut:
        return ut, "unittest"

    go = set(_GO_FAIL.findall(text))
    if go:
        return go, "go"

    cargo = set(_CARGO_FAIL.findall(text))
    if cargo:
        return cargo, "cargo"

    jest = {x.strip() for x in _JEST_X.findall(text) if x.strip()}
    jest |= {x.strip() for x in _JEST_BULLET.findall(text) if x.strip()}
    if jest:
        return jest, "jest"

    mocha = {x.strip() for x in _MOCHA.findall(text) if len(x.strip()) > 3}
    if mocha and "passing" in text:
        return mocha, "mocha"

    return set(), ""


def extract_failure_detail(output: str, failure_id: str, max_lines: int = 15) -> str:
    """截取某个失败用例的关键回溯，控制在 max_lines 行以内。"""
    lines = (output or "").split("\n")
    short = failure_id.split("::")[-1].split(".")[-1]
    anchor = -1
    for i, line in enumerate(lines):
        if failure_id in line or (short and re.search(rf"[_\s]{re.escape(short)}[_\s]", line)):
            anchor = i
            break
    if anchor < 0:
        return ""
    window = lines[anchor : anchor + max_lines * 2]
    keep = [
        ln for ln in window
        if ln.strip() and (
            ln.lstrip().startswith(("E ", ">", "assert", "Error", "Expected", "Received",
                                    "AssertionError", "TypeError", "ValueError", "panic:"))
            or re.search(r"\.(py|js|ts|go|rs|java):\d+", ln)
            or ln.startswith(("FAILED", "ERROR", "---", "___"))
        )
    ]
    if not keep:
        keep = [ln for ln in window if ln.strip()][:max_lines]
    return "\n".join(f"    {ln.strip()[:160]}" for ln in keep[:max_lines])


# ---------------------------------------------------------------------------
@dataclass
class VerifyPlan:
    test_cmd: str = ""
    lint_cmd: str = ""
    mode: str = "test"          # test | lint | syntax | manual | skipped
    reason: str = ""

    def describe(self) -> str:
        if self.mode == "test" and self.test_cmd:
            extra = f"，静态检查：{self.lint_cmd}" if self.lint_cmd else ""
            return (
                f"完成后系统会运行 `{self.test_cmd}`{extra} 进行验收，并与任务开始时的基线对比。\n"
                f"判据：不得引入新的失败；并且，如果某个既有失败的回溯指向了你修改过的文件，"
                f"说明它属于本次任务的目标，也必须被修好。"
            )
        if self.mode == "lint" and self.lint_cmd:
            return f"本项目没有检测到测试，验收方式是静态检查：`{self.lint_cmd}`。"
        if self.mode == "syntax":
            return "本项目没有检测到测试与静态检查，验收方式是对你改动过的文件做语法/导入检查。"
        if self.mode == "skipped":
            return "本次运行关闭了自动验收（--no-verify）。"
        return "本项目无法自动验收，请在 finish 的 summary 中说明你如何确认改动正确。"


@dataclass
class VerifyGate:
    cfg: Config
    detected: dict = field(default_factory=dict)
    plan: VerifyPlan = field(default_factory=VerifyPlan)
    baseline_failures: set[str] = field(default_factory=set)
    baseline_output: str = ""
    baseline_done: bool = False
    baseline_note: str = ""
    attempts: int = 0
    history: list[VerifyReport] = field(default_factory=list)
    _mutated: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def make_plan(self) -> VerifyPlan:
        c = self.cfg
        if not c.verify.enabled:
            self.plan = VerifyPlan(mode="skipped", reason="--no-verify")
            return self.plan
        if c.verify.command:
            self.plan = VerifyPlan(test_cmd=c.verify.command, mode="test", reason="用户手动指定")
            return self.plan

        d = self.detected or {}
        test, lint = d.get("test", ""), d.get("lint", "")
        if test:
            self.plan = VerifyPlan(test_cmd=test, lint_cmd=lint, mode="test", reason="自动探测")
        elif lint:
            self.plan = VerifyPlan(lint_cmd=lint, mode="lint", reason="只探测到静态检查")
        else:
            self.plan = VerifyPlan(mode="syntax", reason="未探测到测试与静态检查")
        return self.plan

    # ------------------------------------------------------------------
    def run_baseline(self) -> str:
        """任务开始时跑一次，记录既有失败集合 F₀。"""
        self.baseline_done = True
        if self.plan.mode != "test" or not self.cfg.verify.baseline:
            self.baseline_note = "未采集基线"
            return self.baseline_note
        code, out, timed_out = _run(self.plan.test_cmd, self.cfg.cwd, self.cfg.verify.baseline_timeout)
        if timed_out:
            self.baseline_note = f"基线采集超时（>{self.cfg.verify.baseline_timeout}s），本次验收退化为只比较退出码"
            self.cfg.verify.baseline = False
            return self.baseline_note
        if _not_runnable(code, out):
            # 项目声明了测试框架，但当前环境里跑不起来（没装 dev 依赖是最常见的原因）。
            # 这时不能假装"全部通过"——那会让 Verify-Gate 变成橡皮图章。
            # 正确的做法是降级到语法检查，并在报告里诚实标注 verified=false。
            failed_cmd = self.plan.test_cmd
            self.plan = VerifyPlan(
                mode="syntax",
                reason=f"验收命令 `{failed_cmd}` 在当前环境无法执行",
            )
            self.cfg.verify.baseline = False
            self.baseline_note = (
                f"验收命令 `{failed_cmd}` 在当前环境跑不起来（退出码 {code}），"
                f"已降级为语法/导入检查，结果会标记为 verified=false"
            )
            return self.baseline_note

        failures, framework = parse_failures(out)
        self.baseline_failures = failures
        self.baseline_output = out
        if failures:
            note = f"，已有 {len(failures)} 个失败用例（{framework}）：{', '.join(sorted(failures)[:3])}"
        elif code == 0:
            note = "，全部通过"
        else:
            # 退出码非零却解析不出用例：可能是收集错误。退化为只比较退出码。
            note = "，退出码非零但未解析出具体失败用例，本次验收退化为只比较退出码"
            self.cfg.verify.baseline = False
        self.baseline_note = f"基线：退出码 {code}{note}"
        return self.baseline_note

    # ------------------------------------------------------------------
    def target_failures(self, mutated: list[str]) -> set[str]:
        """基线失败中，与本次改动直接相关、因此必须被修好的那一部分。

        为什么需要这一层：修 bug 类任务里，待修的失败本来就在基线 F₀ 中，
        单纯的"不引入新失败"会被平凡地满足——什么都不改也能通过。
        判据是：某个既有失败的回溯里出现了本次被修改的文件，
        就认为这个失败是本次任务的目标，必须转绿。

        这个判据是启发式的，但它是可计算、可解释的：
        既不需要模型自我申报，也不需要用户额外标注。
        """
        if not self.baseline_failures or not mutated:
            return set()
        norm = [m for m in mutated if m and m != "<workspace>"]
        targets: set[str] = set()
        for fid in self.baseline_failures:
            blob = fid + "\n" + (extract_failure_detail(self.baseline_output, fid, 30) or "")
            for m in norm:
                stem = m.rsplit("/", 1)[-1]
                if m in blob or (stem and stem in blob):
                    targets.add(fid)
                    break
        return targets

    def run(self, mutated: list[str] | None = None) -> VerifyReport:
        self.attempts += 1
        self._mutated = list(mutated or [])
        t0 = time.time()
        mode = self.plan.mode

        if mode == "skipped":
            rep = VerifyReport(passed=True, mode="skipped", verified=False,
                               output="本次运行关闭了自动验收。")
        elif mode == "test":
            rep = self._run_test(self._mutated)
        elif mode == "lint":
            rep = self._run_lint_only()
        else:
            rep = self._run_syntax()

        rep.duration_ms = int((time.time() - t0) * 1000)
        self.history.append(rep)
        return rep

    # ------------------------------------------------------------------
    def _run_test(self, mutated: list[str] | None = None) -> VerifyReport:
        cmd = self.plan.test_cmd
        code, out, timed_out = _run(cmd, self.cfg.cwd, self.cfg.verify.timeout)
        if timed_out:
            kept, _ = truncate_head_tail(out, 2500)
            return VerifyReport(passed=False, mode="test", command=cmd, exit_code=-1,
                                output=f"测试执行超过 {self.cfg.verify.timeout} 秒被终止。\n{kept}")

        failures, framework = parse_failures(out)
        targets = self.target_failures(mutated or [])
        unfixed = sorted(failures & targets)
        known = sorted(failures & self.baseline_failures - set(unfixed))
        new = sorted(failures - self.baseline_failures)

        if self.cfg.verify.baseline and (failures or self.baseline_failures):
            passed = not new and not unfixed
        else:
            passed = code == 0
        if code != 0 and not failures:
            # 退出码非零但没解析出用例（可能是收集错误 / 语法错误）
            passed = False

        detail_parts: list[str] = []
        for fid in (new + unfixed)[:4]:
            d = extract_failure_detail(out, fid)
            detail_parts.append(f"  {fid}\n{d}" if d else f"  {fid}")
        if not new and not unfixed and not passed:
            kept, _ = truncate_head_tail(out, 2000)
            detail_parts.append(kept)

        rep = VerifyReport(
            passed=passed, mode="test", command=cmd, exit_code=code,
            new_failures=new, known_failures=known,
            output="\n".join(detail_parts)[:5000],
        )
        rep.unfixed_targets = unfixed

        # 测试通过还要再跑一次静态检查
        if passed and self.plan.lint_cmd:
            lcode, lout, ltimed = _run(self.plan.lint_cmd, self.cfg.cwd, min(self.cfg.verify.timeout, 120))
            if not ltimed and lcode != 0:
                kept, _ = truncate_head_tail(lout, 1800)
                rep.passed = False
                rep.command = f"{cmd} && {self.plan.lint_cmd}"
                rep.exit_code = lcode
                rep.output = (rep.output + f"\n\n静态检查未通过（{self.plan.lint_cmd}，退出码 {lcode}）：\n{kept}").strip()
        return rep

    def _run_lint_only(self) -> VerifyReport:
        cmd = self.plan.lint_cmd
        code, out, timed_out = _run(cmd, self.cfg.cwd, min(self.cfg.verify.timeout, 180))
        kept, _ = truncate_head_tail(out, 2500)
        return VerifyReport(
            passed=(code == 0 and not timed_out), mode="lint", command=cmd,
            exit_code=code, output=kept, verified=True,
        )

    def _run_syntax(self, changed: list[str] | None = None) -> VerifyReport:
        """降级模式：对改动过的文件做语法与导入检查。"""
        files = [f for f in (changed or []) if f]
        if not files:
            files = _recent_changed(self.cfg.cwd)

        py = [f for f in files if f.endswith(".py")]
        js = [f for f in files if f.endswith((".js", ".mjs", ".cjs"))]
        ts = [f for f in files if f.endswith((".ts", ".tsx"))]

        checks: list[tuple[str, str]] = []
        if py:
            checks.append(
                (
                    "python",
                    f"{_q(sys.executable)} -m compileall -q "
                    + " ".join(_q(f) for f in py[:30]),
                )
            )
        if js and shutil.which("node"):
            checks.extend(("node", f"node --check {_q(f)}") for f in js[:20])
        if ts and (Path(self.cfg.cwd) / "tsconfig.json").is_file() and shutil.which("npx"):
            checks.append(("tsc", "npx tsc --noEmit"))

        if not checks:
            return VerifyReport(
                passed=True, mode="manual", verified=False,
                output="没有可自动验收的信号（未检测到测试、静态检查，也没有可做语法检查的改动文件）。"
                       "结果标记为 verified=false。",
            )

        outputs: list[str] = []
        bad = 0
        for name, cmd in checks:
            code, out, timed = _run(cmd, self.cfg.cwd, 90)
            if code != 0 or timed:
                bad += 1
                kept, _ = truncate_head_tail(out, 1200)
                outputs.append(f"[{name}] 退出码 {code}\n{kept}")
        return VerifyReport(
            passed=bad == 0, mode="syntax",
            command="; ".join(c for _, c in checks)[:200],
            exit_code=0 if bad == 0 else 1,
            output="\n".join(outputs) or "语法与导入检查通过。",
            verified=False if bad == 0 else True,
        )

    # ------------------------------------------------------------------
    def render_failure(self, rep: VerifyReport) -> str:
        from .prompts import VERIFY_FAIL_TAIL

        lines = [f"[VERIFY-GATE 第 {self.attempts} 次验收未通过]"]
        if rep.command:
            lines.append(f"命令：{rep.command}")
            lines.append(f"退出码：{rep.exit_code}")
        if rep.new_failures:
            lines.append(f"新增失败（任务开始时并不存在的，共 {len(rep.new_failures)} 个）：")
            lines.append(rep.output)
            if len(rep.new_failures) > 4:
                lines.append(f"  …另有 {len(rep.new_failures) - 4} 个新增失败未展开")
        elif rep.unfixed_targets:
            lines.append(
                f"以下失败的回溯指向了你本次修改过的文件，因此属于本次任务的目标，"
                f"必须修好（共 {len(rep.unfixed_targets)} 个）："
            )
            lines.append(rep.output)
        else:
            lines.append("输出：")
            lines.append(rep.output)
        if rep.known_failures:
            lines.append(
                f"既有失败（基线中已存在，不计入本次判定）：{', '.join(rep.known_failures[:6])}"
            )
        lines.append(VERIFY_FAIL_TAIL)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
_NOT_RUNNABLE = (
    "command not found", "not found", "No module named", "is not recognized",
    "no such file or directory", "cannot be loaded",
)


def _not_runnable(code: int, out: str) -> bool:
    """判断验收命令是不是根本没跑起来（而不是跑了但失败）。"""
    if code == 127:
        return True
    if code == 0:
        return False
    head = (out or "")[:400].lower()
    return any(m.lower() in head for m in _NOT_RUNNABLE)


def _q(s: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([str(s)])
    return "'" + str(s).replace("'", "'\\''") + "'"


def _recent_changed(root: Path, cap: int = 30) -> list[str]:
    from .util.paths import git_dirty_files

    files = git_dirty_files(Path(root), cap)
    if files:
        return files
    out: list[str] = []
    cutoff = time.time() - 3600
    skip = {".git", ".vista", "node_modules", "__pycache__", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in (".py", ".js", ".ts", ".tsx", ".mjs"):
                continue
            try:
                if p.stat().st_mtime >= cutoff:
                    out.append(rel_to(p, Path(root)))
            except OSError:
                continue
            if len(out) >= cap:
                return out
    return out


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str, bool]:
    """执行验收命令。与 bash 工具共享同样的进程组超时策略。"""
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.setdefault("FORCE_COLOR", "0")
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace", **kwargs,
        )
    except OSError as e:
        return 127, f"无法执行验收命令：{e}", False
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
        except OSError:
            pass
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        return -1, out or "", True
