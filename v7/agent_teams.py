"""
Agent Teams 模块 — 多 Agent 协作 + 结构化协议

整合 s15_agent_teams 与 s16_team_protocols 的核心功能：
  s15: MessageBus（文件邮箱）+ spawn_teammate_thread（队友线程）+ inbox 注入
  s16: ProtocolState（请求-响应状态机）+ dispatch_message（消息路由）+ idle loop

核心设计：
  - MessageBus：.mailboxes/ 目录下的 .jsonl 文件邮箱，读取即消费
  - ProtocolState：request_id 关联的请求-响应状态机（pending → approved | rejected）
  - Lead 工具（6 个）：spawn_teammate / send_message / check_inbox
                        request_shutdown / request_plan / review_plan
  - Teammate 工具（5 个）：bash / read_file / write_file / send_message / submit_plan
  - Teammate 生命周期：idle loop（等待收件箱消息）替代固定轮次限制
  - 消息路由：dispatch_message 按 type 字段分发到对应 handler

ASCII 流程：
  Lead: LLM → spawn_teammate → 线程启动
                    ↓
  Teammate: inbox → dispatch → LLM → bash/read/write/send → idle loop
                    ↑                                      ↓
  Lead: consume_lead_inbox ← MessageBus ← send_message ←──┘
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
    """将响应关联到原始请求。验证 response_type 与 request type 匹配。
    防止重复解决（已 approved/rejected 的请求跳过）。"""
    state = pending_requests.get(request_id)
    if not state:
        log_event("PROTOCOL", "unknown_request", request_id=request_id)
        return
    # 类型验证：shutdown_response 不能误批准 plan_approval
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
#  统一 Lead 收件箱消费（s16）— 协议路由 + 消息返回
# ═══════════════════════════════════════════════════════════

def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """读取 Lead 收件箱。路由协议响应到 match_response，返回所有消息。
    check_inbox 工具和主循环都调用此函数，避免消息被消费但协议状态未更新。"""
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
#  Teammate 线程（s15 + s16 idle loop）
# ═══════════════════════════════════════════════════════════

# 追踪活跃的队友
active_teammates: dict[str, bool] = {}

# Teammate 专用系统提示
TEAMMATE_SYSTEM = (
    "You are '{name}', a {role}. "
    "Use tools to complete tasks. "
    "Check inbox for protocol messages (shutdown_request, etc). "
    "Send results via send_message to 'lead'."
)

# Teammate 工具定义（精简版，无 spawn_subagent / task 工具）
TEAMMATE_TOOLS = [
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
]


def _handle_inbox_message(name: str, msg: dict, messages: list) -> bool:
    """队友的收件箱消息路由（s16 dispatch_message）。
    按 type 字段分发到对应 handler。返回 True 表示应停止循环。"""
    msg_type = msg.get("type", "message")
    meta = msg.get("metadata", {})
    req_id = meta.get("request_id", "")

    if msg_type == "shutdown_request":
        # 优雅关闭：发送确认响应，返回 True 停止循环
        BUS.send(name, "lead", "Shutting down gracefully.",
                 "shutdown_response",
                 {"request_id": req_id, "approve": True})
        log_event("PROTOCOL", "shutdown_approved",
                  teammate=name, request_id=req_id)
        return True

    if msg_type == "plan_approval_response":
        # 计划审批结果：注入到队友的消息历史
        approve = meta.get("approve", False)
        if approve:
            messages.append({"role": "user",
                             "content": "[Plan approved] Proceed with the task."})
        else:
            messages.append({"role": "user",
                             "content": f"[Plan rejected] Feedback: {msg['content']}"})
        return False

    return False


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友向 Lead 提交计划等待审批。
    注意：这是协议级请求，不是代码级拦截。
    提交后线程继续运行——真实 CC 中模型会等待审批响应后再行动。"""
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
    """在后台线程中启动队友 agent。
    使用 idle loop：LLM 轮次结束后等待收件箱消息（shutdown_request → 退出，
    新消息 → 恢复 LLM 轮次），而非固定轮次限制。"""
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = TEAMMATE_SYSTEM.format(name=name, role=role)

    def run():
        from tools import run_bash, run_read, run_write

        messages = [{"role": "user", "content": prompt}]
        sub_handlers = {
            "bash": run_bash,
            "read_file": run_read,
            "write_file": run_write,
            "send_message": lambda to, content: (
                BUS.send(name, to, content), "Sent")[1],
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
        }

        shutdown_requested = False
        while not shutdown_requested:
            # 检查收件箱中的协议消息
            inbox = BUS.read_inbox(name)
            should_stop = False
            non_protocol = []
            for msg in inbox:
                if msg.get("type") in ("shutdown_request",
                                        "plan_approval_response"):
                    should_stop = _handle_inbox_message(name, msg, messages)
                    if should_stop:
                        break
                else:
                    non_protocol.append(msg)
            if should_stop:
                shutdown_requested = True
                break
            if non_protocol:
                inbox_json = json.dumps(non_protocol, ensure_ascii=False)
                messages.append({"role": "user",
                                 "content": f"<inbox>{inbox_json}</inbox>"})

            # LLM 轮次
            try:
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=TEAMMATE_TOOLS, max_tokens=SUB_MAX_TOKENS)
            except Exception as e:
                log_event("TEAM", "teammate_error", name=name,
                          error=str(e)[:100])
                break

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                # Idle loop：等待收件箱消息，而非退出
                # 真实 CC 在此发送 idle_notification 给 Lead
                non_protocol = []
                while not shutdown_requested:
                    time.sleep(1)
                    inbox = BUS.read_inbox(name)
                    if not inbox:
                        continue
                    for msg in inbox:
                        if msg.get("type") in ("shutdown_request",
                                                "plan_approval_response"):
                            should_stop = _handle_inbox_message(
                                name, msg, messages)
                            if should_stop:
                                shutdown_requested = True
                                break
                        else:
                            non_protocol.append(msg)
                    if shutdown_requested:
                        break
                    if non_protocol:
                        inbox_json = json.dumps(non_protocol,
                                                ensure_ascii=False)
                        messages.append({"role": "user",
                            "content": f"<inbox>{inbox_json}</inbox>"})
                        non_protocol = []
                        break  # 回到 LLM 轮次处理新消息

            # 执行工具调用
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
    return f"Teammate '{name}' spawned as {role}"


# ═══════════════════════════════════════════════════════════
#  Lead 工具 Handler
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """启动一个队友 agent。"""
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    """Lead 向队友发送消息。"""
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    """检查 Lead 收件箱。自动路由协议响应。"""
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
    """请求队友优雅关闭（s16 协议）。"""
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
    """Lead 要求队友提交计划（s16 协议）。"""
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    log_event("PROTOCOL", "plan_requested", teammate=teammate, task=task[:60])
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    """Lead 审批计划（s16 协议）。"""
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
     "description": "Spawn a teammate agent in a background thread. "
                    "Each teammate has its own simplified tool set "
                    "(bash, read, write, send_message, submit_plan).",
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
