"""
hook 逻辑
将权限认证、日志等逻辑放在这里，方便后续维护和扩展

控制流 hook（返回值可影响执行）：
  UserPromptSubmit: 用户提交输入时触发
  PreToolUse:       agent使用工具前触发（返回非 None 可阻断）
  PostToolUse:      agent使用工具后触发
  Stop:             agent停止时触发（返回非 None 可强制继续）

日志 hook（纯观察，返回值忽略）：
  OnToolStart:      工具执行前自动触发（由 PreToolUse 自动派发）
  OnToolEnd:        工具执行后自动触发（由 PostToolUse 自动派发）
"""
import time

from config import WORKDIR
from log import log_event

# ── hook 注册表 ────────────────────────────────────────
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
    # 日志专用 hook（由 trigger_hook 自动派发）
    "OnToolStart": [],
    "OnToolEnd": [],
}

# 工具执行计时（PreToolUse 记录，PostToolUse 读取）
_tool_start_times: dict[str, float] = {}


def register_hook(hook_name, func):
    if hook_name in HOOKS:
        HOOKS[hook_name].append(func)
    else:
        raise ValueError(f"Invalid hook name: {hook_name}")


def trigger_hook(hook_name, *args, **kwargs):
    """
    触发 hook，返回第一个非 None 的结果（用于阻断判断）。
    PreToolUse / PostToolUse 会自动派发对应的 On* 日志 hook。
    """
    if hook_name not in HOOKS:
        raise ValueError(f"Invalid hook name: {hook_name}")

    # PreToolUse 自动派发 OnToolStart
    if hook_name == "PreToolUse" and args:
        block = args[0]
        _tool_start_times[block.id] = time.time()
        for func in HOOKS["OnToolStart"]:
            func(block)

    # 执行控制流 hook
    handlers = HOOKS[hook_name]
    for func in handlers:
        result = func(*args, **kwargs)
        if result is not None:
            return result

    # PostToolUse 自动派发 OnToolEnd
    if hook_name == "PostToolUse" and len(args) >= 2:
        block, output = args[0], args[1]
        elapsed = time.time() - _tool_start_times.pop(block.id, time.time())
        for func in HOOKS["OnToolEnd"]:
            func(block, output, elapsed)

    return None


# ═══════════════════════════════════════════════════════════
#  控制流 hook
# ═══════════════════════════════════════════════════════════

# ── 权限 hook ─────────────────────────────────────────
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def permission_hook(block):
    """PreToolUse: 检查工具调用权限"""
    if block.name == "bash":
        cmd = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in cmd:
                log_event("TOOL_BLOCKED", "blocked", tool=block.name, reason=f"deny list: '{pattern}'")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in cmd:
                log_event("TOOL_BLOCKED", "warning", tool=block.name, cmd=cmd[:80])
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            log_event("TOOL_BLOCKED", "warning", tool=block.name, reason="writing outside workspace", path=path)
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    size = len(str(output))
    if size > 100000:
        log_event("TOOL", "warning", tool=block.name, msg=f"large output: {size} chars")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit: log user input before it reaches the LLM."""
    log_event("SESSION", "prompt", workdir=str(WORKDIR))
    return None


def summary_hook(messages: list):
    """Stop: print summary when loop is about to exit."""
    tool_count = sum(
        1 for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    log_event("SESSION", "stop", tool_calls=tool_count)
    return None


# ═══════════════════════════════════════════════════════════
#  日志 hook（纯观察，由 trigger_hook 自动派发）
# ═══════════════════════════════════════════════════════════

def tool_start_hook(block):
    """OnToolStart: 自动记录工具开始执行。"""
    args_preview = str(list(block.input.values())[:2])[:60]
    log_event("TOOL", "start", name=block.name, id=block.id[:12], args=args_preview)


def tool_end_hook(block, output, elapsed):
    """OnToolEnd: 自动记录工具执行结果。"""
    output_str = str(output)
    # bash 命令提取 exit code
    rc = None
    if block.name == "bash" and "Error:" not in output_str:
        rc = "(ok)"
    log_event("TOOL", "end", name=block.name, elapsed=f"{elapsed:.2f}s",
              output=f"{len(output_str)} chars", rc=rc or "")


# ═══════════════════════════════════════════════════════════
#  注册 hook
# ═══════════════════════════════════════════════════════════

# 控制流 hook
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)

# 日志 hook（自动派发）
register_hook("OnToolStart", tool_start_hook)
register_hook("OnToolEnd", tool_end_hook)
