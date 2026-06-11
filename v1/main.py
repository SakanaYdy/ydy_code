"""
Agent v1
最简单的版本，直接在主循环里处理工具调用，没有权限控制和钩子机制
"""

import os, subprocess
from pathlib import Path
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

WORKDIR = Path.cwd()
client = Anthropic()
MODEL = os.environ["MODEL_ID"]

system_prompt = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."

from tools import TOOLS, TOOL_HANDLERS

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=system_prompt, messages=messages,
            tools=TOOLS, max_tokens=8000
        )
        # 将模型返回的 content blocks 追加到 messages
        messages.append({"role": "assistant", "content": response.content})
        # 检查是否有 tool_use 类型的 content block
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if tool_use_blocks:
            # 依次调用工具，结果以 tool_result 追加到 messages
            tool_results = []
            for block in tool_use_blocks:
                # 工具调用之前 进行权限检查
                tool_name = block.name
                tool_input = block.input
                tool_handler = TOOL_HANDLERS.get(tool_name)
                
                from permission import check_permission
                if not check_permission(tool_name, tool_input):
                    tool_output = f"Error: Permission denied for tool {tool_name} with input {tool_input}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    })
                    continue
                
                if tool_handler:
                    tool_output = tool_handler(**tool_input)
                else:
                    tool_output = f"Error: No handler for tool {tool_name}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_output,
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            # 没有工具调用，提取文本返回
            return "".join(b.text for b in response.content if b.type == "text")
        

# 用户主循环
if __name__ == "__main__":
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
