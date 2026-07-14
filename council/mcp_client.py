import os
import json
import subprocess
import sys
from typing import List, Dict, Any, Tuple

MCP_CONFIG_PATH = "mcp.json"

def load_mcp_config() -> dict:
    if os.path.exists(MCP_CONFIG_PATH):
        try:
            with open(MCP_CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"servers": {}}

def send_and_wait(p, req: dict, expected_id: int) -> dict:
    req_str = json.dumps(req) + "\n"
    p.stdin.write(req_str)
    p.stdin.flush()
    
    # Read stdout until we find the response matching expected_id
    while True:
        line = p.stdout.readline()
        if not line:
            raise RuntimeError("EOF reached while waiting for response")
        try:
            data = json.loads(line)
            if data.get("id") == expected_id:
                return data
        except Exception:
            pass

def execute_stdio_mcp(server_name: str, config: dict, method: str, params: dict = None) -> dict:
    """Spawns the stdio process, does handshake, executes method, and kills it."""
    cmd = [config["command"]] + config.get("args", [])
    
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True
    )
    try:
        # 1. Handshake Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "council-client", "version": "1.0.0"}
            }
        }
        send_and_wait(p, init_req, 1)
        
        # 2. Handshake Initialized notification
        init_notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        p.stdin.write(json.dumps(init_notif) + "\n")
        p.stdin.flush()
        
        # 3. Call method
        method_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": method,
            "params": params or {}
        }
        res = send_and_wait(p, method_req, 2)
        return res.get("result", {})
    finally:
        try:
            if p.stdin:
                p.stdin.close()
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

def list_tools() -> List[Dict[str, Any]]:
    """Lists all tools namespaced under their server name."""
    config = load_mcp_config()
    tools = []
    
    # Fallback/Default tools: if mcp.json has no servers, provide mock filesystem tools
    if not config.get("servers"):
        return [
            {
                "name": "filesystem/list_dir",
                "description": "Lists files in the workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace path."}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "filesystem/read_file",
                "description": "Reads the content of a file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path."}
                    },
                    "required": ["path"]
                }
            }
        ]

    for name, srv_config in config["servers"].items():
        try:
            res = execute_stdio_mcp(name, srv_config, "tools/list")
            srv_tools = res.get("tools", [])
            for t in srv_tools:
                prefixed_tool = t.copy()
                prefixed_tool["name"] = f"{name}/{t['name']}"
                tools.append(prefixed_tool)
        except Exception as e:
            print(f"WARNING: failed to list tools for server {name}: {str(e)}", file=sys.stderr)
            
    return tools

def call_tool(prefixed_name: str, arguments: dict) -> str:
    """Executes a tool call on the namespaced server."""
    config = load_mcp_config()
    
    if "/" not in prefixed_name:
        # Check if fallback mode list
        if not config.get("servers"):
            return handle_mock_tool(prefixed_name, arguments)
        return f"Error: Tool name '{prefixed_name}' must be formatted as 'server_name/tool_name'."
        
    server_name, tool_name = prefixed_name.split("/", 1)
    
    # Fallback mock tools support
    if not config.get("servers") and server_name == "filesystem":
        return handle_mock_tool(tool_name, arguments)
        
    if server_name not in config.get("servers", {}):
        return f"Error: Server '{server_name}' is not configured in mcp.json."
        
    srv_config = config["servers"][server_name]
    try:
        res = execute_stdio_mcp(server_name, srv_config, "tools/call", {"name": tool_name, "arguments": arguments})
        content = res.get("content", [])
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(text_parts) if text_parts else "Success (empty response)"
    except Exception as e:
        return f"Error executing tool: {str(e)}"

def handle_mock_tool(tool_name: str, arguments: dict) -> str:
    """Simple mock tool runner for fallback/local execution."""
    path = arguments.get("path", ".")
    if tool_name == "list_dir":
        try:
            files = os.listdir(path)
            return "\n".join(files) if files else "(empty directory)"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
    elif tool_name == "read_file":
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            return f"Error reading file: {str(e)}"
    return f"Error: Tool '{tool_name}' not found."
