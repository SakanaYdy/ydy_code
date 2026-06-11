"""
Agent v4版本
在v3的基础上加入一些稳定agent的功能，例如：上下文压缩，todo列表管理。
日志统一通过 hook 系统自动触发，业务代码使用 log_event() 输出。
"""
import time

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from config import MODEL, MAX_TOKENS, PROMPT_NAME, WORKDIR, SKILLS_DIR, SUB_MAX_TOKENS, SUB_MAX_TURNS
from llm import client, base_url
from hook import trigger_hook
from log import log_event
from skill import build_system
from tools import TOOLS, TOOL_HANDLERS
from subagent import spawn_subagent

from compact import tool_result_budget, snip_compact, micro_compact, compact_history, run_compact

# ── 注册扩展工具（放在 tools 模块加载完成之后，避免循环导入）──
TOOLS.append({"name": "spawn_subagent", "description": "Spawn a sub-agent to complete a subtask. Input is a text description of the subtask."})
TOOL_HANDLERS["spawn_subagent"] = spawn_subagent
TOOL_HANDLERS["compact"] = run_compact

# ── 启动日志（统一由 log_event 输出）──
log_event("STARTUP", "config",
          workdir=str(WORKDIR),
          skills_dir=f"{SKILLS_DIR} (exists={SKILLS_DIR.exists()})",
          model=MODEL,
          max_tokens=MAX_TOKENS,
          sub_max_tokens=SUB_MAX_TOKENS,
          sub_max_turns=SUB_MAX_TURNS,
          prompt_name=PROMPT_NAME)
log_event("STARTUP", "llm", base_url=base_url or "(default)")
log_event("STARTUP", "tools", registered=[t['name'] for t in TOOLS])

system_prompt = build_system()
log_event("STARTUP", "system_prompt", chars=len(system_prompt))

CONTEXT_LIMIT = 50000


def agent_loop(messages: list):
    iteration = 0
    while True:
        iteration += 1
        log_event("ITERATION", "start", iteration=iteration, messages=len(messages))

        # ── 上下文压缩（0 API 调用的预处理器）──
        # 顺序：budget 先跑，确保大内容落盘后再做占位和裁剪
        messages[:] = tool_result_budget(messages)    # L3: 大结果落盘
        messages[:] = snip_compact(messages)          # L1: 裁中间
        messages[:] = micro_compact(messages)         # L2: 旧结果占位

        # 还不够？LLM 摘要（1 API 调用）
        if len(str(messages)) > CONTEXT_LIMIT:
            messages[:] = compact_history(messages)

        # ── 调用 LLM ──
        t0 = time.time()
        response = client.messages.create(
            model=MODEL, system=system_prompt, messages=messages,
            tools=TOOLS, max_tokens=MAX_TOKENS,
        )
        api_elapsed = time.time() - t0
        messages.append({"role": "assistant", "content": response.content})

        # ── 响应摘要日志 ──
        text_blocks = [b for b in response.content if getattr(b, "type", None) == "text"]
        tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        token_info = {}
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            token_info = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
        log_event("API", "response",
                  elapsed=f"{api_elapsed:.2f}s",
                  stop_reason=response.stop_reason,
                  text_blocks=len(text_blocks),
                  tool_blocks=len(tool_blocks),
                  **token_info)

        # ── 非工具调用：agent 准备结束 ──
        if response.stop_reason != "tool_use":
            log_event("SESSION", "agent_finished", stop_reason=response.stop_reason)
            force = trigger_hook("Stop", messages)
            if force:
                log_event("SESSION", "force_continue", reason=str(force)[:80])
                messages.append({"role": "user", "content": force})
                continue
            return

        # ── 处理工具调用 ──
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # PreToolUse 会自动派发 OnToolStart 日志；permission_hook 已记录阻断信息
            blocked = trigger_hook("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            if not handler:
                log_event("ERROR", "no_handler", tool=block.name)
            output = handler(block.input) if handler else f"No handler for tool {block.name}"
            trigger_hook("PostToolUse", block, output)

            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output})
        messages.append({"role": "user", "content": results})
        log_event("ITERATION", "end", iteration=iteration, tool_results=len(results))


if __name__ == "__main__":
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    round_num = 0
    while True:
        try:
            query = input(f"\033[36m{PROMPT_NAME} >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            log_event("SESSION", "end", reason="user interrupt")
            break
        if query.strip().lower() in ("q", "exit", ""):
            log_event("SESSION", "end", reason="user quit")
            break
        round_num += 1
        log_event("SESSION", "round_start", round=round_num, input=query[:100])
        trigger_hook("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
