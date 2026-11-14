"""In-memory store for the forge: repos, git objects/refs, issues, PRs, labels.

One Store per process. All state is plain dicts/lists; every id is a deterministic
function of content or a per-repo counter, so a replay leaves identical state.
No wall clock anywhere: server-made commits carry a fixed timestamp.
"""
import hashlib

from . import util


def _tree_entries(flat: dict, prefix: str = ""):
    """Split a flat path->bytes map into (name, is_dir, subtree-or-bytes) at one level."""
    dirs = {}
    files = {}
    plen = len(prefix)
    for path, data in flat.items():
        rest = path[plen:]
        if "/" in rest:
            head, _ = rest.split("/", 1)
            dirs.setdefault(head, {})[path] = data
        else:
            files[rest] = data
    return dirs, files


class Repo:
    def __init__(self, owner: str, name: str, database_id: int, private: bool, description: str = ""):
        self.owner = owner
        self.name = name
        self.database_id = database_id
        self.private = private
        self.description = description
        self.default_branch = util.DEFAULT_BRANCH
        # sha hex -> (type, body) ; body excludes the "<type> <len>\0" header
        self.objects: dict = {}
        # compressed full-object bytes cache: sha -> zlib bytes
        self._compressed: dict = {}
        # refname (e.g. "refs/heads/main") -> commit sha hex
        self.refs: dict = {}
        # issues / prs are lists of dicts in creation order
        self.issues: list = []
        self.prs: list = []
        self.labels: dict = {}
        self.comments: dict = {}  # issue number -> list of comment dicts
        self.next_issue = 1
        self.next_pr = 1
        self.next_comment_id = 1
        # caches
        self._flat_cache: dict = {}   # commit sha -> flat path->bytes map
        self._pack_cache: dict = {}   # (tip, depth) -> pack bytes
        self._issue_json_cache: dict = {}  # issue number -> serialized JSON bytes
        self._repo_json_cache = None  # serialized repo JSON bytes

    # ---- git objects -------------------------------------------------
    def put_object(self, obj_type: str, body: bytes) -> str:
        sha = util.git_object_id(obj_type, body)
        if sha not in self.objects:
            self.objects[sha] = (obj_type, body)
        return sha

    def get_object(self, sha: str):
        return self.objects.get(sha)

    def has_object(self, sha: str) -> bool:
        return sha in self.objects

    def compressed_object(self, sha: str) -> bytes:
        data = self._compressed.get(sha)
        if data is None:
            obj_type, body = self.objects[sha]
            data = util.compress(util.git_object_bytes(obj_type, body))
            self._compressed[sha] = data
        return data

    # ---- trees -------------------------------------------------------
    def build_tree(self, flat: dict, prefix: str = "") -> str:
        """Build (and store) the git tree for a flat path->bytes map at `prefix`."""
        dirs, files = _tree_entries(flat, prefix)
        entries = []
        for name in sorted(files):
            blob_sha = self.put_object("blob", files[name])
            entries.append((name, False, blob_sha))
        for name in sorted(dirs):
            sub = self.build_tree(flat, prefix + name + "/")
            entries.append((name, True, sub))
        # git sorts tree entries with directories as if their name had a trailing "/"
        entries.sort(key=lambda e: e[0] + ("/" if e[1] else ""))
        body = bytearray()
        for name, is_dir, sha in entries:
            mode = b"40000" if is_dir else b"100644"
            body += mode + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
        return self.put_object("tree", bytes(body))

    def tree_flat(self, commit_sha: str) -> dict:
        """Flat path->bytes map of a commit's tree (memoized per commit)."""
        flat = self._flat_cache.get(commit_sha)
        if flat is not None:
            return flat
        flat = {}
        obj = self.objects.get(commit_sha)
        if obj is None or obj[0] != "commit":
            self._flat_cache[commit_sha] = flat
            return flat
        tree_sha = parse_commit(obj[1])[0]
        self._walk_tree(tree_sha, "", flat)
        self._flat_cache[commit_sha] = flat
        return flat

    def _walk_tree(self, tree_sha: str, prefix: str, flat: dict) -> None:
        obj = self.objects.get(tree_sha)
        if obj is None or obj[0] != "tree":
            return
        body = obj[1]
        i = 0
        n = len(body)
        while i < n:
            sp = body.index(b" ", i)
            mode = body[i:sp]
            nul = body.index(b"\x00", sp)
            name = body[sp + 1:nul].decode("utf-8", "surrogateescape")
            sha = body[nul + 1:nul + 21].hex()
            if mode == b"40000":
                self._walk_tree(sha, prefix + name + "/", flat)
            else:
                blob = self.objects.get(sha)
                if blob is not None:
                    flat[prefix + name] = blob[1]
            i = nul + 21

    # ---- commits -----------------------------------------------------
    def make_commit(self, tree_sha: str, parents: list, message: str) -> str:
        lines = ["tree " + tree_sha]
        for p in parents:
            lines.append("parent " + p)
        lines.append("author " + util.GIT_AUTHOR + " %d +0000" % util.GIT_EPOCH)
        lines.append("committer " + util.GIT_AUTHOR + " %d +0000" % util.GIT_EPOCH)
        body = ("\n".join(lines) + "\n\n" + message).encode("utf-8")
        return self.put_object("commit", body)

    def commit_parents(self, commit_sha: str) -> list:
        obj = self.objects.get(commit_sha)
        if obj is None or obj[0] != "commit":
            return []
        return parse_commit(obj[1])[1]

    def is_ancestor(self, anc: str, desc: str) -> bool:
        """True if anc is an ancestor of desc (or equal). Walks parents."""
        if anc == desc:
            return True
        seen = set()
        stack = [desc]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for p in self.commit_parents(cur):
                if p == anc:
                    return True
                stack.append(p)
        return False

    def ref_tip(self, branch: str):
        return self.refs.get("refs/heads/" + branch)

    def invalidate_caches(self) -> None:
        self._pack_cache.clear()
        self._repo_json_cache = None

    def invalidate_issue_cache(self, number: int) -> None:
        self._issue_json_cache.pop(number, None)


