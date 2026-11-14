"""GraphQL v4 surface (POST /api/graphql). Hand-written parser + resolvers.

Supports exactly the documents gh / github-mcp-server send (see DESIGN.md):
named operations, variables, aliases, inline enums / input objects / literals,
inline fragments, batch arrays. Unknown field -> HTTP 200 with errors naming it.
Unknown repo -> data.repository null, no errors. No token -> 401.
"""
import json

from . import util


# ---------------------------------------------------------------- tokenizer
class _Tok:
    def __init__(self, kind, val):
        self.kind = kind
        self.val = val

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.val)


def _tokenize(src: str):
    toks = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "." and src[i:i + 3] == "...":
            toks.append(_Tok("...", "..."))
            i += 3
            continue
        if c in "{}():!=$@[]":
            toks.append(_Tok(c, c))
            i += 1
            continue
        if c == '"':
            j = i + 1
            out = []
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    out.append(src[j + 1])
                    j += 2
                else:
                    out.append(src[j])
                    j += 1
            toks.append(_Tok("string", "".join(out)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isdigit() or src[j] in ".eE+-"):
                j += 1
            text = src[i:j]
            toks.append(_Tok("number", float(text) if any(ch in text for ch in ".eE") else int(text)))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(_Tok("name", src[i:j]))
            i = j
            continue
        raise ValueError("unexpected character %r" % c)
    toks.append(_Tok("eof", None))
    return toks


# ---------------------------------------------------------------- parser
class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind):
        t = self.next()
        if t.kind != kind:
            raise ValueError("expected %s, got %s" % (kind, t.kind))
        return t

    def parse_document(self):
        ops = []
        while self.peek().kind != "eof":
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        t = self.peek()
        if t.kind == "{":
            return {"kind": "query", "name": None, "vars": {}, "selections": self.parse_selections()}
        op_type = self.next().val  # query | mutation | subscription
        name = None
        if self.peek().kind == "name":
            name = self.next().val
        var_defs = {}
        if self.peek().kind == "(":
            self.next()
            while self.peek().kind != ")":
                self.expect("$")
                vname = self.expect("name").val
                self.expect(":")
                var_defs[vname] = self.parse_type()
                if self.peek().kind == "=":
                    self.next()
                    self.parse_value()
            self.expect(")")
        return {"kind": op_type, "name": name, "vars": var_defs, "selections": self.parse_selections()}

    def parse_type(self):
        if self.peek().kind == "[":
            self.next()
            inner = self.parse_type()
            self.expect("]")
            base = "[" + inner + "]"
        else:
            base = self.expect("name").val
        if self.peek().kind == "!":
            self.next()
            base += "!"
        return base

    def parse_selections(self):
        self.expect("{")
        sels = []
        while self.peek().kind != "}":
            sels.append(self.parse_selection())
        self.expect("}")
        return sels

    def parse_selection(self):
        if self.peek().kind == "...":
            self.next()
            if self.peek().kind == "name" and self.peek().val == "on":
                self.next()
                type_name = self.expect("name").val
                return {"fragment": True, "type": type_name, "selections": self.parse_selections()}
            if self.peek().kind == "name":
                # named fragment spread (not used by the suite); parse selections after it
                self.next()
                if self.peek().kind == "{":
                    return {"fragment": True, "type": None, "selections": self.parse_selections()}
                return {"fragment": True, "type": None, "selections": []}
            return {"fragment": True, "type": None, "selections": self.parse_selections()}
        name = self.expect("name").val
        alias = None
        if self.peek().kind == ":":
            self.next()
            alias = name
            name = self.expect("name").val
        args = {}
        if self.peek().kind == "(":
            self.next()
            while self.peek().kind != ")":
                aname = self.expect("name").val
                self.expect(":")
                args[aname] = self.parse_value()
            self.expect(")")
        sels = []
        if self.peek().kind == "{":
            sels = self.parse_selections()
        return {"field": name, "alias": alias or name, "args": args, "selections": sels}

    def parse_value(self):
        t = self.next()
        if t.kind == "$":
            return ("var", self.expect("name").val)
        if t.kind == "string":
            return t.val
        if t.kind == "number":
            return t.val
        if t.kind == "name":
            if t.val == "true":
                return True
            if t.val == "false":
                return False
            if t.val == "null":
                return None
            return ("enum", t.val)
        if t.kind == "[":
            items = []
            while self.peek().kind != "]":
                items.append(self.parse_value())
            self.expect("]")
            return items
        if t.kind == "{":
            obj = {}
            while self.peek().kind != "}":
                k = self.expect("name").val
                self.expect(":")
                obj[k] = self.parse_value()
            self.expect("}")
            return obj
        raise ValueError("unexpected value token %s" % t.kind)


# ---------------------------------------------------------------- schema
TYPE_FIELDS = {
    "Query": {"viewer", "repository", "node", "__type"},
    "Mutation": {"createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"},
    "User": {"login"},
    "Repository": {"id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests", "pullRequest"},
    "Ref": {"name"},
    "Issue": {"id", "number", "title", "state", "body"},
    "IssueConnection": {"nodes", "pageInfo"},
    "PageInfo": {"hasNextPage", "endCursor"},
    "PullRequest": {"id", "number", "state", "merged", "headRefName", "baseRefName", "mergeable", "files"},
    "PullRequestConnection": {"nodes"},
    "PullRequestChangedFileConnection": {"nodes"},
    "PullRequestChangedFile": {"path"},
    "CreateIssuePayload": {"issue"},
    "CloseIssuePayload": {"issue"},
    "CreatePullRequestPayload": {"pullRequest"},
    "MergePullRequestPayload": {"pullRequest"},
    "AddCommentPayload": {"commentEdge"},
    "IssueCommentEdge": {"node"},
    "IssueComment": {"id", "body"},
    "__Type": {"name", "fields", "enumValues"},
    "__Field": {"name"},
    "__EnumValue": {"name"},
}
TYPE_FIELD_NAMES = {
    "Issue": ["id", "number", "title", "state", "body"],
    "PullRequest": ["id", "number", "headRefName", "baseRefName", "mergeable", "state", "merged", "files"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests", "pullRequest"],
}
ENUM_VALUES = {
    "SearchType": ["ISSUE"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
}


class _ExecError(Exception):
    pass


class GraphQL:
    def __init__(self, ctx, rest):
        self.ctx = ctx
        self.store = ctx.store
        self.rest = rest  # reuse REST merge/conflict logic

    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        if method != "POST":
            return 404, [("content-type", "application/json")], b'{"message":"Not Found"}'
        if not self.ctx.authed(headers):
            return 401, [("content-type", "application/json")], b'{"message":"Requires authentication"}'
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, [("content-type", "application/json")], b'{"message":"malformed JSON body"}'
        if isinstance(payload, list):
            results = [self._execute_one(op) for op in payload]
            out = json.dumps(results, separators=(",", ":")).encode("utf-8")
        else:
            out = json.dumps(self._execute_one(payload), separators=(",", ":")).encode("utf-8")
        return 200, [("content-type", "application/json"),
                     ("x-github-request-id", self.ctx.req_ids.next())], out + b"\n"

    # ---- execution ---------------------------------------------------------
    def _execute_one(self, op):
        if not isinstance(op, dict) or "query" not in op:
            return {"data": None, "errors": [{"message": "request must carry a query"}]}
        variables = op.get("variables") or {}
        try:
            doc = _Parser(_tokenize(op["query"])).parse_document()
        except ValueError as e:
            return {"data": None, "errors": [{"message": str(e)}]}
        if not doc:
            return {"data": None, "errors": [{"message": "empty document"}]}
        operation = doc[0]
        op_name = op.get("operationName")
        if op_name:
            for cand in doc:
                if cand["name"] == op_name:
                    operation = cand
                    break
        try:
            self._validate(operation["selections"], "Query" if operation["kind"] == "query" else "Mutation")
            if operation["kind"] == "mutation":
                data = self._resolve_selections(operation["selections"], ("Mutation", None), variables)
            else:
                data = self._resolve_selections(operation["selections"], ("Query", None), variables)
            return {"data": data}
        except _ExecError as e:
            return {"data": None, "errors": [{"message": str(e)}]}

    def _validate(self, selections, type_name):
        allowed = TYPE_FIELDS.get(type_name, set())
        for sel in selections:
            if "fragment" in sel:
                if sel["type"]:
                    self._validate(sel["selections"], sel["type"])
                continue
            name = sel["field"]
            if name not in allowed:
                raise _ExecError('Cannot query field "%s" on type "%s"' % (name, type_name))
            child = _child_type(type_name, name)
            if child and sel["selections"]:
                self._validate(sel["selections"], child)

    def _resolve_selections(self, selections, node, variables):
        type_name, value = node
        out = {}
        for sel in selections:
            if "fragment" in sel:
                if sel["type"] is None or sel["type"] == _typename_of(node):
                    out.update(self._resolve_selections(sel["selections"], node, variables))
                continue
            field = sel["field"]
            args = {k: self._resolve_value(v, variables) for k, v in sel["args"].items()}
            out[sel["alias"]] = self._resolve_field(type_name, value, field, args, sel["selections"], variables)
        return out

    @staticmethod
    def _resolve_value(v, variables):
        if isinstance(v, tuple) and v and v[0] == "var":
            return variables.get(v[1])
        if isinstance(v, tuple) and v and v[0] == "enum":
            return v[1]
        if isinstance(v, list):
            return [GraphQL._resolve_value(x, variables) for x in v]
        if isinstance(v, dict):
            return {k: GraphQL._resolve_value(x, variables) for k, x in v.items()}
        return v

    # ---- field resolution ---------------------------------------------------
    def _resolve_field(self, type_name, value, field, args, selections, variables):
        if type_name == "Query":
            if field == "viewer":
                return self._resolve_selections(selections, ("User", None), variables)
            if field == "repository":
                repo = self.store.get_repo(args.get("owner"), args.get("name"))
                if repo is None or repo.owner != util.VIEWER_LOGIN:
                    return None
                return self._resolve_selections(selections, ("Repository", repo), variables)
            if field == "node":
                gid = args.get("id")
                repo = self.store.repo_by_global_id(gid) if gid else None
                if repo is None:
                    return None
                return self._resolve_selections(selections, ("Repository", repo), variables)
            if field == "__type":
                name = args.get("name")
                return self._resolve_selections(selections, ("__Type", name), variables)
        if type_name == "Mutation":
            return self._mutation(field, args, selections, variables)
        if type_name == "__Type":
            name = value
            if field == "name":
                return name
            if field == "fields":
                return [{"name": n} for n in TYPE_FIELD_NAMES.get(name, [])]
            if field == "enumValues":
                return [{"name": n} for n in ENUM_VALUES.get(name, [])]
        if type_name == "User":
            if field == "login":
                return util.VIEWER_LOGIN
        if type_name == "Repository":
            return self._repo_field(value, field, args, selections, variables)
        if type_name == "Ref":
            if field == "name":
                return value
        if type_name == "Issue":
            return self._issue_field(value, field)
        if type_name == "PullRequest":
            return self._pr_field(value, field, args, selections, variables)
        if type_name in ("IssueConnection", "PullRequestConnection", "PullRequestChangedFileConnection"):
            if field == "nodes":
                items = value["nodes"]
                child = {"IssueConnection": "Issue", "PullRequestConnection": "PullRequest",
                         "PullRequestChangedFileConnection": "PullRequestChangedFile"}[type_name]
                return [self._resolve_selections(selections, (child, item), variables) for item in items]
            if field == "pageInfo":
                return self._resolve_selections(selections, ("PageInfo", value["pageInfo"]), variables)
        if type_name == "PageInfo":
            return value[field]
        if type_name == "PullRequestChangedFile":
            return value[field]
        if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
            if field == "issue":
                return self._resolve_selections(selections, ("Issue", value), variables)
        if type_name in ("CreatePullRequestPayload", "MergePullRequestPayload"):
            if field == "pullRequest":
                return self._resolve_selections(selections, ("PullRequest", value), variables)
        if type_name == "AddCommentPayload":
            if field == "commentEdge":
                return self._resolve_selections(selections, ("IssueCommentEdge", value), variables)
        if type_name == "IssueCommentEdge":
            if field == "node":
                return self._resolve_selections(selections, ("IssueComment", value), variables)
        if type_name == "IssueComment":
            return value[field]
        raise _ExecError('Cannot resolve field "%s" on type "%s"' % (field, type_name))

    def _repo_field(self, repo, field, args, selections, variables):
        if field == "id":
            return util.repo_global_id(repo.owner, repo.name)
        if field == "databaseId":
            return repo.database_id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return repo.owner + "/" + repo.name
        if field == "owner":
            return self._resolve_selections(selections, ("User", None), variables)
        if field == "defaultBranchRef":
            tip = repo.ref_tip(repo.default_branch)
            if tip is None:
                return None
            return self._resolve_selections(selections, ("Ref", repo.default_branch), variables)
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "issue":
            number = args.get("number")
            iss = next((i for i in repo.issues if i["number"] == number), None)
            if iss is None:
                return None
            return self._resolve_selections(selections, ("Issue", (repo, iss)), variables)
        if field == "issues":
            return self._issues_connection(repo, args, selections, variables)
        if field == "pullRequests":
            nodes = repo.prs
            first = args.get("first")
            if first is not None:
                nodes = nodes[:first]
            return self._resolve_selections(selections,
                ("PullRequestConnection", {"nodes": [(repo, p) for p in nodes], "pageInfo": {}}), variables)
        if field == "pullRequest":
            number = args.get("number")
            pr = next((p for p in repo.prs if p["number"] == number), None)
            if pr is None:
                return None
            return self._resolve_selections(selections, ("PullRequest", (repo, pr)), variables)
        raise _ExecError('Cannot resolve field "%s" on Repository' % field)

    def _issues_connection(self, repo, args, selections, variables):
        items = list(repo.issues)
        states = args.get("states")
        if states:
            items = [i for i in items if i["state"].lower() in [str(s).lower() for s in states]]
        order_by = args.get("orderBy") or {}
        if isinstance(order_by, dict) and str(order_by.get("direction", "")).upper() == "DESC":
            items = sorted(items, key=lambda i: i["number"], reverse=True)
        else:
            items = sorted(items, key=lambda i: i["number"])
        after = args.get("after")
        if after:
            try:
                cut = int(str(after)[1:])
                items = [i for i in items if i["number"] > cut]
            except (ValueError, IndexError):
                pass
        first = args.get("first")
        page = items
        has_next = False
        if first is not None:
            page = items[:first]
            has_next = len(items) > first
        end_cursor = ("c%d" % page[-1]["number"]) if page else None
        conn = {"nodes": [(repo, i) for i in page],
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}}
        return self._resolve_selections(selections, ("IssueConnection", conn), variables)

    def _issue_field(self, value, field):
        repo, iss = value
        if field == "id":
            return util.issue_global_id(repo.owner, repo.name, iss["number"])
        if field == "number":
            return iss["number"]
        if field == "title":
            return iss["title"]
        if field == "state":
            return iss["state"].upper()
        if field == "body":
            return iss["body"]
        raise _ExecError('Cannot resolve field "%s" on Issue' % field)

    def _pr_field(self, value, field, args, selections, variables):
        repo, pr = value
        if field == "id":
            return util.pr_global_id(repo.owner, repo.name, pr["number"])
        if field == "number":
            return pr["number"]
        if field == "state":
            if pr["merged"]:
                return "MERGED"
            return pr["state"].upper()
        if field == "merged":
            return pr["merged"]
        if field == "headRefName":
            return pr["head_ref"]
        if field == "baseRefName":
            return pr["base_ref"]
        if field == "mergeable":
            return "CONFLICTING" if self.rest._pr_conflicts(repo, pr) else "MERGEABLE"
        if field == "files":
            first = args.get("first")
            nodes = [{"path": f["filename"]} for f in pr["files"]]
            if first is not None:
                nodes = nodes[:first]
            return self._resolve_selections(selections,
                ("PullRequestChangedFileConnection", {"nodes": nodes, "pageInfo": {}}), variables)
        raise _ExecError('Cannot resolve field "%s" on PullRequest' % field)

    # ---- mutations -------------------------------------------------------------
    def _mutation(self, field, args, selections, variables):
        inp = args.get("input") or {}
        if field == "createIssue":
            repo = self.store.repo_by_global_id(inp.get("repositoryId"))
            if repo is None:
                raise _ExecError("repository not found")
            iss = {"number": repo.next_issue, "title": inp.get("title") or "",
                   "body": inp.get("body") or "", "state": "open", "labels": []}
            repo.next_issue += 1
            repo.issues.append(iss)
            return self._resolve_selections(selections, ("CreateIssuePayload", (repo, iss)), variables)
        if field == "closeIssue":
            repo, iss = self.store.issue_by_global_id(inp.get("issueId"))
            if iss is None:
                raise _ExecError("issue not found")
            iss["state"] = "closed"
            repo.invalidate_issue_cache(iss["number"])
            return self._resolve_selections(selections, ("CloseIssuePayload", (repo, iss)), variables)
        if field == "createPullRequest":
            repo = self.store.repo_by_global_id(inp.get("repositoryId"))
            if repo is None:
                raise _ExecError("repository not found")
            head_ref = inp.get("headRefName")
            base_ref = inp.get("baseRefName")
            head_sha = repo.ref_tip(head_ref)
            base_sha = repo.ref_tip(base_ref)
            if head_sha is None or base_sha is None:
                raise _ExecError("head or base branch does not exist")
            pr = {"number": repo.next_pr, "title": inp.get("title") or "",
                  "body": "", "state": "open",
                  "head_ref": head_ref, "head_sha": head_sha,
                  "base_ref": base_ref, "base_sha": base_sha,
                  "merged": False, "merge_commit_sha": None,
                  "files": self.rest._diff_files(repo, base_sha, head_sha)}
            repo.next_pr += 1
            repo.prs.append(pr)
            return self._resolve_selections(selections, ("CreatePullRequestPayload", (repo, pr)), variables)
        if field == "mergePullRequest":
            repo, pr = self.store.pr_by_global_id(inp.get("pullRequestId"))
            if pr is None:
                raise _ExecError("pull request not found")
            if pr["merged"]:
                raise _ExecError("pull request already merged")
            if self.rest._pr_conflicts(repo, pr):
                raise _ExecError("merge conflict")
            # reuse REST merge (squash): build a minimal body call
            status, _headers, _body = self.rest._merge_pr(repo, pr, b'{"merge_method":"squash"}')
            if status != 200:
                raise _ExecError("merge failed")
            return self._resolve_selections(selections, ("MergePullRequestPayload", (repo, pr)), variables)
        if field == "addComment":
            repo, iss = self.store.issue_by_global_id(inp.get("subjectId"))
            if iss is None:
                raise _ExecError("issue not found")
            text = inp.get("body") or ""
            cid = repo.next_comment_id
            repo.next_comment_id += 1
            comment = {"id": cid, "body": text}
            repo.comments.setdefault(iss["number"], []).append(comment)
            node = {"id": util.comment_global_id(repo.owner, repo.name, iss["number"], cid), "body": text}
            return self._resolve_selections(selections, ("AddCommentPayload", node), variables)
        raise _ExecError('Unknown mutation "%s"' % field)


def _child_type(parent: str, field: str):
    mapping = {
        ("Query", "viewer"): "User",
        ("Query", "repository"): "Repository",
        ("Query", "node"): None,  # resolved via inline fragment
        ("Query", "__type"): "__Type",
        ("Repository", "owner"): "User",
        ("Repository", "defaultBranchRef"): "Ref",
        ("Repository", "issue"): "Issue",
        ("Repository", "issues"): "IssueConnection",
        ("Repository", "pullRequests"): "PullRequestConnection",
        ("Repository", "pullRequest"): "PullRequest",
        ("IssueConnection", "nodes"): "Issue",
        ("IssueConnection", "pageInfo"): "PageInfo",
        ("PullRequestConnection", "nodes"): "PullRequest",
        ("PullRequest", "files"): "PullRequestChangedFileConnection",
        ("PullRequestChangedFileConnection", "nodes"): "PullRequestChangedFile",
        ("Mutation", "createIssue"): "CreateIssuePayload",
        ("Mutation", "closeIssue"): "CloseIssuePayload",
        ("Mutation", "createPullRequest"): "CreatePullRequestPayload",
        ("Mutation", "mergePullRequest"): "MergePullRequestPayload",
        ("Mutation", "addComment"): "AddCommentPayload",
        ("CreateIssuePayload", "issue"): "Issue",
        ("CloseIssuePayload", "issue"): "Issue",
        ("CreatePullRequestPayload", "pullRequest"): "PullRequest",
        ("MergePullRequestPayload", "pullRequest"): "PullRequest",
        ("AddCommentPayload", "commentEdge"): "IssueCommentEdge",
        ("IssueCommentEdge", "node"): "IssueComment",
        ("__Type", "fields"): "__Field",
        ("__Type", "enumValues"): "__EnumValue",
    }
    return mapping.get((parent, field))


def _typename_of(node):
    type_name, value = node
    if type_name == "Repository":
        return "Repository"
    return type_name
