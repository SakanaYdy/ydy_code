"""
Agent v2版本
在v1版本的基础上，增加了技能管理和权限控制的功能。技能管理将技能的定义和加载逻辑抽象出来，方便后续维护和扩展。权限控制则在工具调用前进行检查，确保安全性。
同时加入了hook机制，将权限控制等逻辑从主循环中抽离出来，使agent_loop保持简洁，专注于流程控制。
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
from hook import trigger_hook
load_dotenv(override=True)

WORKDIR = Path.cwd()
client = Anthropic()
MODEL = os.environ["MODEL_ID"]

system_prompt = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."

from skill import build_system
system_prompt =  build_system()

print(f"\033[90m[SYSTEM PROMPT]\n{system_prompt}\n\033[0m")

from tools import TOOLS, TOOL_HANDLERS

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=system_prompt, messages=messages,
            tools=TOOLS, max_tokens=8000
        )
        # 将模型返回的 content blocks 追加到 messages
        messages.append({"role": "assistant", "content": response.content})
        
        # 不需要工具调用 直接返回答案
        if response.stop_reason != 'tool_use':
            force = trigger_hook("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return
        
        # 处理工具调用，生成工具结果，并将结果追加到 messages 中，供模型后续使用
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
        trigger_hook("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
