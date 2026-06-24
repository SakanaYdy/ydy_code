"""
统一日志模块
所有模块通过 log_event() 输出日志，由 hook 系统自动触发
不再在业务代码中直接 print()

颜色方案：
  grey   (90) — 普通信息
  yellow (33) — 警告
  red    (31) — 错误
  magenta(35) — 子 agent
  cyan   (36) — 用户交互
"""

# ANSI 颜色码
_GREY = "\033[90m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"
_RESET = "\033[0m"

# category -> 默认颜色
_CATEGORY_COLORS = {
    "CONFIG": _GREY,
    "LLM": _GREY,
    "STARTUP": _GREY,
    "ITERATION": _GREY,
    "API": _GREY,
    "TOOL": _GREY,
    "TOOL_BLOCKED": _YELLOW,
    "COMPACT": _GREY,
    "COMPACT_WARN": _YELLOW,
    "SUBAGENT": _MAGENTA,
    "SESSION": _CYAN,
    "HOOK": _GREY,
    "ERROR": _RED,
    "TASK": _CYAN,
    "BACKGROUND": _YELLOW,
    "TEAM": _MAGENTA,
    "PROTOCOL": _YELLOW,
}

# event -> 覆盖颜色（优先于 category 颜色）
_EVENT_COLORS = {
    "blocked": _RED,
    "error": _RED,
    "timeout": _RED,
    "warning": _YELLOW,
    "emergency": _YELLOW,
}


def log_event(category: str, event: str, **data):
    """
    统一日志入口。

    用法:
        log_event("TOOL", "start", name="bash", id="xxx")
        log_event("API", "response", elapsed=1.23, stop_reason="tool_use")
        log_event("COMPACT", "snip", before=30, after=20)
    """
    color = _EVENT_COLORS.get(event, _CATEGORY_COLORS.get(category, _GREY))

    # 构建 key=value 部分
    parts = []
    for k, v in data.items():
        val = str(v)
        if len(val) > 120:
            val = val[:117] + "..."
        parts.append(f"{k}={val}")
    detail = ", ".join(parts)

    # 格式: [CATEGORY] event: detail
    line = f"[{category}] {event}"
    if detail:
        line += f": {detail}"

    print(f"{color}{line}{_RESET}")
