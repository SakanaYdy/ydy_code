"""
统一配置中心
所有模块从这里导入配置，避免重复定义
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# ── 路径 ──────────────────────────────────────────────
WORKDIR = Path.cwd()
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", WORKDIR / "skill"))
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", WORKDIR / "memory"))
TOOL_RESULT_DIR = Path(os.environ.get("TOOL_RESULT_DIR", WORKDIR / "tool_result"))

# ── 模型 ──────────────────────────────────────────────
MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")  # 529 连续超载时的备选模型
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8000"))
SUB_MAX_TOKENS = int(os.environ.get("SUB_MAX_TOKENS", "4000"))
SUB_MAX_TURNS = int(os.environ.get("SUB_MAX_TURNS", "30"))

# ── 显示 ──────────────────────────────────────────────
PROMPT_NAME = os.environ.get("PROMPT_NAME", "s03")

# 配置日志由 main.py 启动时通过 log_event 统一输出
