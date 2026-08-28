"""VISTA 命令行入口。

    vista                        交互式 REPL
    vista run "任务"             一次性任务
    vista resume <session_id>    续跑一个会话
    vista report [session_id]    生成 HTML 报告
    vista map [文件...]          打印 L1 仓库索引
    vista memory show|edit|clear L2 项目记忆
    vista skills list|disable|enable|rm  L3 技能卡
    vista sessions               列出历史会话
    vista clean                  清理会话与快照
    vista demo                   离线演示（不需要 API key）
    vista doctor                 环境自检

消融开关（这是评测用的接口，不是调试残留）：
    --no-repomap  --no-compact  --no-skills  --no-verify  --baseline
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_TOML, load_config
from .errors import ConfigError, VistaError
from .memory.archive import list_sessions, load_session, resume_context
from .report import write_report


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vista",
        description="VISTA — Verified, Indexed, Self-evolving, Tiered-memory, "
                    "Anchored-context Coding Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带子命令直接运行 vista 会进入交互模式。",
    )
    p.add_argument("--version", action="version", version=f"vista {__version__}")

    g = p.add_argument_group("通用")
    g.add_argument("-C", "--cwd", default=None, help="工作区目录，默认当前目录")
    g.add_argument("--model", default=None, help="主模型名")
    g.add_argument("--weak-model", default=None, help="弱模型名（压缩与蒸馏用）")
    g.add_argument("--provider", default=None, choices=["http", "openai", "mock"],
                   help="模型接入方式")
    g.add_argument("--base-url", default=None, help="OpenAI 兼容网关地址")
    g.add_argument("--budget", type=float, default=None, help="成本上限（美元）")
    g.add_argument("--max-steps", type=int, default=None, help="步数上限")
    g.add_argument("--yolo", action="store_true", help="跳过全部权限确认（危险）")
    g.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    g.add_argument("--quiet", action="store_true", help="只输出最终结果")

    a = p.add_argument_group("消融开关")
    a.add_argument("--no-repomap", action="store_true", help="关闭 L1 仓库索引")
    a.add_argument("--no-compact", action="store_true", help="关闭 Anchor Compression")
    a.add_argument("--no-skills", action="store_true", help="关闭 L3 技能卡")
    a.add_argument("--no-verify", action="store_true", help="关闭 Verify-Gate")
    a.add_argument("--verify-cmd", default=None, help="手动指定验收命令")
    a.add_argument("--baseline", action="store_true",
                   help="裸基线：只保留 bash + finish，关闭全部记忆与压缩")

    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="执行一次性任务")
    r.add_argument("task", nargs="*", help="任务描述")
    r.add_argument("-f", "--file", default=None, help="从文件读取任务描述")
    r.add_argument("--json", action="store_true", help="以 JSON 输出结果（评测用）")
    r.add_argument("--report", action="store_true", help="结束后生成 HTML 报告")

    rs = sub.add_parser("resume", help="续跑一个已有会话")
    rs.add_argument("session_id", nargs="?", default=None, help="留空则续跑最近一次")
    rs.add_argument("task", nargs="*", help="可选：补充或替换任务描述")
    rs.add_argument("--json", action="store_true")

    rp = sub.add_parser("report", help="生成 HTML 会话报告")
    rp.add_argument("session_id", nargs="?", default=None)
    rp.add_argument("-o", "--out", default=None)

    mp = sub.add_parser("map", help="打印 L1 仓库索引")
    mp.add_argument("focus", nargs="*", help="聚焦文件")
    mp.add_argument("--budget", type=int, default=None, dest="map_budget")
    mp.add_argument("--top-files", type=int, default=0, help="额外打印前 N 个高分文件")

    mm = sub.add_parser("memory", help="L2 项目记忆")
    mm.add_argument("action", nargs="?", default="show",
                    choices=["show", "edit", "clear", "detect"])

    sk = sub.add_parser("skills", help="L3 技能卡")
    sk.add_argument("action", nargs="?", default="list",
                    choices=["list", "show", "disable", "enable", "rm"])
    sk.add_argument("name", nargs="?", default=None)

    sub.add_parser("sessions", help="列出历史会话")

    cl = sub.add_parser("clean", help="清理会话与快照")
    cl.add_argument("--all", action="store_true", help="连同项目记忆与技能卡一起删除")
    cl.add_argument("--yes", action="store_true", help="不询问直接执行")

    dm = sub.add_parser("demo", help="离线演示（无需 API key）")
    dm.add_argument("--keep", action="store_true", help="保留演示工作区")

    sub.add_parser("doctor", help="环境自检")
    sub.add_parser("init", help="在当前目录生成 .vista/config.toml 模板")

    return p


# ---------------------------------------------------------------------------
def overrides_from(args: argparse.Namespace) -> dict:
    o: dict = {}
    if args.model:
        o["model.main"] = args.model
    if args.weak_model:
        o["model.weak"] = args.weak_model
    if args.provider:
        o["model.provider"] = args.provider
    if args.base_url:
        o["model.base_url"] = args.base_url
    if args.budget is not None:
        o["limits.max_cost"] = args.budget
    if args.max_steps is not None:
        o["limits.max_steps"] = args.max_steps
    if args.yolo:
        o["permission.yolo"] = True
    if args.no_color:
        o["color"] = False
    if args.no_repomap:
        o["repomap.enabled"] = False
    if args.no_compact:
        o["context.enabled"] = False
    if args.no_skills:
        o["skills.enabled"] = False
    if args.no_verify:
        o["verify.enabled"] = False
    if args.verify_cmd:
        o["verify.command"] = args.verify_cmd
    if args.baseline:
        o["baseline_mode"] = True
        o["repomap.enabled"] = False
        o["context.enabled"] = False
        o["skills.enabled"] = False
    return o


def _console(cfg, quiet: bool = False):
    from .cli.render import Console

    return Console(color=cfg.color, quiet=quiet)


def _resolve_session(cfg, session_id: str | None) -> Path | None:
    if session_id:
        d = cfg.sessions_dir / session_id
        return d if d.is_dir() else None
    rows = list_sessions(cfg.sessions_dir, limit=1)
    return Path(rows[0]["dir"]) if rows else None


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_run(cfg, args) -> int:
    from .cli.render import TerminalUI
    from .llm.client import LLM
    from .loop import Agent

    task = " ".join(args.task).strip()
    if args.file:
        task = Path(args.file).read_text(encoding="utf-8").strip()
    if not task and not sys.stdin.isatty():
        task = sys.stdin.read().strip()
    if not task:
        _console(cfg).error("请给出任务描述，例如：vista run \"给 /api/todos 加分页\"")
        return 2

    quiet = args.quiet or args.json
    console = _console(cfg, quiet=quiet)
    cfg.interactive = False
    ui = TerminalUI(console, interactive=False)

    if not quiet:
        console.banner(__version__,
                       f"{cfg.model.provider} · {cfg.model.main} · 工作区 {cfg.cwd}")
        if cfg.permission.yolo:
            console.write(console.style("  !! YOLO 模式：所有权限确认已被跳过 !!", "bold", "brightred"))
        if cfg.baseline_mode:
            console.warn("  裸基线消融模式：只有 bash + finish，无记忆、无索引、无压缩。")
        console.task(task)
        console.write()

    agent = Agent(cfg, llm=LLM(cfg), ui=ui, on_event=None if quiet else console.event)
    res = agent.run(task)

    report_path = None
    if args.report:
        report_path = write_report(agent.session_dir)

    if args.json:
        out = {
            "status": res.status, "ok": res.ok, "verified": res.verified,
            "steps": res.steps, "in_tokens": res.usage.in_tokens,
            "out_tokens": res.usage.out_tokens, "cost": round(res.cost, 6),
            "wall_ms": res.wall_ms, "session_id": res.session_id,
            "session_dir": res.session_dir, "mutated": res.mutated,
            "reason": res.reason, "summary": res.summary[:2000],
            "compactions": agent.history.n_compactions,
            "tool_stats": agent.ctx.stats.to_dict(),
            "error": res.error,
        }
        print(json.dumps(out, ensure_ascii=False))
    else:
        console.result(res)
        if report_path:
            console.write(console.style(f"HTML 报告：{report_path}", "grey"))
    return 0 if res.ok else 1


def cmd_resume(cfg, args) -> int:
    from .cli.render import TerminalUI
    from .llm.client import LLM
    from .loop import Agent

    console = _console(cfg, quiet=args.json)
    d = _resolve_session(cfg, args.session_id)
    if d is None:
        console.error("找不到可续跑的会话。用 vista sessions 查看历史会话。")
        return 2

    ctxdata = resume_context(d, recent_keep=cfg.context.recent_keep)
    task = " ".join(args.task).strip() or ctxdata.get("task", "")
    if not task:
        console.error("该会话没有记录任务描述，请显式给出任务。")
        return 2

    if not args.json:
        console.banner(__version__, f"续跑会话 {d.name}（已进行 {ctxdata.get('steps', 0)} 步）")
        console.task(task)
        console.write()

    cfg.interactive = False
    ui = TerminalUI(console, interactive=False)
    agent = Agent(cfg, llm=LLM(cfg), ui=ui, on_event=None if args.json else console.event)
    res = agent.run(task, resume=ctxdata)

    if args.json:
        print(json.dumps({"status": res.status, "ok": res.ok, "steps": res.steps,
                          "session_id": res.session_id}, ensure_ascii=False))
    else:
        console.result(res)
    return 0 if res.ok else 1


def cmd_report(cfg, args) -> int:
    console = _console(cfg)
    d = _resolve_session(cfg, args.session_id)
    if d is None:
        console.error("找不到会话。用 vista sessions 查看历史会话。")
        return 2
    path = write_report(d, Path(args.out) if args.out else None)
    console.ok(f"报告已生成：{path}")
    console.write(console.style("用浏览器打开即可，报告是单文件、无外部依赖的。", "grey"))
    return 0


def cmd_map(cfg, args) -> int:
    from .memory.repomap import RepoMap

    console = _console(cfg)
    rm = RepoMap(cfg.cwd, cfg.repomap, model=cfg.model.main)
    stats = rm.build()
    if not rm.available:
        console.warn(f"仓库索引未启用：{stats.reason or '没有解析到符号'}")
        return 1
    text, n = rm.render(args.focus, args.map_budget or cfg.repomap.budget)
    console.write(text)
    console.write()
    console.write(console.style(
        f"{n} tokens · {stats.n_files} 文件 · {stats.n_defs} 符号 · "
        f"{stats.n_edges} 条引用边 · {stats.build_ms}ms · "
        f"{'tree-sitter' if stats.tree_sitter else '正则抽取器'}", "grey"))
    if args.top_files:
        console.write()
        console.write(console.style("结构中心性最高的文件：", "grey"))
        for rel, score in rm.top_files(args.top_files, args.focus):
            console.write(f"  {score:8.5f}  {rel}")
    return 0


def cmd_memory(cfg, args) -> int:
    from .memory.project import ProjectMemory, detect_commands, merge_detected

    console = _console(cfg)
    pm = ProjectMemory.load(cfg.project_file)

    if args.action == "show":
        text = pm.render(8000, cfg.model.main)
        console.write(text or console.style("项目记忆为空。", "grey"))
        console.write()
        console.write(console.style(f"文件：{cfg.project_file}", "grey"))
    elif args.action == "detect":
        det = detect_commands(cfg.cwd)
        for k, v in det.items():
            console.kv(k, str(v) or "-")
    elif args.action == "edit":
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        cfg.ensure_dirs()
        if not cfg.project_file.exists():
            merge_detected(pm, detect_commands(cfg.cwd))
            pm.save()
        os.system(f'{editor} "{cfg.project_file}"')
    elif args.action == "clear":
        if cfg.project_file.exists():
            cfg.project_file.unlink()
            console.ok(f"已删除 {cfg.project_file}")
        else:
            console.write(console.style("项目记忆本来就是空的。", "grey"))
    return 0


def cmd_skills(cfg, args) -> int:
    from .memory.skills import SkillIndex

    console = _console(cfg)
    idx = SkillIndex.load(cfg.skills_dir, cfg.skills, model=cfg.model.main)

    if args.action == "list":
        rows = idx.summary()
        if not rows:
            console.write(console.style(
                "技能库为空。当任务通过 Verify-Gate、步数达到阈值且产生过文件改动时，"
                "会自动蒸馏出技能卡。", "grey"))
            return 0
        for r in rows:
            flag = console.style("启用", "green") if r["enabled"] else console.style("停用", "red")
            console.write(f'{console.style(r["name"], "cyan")}  {r["title"]}  [{flag}]  '
                          f'{r["success"]}/{r["usage"]} 成功  连续失败 {r["fail_streak"]}')
            console.write(console.style(f'  触发词：{", ".join(r["triggers"])}', "grey"))
            console.write(console.style(f'  文件：{r["path"]}', "grey"))
        return 0

    if not args.name:
        console.error(f"{args.action} 需要给出技能卡名字。")
        return 2

    if args.action == "show":
        for c in idx.cards:
            if c.name == args.name:
                console.write(c.render())
                return 0
        console.error(f"没有找到技能卡 {args.name}")
        return 1

    if args.action in ("disable", "enable"):
        ok = idx.set_enabled(args.name, args.action == "enable")
        (console.ok if ok else console.error)(
            f"{args.name} 已{'启用' if args.action == 'enable' else '停用'}" if ok
            else f"没有找到技能卡 {args.name}")
        return 0 if ok else 1

    if args.action == "rm":
        ok = idx.remove(args.name)
        (console.ok if ok else console.error)(
            f"已删除技能卡 {args.name}" if ok else f"没有找到技能卡 {args.name}")
        return 0 if ok else 1
    return 0


def cmd_sessions(cfg, args) -> int:
    console = _console(cfg)
    rows = list_sessions(cfg.sessions_dir)
    if not rows:
        console.write(console.style("还没有任何会话记录。", "grey"))
        return 0
    for r in rows:
        color = {"success": "green", "answered": "green"}.get(r["status"], "yellow")
        console.write(
            f'{console.style(r["session_id"], "cyan")}  '
            f'{console.style(r["status"].ljust(16), color)}  '
            f'{r["steps"]:>3} 步  ${r["cost"]:.4f}  {r["task"]}'
        )
    return 0


def cmd_clean(cfg, args) -> int:
    console = _console(cfg)
    targets = [cfg.sessions_dir]
    if args.all:
        targets += [cfg.skills_dir, cfg.project_file]
    existing = [t for t in targets if t.exists()]
    if not existing:
        console.write(console.style("没有需要清理的内容。", "grey"))
        return 0
    console.write("将要删除：")
    for t in existing:
        console.write(f"  {t}")
    if not args.yes:
        try:
            if input("确认？[y/N] ").strip().lower() not in ("y", "yes"):
                console.write("已取消。")
                return 0
        except (EOFError, KeyboardInterrupt):
            return 0
    for t in existing:
        shutil.rmtree(t) if t.is_dir() else t.unlink()
    console.ok("清理完成。")
    return 0


def cmd_doctor(cfg, args) -> int:
    console = _console(cfg)
    console.rule("VISTA 环境自检")
    console.kv("Python", sys.version.split()[0])
    console.kv("VISTA", __version__)
    console.kv("工作区", str(cfg.cwd))

    def probe(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)
        mark = console.style("可用", "green") if ok else console.style("不可用（已降级）", "yellow")
        console.kv(name, f"{mark}  {detail}")

    def _tiktoken():
        from .llm import tokens as T

        T.count_tokens("probe", cfg.model.main)
        return T.using_tiktoken(), "精确计数" if T.using_tiktoken() else "中英混合启发式估算"

    def _ts():
        from .memory.symbols import extract_tags, using_tree_sitter

        extract_tags("a.py", "def f():\n    pass\n")
        return using_tree_sitter(), "语法树抽取" if using_tree_sitter() else "正则抽取器"

    def _yaml():
        from .util import miniyaml

        return miniyaml._pyyaml is not None, "PyYAML" if miniyaml._pyyaml else "内置 YAML 子集解析器"

    def _rg():
        return shutil.which("rg") is not None, "ripgrep" if shutil.which("rg") else "Python 正则遍历"

    def _git():
        return shutil.which("git") is not None, "git ls-files 可用于文件枚举" if shutil.which("git") else "退化为目录遍历"

    probe("tiktoken", _tiktoken)
    probe("tree-sitter", _ts)
    probe("PyYAML", _yaml)
    probe("ripgrep", _rg)
    probe("git", _git)

    console.rule("模型配置")
    console.kv("provider", cfg.model.provider)
    console.kv("base_url", cfg.model.base_url)
    console.kv("main", cfg.model.main)
    console.kv("weak", cfg.model.weak_model)
    key = cfg.api_key
    console.kv("API key", console.style("已设置（" + key[:6] + "…）", "green") if key
               else console.style("未设置 —— 请 export VISTA_API_KEY=...", "yellow"))
    console.kv("上下文预算", f"{cfg.context_budget:,} tokens（θ={cfg.context.theta}，"
                             f"阈值 {cfg.compact_threshold:,}）")

    console.rule("验收探测")
    from .memory.project import detect_commands

    det = detect_commands(cfg.cwd)
    for k in ("language", "test", "lint", "build"):
        console.kv(k, str(det.get(k) or "-"))

    console.rule()
    console.write(console.style(
        "所有\"不可用\"项都有降级路径，不影响 VISTA 运行——这是零必需依赖设计的一部分。", "grey"))
    return 0


def cmd_init(cfg, args) -> int:
    console = _console(cfg)
    cfg.ensure_dirs()
    path = cfg.vista_dir / "config.toml"
    if path.exists():
        console.warn(f"{path} 已存在，未覆盖。")
        return 1
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    console.ok(f"已生成 {path}")
    console.write(console.style("凭据请放在环境变量 VISTA_API_KEY，不要写进配置文件。", "grey"))
    return 0


def cmd_demo(cfg, args) -> int:
    """离线演示：用 mock provider 回放一段脚本，跑通完整循环并产出报告。"""
    from .demo import run_demo

    return run_demo(cfg, keep=args.keep)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.cwd, overrides_from(args))
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    cmd = args.command
    try:
        if cmd is None:
            from .cli.repl import main as repl_main

            return repl_main(cfg)
        handler = {
            "run": cmd_run, "resume": cmd_resume, "report": cmd_report,
            "map": cmd_map, "memory": cmd_memory, "skills": cmd_skills,
            "sessions": cmd_sessions, "clean": cmd_clean, "doctor": cmd_doctor,
            "init": cmd_init, "demo": cmd_demo,
        }[cmd]
        return handler(cfg, args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except VistaError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
