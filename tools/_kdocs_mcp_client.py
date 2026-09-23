"""Minimal kdocs MCP (streamable-http) client.

Calls the SAME `read_file` tool the agent would, but over HTTP JSON-RPC so a
script can fetch the full 返件登记 sheet and persist it without hand-transcribing
~40 MB of structured JSON. Read-only.
"""
import json
import sys
import urllib.request
import urllib.error
import ssl
import os

MCP_URL = "https://mcp-center.wps.cn/skill_hub/mcp"

# WPS / 金山文档 MCP 的访问令牌。token 文件路径**不写死在本仓库里**
# （那是本机私有信息），改由环境变量指定；它一般在 WorkBuddy 连接器目录下：
#     <用户目录>/.workbuddy/connectors/<连接器 id>/tokens/wps.txt
TOKEN_FILE = os.environ.get("ARS_KDOCS_TOKEN_FILE", "")
if not TOKEN_FILE:
    raise SystemExit("未设置 ARS_KDOCS_TOKEN_FILE（指向金山文档 MCP 的 token 文件）")
TOKEN = open(TOKEN_FILE, encoding="utf-8").read().strip()
STATIC_HEADERS = {
    "X-Request-Source": "workbuddy",
    "X-Skill-Version": "1.4.12",
}
CTX = ssl._create_unverified_context()


class MCPClient:
    def __init__(self):
        self.session_id = None
        self._id = 0

    def _post(self, payload, is_notification=False):
        self._id += 1
        if not is_notification:
            payload["id"] = self._id
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + TOKEN,
            **STATIC_HEADERS,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(MCP_URL, data=body, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=120, context=CTX)
        if "Mcp-Session-Id" in resp.headers:
            self.session_id = resp.headers["Mcp-Session-Id"]
        raw = resp.read().decode("utf-8", "replace")
        return raw

    def _parse(self, raw):
        # response may be SSE (text/event-stream) or plain JSON
        if raw.strip().startswith("event:") or "data:" in raw:
            out = None
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[len("data:"):].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            out = json.loads(chunk)
                        except Exception:
                            pass
            return out
        try:
            return json.loads(raw)
        except Exception:
            return {"__raw__": raw[:500]}

    def initialize(self):
        raw = self._post({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kdocs-snapshot", "version": "1.0"},
            },
        })
        self._parse(raw)  # consume
        # initialized notification
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   is_notification=True)

    def call_tool(self, name, arguments):
        raw = self._post({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return self._parse(raw)


def extract_text(result):
    """Pull the inner JSON text out of an MCP tools/call result."""
    if result is None:
        return None
    if "__raw__" in result:
        return result
    try:
        contents = result["result"]["content"]
    except Exception:
        return result
    text = ""
    for c in contents:
        if c.get("type") == "text":
            text += c.get("text", "")
    return text


if __name__ == "__main__":
    client = MCPClient()
    client.initialize()
    res = client.call_tool("read_file", {
        "file_id": "qAqmwXpUP1Mji2UbPz7orxkqEBjTbWH4Z",
        "sheet_id": 1,
        "sheet_range": {"row_from": 0, "row_to": 3, "col_from": 0, "col_to": 25},
    })
    txt = extract_text(res)
    print("=== RESULT TYPE ===")
    print(type(txt))
    if isinstance(txt, str):
        print(txt[:800])
    else:
        print(json.dumps(txt, ensure_ascii=False)[:800])
