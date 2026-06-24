"""
错误恢复模块 — 三条恢复路径 + 指数退避

Path 1: max_tokens → 升级 token 上限（8K→64K），再截断则注入 continuation prompt（最多 3 次）
Path 2: prompt_too_long → 应急压缩（reactive compact）→ 重试一次
Path 3: 429/529 → 指数退避 + 抖动（最多 10 次），连续 529 达阈值切换 fallback 模型

ASCII flow:
  messages -> prompt assembly -> compress+load -> [try] LLM [except] -> tools -> loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? -> compact
                                              escalate /    429/529? -> backoff
                                              continue      other? -> log + exit
"""
import time
import random

from config import MAX_TOKENS
from log import log_event

# ── 常量 ──────────────────────────────────────────────────

ESCALATED_MAX_TOKENS = 64000
MAX_RECOVERY_RETRIES = 3   # continuation prompt 最大次数
MAX_RETRIES = 10           # 429/529 最大重试次数
BASE_DELAY_MS = 500        # 退避基数（毫秒）
MAX_DELAY_MS = 32000       # 退避上限（毫秒）
MAX_CONSECUTIVE_529 = 3    # 连续 529 达此数切换 fallback

CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)


# ═══════════════════════════════════════════════════════════
#  RecoveryState — 跟踪整个 loop 生命周期内的恢复状态
# ═══════════════════════════════════════════════════════════

class RecoveryState:
    """跟踪恢复尝试的状态机。每次 agent_loop 调用创建一个新实例。"""

    def __init__(self, primary_model: str, fallback_model: str = None):
        self.has_escalated = False              # Path 1: 是否已升级 max_tokens
        self.recovery_count = 0                 # Path 1: continuation 次数
        self.consecutive_529 = 0                # Path 3: 连续 529 计数
        self.has_attempted_reactive_compact = False  # Path 2: 是否已尝试应急压缩
        self.current_model = primary_model      # Path 3: 当前使用的模型
        self.primary_model = primary_model
        self.fallback_model = fallback_model

    def reset_529_counter(self):
        """成功调用后重置连续 529 计数。"""
        self.consecutive_529 = 0


# ═══════════════════════════════════════════════════════════
#  退避策略
# ═══════════════════════════════════════════════════════════

def retry_delay(attempt: int, retry_after: float = None) -> float:
    """指数退避 + 抖动。Retry-After 头优先。

    公式: min(500 * 2^attempt, 32000) / 1000 + random(0, 25%)
    """
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), MAX_DELAY_MS) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


# ═══════════════════════════════════════════════════════════
#  with_retry — 429/529 瞬态错误的指数退避重试
# ═══════════════════════════════════════════════════════════

def with_retry(fn, state: RecoveryState):
    """包装 LLM 调用，对 429/529 做指数退避重试。

    非瞬态异常直接抛出，由外层 try/except 处理。
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.reset_529_counter()
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # 429 rate limit → 指数退避
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                log_event("RECOVERY", "429_rate_limit",
                          retry=f"{attempt+1}/{MAX_RETRIES}",
                          wait=f"{delay:.1f}s")
                time.sleep(delay)
                continue

            # 529 overloaded → 指数退避 + 可能切换 fallback 模型
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if state.fallback_model:
                        state.current_model = state.fallback_model
                        state.consecutive_529 = 0
                        log_event("RECOVERY", "529_fallback_switch",
                                  model=state.current_model,
                                  reason=f"consecutive_529 >= {MAX_CONSECUTIVE_529}")
                    else:
                        state.consecutive_529 = 0
                        log_event("RECOVERY", "529_no_fallback",
                                  reason="FALLBACK_MODEL_ID not configured")
                delay = retry_delay(attempt)
                log_event("RECOVERY", "529_overloaded",
                          retry=f"{attempt+1}/{MAX_RETRIES}",
                          wait=f"{delay:.1f}s",
                          consecutive=state.consecutive_529)
                time.sleep(delay)
                continue

            # 非瞬态 → 抛出给外层处理
            raise

    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded for transient errors")


# ═══════════════════════════════════════════════════════════
#  错误类型判断
# ═══════════════════════════════════════════════════════════

def is_prompt_too_long_error(e: Exception) -> bool:
    """判断 API 错误是否为 prompt/context 过长。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


# ═══════════════════════════════════════════════════════════
#  Path 2: 应急压缩（reactive compact）
# ═══════════════════════════════════════════════════════════

def reactive_compact(messages: list) -> list:
    """应急压缩 — 比 s08 的 auto compact 更激进。

    使用 compact.py 的 emergency_compact（LLM 摘要 + 保留最近 5 条），
    如果 emergency_compact 本身也失败，则回退到简单的尾部保留。
    """
    from compact import emergency_compact
    try:
        log_event("RECOVERY", "reactive_compact", method="llm_summary")
        return emergency_compact(messages)
    except Exception:
        # LLM 摘要也失败了，回退到尾部保留
        log_event("RECOVERY", "reactive_compact", method="tail_fallback",
                  warning="LLM summary failed, keeping last 5 messages")
        tail = messages[-5:]
        return [{"role": "user",
                 "content": "[Reactive compact] Earlier conversation trimmed. "
                            "Continue from where you left off."}, *tail]
