#!/usr/bin/env python3
"""VISTA mini-benchmark 运行器。

用法：

    # 跑完整配置的全部题目（需要真实 API key）
    python evals/run_eval.py --config full

    # 跑六组消融
    python evals/run_eval.py --ablation --repeat 1

    # 只跑某几题
    python evals/run_eval.py --tasks fix-timezone-bug,add-pagination

    # 汇总已有结果
    python evals/run_eval.py --summarize evals/results/

设计要点：

  1. 每道题都在 fixture 的**干净副本**上跑（cp -r 到临时目录），
     题目之间、配置之间互不污染。
  2. 判分不看 agent 自己怎么说，只看 `verify` 命令的退出码 ——
     与 Verify-Gate 同一个哲学：终止与成败都由环境裁定。
  3. 消融通过 CLI 开关实现，六组配置共用同一份代码路径，
     因此对照是干净的（不是"另写了一个简化版"）。
  4. self-evolve 这一组需要"跑热"记忆：先在配对题上跑一遍生成技能卡，
     再在测试题上跑。协议写在下面 run_ablation() 里，README 必须同步说明，
     否则那一行数字没有意义。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO / "src"))

from vista.util import miniyaml  # noqa: E402

RESULTS = ROOT / "results"

# 六组消融配置：名字 → 额外的 CLI 参数
ABLATIONS: dict[str, list[str]] = {
    "full": [],
    "no-repomap": ["--no-repomap"],
    "no-compact": ["--no-compact"],
    "no-skills": ["--no-skills"],
    "no-verify": ["--no-verify"],
    "baseline": ["--baseline"],
}


# ---------------------------------------------------------------------------
@dataclass
class Task:
    id: str
    fixture: str
    prompt: str
    verify: str
    timeout: int = 900
    max_steps: int = 40
    tags: list[str] = field(default_factory=list)
    pair: str = ""          # 配对题：self-evolve 组用它做"跑热"
    path: str = ""

    @staticmethod
    def load(path: Path) -> "Task":
        d = miniyaml.loads(path.read_text(encoding="utf-8")) or {}
        return Task(
            id=str(d.get("id") or path.stem),
            fixture=str(d.get("fixture") or ""),
            prompt=str(d.get("prompt") or ""),
            verify=str(d.get("verify") or ""),
            timeout=int(d.get("timeout") or 900),
            max_steps=int(d.get("max_steps") or 40),
            tags=[str(t) for t in (d.get("tags") or [])],
            pair=str(d.get("pair") or ""),
            path=str(path),
        )


def load_tasks(only: set[str] | None = None) -> list[Task]:
    tasks = [Task.load(p) for p in sorted((ROOT / "tasks").glob("*.yaml"))]
    tasks = [t for t in tasks if t.prompt and t.fixture]
    if only:
        tasks = [t for t in tasks if t.id in only]
    return tasks


# ---------------------------------------------------------------------------
def materialize(task: Task, workdir: Path) -> Path:
    src = ROOT / "fixtures" / task.fixture
    if not src.is_dir():
        raise FileNotFoundError(f"找不到 fixture：{src}")
    dst = workdir / task.fixture
    shutil.copytree(src, dst)
    return dst


def run_verify(task: Task, cwd: Path) -> tuple[bool, str]:
    """判分：只看退出码，不看 agent 的自述。"""
    try:
        p = subprocess.run(task.verify, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, errors="replace", timeout=300)
    except subprocess.SubprocessError as e:
        return False, f"验收命令异常：{e}"
    tail = (p.stdout or "")[-1200:]
    return p.returncode == 0, tail


def run_one(task: Task, config: str, extra: list[str], keep: bool = False) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix=f"vista-eval-{task.id}-"))
    t0 = time.time()
    record: dict = {
        "task": task.id, "config": config, "tags": task.tags,
        "fixture": task.fixture, "ts": time.time(),
    }
    try:
        cwd = materialize(task, workdir)
        # 注意参数顺序：--max-steps 与消融开关都是全局参数，
        # 必须放在子命令 run 之前，否则 argparse 会拒绝。
        cmd = (
            [sys.executable, "-m", "vista", "-C", str(cwd), "--max-steps", str(task.max_steps)]
            + extra
            + ["run", task.prompt, "--json"]
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["NO_COLOR"] = "1"

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  errors="replace", timeout=task.timeout, env=env)
            out = (proc.stdout or "").strip().splitlines()
            agent = json.loads(out[-1]) if out else {}
            record["agent_error"] = (proc.stderr or "")[-600:] if proc.returncode not in (0, 1) else ""
        except subprocess.TimeoutExpired:
            agent = {"status": "eval_timeout"}
            record["agent_error"] = f"超过 {task.timeout}s"
        except (json.JSONDecodeError, IndexError) as e:
            agent = {"status": "eval_parse_error"}
            record["agent_error"] = f"无法解析 agent 输出：{e}"

        passed, tail = run_verify(task, cwd)
        record.update({
            "passed": passed,
            "status": agent.get("status", "unknown"),
            "agent_claimed_ok": bool(agent.get("ok")),
            "verified": bool(agent.get("verified")),
            "steps": agent.get("steps", 0),
            "in_tokens": agent.get("in_tokens", 0),
            "out_tokens": agent.get("out_tokens", 0),
            "cost": agent.get("cost", 0.0),
            "wall_ms": agent.get("wall_ms", 0),
            "compactions": agent.get("compactions", 0),
            "tool_stats": agent.get("tool_stats", {}),
            "mutated": agent.get("mutated", []),
            "verify_tail": tail,
            "eval_ms": int((time.time() - t0) * 1000),
        })
        if keep:
            record["workdir"] = str(workdir)
        return record
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
def warm_up_skills(tasks: list[Task], extra: list[str]) -> Path | None:
    """self-evolve 组的"跑热"阶段。

    技能卡需要先由同类任务蒸馏出来才可能被命中。做法是：
    在一个共享的技能库目录里先跑一遍每道题的配对题，把技能卡攒出来，
    再把这个目录注入到正式测试的 fixture 里。
    """
    pairs = [t for t in tasks if t.pair]
    if not pairs:
        return None
    shared = Path(tempfile.mkdtemp(prefix="vista-skills-"))
    print(f"[warm-up] 用 {len(pairs)} 道配对题预热技能库 → {shared}")
    for t in pairs:
        pair_task = Task(id=t.pair, fixture=t.fixture, prompt=t.pair,
                         verify=t.verify, timeout=t.timeout, max_steps=t.max_steps)
        rec = run_one(pair_task, "warmup", extra, keep=True)
        wd = rec.get("workdir")
        if wd:
            skills = Path(wd) / t.fixture / ".vista" / "skills"
            if skills.is_dir():
                shared.mkdir(parents=True, exist_ok=True)
                for card in skills.glob("*.yaml"):
                    shutil.copy(card, shared / card.name)
            shutil.rmtree(wd, ignore_errors=True)
    n = len(list(shared.glob("*.yaml")))
    print(f"[warm-up] 蒸馏出 {n} 张技能卡")
    return shared if n else None


# ---------------------------------------------------------------------------
def summarize(rows: list[dict]) -> dict:
    by_config: dict[str, list[dict]] = {}
    for r in rows:
        by_config.setdefault(r.get("config", "?"), []).append(r)

    out: dict = {}
    for cfg, items in by_config.items():
        n = len(items)
        if not n:
            continue
        passed = sum(1 for r in items if r.get("passed"))
        claimed = sum(1 for r in items if r.get("agent_claimed_ok"))
        # 谎报率：agent 说完成了，但验收命令不通过
        lied = sum(1 for r in items if r.get("agent_claimed_ok") and not r.get("passed"))
        out[cfg] = {
            "n": n,
            "pass_rate": round(passed / n, 4),
            "passed": passed,
            "claimed_ok": claimed,
            "false_claim": lied,
            "false_claim_rate": round(lied / max(claimed, 1), 4),
            "avg_steps": round(statistics.fmean([r.get("steps", 0) for r in items]), 2),
            "avg_in_tokens": int(statistics.fmean([r.get("in_tokens", 0) for r in items])),
            "avg_out_tokens": int(statistics.fmean([r.get("out_tokens", 0) for r in items])),
            "avg_cost": round(statistics.fmean([r.get("cost", 0.0) for r in items]), 5),
            "avg_wall_s": round(statistics.fmean([r.get("wall_ms", 0) for r in items]) / 1000, 1),
            "avg_compactions": round(statistics.fmean([r.get("compactions", 0) for r in items]), 2),
            "stale_blocked": sum((r.get("tool_stats") or {}).get("stale_blocked", 0) for r in items),
        }
    return out


LABELS = {
    "full": "VISTA 完整",
    "no-repomap": "− Index（L1 RepoMap）",
    "no-compact": "− Anchor Compression",
    "no-skills": "− Self-evolve（L3）",
    "no-verify": "− Verify-Gate",
    "baseline": "裸基线（仅 bash）",
}


def render_table(summary: dict) -> str:
    order = [k for k in ABLATIONS if k in summary] + [k for k in summary if k not in ABLATIONS]
    head = ("| 配置 | n | pass@1 | 谎报率 | 平均步数 | 平均输入 tok | "
            "平均成本 | 平均耗时 | 平均压缩次数 |")
    sep = "|---|---|---|---|---|---|---|---|---|"
    lines = [head, sep]
    for k in order:
        s = summary[k]
        lines.append(
            f'| **{LABELS.get(k, k)}** | {s["n"]} | {s["pass_rate"] * 100:.1f}% | '
            f'{s["false_claim_rate"] * 100:.1f}% | {s["avg_steps"]} | '
            f'{s["avg_in_tokens"]:,} | ${s["avg_cost"]:.4f} | '
            f'{s["avg_wall_s"]}s | {s["avg_compactions"]} |'
        )
    return "\n".join(lines)


def analyze_by_length(rows: list[dict], threshold: int = 15) -> str:
    """按任务长度切分子集。

    完整配置未必在短任务上赢——压缩与索引都有固定成本，任务太短摊不薄。
    诚实地做子集切分，比掩盖总体数字更有说服力。
    """
    long_rows = [r for r in rows if r.get("steps", 0) >= threshold]
    short_rows = [r for r in rows if 0 < r.get("steps", 0) < threshold]
    parts = []
    for name, subset in (("长任务（≥%d 步）" % threshold, long_rows),
                         ("短任务（<%d 步）" % threshold, short_rows)):
        if not subset:
            continue
        parts.append(f"\n**{name}**（n={len(subset)}）\n")
        parts.append(render_table(summarize(subset)))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="VISTA mini-benchmark 运行器")
    ap.add_argument("--config", default="full", choices=list(ABLATIONS),
                    help="单组配置")
    ap.add_argument("--ablation", action="store_true", help="跑全部六组消融")
    ap.add_argument("--tasks", default="", help="逗号分隔的题目 id，留空表示全部")
    ap.add_argument("--repeat", type=int, default=1, help="每题重复次数")
    ap.add_argument("--out", default=str(RESULTS / "results.jsonl"))
    ap.add_argument("--summarize", default="", help="只汇总已有的 results.jsonl")
    ap.add_argument("--keep", action="store_true", help="保留每题的临时工作区")
    ap.add_argument("--list", action="store_true", help="列出全部题目后退出")
    args = ap.parse_args()

    if args.summarize:
        p = Path(args.summarize)
        p = p / "results.jsonl" if p.is_dir() else p
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        print(render_table(summarize(rows)))
        print(analyze_by_length(rows))
        return 0

    only = {t.strip() for t in args.tasks.split(",") if t.strip()} or None
    tasks = load_tasks(only)
    if not tasks:
        print("没有找到题目。检查 evals/tasks/*.yaml", file=sys.stderr)
        return 2

    if args.list:
        for t in tasks:
            print(f'{t.id:28} {t.fixture:16} {",".join(t.tags):24} {t.prompt[:60]}')
        return 0

    if not (os.environ.get("VISTA_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        print("警告：没有检测到 VISTA_API_KEY，评测需要真实模型接口。", file=sys.stderr)
        print("      离线体验请改用：vista demo", file=sys.stderr)
        return 2

    configs = list(ABLATIONS) if args.ablation else [args.config]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    rows: list[dict] = []

    total = len(configs) * len(tasks) * args.repeat
    done = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for cfg in configs:
            extra = list(ABLATIONS[cfg])
            warm: Path | None = None
            if cfg == "full" and args.ablation:
                # full 组同时作为 self-evolve 的"有记忆"对照
                warm = warm_up_skills(tasks, extra)
            for rep in range(args.repeat):
                for t in tasks:
                    done += 1
                    print(f"[{done}/{total}] {cfg} · {t.id} · rep{rep + 1}", flush=True)
                    rec = run_one(t, cfg, extra, keep=args.keep)
                    rec["repeat"] = rep + 1
                    if warm:
                        rec["warmed"] = True
                    rows.append(rec)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    mark = "PASS" if rec.get("passed") else "FAIL"
                    print(f'    → {mark}  {rec.get("status")}  '
                          f'{rec.get("steps")} 步  ${rec.get("cost", 0):.4f}', flush=True)
            if warm:
                shutil.rmtree(warm, ignore_errors=True)

    print()
    print(render_table(summarize(rows)))
    print(analyze_by_length(rows))
    print(f"\n原始结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
