"""路径安全与文件基础操作。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..errors import PathEscape

# 禁止直接进行文件级操作的目录（git 对象、凭据等）
FORBIDDEN_PARTS = {".git", ".env", ".ssh", ".aws", ".gnupg", "node_modules/.bin"}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".bz2", ".xz", ".7z", ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".pyc", ".pyo", ".o", ".a", ".bin", ".wasm", ".mp3",
    ".mp4", ".mov", ".avi", ".woff", ".woff2", ".ttf", ".otf", ".sqlite", ".db",
}


def sha_of(data: str | bytes, n: int = 12) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()[:n]


def resolve_safe(path: str, root: Path) -> Path:
    """把用户/模型给的路径解析成工作区内的绝对路径。

    越界或触碰敏感目录时抛出 PathEscape。
    """
    root = Path(root).resolve()
    raw = Path(path)
    p = (raw if raw.is_absolute() else root / raw)
    try:
        resolved = p.resolve()
    except OSError:
        raise PathEscape(path)

    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise PathEscape(path)

    for part in rel.parts:
        if part in FORBIDDEN_PARTS:
            raise PathEscape(path)
    return resolved


def rel_to(p: Path, root: Path) -> str:
    """返回相对于工作区的、始终使用正斜杠的路径字符串。"""
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except Exception:
        return Path(p).as_posix()


def is_binary(p: Path, sniff: int = 4096) -> bool:
    if p.suffix.lower() in BINARY_EXT:
        return True
    try:
        with p.open("rb") as fh:
            chunk = fh.read(sniff)
    except OSError:
        return False
    if b"\x00" in chunk:
        return True
    # 高比例不可解码字节
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        nontext = sum(1 for b in chunk if b < 9 or (13 < b < 32))
        return nontext / max(len(chunk), 1) > 0.05
    return False


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def truncate_head_tail(text: str, limit: int, head_ratio: float = 0.4) -> tuple[str, dict | None]:
    """保留头部与尾部，中间省略。尾部权重更高——报错信息通常在末尾。"""
    raw = text or ""
    if len(raw) <= limit:
        return raw, None
    head_n = int(limit * head_ratio)
    tail_n = limit - head_n
    omitted = len(raw) - limit
    kept = raw[:head_n] + f"\n… [已省略 {omitted} 字节] …\n" + raw[-tail_n:]
    return kept, {"orig_bytes": len(raw), "kept_bytes": limit, "mode": "head_tail"}


def iter_source_files(
    root: Path,
    exts: set[str],
    max_files: int = 3000,
    max_bytes: int = 512 * 1024,
) -> list[Path]:
    """枚举仓库中的源文件。优先使用 git ls-files（天然遵守 .gitignore）。"""
    root = Path(root).resolve()
    files: list[Path] = []

    tracked = _git_ls_files(root)
    if tracked is not None:
        candidates = [root / f for f in tracked]
    else:
        candidates = []
        skip_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
            ".pytest_cache", "dist", "build", ".vista", "target", ".idea", ".tox",
            ".next", ".ruff_cache", "site-packages", "coverage",
        }
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".git")]
            for fn in filenames:
                candidates.append(Path(dirpath) / fn)
            if len(candidates) > max_files * 6:
                break

    for p in candidates:
        if p.suffix.lower() not in exts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file() or st.st_size > max_bytes or st.st_size == 0:
            continue
        files.append(p)

    if len(files) > max_files:
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        files = files[:max_files]
    return sorted(files)


def _git_ls_files(root: Path) -> list[str] | None:
    import subprocess

    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def git_head(root: Path) -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_dirty_files(root: Path, cap: int = 20) -> list[str]:
    """返回工作区中已修改/新增的文件（用于 bash 变更前的快照）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    files: list[str] = []
    for line in out.stdout.splitlines():
        if len(line) > 3:
            files.append(line[3:].strip().strip('"'))
        if len(files) >= cap:
            break
    return files


def workspace_fingerprint(root: Path, exts: set[str] | None = None) -> str:
    """工作区状态指纹，用于无进展检测。基于 (相对路径, 大小, mtime)。"""
    root = Path(root).resolve()
    h = hashlib.sha256()
    count = 0
    skip = {".git", ".vista", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if exts and p.suffix.lower() not in exts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{rel_to(p, root)}|{st.st_size}|{int(st.st_mtime_ns)}".encode())
            count += 1
            if count > 8000:
                break
    return h.hexdigest()[:16]
