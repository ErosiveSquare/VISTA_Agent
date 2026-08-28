"""VISTA 配置系统。

优先级（低 → 高）：
    内置默认值 → ~/.vista/config.toml → <项目>/.vista/config.toml
    → 环境变量 → 命令行参数

凭据只从环境变量读取。若在 config.toml 中检测到疑似 API key，直接报错退出
（防呆设计：避免凭据被误提交进仓库）。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

VISTA_DIR = ".vista"


# ---------------------------------------------------------------------------
# 各配置段
# ---------------------------------------------------------------------------
@dataclass
class ModelCfg:
    provider: str = "http"  # http | openai | mock
    base_url: str = "https://api.openai.com/v1"
    main: str = "gpt-4o-mini"
    weak: str = ""  # 留空则复用 main
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 180
    max_retries: int = 3
    context_window: int = 128_000
    # 每百万 token 的价格（美元），仅用于成本估算
    price_in: float = 0.0
    price_out: float = 0.0
    stream: bool = True

    @property
    def weak_model(self) -> str:
        return self.weak or self.main


@dataclass
class LimitsCfg:
    max_steps: int = 40
    max_cost: float = 1.0
    stuck_window: int = 4
    max_parse_failures: int = 3


@dataclass
class ContextCfg:
    budget: int = 0  # 0 表示按 context_window * 0.7 自动推导
    theta: float = 0.6
    gamma: float = 0.35
    recent_keep: int = 6
    min_span: int = 4
    max_overdue: int = 10
    anchor_cap: int = 30
    enabled: bool = True
    probe: bool = False  # 压缩验证探针（可选加强）


@dataclass
class RepoMapCfg:
    enabled: bool = True
    budget: int = 1024
    min_files: int = 15
    focus_weight: float = 20.0
    damping: float = 0.85
    max_files: int = 3000
    per_file_cap: int = 10


@dataclass
class SkillsCfg:
    enabled: bool = True
    min_steps: int = 6
    min_score: float = 0.5
    top_k: int = 2
    max_fail_streak: int = 2


@dataclass
class VerifyCfg:
    enabled: bool = True
    baseline: bool = True
    timeout: int = 300
    baseline_timeout: int = 120
    max_attempts: int = 3
    command: str = ""  # 手动指定则跳过探测


@dataclass
class PermissionCfg:
    mode: str = "ask"  # ask | allow | deny
    yolo: bool = False
    allow_bash_in_run_mode: bool = True


@dataclass
class ToolsCfg:
    max_file_bytes: int = 512 * 1024
    read_limit: int = 400
    bash_timeout: int = 120
    bash_output_bytes: int = 4096
    grep_max_results: int = 40
    tool_result_bytes: int = 12_000


@dataclass
class MemoryCfg:
    project_budget: int = 400
    skill_budget: int = 500


@dataclass
class Config:
    cwd: Path = field(default_factory=Path.cwd)
    model: ModelCfg = field(default_factory=ModelCfg)
    limits: LimitsCfg = field(default_factory=LimitsCfg)
    context: ContextCfg = field(default_factory=ContextCfg)
    repomap: RepoMapCfg = field(default_factory=RepoMapCfg)
    skills: SkillsCfg = field(default_factory=SkillsCfg)
    verify: VerifyCfg = field(default_factory=VerifyCfg)
    permission: PermissionCfg = field(default_factory=PermissionCfg)
    tools: ToolsCfg = field(default_factory=ToolsCfg)
    memory: MemoryCfg = field(default_factory=MemoryCfg)

    interactive: bool = False
    baseline_mode: bool = False  # 消融：只保留 bash + finish
    api_key: str = ""
    color: bool = True

    # ---------------- 派生属性 ----------------
    @property
    def vista_dir(self) -> Path:
        return Path(self.cwd) / VISTA_DIR

    @property
    def sessions_dir(self) -> Path:
        return self.vista_dir / "sessions"

    @property
    def skills_dir(self) -> Path:
        return self.vista_dir / "skills"

    @property
    def project_file(self) -> Path:
        return self.vista_dir / "project.md"

    @property
    def context_budget(self) -> int:
        if self.context.budget > 0:
            return self.context.budget
        return max(8000, min(int(self.model.context_window * 0.7), 200_000))

    @property
    def compact_threshold(self) -> int:
        return int(self.context_budget * self.context.theta)

    @property
    def compact_target(self) -> int:
        return int(self.context_budget * self.context.gamma)

    def ensure_dirs(self) -> None:
        self.vista_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cwd"] = str(self.cwd)
        d.pop("api_key", None)  # 绝不序列化凭据
        # 派生属性不是 dataclass 字段，但报告与评测需要它们
        d["context_budget"] = self.context_budget
        d["compact_threshold"] = self.compact_threshold
        return d


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,})")
_SECRET_KEYS = {"api_key", "apikey", "key", "secret", "token", "password"}


def _scan_secrets(data: dict, where: Path) -> None:
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in _SECRET_KEYS and isinstance(v, str) and v.strip():
                    raise ConfigError(
                        f"{where} 中出现了疑似凭据字段 '{path}{k}'。\n"
                        f"VISTA 只从环境变量读取凭据：请设置 VISTA_API_KEY，并把该字段从配置文件中删除。"
                    )
                walk(v, f"{path}{k}.")
        elif isinstance(node, str) and _SECRET_RE.search(node):
            raise ConfigError(f"{where} 中出现了疑似 API key 字符串（{path}）。请改用环境变量 VISTA_API_KEY。")

    walk(data, "")


def _read_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ConfigError(f"无法解析配置文件 {p}：{e}")
    _scan_secrets(data, p)
    return data


def _apply(section: Any, data: dict, prefix: str) -> None:
    if not isinstance(data, dict):
        return
    valid = {f.name: f.type for f in fields(section)}
    for k, v in data.items():
        if k not in valid:
            continue
        cur = getattr(section, k)
        try:
            if isinstance(cur, bool):
                setattr(section, k, bool(v))
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(section, k, int(v))
            elif isinstance(cur, float):
                setattr(section, k, float(v))
            else:
                setattr(section, k, v)
        except (TypeError, ValueError):
            raise ConfigError(f"配置项 {prefix}{k} 的值不合法：{v!r}")


def load_config(cwd: Path | str | None = None, overrides: dict | None = None) -> Config:
    cfg = Config(cwd=Path(cwd or Path.cwd()).resolve())

    layers = [
        Path.home() / ".vista" / "config.toml",
        cfg.vista_dir / "config.toml",
    ]
    for p in layers:
        data = _read_toml(p)
        for name in ("model", "limits", "context", "repomap", "skills", "verify",
                     "permission", "tools", "memory"):
            if name in data:
                _apply(getattr(cfg, name), data[name], f"{name}.")

    # ---- 环境变量 ----
    env = os.environ
    cfg.api_key = env.get("VISTA_API_KEY", "") or env.get("OPENAI_API_KEY", "")
    if env.get("VISTA_BASE_URL"):
        cfg.model.base_url = env["VISTA_BASE_URL"]
    if env.get("VISTA_MODEL"):
        cfg.model.main = env["VISTA_MODEL"]
    if env.get("VISTA_WEAK_MODEL"):
        cfg.model.weak = env["VISTA_WEAK_MODEL"]
    if env.get("VISTA_PROVIDER"):
        cfg.model.provider = env["VISTA_PROVIDER"]
    if env.get("NO_COLOR"):
        cfg.color = False

    # ---- CLI 覆盖 ----
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if "." in key:
            sec_name, field_name = key.split(".", 1)
            sec = getattr(cfg, sec_name, None)
            if sec is not None and is_dataclass(sec):
                _apply(sec, {field_name: value}, f"{sec_name}.")
        else:
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    if cfg.permission.yolo:
        cfg.permission.mode = "allow"
    return cfg


DEFAULT_CONFIG_TOML = """\
# VISTA 配置文件。凭据请放在环境变量 VISTA_API_KEY，不要写在这里。

[model]
provider    = "http"          # http（标准库直连）| openai（官方 SDK）| mock（离线脚本）
base_url    = "https://api.openai.com/v1"
main        = "gpt-4o-mini"
weak        = ""              # 留空则复用 main；建议填一个更便宜的模型
temperature = 0.2
context_window = 128000
price_in    = 0.0             # 每百万输入 token 的美元价格，仅用于成本估算
price_out   = 0.0

[limits]
max_steps = 40
max_cost  = 1.0

[context]
enabled     = true
theta       = 0.6
gamma       = 0.35
recent_keep = 6

[repomap]
enabled   = true
budget    = 1024
min_files = 15

[skills]
enabled   = true
min_steps = 6
min_score = 0.5

[verify]
enabled  = true
baseline = true
timeout  = 300

[permission]
mode = "ask"                  # ask | allow | deny
"""
