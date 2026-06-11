"""
Agent v5版本
在v4版本基础上增加了memory模块、动态system_prompt、错误恢复机制。
至此 参考Claude的Agent设计的一个基础完整版本已经完成。

核心改动：
  s10: system_prompt 动态组装 + 缓存
  s11: 三条错误恢复路径 + 指数退避
       Path 1: max_tokens → 升级 token 上限 → continuation prompt
       Path 2: prompt_too_long → 应急压缩 → 重试
       Path 3: 429/529 → 指数退避 + fallback 模型
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

from config import MODEL, FALLBACK_MODEL, MAX_TOKENS, PROMPT_NAME, WORKDIR, SKILLS_DIR, SUB_MAX_TOKENS, SUB_MAX_TURNS
from llm import client, base_url
from hook import trigger_hook
from log import log_event
from skill import get_system_prompt, update_context
from tools import TOOLS, TOOL_HANDLERS
from subagent import spawn_subagent

from compact import tool_result_budget, snip_compact, micro_compact, compact_history, run_compact
from memory import load_memories, extract_memories, consolidate_memories
from recovery import (RecoveryState, with_retry, is_prompt_too_long_error,
                      reactive_compact, ESCALATED_MAX_TOKENS,
                      MAX_RECOVERY_RETRIES, CONTINUATION_PROMPT)

# ── 注册扩展工具（放在 tools 模块加载完成之后，避免循环导入）──
TOOLS.append({"name": "spawn_subagent", "description": "Spawn a sub-agent to complete a subtask. Input is a text description of the subtask."})
TOOL_HANDLERS["spawn_subagent"] = spawn_subagent
TOOL_HANDLERS["compact"] = run_compact

# ── 启动日志（统一由 log_event 输出）──
log_event("STARTUP", "config",
          workdir=str(WORKDIR),
          skills_dir=f"{SKILLS_DIR} (exists={SKILLS_DIR.exists()})",
          model=MODEL,
          fallback_model=FALLBACK_MODEL or "(none)",
          max_tokens=MAX_TOKENS,
          sub_max_tokens=SUB_MAX_TOKENS,
          sub_max_turns=SUB_MAX_TURNS,
          prompt_name=PROMPT_NAME)
log_event("STARTUP", "llm", base_url=base_url or "(default)")
log_event("STARTUP", "tools", registered=[t['name'] for t in TOOLS])

CONTEXT_LIMIT = 50000


def agent_loop(messages: list, context: dict):
    """主循环 — 动态 system prompt + 三条错误恢复路径。"""
    iteration = 0
    state = RecoveryState(primary_model=MODEL, fallback_model=FALLBACK_MODEL)
    max_tokens = MAX_TOKENS

    while True:
        iteration += 1
        log_event("ITERATION", "start", iteration=iteration, messages=len(messages))

        # ── 动态获取 system prompt（context 不变则命中缓存）──
        system = get_system_prompt(context)

        # ── 记忆加载（压缩前保存快照，避免细节丢失）──
        pre_compress = [msg.copy() for msg in messages]
        memory_content = load_memories(messages)

        # ── 上下文压缩（0 API 调用的预处理器）──
        messages[:] = tool_result_budget(messages)    # L3: 大结果落盘
        messages[:] = snip_compact(messages)          # L1: 裁中间
        messages[:] = micro_compact(messages)         # L2: 旧结果占位

        if len(str(messages)) > CONTEXT_LIMIT:
            messages[:] = compact_history(messages)

        # ── 注入相关记忆到最后一条用户消息 ──
        if memory_content and messages:
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    if isinstance(msg["content"], str):
                        msg["content"] = memory_content + "\n\n" + msg["content"]
                    elif isinstance(msg["content"], list):
                        msg["content"].insert(0, {"type": "text", "text": memory_content})
                    break

        # ── 调用 LLM（with_retry 处理 429/529，外层处理其余错误）──
        try:
            t0 = time.time()
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
            api_elapsed = time.time() - t0
        except Exception as e:
            # Path 2: prompt_too_long → 应急压缩 → 重试一次
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    log_event("RECOVERY", "prompt_too_long", action="reactive_compact_retry")
                    continue
                log_event("RECOVERY", "prompt_too_long", action="unrecoverable",
                          reason="still too long after compact")
                messages.append({"role": "assistant", "content": [
                    {"type": "text", "text": "[Error] Context too large, cannot continue."}]})
                return

            # 不可恢复的错误
            name = type(e).__name__
            log_event("RECOVERY", "unrecoverable", error=name, detail=str(e)[:200])
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ── Path 1: max_tokens → 升级或 continuation ──
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                # 首次：不追加截断输出，直接升级 token 上限重试
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                log_event("RECOVERY", "max_tokens_escalate",
                          from_=MAX_TOKENS, to=ESCALATED_MAX_TOKENS)
                continue

            # 64K 仍然截断：追加截断输出 + continuation prompt
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                log_event("RECOVERY", "max_tokens_continuation",
                          count=f"{state.recovery_count}/{MAX_RECOVERY_RETRIES}")
                continue

            log_event("RECOVERY", "max_tokens_limit_reached",
                      continuations=state.recovery_count)
            return

        # ── 正常完成：追加 assistant 响应 ──
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
                  model=state.current_model,
                  text_blocks=len(text_blocks),
                  tool_blocks=len(tool_blocks),
                  **token_info)

        # ── 非工具调用：agent 准备结束 ──
        if response.stop_reason != "tool_use":
            log_event("SESSION", "agent_finished", stop_reason=response.stop_reason)
            extract_memories(pre_compress)
            consolidate_memories()
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

        # ── 刷新 context（工具可能改变了状态）──
        context = update_context(context, messages, TOOL_HANDLERS)


if __name__ == "__main__":
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    context = update_context({}, [], TOOL_HANDLERS)
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
        agent_loop(history, context)
        context = update_context(context, history, TOOL_HANDLERS)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
