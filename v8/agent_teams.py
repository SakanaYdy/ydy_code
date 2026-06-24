"""
Agent Teams 模块 — 多 Agent 协作 + 自主 Agent + 结构化协议

整合 s15（消息总线）+ s16（结构化协议）+ s17（自主 Agent）+ s18（worktree cwd）：
  s15: MessageBus（文件邮箱）+ spawn_teammate_thread（队友线程）+ inbox 注入
  s16: ProtocolState（请求-响应状态机）+ dispatch_message（消息路由）
  s17: 自主 Agent（idle_poll + auto-claim + WORK/IDLE 生命周期 + 身份重注入）
  s18: worktree cwd（队友在绑定的 worktree 目录中执行文件操作）

核心设计：
  - MessageBus：.mailboxes/ 目录下的 .jsonl 文件邮箱，读取即消费
  - ProtocolState：request_id 关联的请求-响应状态机（pending → approved | rejected）
  - 自主 Agent：WORK → IDLE 循环，IDLE 期间轮询收件箱 + 自动认领任务
  - Teammate 工具（8 个）：bash / read_file / write_file / send_message / submit_plan
                           list_tasks / claim_task / complete_task
  - worktree cwd：claim_task 时自动切换到 worktree 目录

队友生命周期：
  WORK: inbox → LLM → tools → (tool_use? loop) → (done? → IDLE)
  IDLE: 5s poll → inbox? → WORK / unclaimed? → claim → WORK / 60s? → SHUTDOWN
"""

import json
import time
import random
import threading
from pathlib import Path
from dataclasses import dataclass, field

from config import WORKDIR, MODEL, SUB_MAX_TOKENS
from llm import client
from log import log_event
from task_system import (load_task, save_task, list_tasks, claim_task,
                         complete_task, scan_unclaimed_tasks, can_start)

# ═══════════════════════════════════════════════════════════
#  MessageBus（s15）— 文件邮箱消息总线
# ═══════════════════════════════════════════════════════════

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """基于文件的消息总线。每个 agent 有一个 .jsonl 收件箱。
    读取即消费：read_text + unlink（教学版本，真实 CC 使用 proper-lockfile）。"""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        """发送消息到目标 agent 的收件箱。"""
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        log_event("TEAM", "send", from_=from_agent, to=to_agent,
                  type=msg_type, preview=content[:60])

    def read_inbox(self, agent: str) -> list[dict]:
        """读取 agent 的收件箱（消费式：读后删除）。"""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in
                inbox.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()

# ═══════════════════════════════════════════════════════════
#  Protocol State（s16）— 请求-响应状态机
# ═══════════════════════════════════════════════════════════


@dataclass
class ProtocolState:
    """协议请求的状态记录。request_id 关联请求与响应。"""
    request_id: str       # e.g. "req_004281"
    type: str             # "shutdown" | "plan_approval"
    sender: str
    target: str
    status: str           # pending | approved | rejected
    payload: str          # plan 文本或 shutdown 原因
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    """生成唯一的请求 ID。"""
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """将响应关联到原始请求。验证 response_type 与 request type 匹配。"""
    state = pending_requests.get(request_id)
    if not state:
        log_event("PROTOCOL", "unknown_request", request_id=request_id)
        return
    if state.type == "shutdown" and response_type != "shutdown_response":
        log_event("PROTOCOL", "type_mismatch",
                  expected="shutdown_response", got=response_type)
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        log_event("PROTOCOL", "type_mismatch",
                  expected="plan_approval_response", got=response_type)
        return
    if state.status != "pending":
        log_event("PROTOCOL", "duplicate", request_id=request_id,
                  current_status=state.status)
        return
    state.status = "approved" if approve else "rejected"
    log_event("PROTOCOL", "resolved", request_id=request_id,
              type=state.type, status=state.status)


# ═══════════════════════════════════════════════════════════
#  统一 Lead 收件箱消费（s16）
# ═══════════════════════════════════════════════════════════

def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """读取 Lead 收件箱。路由协议响应到 match_response，返回所有消息。"""
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return []
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                approve = meta.get("approve", False)
                match_response(msg_type, req_id, approve)
    return msgs


# ═══════════════════════════════════════════════════════════
#  自主 Agent（s17）— idle_poll + auto-claim
# ═══════════════════════════════════════════════════════════

IDLE_POLL_INTERVAL = 5   # 秒
IDLE_TIMEOUT = 60         # 秒


def idle_poll(agent_name: str, messages: list,
              name: str, role: str) -> str:
    """IDLE 阶段轮询：每 5s 检查收件箱 + 任务看板，最多 60s。
    返回 'work'（有新任务）、'shutdown'（收到关闭请求）或 'timeout'。"""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # 检查收件箱 — 优先处理协议消息
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down gracefully.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    log_event("PROTOCOL", "shutdown_in_idle",
                              teammate=name, request_id=req_id)
                    return "shutdown"

            # 非协议消息：注入并恢复工作
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox,
                    ensure_ascii=False) + "</inbox>"})
            log_event("TEAM", "idle_inbox", name=name)
            return "work"

        # 扫描任务看板 — 自动认领
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                if task_data.get("worktree"):
                    from worktree_isolation import WORKTREES_DIR
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                log_event("TEAM", "auto_claimed", name=name,
                          task=task_data["subject"])
                return "work"
            log_event("TEAM", "claim_failed", name=name, result=result[:80])

    log_event("TEAM", "idle_timeout", name=name, seconds=IDLE_TIMEOUT)
    return "timeout"


# ═══════════════════════════════════════════════════════════
#  Teammate 线程（s15 + s16 + s17 + s18）
# ═══════════════════════════════════════════════════════════

