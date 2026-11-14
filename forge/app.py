"""Router + request context. Dispatches by path prefix:

  GET /healthz            -> 200 {"ok": true} (no auth)
  /api/v3...              -> REST v3
  /api/graphql            -> GraphQL
  /mcp                    -> MCP
  /{owner}/{repo}.git/... -> git smart HTTP
  anything else           -> 404 JSON
"""
import json

from . import util
from .store import Store
from .rest import Rest
from .graphql import GraphQL
from .mcp import MCP
from .git import Git


class Context:
    def __init__(self, token):
        self.store = Store()
        self.token = token
        self.base_url = ""
        self.req_ids = util.RequestIdGen()

    def authed(self, headers):
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        presented = auth[7:]
        if not presented:
            return False
        if not self.token:
            return True
        return presented == self.token


class App:
    def __init__(self, token):
        self.ctx = Context(token)
        self.rest = Rest(self.ctx)
        self.graphql = GraphQL(self.ctx, self.rest)
        self.mcp = MCP(self.ctx, self.rest)
        self.git = Git(self.ctx)

    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        path = raw_path.split("?", 1)[0]
        # The origin the client sees (Link headers and clone URLs must be same-origin).
        host = headers.get("host") or "127.0.0.1"
        self.ctx.base_url = "http://" + host
        if path == "/healthz":
            if method != "GET":
                return 405, [("content-type", "application/json")], b'{"message":"Method Not Allowed"}'
            return 200, [("content-type", "application/json")], b'{"ok":true}'
        if path == "/api/graphql" or path.startswith("/api/graphql?"):
            return self.graphql.handle(method, raw_path, headers, body)
        if path == "/mcp":
            return self.mcp.handle(method, raw_path, headers, body)
        if path == "/api/v3" or path.startswith("/api/v3/"):
            return self.rest.handle(method, raw_path, headers, body)
        parts = [p for p in path.split("/") if p != ""]
        if len(parts) >= 2 and parts[1].endswith(".git"):
            return self.git.handle(method, raw_path, headers, body)
        return 404, [("content-type", "application/json"),
                     ("x-github-request-id", self.ctx.req_ids.next())], \
            json.dumps({"message": "Not Found"}).encode("utf-8")
