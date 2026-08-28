"""L2 项目记忆。

存放跨会话稳定、且对下一次任务真正有用的东西：构建命令、测试命令、
代码风格约定、目录职责。刻意不存放本次任务的临时结论——那属于 L4。

写入时机有两处：
    - agent 通过 memory_write 主动记录（交互模式下需用户确认）
    - 会话开始时自动探测出的 build/verify 命令（任务成功后才落盘）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import tokens as T
from ..util.paths import read_text, write_text
from ..util.text import one_line

SECTIONS = ["build", "verify", "conventions", "layout", "notes"]
SECTION_TITLES = {
    "build": "构建与依赖",
    "verify": "验收方式",
    "conventions": "代码约定",
    "layout": "目录职责",
    "notes": "其它",
}


@dataclass
class ProjectMemory:
    path: Path
    sections: dict[str, list[str]] = field(default_factory=dict)
    dirty: bool = False
    detected: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "ProjectMemory":
        pm = cls(path=Path(path), sections={k: [] for k in SECTIONS})
        if pm.path.is_file():
            pm._parse(read_text(pm.path))
        return pm

    def _parse(self, text: str) -> None:
        current = "notes"
        for raw in text.split("\n"):
            line = raw.rstrip()
            m = re.match(r"^##\s+(\w+)", line)
            if m:
                key = m.group(1).lower()
                current = key if key in SECTIONS else "notes"
                self.sections.setdefault(current, [])
                continue
            if line.startswith("#"):
                continue
            item = line.lstrip("-* ").strip()
            # 跳过我们自己写进文件头的 HTML 注释，否则重新加载时它会被
            # 当成一条真实的记忆条目，并在下次保存时不断累积。
            if item.startswith("<!--") or item.endswith("-->"):
                continue
            if item:
                self.sections.setdefault(current, []).append(item)

    # ------------------------------------------------------------------
    def add(self, key: str, content: str) -> None:
        key = key.strip().lower()
        if key not in SECTIONS:
            key = "notes"
        bucket = self.sections.setdefault(key, [])
        for item in [c.strip() for c in content.split("\n") if c.strip()]:
            if item not in bucket:
                bucket.append(item)
                self.dirty = True

    def get(self, key: str) -> list[str]:
        return list(self.sections.get(key, []))

    def is_empty(self) -> bool:
        return not any(self.sections.values())

    # ------------------------------------------------------------------
    def render(self, budget: int = 400, model: str = "") -> str:
        if self.is_empty():
            return ""
        lines: list[str] = []
        for key in SECTIONS:
            items = self.sections.get(key) or []
            if not items:
                continue
            lines.append(f"[{SECTION_TITLES[key]}]")
            lines += [f"- {one_line(x, 160)}" for x in items]
        text = "\n".join(lines)
        if T.count_tokens(text, model) <= budget:
            return text

        # 超预算：按 SECTIONS 的优先级顺序逐条丢弃末尾的次要条目
        while lines and T.count_tokens("\n".join(lines), model) > budget:
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].startswith("- "):
                    lines.pop(i)
                    break
            else:
                break
        return "\n".join(lines) + "\n[项目记忆已按预算截断]"

    def save(self) -> bool:
        if not self.dirty:
            return False
        out = ["# Project Memory", "", "<!-- 由 VISTA 维护的 L2 项目记忆，可以手动编辑 -->", ""]
        for key in SECTIONS:
            items = self.sections.get(key) or []
            if not items:
                continue
            out.append(f"## {key}")
            out += [f"- {x}" for x in items]
            out.append("")
        write_text(self.path, "\n".join(out))
        self.dirty = False
        return True

    def to_dict(self) -> dict:
        return {"sections": {k: list(v) for k, v in self.sections.items() if v},
                "detected": self.detected}


# ---------------------------------------------------------------------------
# 自动探测
# ---------------------------------------------------------------------------
def detect_commands(root: Path) -> dict:
    """探测项目的测试、静态检查与构建命令。

    只看项目文件的存在性与内容，不执行任何命令，因此是安全且瞬时的。
    """
    root = Path(root)
    out: dict = {"test": "", "lint": "", "build": "", "language": "", "framework": []}

    def exists(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    def read(name: str) -> str:
        p = root / name
        try:
            return read_text(p) if p.is_file() else ""
        except OSError:
            return ""

    # ---- Python ----
    pyproject = read("pyproject.toml")
    if exists("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini", "Pipfile"):
        out["language"] = "python"
    if exists("pytest.ini", "tests", "test") or "[tool.pytest" in pyproject or "pytest" in read("setup.cfg"):
        out["test"] = "pytest -q"
        out["language"] = "python"
    if exists("ruff.toml", ".ruff.toml") or "[tool.ruff" in pyproject:
        out["lint"] = "ruff check ."
    elif exists(".flake8") or "[flake8]" in read("setup.cfg"):
        out["lint"] = "flake8"
    if "[tool.poetry]" in pyproject:
        out["build"] = "poetry install"
    elif exists("requirements.txt"):
        out["build"] = "pip install -r requirements.txt"
    for fw, marker in (("django", "django"), ("fastapi", "fastapi"), ("flask", "flask")):
        if marker in (pyproject + read("requirements.txt")).lower():
            out["framework"].append(fw)
    if exists("manage.py") and not out["test"]:
        out["test"] = "python manage.py test"

    # ---- Node ----
    pkg_raw = read("package.json")
    if pkg_raw:
        out["language"] = out["language"] or "javascript"
        try:
            pkg = json.loads(pkg_raw)
        except json.JSONDecodeError:
            pkg = {}
        scripts = pkg.get("scripts") or {}
        runner = "pnpm" if exists("pnpm-lock.yaml") else ("yarn" if exists("yarn.lock") else "npm")
        if "test" in scripts and not out["test"]:
            out["test"] = f"{runner} test" if runner != "npm" else "npm test --silent"
        if "lint" in scripts and not out["lint"]:
            out["lint"] = f"{runner} run lint"
        if "build" in scripts and not out["build"]:
            out["build"] = f"{runner} run build"
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        for fw in ("react", "vue", "express", "next", "nest", "svelte"):
            if fw in deps:
                out["framework"].append(fw)
        if exists("tsconfig.json"):
            out["language"] = "typescript"
            out["lint"] = out["lint"] or "npx tsc --noEmit"

    # ---- 其它语言 ----
    if exists("go.mod"):
        out["language"] = out["language"] or "go"
        out["test"] = out["test"] or "go test ./..."
        out["build"] = out["build"] or "go build ./..."
        out["lint"] = out["lint"] or "go vet ./..."
    if exists("Cargo.toml"):
        out["language"] = out["language"] or "rust"
        out["test"] = out["test"] or "cargo test"
        out["build"] = out["build"] or "cargo build"
        out["lint"] = out["lint"] or "cargo clippy -- -D warnings"
    if exists("pom.xml"):
        out["language"] = out["language"] or "java"
        out["test"] = out["test"] or "mvn -q test"
    if exists("build.gradle", "build.gradle.kts"):
        out["language"] = out["language"] or "java"
        out["test"] = out["test"] or "./gradlew test"

    # ---- Makefile 兜底 ----
    mk = read("Makefile") or read("makefile")
    if mk:
        for target, key in (("test", "test"), ("lint", "lint"), ("build", "build")):
            if re.search(rf"^{target}\s*:", mk, re.M) and not out[key]:
                out[key] = f"make {target}"

    # pytest 没装时降级到标准库 unittest —— 让"没有第三方依赖的项目"也能被验收
    if out["test"].startswith("pytest") and not _has_pytest():
        tests_dir = "tests" if (root / "tests").is_dir() else ("test" if (root / "test").is_dir() else "")
        out["test"] = (
            f"python3 -m unittest discover -s {tests_dir} -t . -q" if tests_dir
            else "python3 -m unittest discover -q"
        )

    out["framework"] = sorted(set(out["framework"]))
    return out


def _has_pytest() -> bool:
    import importlib.util
    import shutil

    if shutil.which("pytest"):
        return True
    try:
        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False


def merge_detected(pm: ProjectMemory, detected: dict) -> None:
    """把探测结果写入内存中的项目记忆（任务成功后才落盘）。"""
    pm.detected = detected
    if detected.get("test"):
        pm.add("verify", f"测试命令：{detected['test']}")
    if detected.get("lint"):
        pm.add("verify", f"静态检查：{detected['lint']}")
    if detected.get("build"):
        pm.add("build", f"依赖安装/构建：{detected['build']}")
    if detected.get("language"):
        fw = f"，框架：{', '.join(detected['framework'])}" if detected.get("framework") else ""
        pm.add("layout", f"主语言：{detected['language']}{fw}")
