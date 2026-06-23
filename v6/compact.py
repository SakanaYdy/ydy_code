""""
上下文压缩管线
1. toolResultBudget（工具结果预算控制）
2. snipCompact（消息数量裁剪）
3. microCompact（旧工具结果压缩）
4. 自动压缩决策：预处理不够时触发，1 次 API 调用
5. 应急兜底：API 报错时触发，强制裁剪保可用
"""
import time
import json
from pathlib import Path

from log import log_event
from llm import client, MODEL

TOOL_RESULT_DIR = Path("./tool_results")
TRANSCRIPT_DIR = Path("./transcripts")


def tool_result_budget(messages: list, budget: int = 2048):
    """L3: 将超过阈值的工具结果写入磁盘，消息中保留占位符。"""
    truncated = 0
    for message in messages:
        if message['role'] == 'user' and isinstance(message.get('content'), list):
            for block in message['content']:
                if getattr(block, 'type', None) == 'tool_result':
                    content_length = len(str(block.content)) if hasattr(block, 'content') else 0
                    if content_length > budget:
                        tool_result_id = getattr(block, 'id', 'unknown_id')
                        TOOL_RESULT_DIR.mkdir(parents=True, exist_ok=True)
                        file_path = TOOL_RESULT_DIR / f"{tool_result_id}.txt"
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(str(block.content))
                        block.content = block.content[:2000] + f"\n[工具结果过大，已保存到 {file_path}，原内容长度={content_length} chars]"
                        truncated += 1
    if truncated:
        log_event("COMPACT", "budget", truncated=truncated)


def snip_compact(messages: list, max_messages: int = 20):
    """L1: 消息数量裁剪，保留头尾，丢弃中间。"""
    before = len(messages)
    if before <= max_messages:
        return messages

    m, n = max_messages // 2, max_messages - max_messages // 2
    result = messages[:m] + messages[-n:]
    log_event("COMPACT", "snip", before=before, after=len(result), head=m, tail=n)
    return result


def micro_compact(messages: list, max_tool_results: int = 5):
    """L2: 旧工具结果压缩，只保留最新的 max_tool_results 个。"""
    tool_results = []
    for message in messages:
        if message["role"] == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if getattr(block, "type", None) == "tool_result":
                    tool_results.append(block)

    if len(tool_results) <= max_tool_results:
        return messages

    tool_results_to_compress = tool_results[:-max_tool_results]
    compressed_count = 0
    total_saved_chars = 0
    for block in tool_results_to_compress:
        original_size = len(str(block.content)) if hasattr(block, 'content') else 0
        block.content = "[工具结果被压缩]"
        total_saved_chars += original_size
        compressed_count += 1

    log_event("COMPACT", "micro", compressed=compressed_count,
              saved=f"{total_saved_chars} chars", kept=max_tool_results)
    return messages


def write_transcript(messages):
    """将对话记录写入磁盘。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages):
    """使用一次 LLM 调用生成对话摘要。"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = client.messages.create(model=MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"


def compact_history(messages):
    """LLM 摘要压缩：写 transcript + 生成摘要。"""
    transcript_path = write_transcript(messages)
    log_event("COMPACT", "llm_summary", transcript=str(transcript_path))
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def emergency_compact(messages: list):
    """应急压缩：API 报错时强制裁剪，保留最近 5 条消息 + 摘要。"""
    log_event("COMPACT", "emergency", msg_count=len(messages))
    write_transcript(messages)
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]


def run_compact(messages: list, MAX_TOKENS: int = 2000):
    """整个压缩流程（作为 compact 工具的 handler）。"""
    tool_result_budget(messages)
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if len(str(messages)) > MAX_TOKENS:
        messages[:] = compact_history(messages)
