"""
任务系统模块 — 持久化任务图（DAG）+ 后台异步执行

整合 s12_task_system 与 s13_background_tasks 的功能：
  s12: 文件持久化的任务依赖图（blockedBy DAG）
  s13: 后台线程执行慢操作 + 通知注入

核心设计：
  - Task 数据类（id, subject, description, status, owner, blockedBy）
  - .tasks/ 目录存储 JSON 文件，支持跨会话恢复
  - can_start() 检查依赖是否全部完成
  - claim_task / complete_task 驱动状态机 pending → in_progress → completed
  - 完成后自动报告解锁的下游任务
  - 后台任务通过 daemon 线程执行，完成后注入 <task_notification>
  - 模型可通过 run_in_background=true 显式请求后台执行
"""

import json
import time
import random
import threading
from pathlib import Path
from dataclasses import dataclass, asdict

from config import WORKDIR
from log import log_event

# ═══════════════════════════════════════════════════════════
#  Task DAG（s12）— 文件持久化的任务依赖图
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    """任务数据结构。blockedBy 形成有向无环图（DAG）。"""
    id: str
    subject: str
    description: str
    status: str           # pending | in_progress | completed
    owner: str | None     # Agent 名称（多 Agent 协作场景）
    blockedBy: list[str]  # 依赖的任务 ID 列表


def _task_path(task_id: str) -> Path:
    """获取任务 JSON 文件路径。"""
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    """创建新任务并持久化到 .tasks/ 目录。"""
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    log_event("TASK", "create", id=task.id, subject=subject,
              blockedBy=blockedBy or [])
    return task


def save_task(task: Task):
    """将任务写入 JSON 文件。"""
    _task_path(task.id).write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False),
        encoding="utf-8")


def load_task(task_id: str) -> Task:
    """从 JSON 文件加载任务。"""
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


def list_tasks() -> list[Task]:
    """列出所有任务（按 ID 排序）。"""
    return [Task(**json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task(task_id: str) -> str:
    """返回任务详情的 JSON 字符串。"""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


def can_start(task_id: str) -> bool:
    """检查 blockedBy 依赖是否全部完成。缺失的依赖视为阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领任务：pending → in_progress。需要依赖全部完成。"""
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists()
                or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    log_event("TASK", "claim", id=task_id, subject=task.subject, owner=owner)
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成任务：in_progress → completed。自动报告解锁的下游任务。"""
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    # 扫描下游：哪些 pending 任务现在可以开始了
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    log_event("TASK", "complete", id=task_id, subject=task.subject,
              unblocked=unblocked)
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


# ── Task 工具包装 ──

def _run_create_task(subject: str, description: str = "",
                     blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    return f"Created {task.id}: {task.subject}{deps}"


def _run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def _run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def _run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def _run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ═══════════════════════════════════════════════════════════
#  Background Tasks（s13）— 后台线程异步执行
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}    # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()

# 慢操作关键词（fallback 启发式判断）
SLOW_KEYWORDS = [
    "install", "build", "test", "deploy", "compile",
    "docker build", "pip install", "npm install",
    "cargo build", "pytest", "make", "webpack",
]


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """启发式判断：命令是否可能耗时 > 30s。仅作为 fallback。"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    return any(kw in cmd for kw in SLOW_KEYWORDS)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断是否应后台执行。模型显式请求优先，否则 fallback 到启发式。"""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def start_background_task(block, handler) -> str:
    """将工具调用分发到 daemon 线程。返回后台任务 ID。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        try:
            result = handler(block.input)
        except Exception as e:
            result = f"Error: {type(e).__name__}: {e}"
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    log_event("BACKGROUND", "dispatched", bg_id=bg_id, command=cmd[:60])
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成的后台任务，返回 <task_notification> 格式的通知列表。"""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        log_event("BACKGROUND", "completed", bg_id=bg_id,
                  command=task["command"][:40], output_chars=len(output))
    return notifications


# ═══════════════════════════════════════════════════════════
#  导出：工具定义 & Handler 映射
# ═══════════════════════════════════════════════════════════

TASK_TOOLS = [
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string",
                                      "description": "Task title"},
                          "description": {"type": "string",
                                          "description": "Detailed description"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"},
                                        "description": "Task IDs this depends on"}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress. "
                    "Requires all blockedBy dependencies to be completed.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]

TASK_TOOL_HANDLERS = {
    "create_task": _run_create_task,
    "list_tasks": lambda: _run_list_tasks(),
    "get_task": _run_get_task,
    "claim_task": _run_claim_task,
    "complete_task": _run_complete_task,
}
