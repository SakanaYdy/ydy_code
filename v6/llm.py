"""
统一 LLM 客户端
全局唯一的 Anthropic client 实例
"""
import os
from anthropic import Anthropic

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

base_url = os.getenv("ANTHROPIC_BASE_URL")
client = Anthropic(base_url=base_url)
# LLM 初始化日志由 main.py 启动时通过 log_event 统一输出
