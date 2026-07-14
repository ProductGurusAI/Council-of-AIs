import sys
import json

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            
            if method == "initialize":
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "mock-filesystem-server", "version": "1.0.0"}
                    }
                }
                print(json.dumps(res), flush=True)
            elif method == "tools/list":
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {
                                "name": "list_dir",
                                "description": "Lists files in directory.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"}
                                    }
                                }
                            },
                            {
                                "name": "read_file",
                                "description": "Reads file contents.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"}
                                    }
                                }
                            }
                        ]
                    }
                }
                print(json.dumps(res), flush=True)
            elif method == "tools/call":
                params = req.get("params", {})
                name = params.get("name")
                args = params.get("arguments", {})
                path = args.get("path", "")
                
                # Mock result based on name
                if name == "list_dir":
                    text = "file1.txt\nfile2.txt"
                elif name == "read_file":
                    text = f"Content of {path}: simulated content"
                else:
                    text = f"Unknown tool: {name}"
                    
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": text}
                        ]
                    }
                }
                print(json.dumps(res), flush=True)
        except Exception:
            pass

if __name__ == "__main__":
    main()
