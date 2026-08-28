"""源码符号抽取。

RepoMap 需要两样东西：每个文件"定义了哪些符号"和"引用了哪些符号"。

优先使用 tree-sitter（若环境中安装了 tree_sitter_language_pack）；
否则退化到按语言编写的正则抽取器。正则版本在签名精度上不如 AST，
但对 PageRank 排序而言足够——排序只依赖符号名与引用计数，
不依赖精确的语法树结构。

这个"可降级"的设计是刻意的：VISTA 承诺零必需依赖，
tree-sitter 装不上时功能只是变弱，不会不可用。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
LANG_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".hh": "cpp", ".cxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "shell", ".bash": "shell",
}

SOURCE_EXTS = set(LANG_BY_EXT)

KEYWORDS: set[str] = {
    # 跨语言的通用关键字与烂大街的名字，作为引用统计的停用词
    "if", "else", "elif", "for", "while", "return", "break", "continue", "pass",
    "def", "class", "import", "from", "as", "with", "try", "except", "finally",
    "raise", "yield", "lambda", "global", "nonlocal", "assert", "del", "in", "is",
    "not", "and", "or", "none", "true", "false", "self", "cls", "super", "print",
    "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "type",
    "range", "enumerate", "zip", "map", "filter", "sorted", "sum", "min", "max",
    "open", "format", "isinstance", "hasattr", "getattr", "setattr", "repr",
    "function", "var", "let", "const", "new", "this", "typeof", "instanceof",
    "async", "await", "export", "default", "extends", "implements", "interface",
    "public", "private", "protected", "static", "void", "null", "undefined",
    "console", "log", "require", "module", "exports", "package", "func", "struct",
    "impl", "trait", "enum", "match", "fn", "mut", "pub", "use", "mod", "crate",
    "string", "number", "object", "array", "error", "err", "ok", "value", "data",
    "result", "args", "kwargs", "params", "options", "config", "name", "key",
    "get", "set", "run", "main", "init", "test", "add", "remove", "update",
}


@dataclass
class Definition:
    name: str
    kind: str
    line: int
    signature: str


@dataclass
class FileTags:
    path: str
    lang: str = ""
    defs: list[Definition] = field(default_factory=list)
    refs: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------------
# 正则抽取器
# ---------------------------------------------------------------------------
_DEF_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", "function"),
        (r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=", "const"),
    ],
    "javascript": [
        (r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", "function"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)", "function"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z][\w$]*)\s*=", "const"),
    ],
    "go": [
        (r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", "function"),
        (r"^type\s+([A-Za-z_]\w*)", "type"),
        (r"^(?:var|const)\s+([A-Za-z_]\w*)", "const"),
    ],
    "rust": [
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)", "struct"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_]\w*)", "enum"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)", "trait"),
        (r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)", "impl"),
    ],
    "java": [
        (r"^\s*(?:public|private|protected)?\s*(?:abstract\s+|final\s+)?(?:class|interface|enum|record)\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],\s.?]+\s+([A-Za-z_]\w*)\s*\(", "method"),
    ],
    "c": [
        (r"^\s*(?:typedef\s+)?struct\s+([A-Za-z_]\w*)", "struct"),
        (r"^[A-Za-z_][\w\s*]*\s+\*?([A-Za-z_]\w*)\s*\([^;]*\)\s*\{", "function"),
        (r"^\s*#define\s+([A-Z_][A-Z0-9_]*)", "macro"),
    ],
    "ruby": [
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*module\s+([A-Za-z_]\w*)", "module"),
        (r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)", "method"),
    ],
    "php": [
        (r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)", "class"),
        (r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)", "function"),
    ],
    "shell": [
        (r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*\{", "function"),
    ],
}
_DEF_PATTERNS["typescript"] = _DEF_PATTERNS["javascript"] + [
    (r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", "type"),
    (r"^\s*(?:public|private|protected)?\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|\s]+)?\s*\{", "method"),
]
_DEF_PATTERNS["cpp"] = _DEF_PATTERNS["c"] + [
    (r"^\s*(?:template\s*<[^>]*>\s*)?class\s+([A-Za-z_]\w*)", "class"),
    (r"^\s*namespace\s+([A-Za-z_]\w*)", "namespace"),
]
_DEF_PATTERNS["csharp"] = _DEF_PATTERNS["java"]
_DEF_PATTERNS["kotlin"] = [
    (r"^\s*(?:data\s+|sealed\s+|open\s+)?class\s+([A-Za-z_]\w*)", "class"),
    (r"^\s*(?:suspend\s+)?fun\s+([A-Za-z_]\w*)", "function"),
]
_DEF_PATTERNS["swift"] = [
    (r"^\s*(?:public\s+|private\s+)?(?:final\s+)?(?:class|struct|enum|protocol)\s+([A-Za-z_]\w*)", "type"),
    (r"^\s*(?:public\s+|private\s+)?func\s+([A-Za-z_]\w*)", "function"),
]
_DEF_PATTERNS["scala"] = [
    (r"^\s*(?:case\s+)?(?:class|object|trait)\s+([A-Za-z_]\w*)", "type"),
    (r"^\s*def\s+([A-Za-z_]\w*)", "function"),
]
_DEF_PATTERNS["lua"] = [
    (r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)", "function"),
]

_COMPILED_DEFS = {
    lang: [(re.compile(p), kind) for p, kind in pats] for lang, pats in _DEF_PATTERNS.items()
}

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_COMMENT_PREFIX = ("#", "//", "*", "/*", "--", '"""', "'''")


