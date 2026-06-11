"""
skill_loader 逻辑
将技能加载和管理的逻辑放在这里，方便后续维护和扩展
"""
from config import WORKDIR, SKILLS_DIR

SKILL_REGISTRY: dict[str, dict] = {}


def build_system() -> str:
    """Build SYSTEM prompt with skill catalog injected at startup."""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


def get_skill(skill_name):
    return SKILL_REGISTRY.get(skill_name)


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
    if not SKILLS_DIR.exists() or not SKILLS_DIR.is_dir():
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


def list_skills():
    _scan_skills()
    return [(name, info["description"]) for name, info in SKILL_REGISTRY.items()]


if __name__ == "__main__":
    print(list_skills())
