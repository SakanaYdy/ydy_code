"""
MCP Plugin 模块 — Model Context Protocol 插件系统

整合 s19_mcp_plugin 的核心功能：
  - MCPClient：发现并调用 MCP 服务器上的工具
  - normalize_mcp_name：名称规范化（防止注入）
  - assemble_tool_pool：合并内置工具 + MCP 工具
  - connect_mcp：连接 MCP 服务器，发现工具
  - 工具命名：mcp__{server}__{tool}
  - MCP 工具携带 readOnly / destructive 注释

核心设计：
  - MCPClient 封装单个 MCP 服务器的工具发现和调用
  - connect_mcp() 连接服务器并将工具注册到全局 mcp_clients
  - assemble_tool_pool() 每次调用合并 BUILTIN + MCP 工具
  - agent_loop 中 connect_mcp 后立即重建工具池，新工具即时可用
"""

import re
from log import log_event

# ═══════════════════════════════════════════════════════════
#  MCPClient — MCP 服务器客户端
# ═══════════════════════════════════════════════════════════


class MCPClient:
    """发现并调用 MCP 服务器上的工具（教学版本使用 mock handler）。
    真实 CC 中通过 stdio/SSE 与 MCP 服务器通信。"""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, callable]):
        """注册工具定义和 handler（模拟 tools/list）。"""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """调用工具（模拟 tools/call）。"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


# 全局 MCP 客户端注册表
mcp_clients: dict[str, MCPClient] = {}

# 名称规范化：只保留 [a-zA-Z0-9_-]
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """将非 [a-zA-Z0-9_-] 字符替换为下划线。"""
    return _DISALLOWED_CHARS.sub('_', name)


# ═══════════════════════════════════════════════════════════
#  Mock MCP 服务器（教学演示）
# ═══════════════════════════════════════════════════════════

def _mock_server_docs() -> MCPClient:
    """文档搜索服务器（readOnly）。"""
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search",
             "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version",
             "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy() -> MCPClient:
    """部署服务器（destructive）。"""
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. "
                           "(destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status",
             "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


# 可用的 Mock 服务器工厂
MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


# ═══════════════════════════════════════════════════════════
#  连接 & 工具池组装
# ═══════════════════════════════════════════════════════════

def connect_mcp(name: str) -> str:
    """连接到 MCP 服务器，发现工具。"""
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Unknown server '{name}'. Available: {available}"
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    tool_names = [t["name"] for t in mcp_client.tools]
    log_event("MCP", "connected", server=name, tools=tool_names)
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


def assemble_tool_pool(builtin_tools: list[dict],
                       builtin_handlers: dict) -> tuple[list[dict], dict]:
    """合并内置工具 + 所有 MCP 工具为统一工具池。
    MCP 工具命名为 mcp__{normalized_server}__{normalized_tool}。"""
    tools = list(builtin_tools)
    handlers = dict(builtin_handlers)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
            # 使用默认参数捕获避免闭包陷阱
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw:
                    c.call_tool(t, kw))
    return tools, handlers


# ═══════════════════════════════════════════════════════════
#  Lead 工具 Handler
# ═══════════════════════════════════════════════════════════

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


# ═══════════════════════════════════════════════════════════
#  导出：工具定义 & Handler 映射
# ═══════════════════════════════════════════════════════════

MCP_TOOLS = [
    {"name": "connect_mcp",
     "description": "Connect to an MCP server and discover its tools. "
                    "Available servers: docs (readOnly), deploy (destructive). "
                    "MCP tools are prefixed mcp__{server}__{tool}.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string",
                                   "description": "Server name (docs, deploy)"}},
                      "required": ["name"]}},
]

MCP_TOOL_HANDLERS = {
    "connect_mcp": run_connect_mcp,
}
