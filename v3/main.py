"""
Agent v3版本
在v2的基础上加入subAgent模式
主Agent负责整体流程控制和技能管理,子Agent负责具体技能的执行。
"""
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from config import MODEL, MAX_TOKENS, PROMPT_NAME
from llm import client
from hook import trigger_hook
from skill import build_system
from tools import TOOLS, TOOL_HANDLERS
from subagent import spawn_subagent

# 注册 spawn_subagent 工具（放在 tools 模块加载完成之后，避免循环导入）
TOOLS.append({"name": "spawn_subagent", "description": "Spawn a sub-agent to complete a subtask. Input is a text description of the subtask."})
TOOL_HANDLERS["spawn_subagent"] = spawn_subagent

system_prompt = build_system()
print(f"\033[90m[SYSTEM PROMPT]\n{system_prompt}\n\033[0m")
print(f"\033[90m[TOOLS] {[t['name'] for t in TOOLS]}\033[0m")


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=system_prompt, messages=messages,
            tools=TOOLS, max_tokens=MAX_TOKENS,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hook("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            blocked = trigger_hook("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(block.input) if handler else f"No handler for tool {block.name}"
            trigger_hook("PostToolUse", block, output)

            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input(f"\033[36m{PROMPT_NAME} >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hook("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
