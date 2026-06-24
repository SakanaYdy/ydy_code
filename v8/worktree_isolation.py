"""
Worktree Isolation 模块 — git worktree 任务隔离

整合 s18_worktree_isolation 的核心功能：
  - 任务绑定独立 git worktree（独立工作目录 + 独立分支）
  - 队友在 worktree 目录中执行文件操作，避免并行冲突
  - 安全删除（检查未提交文件 / 未推送提交）

核心设计：
  - .worktrees/ 目录存储 worktree 实例
  - 每个 worktree 对应一个 wt/<name> 分支
  - Task.worktree 字段关联任务与 worktree
  - 队友 claim_task 时自动切换 cwd 到 worktree 目录
  - 事件日志记录在 .worktrees/events.jsonl

目录拓扑：
  Main repo (/)
    ├── .worktrees/auth/  (branch: wt/auth)  ← Task #1
    ├── .worktrees/ui/    (branch: wt/ui)    ← Task #2
    ├── .tasks/task_xxx.json (worktree: "auth")
    └── .worktrees/events.jsonl
"""

import re
import json
import time
import subprocess
from pathlib import Path

from config import WORKDIR
from log import log_event

# ═══════════════════════════════════════════════════════════
#  Worktree 配置
# ═══════════════════════════════════════════════════════════

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str) -> str | None:
    """验证 worktree 名称。返回错误信息或 None（有效）。"""
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


# ═══════════════════════════════════════════════════════════
#  Git 操作
# ═══════════════════════════════════════════════════════════

def run_git(args: list[str]) -> tuple[bool, str]:
    """执行 git 命令。返回 (ok, output)。"""
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def _log_worktree_event(event_type: str, worktree_name: str,
                         task_id: str = ""):
    """记录 worktree 生命周期事件到 events.jsonl。"""
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ═══════════════════════════════════════════════════════════
#  Worktree 操作
# ═══════════════════════════════════════════════════════════

def create_worktree(name: str, task_id: str = "") -> str:
    """创建 git worktree 并绑定到独立分支。可选绑定到任务。"""
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path),
                          "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    _log_worktree_event("create", name, task_id)
    log_event("WORKTREE", "create", name=name, path=str(path),
              task_id=task_id or "(none)")
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    """将 worktree 名称写入任务的 worktree 字段。不改变任务状态。"""
    from task_system import load_task, save_task
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
    log_event("WORKTREE", "bind", task_id=task_id, worktree=worktree_name)


def _count_worktree_changes(path: Path) -> tuple[int, int]:
    """统计 worktree 中的未提交文件数和未推送提交数。"""
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True,
                            timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True,
                            timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    """删除 worktree。有未提交更改时拒绝，除非 discard_changes=True。"""
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return (f"Cannot verify worktree '{name}' status. "
                    "Use discard_changes=true to force removal.")
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} uncommitted file(s) "
                    f"and {commits} unpushed commit(s). "
                    "Use discard_changes=true to force removal, "
                    "or keep_worktree to preserve for review.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree directory for '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    _log_worktree_event("remove", name)
    log_event("WORKTREE", "remove", name=name)
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    """保留 worktree 供手动审查。分支保留不删。"""
    err = validate_worktree_name(name)
    if err:
        return err
    _log_worktree_event("keep", name)
    log_event("WORKTREE", "keep", name=name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


# ═══════════════════════════════════════════════════════════
#  Lead 工具 Handler
# ═══════════════════════════════════════════════════════════

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)


def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)


def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)


# ═══════════════════════════════════════════════════════════
#  导出：工具定义 & Handler 映射
# ═══════════════════════════════════════════════════════════

WORKTREE_TOOLS = [
    {"name": "create_worktree",
     "description": "Create an isolated git worktree with its own branch. "
                    "Optionally bind to a task.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string",
                                   "description": "Worktree name (alphanumeric, dots, dashes, underscores)"},
                          "task_id": {"type": "string",
                                      "description": "Optional task ID to bind"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if uncommitted changes "
                    "unless discard_changes=true.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"},
                          "discard_changes": {"type": "boolean",
                                              "description": "Force removal even with changes"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review. Branch preserved.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"}},
                      "required": ["name"]}},
]

WORKTREE_TOOL_HANDLERS = {
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
}
