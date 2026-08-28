"""VISTA 的异常体系与错误码表。

设计原则（不变式 I4）：工具执行过程中的任何异常都不向上抛出到主循环，
而是在 tools/registry.py 中被捕获并转换为 ToolResult(ok=False)，喂回模型。
只有"scaffold 自身无法继续"的错误才使用这里的异常。
"""

from __future__ import annotations


class VistaError(Exception):
    """所有 VISTA 异常的基类。"""


class ConfigError(VistaError):
    """配置文件或环境变量有误。"""


class PathEscape(VistaError):
    """试图访问工作区之外的路径。"""

    def __init__(self, path: str):
        super().__init__(f"路径越界：{path}")
        self.path = path


class BlockedCommand(VistaError):
    """命中危险命令拦截规则。"""

    def __init__(self, cmd: str, pattern: str):
        super().__init__(f"命令被安全策略禁止：{cmd}")
        self.cmd = cmd
        self.pattern = pattern


class PermissionDenied(VistaError):
    """用户拒绝了本次操作。"""


class RetryableError(VistaError):
    """可重试的模型接口错误（429 / 5xx / 超时 / 网络）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FatalLLMError(VistaError):
    """不可重试的模型接口错误（鉴权失败、请求格式错误等）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ContextOverflow(RetryableError):
    """上下文超过模型窗口。触发一次强制压缩后重试。"""


class UserAbort(VistaError):
    """用户主动中断（Ctrl-C）。"""


# ---------------------------------------------------------------------------
# 错误码表 —— 每个码对应一个默认的可执行建议
# ---------------------------------------------------------------------------
ERROR_HINTS: dict[str, str] = {
    "OK": "",
    "FILE_NOT_FOUND": "请先用 grep 或 repo_map 确认文件路径是否正确。",
    "NOT_READ": "在编辑一个文件之前，必须先用 read_file 读取它。",
    "STALE_CONTEXT": "文件内容已变化，请重新 read_file 后再构造 old_str。",
    "NO_MATCH": "old_str 未匹配。注意不要包含行号前缀，并检查缩进与空白字符。",
    "AMBIGUOUS": "old_str 匹配到多处。请扩大 old_str 使其唯一，或设置 replace_all=true。",
    "BINARY_FILE": "这是二进制文件，无法以文本方式读取。",
    "FILE_TOO_LARGE": "文件过大，请用 offset/limit 参数分段读取。",
    "PATH_ESCAPE": "只能操作工作区内的文件。",
    "TIMEOUT": "命令超时被终止。请缩小命令范围，或提高 timeout 参数。",
    "NONZERO_EXIT": "",
    "BLOCKED_COMMAND": "该命令被安全策略禁止，请换一种做法。",
    "PERMISSION_DENIED": "用户拒绝了该操作，请换一个方案或询问用户。",
    "NO_RESULTS": "没有命中。请放宽 pattern，或换一个目录范围。",
    "MEMORY_NOT_FOUND": "没有找到对应的记忆条目。",
    "NO_INTERACTIVE": "当前是非交互模式，无人可问。请自行决策，并在最终 summary 中说明你做出的假设。",
    "BAD_ARGS": "参数不合法，请检查工具签名后重试。",
    "TOOL_ERROR": "工具执行时发生内部错误。",
    "UNKNOWN_TOOL": "不存在这个工具。请只使用工具列表中给出的工具。",
    "WRITE_FAILED": "写入失败，请检查路径与权限。",
}


def hint_for(code: str) -> str | None:
    return ERROR_HINTS.get(code) or None
