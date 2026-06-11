"""
子Agent模块
负责执行主Agent分配的子任务，不支持嵌套派发
日志通过 log_event 统一输出
"""
import time

from config import WORKDIR, MODEL, SUB_MAX_TOKENS, SUB_MAX_TURNS
from llm import client
from hook import trigger_hook
from log import log_event


def extract_text(content) -> str:
    """Extract text from message content blocks."""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")


SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)


def spawn_subagent(description: str) -> str:
    """Spawn a sub-agent to complete a subtask."""
    # 延迟导入避免循环依赖（tools → subagent → tools）
    from tools import TOOL_HANDLERS

    log_event("SUBAGENT", "spawn", task=description[:120],
              model=MODEL, max_turns=SUB_MAX_TURNS, max_tokens=SUB_MAX_TOKENS)

    messages = [{"role": "user", "content": description}]
    total_tool_calls = 0
    spawn_t0 = time.time()

    for turn in range(SUB_MAX_TURNS):
        log_event("SUBAGENT", "turn", turn=turn + 1, max=SUB_MAX_TURNS, msgs=len(messages))

        t0 = time.time()
        response = client.messages.create(
            model=MODEL, system=SUB_SYSTEM,
            messages=messages,
            tools=[t for t in _sub_tools()],
            max_tokens=SUB_MAX_TOKENS,
        )
        api_elapsed = time.time() - t0
        messages.append({"role": "assistant", "content": response.content})

        log_event("SUBAGENT", "api_response", elapsed=f"{api_elapsed:.2f}s",
                  stop_reason=response.stop_reason)

        if response.stop_reason != "tool_use":
            log_event("SUBAGENT", "finished", reason="no more tool calls")
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # PreToolUse 会自动派发 OnToolStart 日志
            blocked = trigger_hook("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(block.input) if handler else f"No handler for tool {block.name}"
            trigger_hook("PostToolUse", block, output)
            total_tool_calls += 1
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})

    # 提取最终结果
    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after max turns without final answer."

    total_elapsed = time.time() - spawn_t0
    log_event("SUBAGENT", "done", elapsed=f"{total_elapsed:.2f}s",
              tool_calls=total_tool_calls, result_len=len(result))
    return result


def _sub_tools():
    """子Agent可用的工具列表（不含 spawn_subagent，防止嵌套）"""
    return [
        {"name": "bash", "description": "Run a shell command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file contents.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Write content to a file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Replace exact text in a file once.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        {"name": "glob", "description": "Find files matching a glob pattern.",
         "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    ]
