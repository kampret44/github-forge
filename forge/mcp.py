"""MCP surface (POST /mcp). JSON-RPC 2.0 Streamable HTTP.

Modern era (2026-07-28): server/discover answers supportedVersions +
capabilities + serverInfo in one request; no handshake, no session.
Legacy era: initialize (choose from the offered revisions) +
notifications/initialized. tools/list is pre-serialized once so two calls
are byte-identical. tools/call runs against the same store as REST.
"""
import json

from . import util

SERVER_INFO = {"name": "github-forge", "version": "1.0.0"}
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def _tool(name, desc, props, required):
    return {
        "name": name,
        "description": desc,
        "inputSchema": {"type": "object", "properties": props, "required": required},
    }


TOOLS = [
    _tool("create_repo", "Create a repository owned by the authenticated user.",
          {"name": {"type": "string", "description": "Repository name"},
           "description": {"type": "string", "description": "Repository description"},
           "private": {"type": "boolean", "description": "Whether the repository is private"}},
          ["name"]),
    _tool("get_repo", "Get a repository by owner and name.",
          {"owner": {"type": "string", "description": "Repository owner login"},
           "repo": {"type": "string", "description": "Repository name"}},
          ["owner", "repo"]),
    _tool("create_issue", "Create an issue in a repository.",
          {"owner": {"type": "string", "description": "Repository owner login"},
           "repo": {"type": "string", "description": "Repository name"},
           "title": {"type": "string", "description": "Issue title"},
           "body": {"type": "string", "description": "Issue body"}},
          ["owner", "repo", "title"]),
    _tool("list_issues", "List issues in a repository.",
          {"owner": {"type": "string", "description": "Repository owner login"},
           "repo": {"type": "string", "description": "Repository name"},
           "state": {"type": "string", "description": "Issue state: open, closed or all"}},
          ["owner", "repo"]),
    _tool("create_pull_request", "Create a pull request in a repository.",
          {"owner": {"type": "string", "description": "Repository owner login"},
           "repo": {"type": "string", "description": "Repository name"},
           "title": {"type": "string", "description": "Pull request title"},
           "body": {"type": "string", "description": "Pull request body"},
           "head": {"type": "string", "description": "Head branch name"},
           "base": {"type": "string", "description": "Base branch name"}},
          ["owner", "repo", "title", "head", "base"]),
    _tool("merge_pull_request", "Merge a pull request with a squash commit.",
          {"owner": {"type": "string", "description": "Repository owner login"},
           "repo": {"type": "string", "description": "Repository name"},
           "pull_number": {"type": "integer", "description": "Pull request number"}},
          ["owner", "repo", "pull_number"]),
]

# Pre-serialized once: two tools/list calls must be byte-identical.
TOOLS_LIST_BODY = json.dumps(
    {"tools": TOOLS}, separators=(",", ":"), sort_keys=True).encode("utf-8")


