"""
system_prompt 动态组装模块

核心思想（参考 s10）：prompt 不是硬编码的字符串，而是由独立的 section 组成，
根据运行时的真实状态（工具是否注册、记忆文件是否存在）按需组装，并缓存避免重复拼接。

四个 section：
  identity   — 始终加载：角色定义 + 行为准则
  tools      — 始终加载：可用工具列表
  workspace  — 始终加载：工作目录
  memory     — 按需加载：当 .memory/MEMORY.md 存在且有内容时
"""
import json

from config import WORKDIR, SKILLS_DIR, MEMORY_DIR
from log import log_event

SKILL_REGISTRY: dict[str, dict] = {}

MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# ═══════════════════════════════════════════════════════════
#  Prompt Sections — 独立维护，互不影响
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": (
        "You are a coding agent. Act, don't explain.\n"
        "Use tools to accomplish tasks. Be concise and direct."
    ),
    "tools": "",  # 由 update_context 动态填充
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.\n"
              "Respect user preferences from memory.\n"
              "When the user says 'remember' or expresses a clear preference, "
              "extract it as a memory.",
}


# ═══════════════════════════════════════════════════════════
#  组装 + 缓存
# ═══════════════════════════════════════════════════════════

def assemble_system_prompt(context: dict) -> str:
    """根据当前 context 按需组装 system prompt。"""
    sections = []

    # 始终加载
    sections.append(PROMPT_SECTIONS["identity"])

    # tools section — 从 context 获取实际注册的工具
    enabled_tools = context.get("enabled_tools", [])
    if enabled_tools:
        sections.append(f"Available tools: {', '.join(enabled_tools)}.")
    else:
        sections.append(PROMPT_SECTIONS["tools"])

    sections.append(PROMPT_SECTIONS["workspace"])

    # 按需加载 — memory section 仅当 MEMORY.md 存在且有内容时
    memories = context.get("memories", "")
    if memories:
        sections.append(f"{PROMPT_SECTIONS['memory']}\n\nMemories available:\n{memories}")
    else:
        sections.append(PROMPT_SECTIONS["memory"])

    # skills section — 如果有技能
    skills = context.get("skills", "")
    if skills:
        sections.append(f"Skills available:\n{skills}")

    # MCP section — 如果有已连接的 MCP 服务器
    mcp_names = context.get("mcp_servers", "")
    if mcp_names:
        sections.append(f"Connected MCP servers: {mcp_names}")

    return "\n\n".join(sections)


_last_context_key = None
_last_prompt = None


def get_system_prompt(context: dict) -> str:
    """缓存包装 — context 不变时直接返回上次结果，避免重复拼接。

    用 json.dumps 做确定性序列化（不用 hash()，因为 Python hash 有进程随机化，
    且对嵌套 dict/list 会抛 unhashable type）。
    此缓存仅避免进程内的冗余字符串拼接，不同于 Claude Code 的 API 级 prompt cache。
    """
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        log_event("SYSTEM", "cache_hit")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    if context.get("skills"):
        loaded.append("skills")
    log_event("SYSTEM", "assembled", sections=loaded, chars=len(_last_prompt))
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  Context — 基于真实状态，不是关键词猜测
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list, tool_handlers: dict = None) -> dict:
    """从真实状态派生 context：哪些工具存在、记忆文件是否存在、技能列表、MCP 服务器。"""
    # 工具列表
    enabled_tools = []
    if tool_handlers:
        enabled_tools = list(tool_handlers.keys())

    # 记忆索引
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text(encoding="utf-8").strip()
        if content:
            memories = content

    # 技能目录
    skills = list_skills()
    skills_text = "\n".join(f"- {name}: {desc}" for name, desc in skills) if skills else ""

    # MCP 服务器（s19）
    from mcp_plugin import mcp_clients
    mcp_names = ", ".join(mcp_clients.keys()) if mcp_clients else ""

    return {
        "enabled_tools": enabled_tools,
        "workspace": str(WORKDIR),
        "memories": memories,
        "skills": skills_text,
        "mcp_servers": mcp_names,
    }


# ═══════════════════════════════════════════════════════════
#  技能加载（保留原有逻辑）
# ═══════════════════════════════════════════════════════════

def get_skill(skill_name):
    skill = SKILL_REGISTRY.get(skill_name)
    log_event("SKILL", "lookup", name=skill_name, found=skill is not None)
    return skill


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def _scan_skills():
    """遍历技能目录，加载技能定义到全局注册表"""
    log_event("SKILL", "scan_start", dir=str(SKILLS_DIR))

    if not SKILLS_DIR.exists() or not SKILLS_DIR.is_dir():
        log_event("SKILL", "scan_skip", reason="directory not found")
        return

    for d in SKILLS_DIR.iterdir():
        if d.is_dir():
            skill_name = d.name
            skill_file = d / "SKILL.md"
            if skill_file.exists():
                raw = skill_file.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(raw)
                name = meta.get("name", skill_name)
                description = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
                SKILL_REGISTRY[skill_name] = {
                    "name": name,
                    "description": description,
                    "content": raw,
                }
                log_event("SKILL", "loaded", name=skill_name, chars=len(raw))
            else:
                log_event("SKILL", "skipped", name=skill_name, reason="no SKILL.md")

    log_event("SKILL", "scan_done", total=len(SKILL_REGISTRY))


def list_skills():
    _scan_skills()
    return [(name, info["description"]) for name, info in SKILL_REGISTRY.items()]


if __name__ == "__main__":
    print(list_skills())