active_teammates: dict[str, bool] = {}

# Teammate 系统提示（s17 增加任务看板描述）
TEAMMATE_SYSTEM = (
    "You are '{name}', a {role}. "
    "Use tools to complete tasks. "
    "You can list and claim tasks from the board. "
    "If a task has a worktree, work in that directory. "
    "Check inbox for protocol messages."
)


def _handle_inbox_message(name: str, msg: dict, messages: list) -> bool:
    """队友的收件箱消息路由。返回 True 表示应停止循环。"""
    msg_type = msg.get("type", "message")
    meta = msg.get("metadata", {})
    req_id = meta.get("request_id", "")

    if msg_type == "shutdown_request":
        BUS.send(name, "lead", "Shutting down gracefully.",
                 "shutdown_response",
                 {"request_id": req_id, "approve": True})
        log_event("PROTOCOL", "shutdown_approved",
                  teammate=name, request_id=req_id)
        return True

    if msg_type == "plan_approval_response":
        approve = meta.get("approve", False)
        if approve:
            messages.append({"role": "user",
                             "content": "[Plan approved] Proceed with the task."})
        else:
            messages.append({"role": "user",
                             "content": f"[Plan rejected] Feedback: {msg['content']}"})
    return False


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友向 Lead 提交计划等待审批。"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    log_event("PROTOCOL", "plan_submitted",
              teammate=from_name, request_id=req_id)
    return f"Plan submitted ({req_id}). Waiting for approval..."


def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """启动自主队友 agent。
    WORK → IDLE 循环：工作阶段最多 10 轮 LLM 调用，
    完成后进入 IDLE 轮询（60s），有新任务则恢复工作，超时则退出。"""
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = TEAMMATE_SYSTEM.format(name=name, role=role)

    def run():
        from tools import run_bash, run_read, run_write

        # worktree cwd 上下文（s18）
        wt_ctx = {"path": None}

        def _wt_cwd() -> Path | None:
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            return run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str, limit: int = None) -> str:
            return run_read(path, limit, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                if task.worktree:
                    from worktree_isolation import WORKTREES_DIR
                    wt_ctx["path"] = str(WORKTREES_DIR / task.worktree)
                else:
                    wt_ctx["path"] = None
            return result

        def _run_complete_task(task_id: str):
            result = complete_task(task_id)
            wt_ctx["path"] = None  # 完成后重置 cwd
            return result

        messages = [{"role": "user", "content": prompt}]
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            # s17: 队友可以直接操作任务看板
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
            "send_message": lambda to, content: (
                BUS.send(name, to, content), "Sent")[1],
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        # 外层循环：WORK → IDLE（s17）
        while True:
            # 身份重注入（上下文压缩后提醒 agent 身份）
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})

            # WORK 阶段：最多 10 轮 LLM 调用
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = _handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": "<inbox>" + json.dumps(non_protocol,
                                ensure_ascii=False) + "</inbox>"})

                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=SUB_MAX_TOKENS)
                except Exception as e:
                    log_event("TEAM", "teammate_error", name=name,
                              error=str(e)[:100])
                    break
                messages.append({"role": "assistant",
                                 "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = sub_handlers.get(block.name)
                        try:
                            output = handler(**block.input) if handler else \
                                f"Unknown tool: {block.name}"
                        except Exception as e:
                            output = f"Error: {type(e).__name__}: {e}"
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                if results:
                    messages.append({"role": "user", "content": results})

            if should_shutdown:
                break

            # IDLE 阶段（s17）：轮询收件箱 + 任务看板
            idle_result = idle_poll(name, messages, name, role)
            if idle_result in ("shutdown", "timeout"):
                break

        # 发送最终摘要给 Lead
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        log_event("TEAM", "teammate_finished", name=name)

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    log_event("TEAM", "teammate_spawned", name=name, role=role)
    return f"Teammate '{name}' spawned as {role} (autonomous)"


# ═══════════════════════════════════════════════════════════
#  Lead 工具 Handler
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)


def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    log_event("PROTOCOL", "shutdown_request",
              teammate=teammate, request_id=req_id)
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    log_event("PROTOCOL", "plan_requested", teammate=teammate, task=task[:60])
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    log_event("PROTOCOL", "plan_reviewed", request_id=request_id,
              status=state.status)
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"


# ═══════════════════════════════════════════════════════════
#  导出：工具定义 & Handler 映射
# ═══════════════════════════════════════════════════════════

TEAM_TOOLS = [
    {"name": "spawn_teammate",
     "description": "Spawn an autonomous teammate agent. "
                    "Teammates work in WORK/IDLE cycles, auto-claim tasks, "
                    "and have 8 tools (bash, read, write, send_message, "
                    "submit_plan, list_tasks, claim_task, complete_task).",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string",
                                   "description": "Unique teammate name"},
                          "role": {"type": "string",
                                   "description": "Role description"},
                          "prompt": {"type": "string",
                                     "description": "Initial task prompt"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send a message to a teammate via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {
                          "to": {"type": "string"},
                          "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check Lead's inbox for teammate messages. "
                    "Routes protocol responses automatically.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down gracefully "
                    "via protocol handshake.",
     "input_schema": {"type": "object",
                      "properties": {
                          "teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan for review.",
     "input_schema": {"type": "object",
                      "properties": {
                          "teammate": {"type": "string"},
                          "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan by request_id.",
     "input_schema": {"type": "object",
                      "properties": {
                          "request_id": {"type": "string"},
                          "approve": {"type": "boolean"},
                          "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
]

TEAM_TOOL_HANDLERS = {
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
}
