""""
skill_loader 逻辑
将技能加载和管理的逻辑放在这里，方便后续维护和扩展
"""
from pathlib import Path

WORKDIR = Path.cwd()
SKILLS_DIR = Path(r'D:\RunminG-Lab\Claude code\claw-code\ydy\ydy-code\v2\skill')

SKILL_REGISTRY : dict[str:dict] = {}

    
def build_system() -> str:
    """Build SYSTEM prompt with skill catalog injected at startup."""
    catalog = list_skils()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


def get_skill(skill_name):
    # 根据技能名称获取技能的具体实现，可以从全局的技能列表或者字典中查找并返回对应的技能函数或者对象
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
    # 列出所有可用的技能，可以返回一个技能名称的列表或者一个包含技能详细信息的字典
    # 遍历技能目录，加载技能定义，并将技能注册到全局的技能列表或者字典中
    if not SKILLS_DIR.exists() and SKILLS_DIR.is_dir():
        return # 没有技能目录，直接返回
    
    for d in SKILLS_DIR.iterdir():
        if d.is_dir():
            skill_name = d.name
            skill_file = d / "SKILL.md"
            if skill_file.exists():
                raw = skill_file.read_text(encoding='utf-8')
                meta,body = _parse_frontmatter(raw)
                name = meta.get("name", skill_name)
                description = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
                SKILL_REGISTRY[skill_name] = {
                    "name": name,
                    "description": description,
                    "content": raw,
                }                

def list_skils():
    _scan_skills()
    skill_list = []
    for skill_name, info in SKILL_REGISTRY.items():
        # print(f"{skill_name}: {info['description']}")
        skill_list.append((skill_name, info['description']))
    return skill_list
        
        

if __name__ == "__main__":
    list_skils()
    # print(SKILL_REGISTRY)
    # print(get_skill("code_review"))
    
    
