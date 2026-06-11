"""
hook 逻辑
将权限认证等逻辑放在这里，方便后续维护和扩展

目前定义四种状态:
  UserPromptSubmit: 用户提交输入时触发
  PreToolUse:       agent使用工具前触发
  PostToolUse:      agent使用工具后触发
  Stop:             agent停止时触发
"""
from config import WORKDIR

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(hook_name, func):
    if hook_name in HOOKS:
        HOOKS[hook_name].append(func)
    else:
        raise ValueError(f"Invalid hook name: {hook_name}")


def trigger_hook(hook_name, *args, **kwargs):
    """触发 hook，返回第一个非 None 的结果（用于阻断判断）"""
    if hook_name not in HOOKS:
        raise ValueError(f"Invalid hook name: {hook_name}")
    for func in HOOKS[hook_name]:
        result = func(*args, **kwargs)
        if result is not None:
            return result
    return None


# ── 权限 hook ─────────────────────────────────────────
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def permission_hook(block):
    """PreToolUse: 检查工具调用权限"""
    if block.name == "bash":
        cmd = block.input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in cmd:
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in cmd:
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


# ── 其它 hook ─────────────────────────────────────────

def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit: log user input before it reaches the LLM."""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop: print summary when loop is about to exit."""
    tool_count = sum(
        1 for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# ── 注册 hook ─────────────────────────────────────────
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