def parse_commit(body: bytes):
    """Parse a commit body -> (tree_sha, [parent shas], message)."""
    text = body.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    tree = ""
    parents = []
    for line in head.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
    return tree, parents, message


class Store:
    def __init__(self) -> None:
        self.repos: dict = {}  # (owner, name) -> Repo
        self._next_dbid = 1
        self._dbid_by_key: dict = {}

    def create_repo(self, owner: str, name: str, private: bool, description: str = ""):
        key = (owner, name)
        if key in self.repos:
            return None
        repo = Repo(owner, name, self._next_dbid, private, description)
        self._next_dbid += 1
        self.repos[key] = repo
        return repo

    def get_repo(self, owner: str, name: str):
        return self.repos.get((owner, name))

    def repo_by_global_id(self, gid):
        if not gid or not isinstance(gid, str):
            return None
        for repo in self.repos.values():
            if util.repo_global_id(repo.owner, repo.name) == gid:
                return repo
        return None

    def issue_by_global_id(self, gid):
        if not gid or not isinstance(gid, str):
            return None, None
        for repo in self.repos.values():
            for iss in repo.issues:
                if util.issue_global_id(repo.owner, repo.name, iss["number"]) == gid:
                    return repo, iss
        return None, None

    def pr_by_global_id(self, gid):
        if not gid or not isinstance(gid, str):
            return None, None
        for repo in self.repos.values():
            for pr in repo.prs:
                if util.pr_global_id(repo.owner, repo.name, pr["number"]) == gid:
                    return repo, pr
        return None, None

    def delete_repo(self, owner: str, name: str) -> bool:
        return self.repos.pop((owner, name), None) is not None

    def repos_of(self, owner: str) -> list:
        return [r for (o, _), r in self.repos.items() if o == owner]

    def repo_by_global_id(self, gid: str):
        for repo in self.repos.values():
            if util.repo_global_id(repo.owner, repo.name) == gid:
                return repo
        return None

    def issue_by_global_id(self, gid: str):
        for repo in self.repos.values():
            for iss in repo.issues:
                if util.issue_global_id(repo.owner, repo.name, iss["number"]) == gid:
                    return repo, iss
        return None, None

    def pr_by_global_id(self, gid: str):
        for repo in self.repos.values():
            for pr in repo.prs:
                if util.pr_global_id(repo.owner, repo.name, pr["number"]) == gid:
                    return repo, pr
        return None, None

    def sha1(self, data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()