def lang_of(path: str | Path) -> str:
    return LANG_BY_EXT.get(Path(path).suffix.lower(), "")


def _extract_defs_regex(lang: str, lines: list[str]) -> list[Definition]:
    pats = _COMPILED_DEFS.get(lang)
    if not pats:
        return []
    out: list[Definition] = []
    seen: set[tuple[str, int]] = set()
    for i, raw in enumerate(lines, 1):
        if len(raw) > 400:
            continue
        stripped = raw.lstrip()
        if stripped.startswith(_COMMENT_PREFIX):
            continue
        for rx, kind in pats:
            m = rx.match(raw)
            if not m:
                continue
            name = m.group(1)
            if not name or (name, i) in seen:
                continue
            seen.add((name, i))
            sig = raw.strip()
            if len(sig) > 100:
                sig = sig[:99] + "…"
            out.append(Definition(name=name, kind=kind, line=i, signature=sig))
            break
    return out


def _extract_refs(lines: list[str], own: set[str]) -> Counter:
    refs: Counter = Counter()
    for raw in lines:
        if len(raw) > 600:
            raw = raw[:600]
        stripped = raw.lstrip()
        if stripped.startswith(_COMMENT_PREFIX):
            continue
        for name in _IDENT_RE.findall(raw):
            low = name.lower()
            if low in KEYWORDS or name in own:
                continue
            refs[name] += 1
    return refs


# ---------------------------------------------------------------------------
# tree-sitter（可选增强）
# ---------------------------------------------------------------------------
_TS_STATE: dict[str, object] = {"tried": False, "get_parser": None}


def _tree_sitter_parser(lang: str):
    if not _TS_STATE["tried"]:
        _TS_STATE["tried"] = True
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore

            _TS_STATE["get_parser"] = get_parser
        except Exception:
            try:
                from tree_sitter_languages import get_parser  # type: ignore

                _TS_STATE["get_parser"] = get_parser
            except Exception:
                _TS_STATE["get_parser"] = None
    getter = _TS_STATE["get_parser"]
    if getter is None:
        return None
    try:
        return getter(lang)  # type: ignore[operator]
    except Exception:
        return None


_TS_DEF_NODES = {
    "function_definition", "function_declaration", "method_definition",
    "class_definition", "class_declaration", "interface_declaration",
    "type_alias_declaration", "struct_item", "enum_item", "trait_item",
    "function_item", "method_declaration", "type_declaration", "type_spec",
    "module", "impl_item",
}


def _extract_defs_treesitter(lang: str, source: str, lines: list[str]) -> list[Definition] | None:
    parser = _tree_sitter_parser(lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(source.encode("utf-8", "replace"))
    except Exception:
        return None

    out: list[Definition] = []

    def walk(node) -> None:
        if node.type in _TS_DEF_NODES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                try:
                    name = source.encode("utf-8", "replace")[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", "replace")
                except Exception:
                    name = ""
                if name:
                    line = node.start_point[0] + 1
                    sig = lines[line - 1].strip() if 0 < line <= len(lines) else name
                    out.append(
                        Definition(
                            name=name,
                            kind=node.type.replace("_definition", "").replace("_declaration", ""),
                            line=line,
                            signature=sig[:100],
                        )
                    )
        for child in node.children:
            walk(child)

    try:
        walk(tree.root_node)
    except RecursionError:
        return None
    return out


def using_tree_sitter() -> bool:
    return _TS_STATE.get("get_parser") is not None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def extract_tags(path: str, source: str, use_tree_sitter: bool = True) -> FileTags:
    lang = lang_of(path)
    tags = FileTags(path=path, lang=lang)
    if not lang:
        return tags
    lines = source.split("\n")

    defs: list[Definition] | None = None
    if use_tree_sitter:
        defs = _extract_defs_treesitter(lang, source, lines)
    if defs is None:
        defs = _extract_defs_regex(lang, lines)
    tags.defs = defs

    own = {d.name for d in defs}
    tags.refs = _extract_refs(lines, own)
    return tags


def quick_symbols(path: str, lines: list[str], cap: int = 8) -> list[str]:
    """给 anchor digest 用的轻量符号名抽取（永远走正则，避免开销）。"""
    lang = lang_of(path)
    if not lang:
        return []
    return [d.name for d in _extract_defs_regex(lang, lines)][:cap]