class MCP:
    def __init__(self, ctx, rest):
        self.ctx = ctx
        self.store = ctx.store
        self.rest = rest

    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        if method != "POST":
            return 405, [("content-type", "application/json")], b'{"message":"Method Not Allowed"}'
        if not self.ctx.authed(headers):
            return 401, [("content-type", "application/json")], b'{"message":"Requires authentication"}'
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, [("content-type", "application/json")], json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "Parse error"}}).encode("utf-8")
        if not isinstance(req, dict):
            return 400, [("content-type", "application/json")], json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "Parse error"}}).encode("utf-8")
        req_id = req.get("id")
        rpc_method = req.get("method")
        params = req.get("params") or {}
        mirror = []
        if "mcp-method" in headers:
            mirror.append(("mcp-method", headers["mcp-method"]))
        if "mcp-name" in headers:
            mirror.append(("mcp-name", headers["mcp-name"]))
        # Notification (no id): no JSON-RPC response body.
        if req_id is None:
            self._dispatch(rpc_method, params)
            return 202, mirror, b""
        try:
            result = self._dispatch(rpc_method, params)
        except _RpcError as e:
            out = json.dumps({"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": e.code, "message": e.message}},
                             separators=(",", ":")).encode("utf-8")
            return 200, [("content-type", "application/json")] + mirror, out
        out = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                         separators=(",", ":")).encode("utf-8")
        return 200, [("content-type", "application/json")] + mirror, out

    # ---- dispatch ------------------------------------------------------------
    def _dispatch(self, method, params):
        if method == "server/discover":
            return {
                "supportedVersions": SUPPORTED_VERSIONS,
                "capabilities": {"tools": {}},
                "serverInfo": dict(SERVER_INFO),
                "_meta": {"io.modelcontextprotocol/serverInfo": dict(SERVER_INFO)},
            }
        if method == "initialize":
            offered = params.get("protocolVersion")
            chosen = offered if offered in SUPPORTED_VERSIONS else "2025-11-25"
            return {
                "protocolVersion": chosen,
                "capabilities": {"tools": {}},
                "serverInfo": dict(SERVER_INFO),
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments") or {})
        raise _RpcError(-32601, "Method not found: %s" % method)

    # ---- tools ---------------------------------------------------------------
    def _call_tool(self, name, args):
        handler = getattr(self, "_tool_" + (name or ""), None)
        if handler is None:
            raise _RpcError(-32602, "Unknown tool: %s" % name)
        required = next((t["inputSchema"]["required"] for t in TOOLS if t["name"] == name), [])
        missing = [r for r in required if r not in args or args[r] is None]
        if missing:
            return _tool_error("missing required argument(s): %s" % ", ".join(missing))
        try:
            data = handler(args)
        except _ToolError as e:
            return _tool_error(str(e))
        text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        return {"content": [{"type": "text", "text": text}], "structuredContent": data}

    def _repo(self, args):
        repo = self.store.get_repo(args.get("owner"), args.get("repo"))
        if repo is None or repo.owner != util.VIEWER_LOGIN:
            raise _ToolError("repository not found")
        return repo

    def _tool_create_repo(self, args):
        name = args.get("name")
        if not name:
            raise _ToolError("name is required")
        if self.store.get_repo(util.VIEWER_LOGIN, name) is not None:
            raise _ToolError("repository already exists")
        repo = self.store.create_repo(util.VIEWER_LOGIN, name,
                                      bool(args.get("private", False)),
                                      args.get("description") or "")
        return {"name": repo.name, "full_name": repo.owner + "/" + repo.name,
                "private": repo.private, "description": repo.description}

    def _tool_get_repo(self, args):
        repo = self._repo(args)
        return {"name": repo.name, "full_name": repo.owner + "/" + repo.name,
                "private": repo.private, "description": repo.description}

    def _tool_create_issue(self, args):
        repo = self._repo(args)
        iss = {"number": repo.next_issue, "title": args.get("title") or "",
               "body": args.get("body") or "", "state": "open", "labels": []}
        repo.next_issue += 1
        repo.issues.append(iss)
        return {"number": iss["number"], "title": iss["title"], "body": iss["body"],
                "state": "open"}

    def _tool_list_issues(self, args):
        repo = self._repo(args)
        state = str(args.get("state") or "open").lower()
        items = repo.issues
        if state != "all":
            items = [i for i in items if i["state"] == state]
        out = [{"number": i["number"], "title": i["title"], "body": i["body"],
                "state": i["state"]} for i in items]
        return {"issues": out}

    def _tool_create_pull_request(self, args):
        repo = self._repo(args)
        head_ref = args.get("head")
        base_ref = args.get("base")
        head_sha = repo.ref_tip(head_ref)
        base_sha = repo.ref_tip(base_ref)
        if head_sha is None or base_sha is None:
            raise _ToolError("head or base branch does not exist")
        pr = {"number": repo.next_pr, "title": args.get("title") or "",
              "body": args.get("body") or "", "state": "open",
              "head_ref": head_ref, "head_sha": head_sha,
              "base_ref": base_ref, "base_sha": base_sha,
              "merged": False, "merge_commit_sha": None,
              "files": self.rest._diff_files(repo, base_sha, head_sha)}
        repo.next_pr += 1
        repo.prs.append(pr)
        return {"number": pr["number"], "title": pr["title"], "body": pr["body"],
                "head": head_ref, "base": base_ref, "state": "open"}

    def _tool_merge_pull_request(self, args):
        repo = self._repo(args)
        number = args.get("pull_number")
        pr = next((p for p in repo.prs if p["number"] == number), None)
        if pr is None:
            raise _ToolError("pull request not found")
        if pr["merged"]:
            raise _ToolError("pull request already merged")
        if self.rest._pr_conflicts(repo, pr):
            raise _ToolError("merge conflict")
        status, _h, _b = self.rest._merge_pr(repo, pr, b'{"merge_method":"squash"}')
        if status != 200:
            raise _ToolError("merge failed")
        return {"number": pr["number"], "merged": True, "sha": pr["merge_commit_sha"],
                "state": "closed"}


def _tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class _ToolError(Exception):
    pass
