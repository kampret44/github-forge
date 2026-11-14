"""REST v3 surface (/api/v3). Pure functions over the Store.

Every reply is application/json with a non-empty X-GitHub-Request-Id.
Errors are {"message": <string>}. Auth: Bearer token must equal the trial token
(MAKEFUN_SHOP_TOKEN); missing/wrong -> 401. A repo under any owner other than the
token's login (adaiversen) -> 404, never 403.
"""
import json
from urllib.parse import unquote, urlsplit, parse_qs

from . import util
from .store import parse_commit

JSON = "application/json"


class Ctx:
    def __init__(self, store, token: str, base_url: str):
        self.store = store
        self.token = token  # trial token; "" means "any Bearer present is ok"
        self.base_url = base_url.rstrip("/")
        self.req_ids = util.RequestIdGen()

    def authed(self, headers: dict) -> bool:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        presented = auth[7:].strip()
        if not presented:
            return False
        if self.token:
            return presented == self.token
        return True


def _json_body(body: bytes):
    try:
        if not body:
            return {}, None
        return json.loads(body.decode("utf-8")), None
    except Exception:
        return None, "malformed JSON body"


def _qs(raw_path: str):
    parts = urlsplit(raw_path)
    path = unquote(parts.path)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    return path, query


class Rest:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.store = ctx.store

    # ---- response helpers -------------------------------------------
    def _resp(self, status: int, obj, extra=None):
        body = b"" if status == 204 else (json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        headers = [("content-type", JSON), ("x-github-request-id", self.ctx.req_ids.next())]
        if extra:
            headers += extra
        return status, headers, body

    def _err(self, status: int, message: str):
        return self._resp(status, {"message": message})

    # ---- repo JSON ----------------------------------------------------
    def repo_json(self, repo) -> dict:
        return {
            "id": repo.database_id,
            "name": repo.name,
            "full_name": repo.owner + "/" + repo.name,
            "private": repo.private,
            "description": repo.description,
            "default_branch": repo.default_branch,
            "owner": {"login": repo.owner, "id": util.VIEWER_ID if repo.owner == util.VIEWER_LOGIN else 1002},
            "html_url": self.ctx.base_url + "/" + repo.owner + "/" + repo.name,
            "clone_url": self.ctx.base_url + "/" + repo.owner + "/" + repo.name + ".git",
            "git_url": self.ctx.base_url + "/" + repo.owner + "/" + repo.name + ".git",
            "ssh_url": "git@example.invalid:" + repo.owner + "/" + repo.name + ".git",
            "has_issues": True,
            "has_projects": True,
            "has_wiki": False,
        }

    def issue_json(self, repo, iss: dict) -> dict:
        return {
            "id": repo.database_id * 1000000 + iss["number"],
            "number": iss["number"],
            "title": iss["title"],
            "body": iss["body"],
            "state": iss["state"],
            "labels": [{"name": n} for n in iss["labels"]],
            "user": {"login": util.VIEWER_LOGIN},
        }

    def pr_json(self, repo, pr: dict) -> dict:
        out = {
            "id": repo.database_id * 1000000 + pr["number"],
            "number": pr["number"],
            "title": pr["title"],
            "body": pr["body"],
            "state": pr["state"],
            "head": {"ref": pr["head_ref"], "sha": pr["head_sha"]},
            "base": {"ref": pr["base_ref"], "sha": pr["base_sha"]},
            "merged": pr["merged"],
        }
        if pr["merged"]:
            out["merged_at"] = util.MERGED_AT
            out["merge_commit_sha"] = pr["merge_commit_sha"]
        return out

    # ---- routing -------------------------------------------------------
    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        path, query = _qs(raw_path)
        if not self.ctx.authed(headers):
            return self._err(401, "Requires authentication")
        if not path.startswith("/api/v3"):
            return self._err(404, "Not Found")
        sub = path[len("/api/v3"):] or "/"
        parts = [p for p in sub.split("/") if p != ""]

        if method == "GET" and sub in ("/", ""):
            return self._resp(200, {
                "current_user_url": "/api/v3/user",
                "rate_limit_url": "/api/v3/rate_limit",
                "repository_url": "/api/v3/repos/{owner}/{repo}",
            }, [("x-oauth-scopes", "repo, read:org")])
        if method == "GET" and parts == ["meta"]:
            return self._resp(200, {"installed_version": "1.0.0", "verifiable_password_authentication": True})
        if method == "GET" and parts == ["user"]:
            return self._resp(200, {"login": util.VIEWER_LOGIN, "id": util.VIEWER_ID,
                                    "name": "ada iversen", "email": "adaiversen@example.invalid",
                                    "type": "User"})
        if parts[:1] == ["user"] and parts[1:2] == ["repos"]:
            if method == "GET":
                return self._list_user_repos(query)
            if method == "POST":
                return self._create_repo(body)
            return self._err(404, "Not Found")
        if parts[:1] == ["repos"] and len(parts) >= 3:
            return self._repo_routes(method, parts[1:], query, body)
        return self._err(404, "Not Found")

    # ---- user repos ------------------------------------------------------
    def _list_user_repos(self, query):
        try:
            per_page = max(1, min(100, int(query.get("per_page", "30"))))
        except ValueError:
            per_page = 30
        try:
            page = max(1, int(query.get("page", "1")))
        except ValueError:
            page = 1
        repos = self.store.repos_of(util.VIEWER_LOGIN)
        total = len(repos)
        start = (page - 1) * per_page
        chunk = repos[start:start + per_page]
        extra = []
        if start + per_page < total:
            nxt = "%s/api/v3/user/repos?per_page=%d&page=%d" % (self.ctx.base_url, per_page, page + 1)
            extra.append(("link", '<%s>; rel="next"' % nxt))
        return self._resp(200, [self.repo_json(r) for r in chunk], extra)

    def _create_repo(self, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return self._err(422, "name is required")
        private = bool(data.get("private", False))
        description = data.get("description", "") or ""
        repo = self.store.create_repo(util.VIEWER_LOGIN, name, private, description)
        if repo is None:
            return self._err(422, "Repository already exists")
        return self._resp(201, self.repo_json(repo))

    # ---- repo-scoped routes ----------------------------------------------
    def _lookup_repo(self, owner: str, name: str):
        if owner != util.VIEWER_LOGIN:
            return None
        return self.store.get_repo(owner, name)

    def _repo_routes(self, method, parts, query, body):
        owner, name = parts[0], parts[1]
        rest = parts[2:]
        repo = self._lookup_repo(owner, name)
        if repo is None:
            return self._err(404, "Not Found")
        if not rest:
            if method == "GET":
                return self._resp(200, self.repo_json(repo))
            if method == "PATCH":
                data, err = _json_body(body)
                if err:
                    return self._err(400, err)
                if "description" in data:
                    repo.description = data["description"] or ""
                    repo._repo_json_cache = None
                return self._resp(200, self.repo_json(repo))
            if method == "DELETE":
                self.store.delete_repo(owner, name)
                return self._resp(204, None)
            return self._err(404, "Not Found")
        if rest == ["branches"] and method == "GET":
            out = [{"name": ref[len("refs/heads/"):], "commit": {"sha": sha}}
                   for ref, sha in sorted(repo.refs.items()) if ref.startswith("refs/heads/")]
            return self._resp(200, out)
        if rest == ["commits"] and method == "GET":
            return self._list_commits(repo, query)
        if rest[:2] == ["git", "refs"] and method == "POST":
            return self._create_ref(repo, body)
        if rest[:1] == ["contents"]:
            return self._contents(method, repo, rest[1:], query, body)
        if rest == ["labels"] and method == "POST":
            return self._create_label(repo, body)
        if rest[:1] == ["issues"]:
            return self._issues(method, repo, rest[1:], query, body)
        if rest[:1] == ["pulls"]:
            return self._pulls(method, repo, rest[1:], query, body)
        return self._err(404, "Not Found")

    def _create_ref(self, repo, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        ref = data.get("ref", "")
        sha = data.get("sha", "")
        if not ref or not sha:
            return self._err(422, "ref and sha are required")
        if not repo.has_object(sha):
            return self._err(422, "object does not exist")
        repo.refs[ref] = sha
        repo.invalidate_caches()
        return self._resp(201, {"ref": ref, "object": {"sha": sha, "type": "commit"}})

    def _list_commits(self, repo, query):
        ref = query.get("sha", repo.default_branch)
        tip = repo.refs.get("refs/heads/" + ref) or repo.refs.get(ref)
        out = []
        seen = set()
        stack = [tip] if tip else []
        while stack:
            cur = stack.pop(0)
            if cur is None or cur in seen:
                continue
            seen.add(cur)
            obj = repo.get_object(cur)
            if obj is None or obj[0] != "commit":
                continue
            _, parents, message = parse_commit(obj[1])
            out.append({"sha": cur, "commit": {"message": message,
                        "author": {"name": "Forge", "email": "forge@localhost"},
                        "committer": {"name": "Forge", "email": "forge@localhost"}}})
            stack.extend(parents)
        return self._resp(200, out)

    # ---- contents ----------------------------------------------------------
    def _contents(self, method, repo, parts, query, body):
        path = "/".join(parts)
        if method == "PUT":
            return self._contents_put(repo, path, body)
        if method == "DELETE":
            return self._contents_delete(repo, path, body)
        if method == "GET":
            return self._contents_get(repo, path, query)
        return self._err(404, "Not Found")

    def _branch_flat(self, repo, branch):
        tip = repo.refs.get("refs/heads/" + branch)
        if tip is None:
            return {}, None
        return repo.tree_flat(tip), tip

    def _contents_put(self, repo, path, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        if "content" not in data:
            return self._err(422, "content is required")
        try:
            content = util.b64decode(data["content"])
        except Exception:
            return self._err(422, "content is not valid base64")
        branch = data.get("branch") or repo.default_branch
        message = data.get("message", "") or ""
        flat, tip = self._branch_flat(repo, branch)
        blob_sha = None
        existing = flat.get(path)
        if existing is not None:
            blob_sha = util.git_object_id("blob", existing)
        given_sha = data.get("sha")
        if given_sha is not None and blob_sha is not None and given_sha.lower() != blob_sha.lower():
            return self._err(409, "sha does not match the current blob")
        is_new = existing is None
        new_blob_sha = repo.put_object("blob", content)
        new_flat = dict(flat)
        new_flat[path] = content
        tree_sha = repo.build_tree(new_flat)
        parents = [tip] if tip else []
        commit_sha = repo.make_commit(tree_sha, parents, message)
        repo.refs["refs/heads/" + branch] = commit_sha
        repo.invalidate_caches()
        status = 201 if is_new else 200
        return self._resp(status, {
            "content": {"name": path.rsplit("/", 1)[-1], "path": path, "sha": new_blob_sha,
                        "size": len(content), "encoding": "base64", "type": "file"},
            "commit": {"sha": commit_sha, "message": message},
        })

    def _contents_delete(self, repo, path, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        branch = data.get("branch") or repo.default_branch
        flat, tip = self._branch_flat(repo, branch)
        if path not in flat:
            return self._err(404, "Not Found")
        given_sha = data.get("sha")
        if given_sha is not None:
            cur = util.git_object_id("blob", flat[path])
            if given_sha.lower() != cur.lower():
                return self._err(409, "sha does not match the current blob")
        new_flat = dict(flat)
        del new_flat[path]
        tree_sha = repo.build_tree(new_flat)
        message = data.get("message", "") or ""
        commit_sha = repo.make_commit(tree_sha, [tip] if tip else [], message)
        repo.refs["refs/heads/" + branch] = commit_sha
        repo.invalidate_caches()
        return self._resp(200, {"commit": {"sha": commit_sha, "message": message}})

    def _contents_get(self, repo, path, query):
        branch = query.get("ref") or repo.default_branch
        flat, _ = self._branch_flat(repo, branch)
        if path in flat:
            content = flat[path]
            sha = util.git_object_id("blob", content)
            return self._resp(200, {
                "type": "file", "encoding": "base64", "size": len(content),
                "name": path.rsplit("/", 1)[-1], "path": path, "sha": sha,
                "content": util.b64encode(content),
            })
        # directory listing: immediate children of path ("" = root)
        prefix = path + "/" if path else ""
        entries = {}
        for p, content in flat.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if "/" in rest:
                head = rest.split("/", 1)[0]
                if head not in entries:
                    sub = {k: v for k, v in flat.items() if k.startswith(prefix + head + "/")}
                    entries[head] = {"type": "dir", "name": head, "path": prefix + head,
                                     "sha": repo.build_tree(sub, prefix + head + "/")}
            else:
                entries[rest] = {"type": "file", "name": rest, "path": p,
                                 "sha": util.git_object_id("blob", content)}
        if not entries:
            return self._err(404, "Not Found")
        return self._resp(200, [entries[k] for k in sorted(entries)])

    # ---- labels --------------------------------------------------------------
    def _create_label(self, repo, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return self._err(422, "name is required")
        color = data.get("color", "ededed") or "ededed"
        repo.labels[name] = {"name": name, "color": color}
        return self._resp(201, {"name": name, "color": color})

    # ---- issues ---------------------------------------------------------------
    def _issues(self, method, repo, parts, query, body):
        if not parts:
            if method == "POST":
                return self._create_issue(repo, body)
            if method == "GET":
                return self._list_issues(repo, query)
            return self._err(404, "Not Found")
        try:
            number = int(parts[0])
        except ValueError:
            return self._err(404, "Not Found")
        iss = next((i for i in repo.issues if i["number"] == number), None)
        if iss is None:
            return self._err(404, "Not Found")
        if len(parts) == 1:
            if method == "GET":
                return self._resp(200, self.issue_json(repo, iss))
            if method == "PATCH":
                data, err = _json_body(body)
                if err:
                    return self._err(400, err)
                if "state" in data:
                    iss["state"] = data["state"]
                if "title" in data:
                    iss["title"] = data["title"]
                if "body" in data:
                    iss["body"] = data["body"]
                repo.invalidate_issue_cache(number)
                return self._resp(200, self.issue_json(repo, iss))
            return self._err(404, "Not Found")
        if parts[1] == "comments":
            if method == "POST":
                return self._create_comment(repo, iss, body)
            if method == "GET":
                comments = repo.comments.get(number, [])
                return self._resp(200, [{"id": c["id"], "body": c["body"]} for c in comments])
            return self._err(404, "Not Found")
        return self._err(404, "Not Found")

    def _create_issue(self, repo, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        title = data.get("title")
        if not isinstance(title, str) or not title:
            return self._err(422, "title is required")
        labels = data.get("labels") or []
        for name in labels:
            if name not in repo.labels:
                return self._err(422, "label %s does not exist" % name)
        iss = {"number": repo.next_issue, "title": title,
               "body": data.get("body") or "", "state": "open",
               "labels": list(labels)}
        repo.next_issue += 1
        repo.issues.append(iss)
        return self._resp(201, self.issue_json(repo, iss))

    def _list_issues(self, repo, query):
        state = query.get("state", "open")
        items = repo.issues
        if state == "open":
            items = [i for i in items if i["state"] == "open"]
        elif state == "closed":
            items = [i for i in items if i["state"] == "closed"]
        if query.get("sort") == "created":
            if query.get("direction") == "desc":
                items = list(reversed(items))
        try:
            per_page = max(1, int(query.get("per_page", "30")))
        except ValueError:
            per_page = 30
        return self._resp(200, [self.issue_json(repo, i) for i in items[:per_page]])

    def _create_comment(self, repo, iss, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        text = data.get("body")
        if not isinstance(text, str):
            return self._err(422, "body is required")
        cid = repo.next_comment_id
        repo.next_comment_id += 1
        comment = {"id": cid, "body": text}
        repo.comments.setdefault(iss["number"], []).append(comment)
        return self._resp(201, {"id": cid, "body": text})

    # ---- pulls ------------------------------------------------------------------
    def _pulls(self, method, repo, parts, query, body):
        if not parts:
            if method == "POST":
                return self._create_pr(repo, body)
            if method == "GET":
                return self._list_prs(repo, query)
            return self._err(404, "Not Found")
        try:
            number = int(parts[0])
        except ValueError:
            return self._err(404, "Not Found")
        pr = next((p for p in repo.prs if p["number"] == number), None)
        if pr is None:
            return self._err(404, "Not Found")
        if len(parts) == 1:
            if method == "GET":
                return self._resp(200, self.pr_json(repo, pr))
            return self._err(404, "Not Found")
        if parts[1] == "files" and method == "GET":
            return self._resp(200, pr["files"])
        if parts[1] == "merge" and method == "PUT":
            return self._merge_pr(repo, pr, body)
        return self._err(404, "Not Found")

    def _diff_files(self, repo, base_sha, head_sha):
        base_flat = repo.tree_flat(base_sha) if base_sha else {}
        head_flat = repo.tree_flat(head_sha) if head_sha else {}
        files = []
        for path in sorted(set(base_flat) | set(head_flat)):
            old = base_flat.get(path)
            new = head_flat.get(path)
            if old == new:
                continue
            if old is None:
                status = "added"
                additions = _line_count(new)
                deletions = 0
            elif new is None:
                status = "removed"
                additions = 0
                deletions = _line_count(old)
            else:
                status = "modified"
                additions, deletions = _line_diff(old, new)
            files.append({"filename": path, "status": status,
                          "additions": additions, "deletions": deletions,
                          "changes": additions + deletions})
        return files

    def _create_pr(self, repo, body):
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        head_ref = data.get("head")
        base_ref = data.get("base")
        title = data.get("title")
        if not head_ref or not base_ref or not isinstance(title, str):
            return self._err(422, "head, base and title are required")
        head_sha = repo.ref_tip(head_ref)
        base_sha = repo.ref_tip(base_ref)
        if head_sha is None or base_sha is None:
            return self._err(422, "head or base branch does not exist")
        pr = {"number": repo.next_pr, "title": title,
              "body": data.get("body") or "", "state": "open",
              "head_ref": head_ref, "head_sha": head_sha,
              "base_ref": base_ref, "base_sha": base_sha,
              "merged": False, "merge_commit_sha": None,
              "files": self._diff_files(repo, base_sha, head_sha)}
        repo.next_pr += 1
        repo.prs.append(pr)
        return self._resp(201, self.pr_json(repo, pr))

    def _list_prs(self, repo, query):
        state = query.get("state")
        items = repo.prs
        if state != "all":
            items = [p for p in items if p["state"] == "open"]
        try:
            per_page = max(1, int(query.get("per_page", "30")))
        except ValueError:
            per_page = 30
        return self._resp(200, [self.pr_json(repo, p) for p in items[:per_page]])

    def _pr_conflicts(self, repo, pr) -> bool:
        """Three-way file merge conflict: a path the PR changes whose current base
        content differs from the PR's base content AND from the PR's head content."""
        base_flat = repo.tree_flat(pr["base_sha"]) if pr["base_sha"] else {}
        head_flat = repo.tree_flat(pr["head_sha"]) if pr["head_sha"] else {}
        cur_tip = repo.ref_tip(pr["base_ref"])
        cur_flat = repo.tree_flat(cur_tip) if cur_tip else {}
        for path in set(base_flat) | set(head_flat):
            old = base_flat.get(path)
            new = head_flat.get(path)
            if old == new:
                continue
            cur = cur_flat.get(path)
            if cur != old and cur != new:
                return True
        return False

    def _merge_pr(self, repo, pr, body):
        if pr["merged"]:
            return self._err(405, "Pull request already merged")
        if self._pr_conflicts(repo, pr):
            return self._err(405, "Merge conflict")
        data, err = _json_body(body)
        if err:
            return self._err(400, err)
        base_flat = repo.tree_flat(pr["base_sha"]) if pr["base_sha"] else {}
        head_flat = repo.tree_flat(pr["head_sha"]) if pr["head_sha"] else {}
        cur_tip = repo.ref_tip(pr["base_ref"])
        cur_flat = dict(repo.tree_flat(cur_tip)) if cur_tip else {}
        for path in set(base_flat) | set(head_flat):
            old = base_flat.get(path)
            new = head_flat.get(path)
            if old == new:
                continue
            if new is None:
                cur_flat.pop(path, None)
            else:
                cur_flat[path] = new
        tree_sha = repo.build_tree(cur_flat)
        message = "Merge pull request #%d from %s" % (pr["number"], pr["head_ref"])
        merge_sha = repo.make_commit(tree_sha, [cur_tip] if cur_tip else [], message)
        repo.refs["refs/heads/" + pr["base_ref"]] = merge_sha
        repo.invalidate_caches()
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merge_commit_sha"] = merge_sha
        return self._resp(200, {"merged": True, "sha": merge_sha, "message": message})


def _line_count(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _line_diff(old: bytes, new: bytes):
    import difflib
    old_lines = old.decode("utf-8", "surrogateescape").splitlines()
    new_lines = new.decode("utf-8", "surrogateescape").splitlines()
    adds = dels = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels
