"""
记忆模块 — 跨会话持久化知识

存储格式：.memory/ 目录下，每条记忆一个 .md 文件（YAML frontmatter + markdown body），
加上 MEMORY.md 索引文件（每行一条链接，注入 SYSTEM prompt，可被 prompt cache 缓存）。

四个子系统：
  1. 存储：写入 / 读取 / 索引
  2. 加载：索引始终在 SYSTEM prompt；相关记忆按需注入（LLM 选取 + 关键词兜底）
  3. 提取：每轮结束后从原始对话中提取新记忆
  4. 整理：记忆文件达到阈值时，LLM 去重合并（Dream）

四种记忆类型：
  user      — 用户偏好（"用 tab 不用空格"）
  feedback  — 工作指导（"不要 mock 数据库"）
  project   — 项目事实（"Auth 重写是合规驱动的"）
  reference — 外部指针（"Pipeline bug 在 Linear INGEST"）
"""
import json
import re
import time

from config import MEMORY_DIR, MODEL
from llm import client
from log import log_event

MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
CONSOLIDATE_THRESHOLD = 10  # 记忆文件数达到此阈值时触发整理


# ═══════════════════════════════════════════════════════════
#  存储层：读写记忆文件 + 索引
# ═══════════════════════════════════════════════════════════

def _extract_text(content) -> str:
    """从 Anthropic 响应 content 块中提取纯文本。"""
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (metadata_dict, body_text)。"""
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


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """写入单条记忆文件（Markdown + YAML frontmatter），并重建索引。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    _rebuild_index()
    log_event("MEMORY", "write", name=name, type=mem_type, file=filename)
    return filepath


def _rebuild_index():
    """扫描所有记忆文件，重建 MEMORY.md 索引。"""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text(
        "\n".join(lines) + "\n" if lines else "",
        encoding="utf-8",
    )


def read_memory_index() -> str:
    """读取 MEMORY.md 索引内容（注入 SYSTEM prompt）。"""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text(encoding="utf-8").strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """读取单条记忆文件的完整内容。"""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_memory_files() -> list[dict]:
    """列出所有记忆文件及其元数据。"""
    result = []
    if not MEMORY_DIR.exists():
        return result
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


# ═══════════════════════════════════════════════════════════
#  加载层：按相关性选取记忆，注入上下文
# ═══════════════════════════════════════════════════════════

def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """根据最近对话选取相关记忆文件名。

    策略：LLM side-query 选取（轻量，1 次 API 调用），
    失败时降级为关键词匹配 name + description。
    """
    files = list_memory_files()
    if not files:
        return []

    # 收集最近 3 条用户消息作为上下文
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(getattr(b, "text", ""))
                    for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # 构建目录供 LLM 选择
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = _extract_text(response.content).strip()
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            if selected:
                log_event("MEMORY", "select", method="llm", count=len(selected))
                return selected
    except Exception as e:
        log_event("MEMORY", "select_fallback", reason=str(e)[:80])

    # 兜底：关键词匹配
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    log_event("MEMORY", "select", method="keyword", count=len(selected))
    return selected


def load_memories(messages: list) -> str:
    """加载相关记忆内容，包装为 <relevant_memories> 标签，准备注入上下文。"""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════
#  提取层：每轮结束后从对话中提取新记忆
# ═══════════════════════════════════════════════════════════

def extract_memories(messages: list):
    """从最近对话中提取新的用户偏好、约束或项目事实。

    在 agent_loop 中，当 stop_reason != "tool_use" 时调用（自然对话断点）。
    使用压缩前的原始消息，避免细节被压缩丢失。
    """
    # 收集最近 10 条消息
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", ""))
                for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # 已有记忆描述（避免重复）
    existing = list_memory_files()
    existing_desc = (
        "\n".join(f"- {m['name']}: {m['description']}" for m in existing)
        if existing
        else "(none)"
    )

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        text = _extract_text(response.content).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return

        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            log_event("MEMORY", "extracted", count=count)
    except Exception as e:
        log_event("MEMORY", "extract_error", error=str(e)[:80])


# ═══════════════════════════════════════════════════════════
#  整理层：记忆文件过多时去重合并（Dream）
# ═══════════════════════════════════════════════════════════

def consolidate_memories():
    """合并重复/过时的记忆。当记忆文件数 ≥ CONSOLIDATE_THRESHOLD 时触发。"""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        text = _extract_text(response.content).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # 删除旧文件（保留 MEMORY.md）
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        log_event("MEMORY", "consolidated", before=len(files), after=len(items))
    except Exception as e:
        log_event("MEMORY", "consolidate_error", error=str(e)[:80])


