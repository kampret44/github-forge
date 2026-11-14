"""Git smart HTTP: pkt-lines, packfiles, upload-pack v2, receive-pack v0.

No deltas are ever GENERATED (full objects only); deltas ARE applied on receive
(OFS_DELTA + REF_DELTA) so real git clients can push.
Deterministic: pack object order is fixed (type rank, then sha).
"""
import hashlib
import struct
import zlib

from . import util

ZERO_OID = "0" * 40
MAX_PKT_PAYLOAD = 65516  # pkt-line total <= 65520 incl. 4-byte length

TYPE_IDS = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
ID_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


# ---- pkt-line -----------------------------------------------------------
def pkt_line(payload: bytes) -> bytes:
    n = len(payload) + 4
    return ("%04x" % n).encode("ascii") + payload


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    return b"0001"


def parse_pkt_lines(data: bytes):
    """Parse a pkt-line stream -> list of ('data', bytes) | ('flush',) | ('delim',).
    Returns (packets, rest_offset). Raises ValueError on malformed input."""
    packets = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            raise ValueError("truncated pkt-line length")
        head = data[i:i + 4]
        if head == b"0000":
            packets.append(("flush", None))
            i += 4
            continue
        if head == b"0001":
            packets.append(("delim", None))
            i += 4
            continue
        if head == b"0002":
            packets.append(("response-end", None))
            i += 4
            continue
        try:
            size = int(head.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % head)
        if size < 4 or i + size > n:
            raise ValueError("pkt-line size out of range")
        packets.append(("data", data[i + 4:i + size]))
        i += size
    return packets, i


def parse_pkt_stream(data: bytes):
    """Parse pkt-lines until the first flush/delim; return (data_lines, bytes_consumed).
    Bytes after the terminator (e.g. a binary packfile) are NOT parsed.
    Raises ValueError on malformed input."""
    lines = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            raise ValueError("truncated pkt-line length")
        head = data[i:i + 4]
        if head in (b"0000", b"0001", b"0002"):
            return lines, i + 4
        try:
            size = int(head.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % head)
        if size < 4 or i + size > n:
            raise ValueError("pkt-line size out of range")
        lines.append(data[i + 4:i + size])
        i += size
    return lines, n


# ---- deltas ---------------------------------------------------------------
def _read_varint(data: bytes, off: int):
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, off


def apply_delta(base: bytes, delta: bytes) -> bytes:
    src_size, off = _read_varint(delta, 0)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    tgt_size, off = _read_varint(delta, 0)
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[off] << (8 * bit)
                    off += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    cp_size |= delta[off] << (8 * bit)
                    off += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[off:off + op]
            off += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


# ---- packfiles --------------------------------------------------------------
def read_pack(pack: bytes, resolve_base):
    """Parse a packfile. resolve_base(sha) -> (type, body) for REF_DELTA bases
    held outside the pack. Returns dict sha -> (type, body).
    Raises ValueError on corruption."""
    if len(pack) < 12 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    version, count = struct.unpack(">II", pack[4:12])
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    # verify trailer
    trailer = pack[-20:]
    if hashlib.sha1(pack[:-20]).digest() != trailer:
        raise ValueError("pack checksum mismatch")
    entries = []  # (offset, type_id_or_delta, size, extra)
    off = 12
    for _ in range(count):
        start = off
        b = pack[off]
        off += 1
        type_id = (b >> 4) & 7
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = pack[off]
            off += 1
            size |= (b & 0x7F) << shift
            shift += 7
        extra = None
        if type_id == 6:  # OFS_DELTA
            b = pack[off]
            off += 1
            base_off = b & 0x7F
            while b & 0x80:
                b = pack[off]
                off += 1
                base_off = ((base_off + 1) << 7) | (b & 0x7F)
            extra = start - base_off
        elif type_id == 7:  # REF_DELTA
            extra = pack[off:off + 20].hex()
            off += 20
        # zlib stream: decompress incrementally to find its end
        decomp = zlib.decompressobj()
        chunk = pack[off:]
        body = decomp.decompress(chunk, size)
        consumed = len(chunk) - len(decomp.unused_data)
        off += consumed
        entries.append((start, type_id, size, extra, body))
    by_offset = {}
    result = {}
    # first pass: full objects
    for (start, type_id, size, extra, body) in entries:
        if type_id in ID_TYPES:
            sha = util.git_object_id(ID_TYPES[type_id], body)
            by_offset[start] = (ID_TYPES[type_id], body, sha)
            result[sha] = (ID_TYPES[type_id], body)
    # delta passes (bases may be deltas themselves; iterate to fixpoint)
    pending = [e for e in entries if e[1] in (6, 7)]
    guard = 0
    while pending and guard < 100:
        guard += 1
        nxt = []
        for (start, type_id, size, extra, body) in pending:
            if type_id == 6:
                base = by_offset.get(extra)
            else:
                base = result.get(extra)
                base = (base[0], base[1], extra) if base else None
            if base is None:
                nxt.append((start, type_id, size, extra, body))
                continue
            resolved = apply_delta(base[1], body)
            sha = util.git_object_id(base[0], resolved)
            by_offset[start] = (base[0], resolved, sha)
            result[sha] = (base[0], resolved)
        if len(nxt) == len(pending):
            raise ValueError("unresolvable delta base")
        pending = nxt
    if pending:
        raise ValueError("delta resolution did not converge")
    return result


def _pack_entry_header(type_id: int, size: int) -> bytes:
    out = bytearray()
    first = (type_id << 4) | (size & 0x0F)
    size >>= 4
    if size:
        first |= 0x80
    out.append(first)
    while size:
        b = size & 0x7F
        size >>= 7
        if size:
            b |= 0x80
        out.append(b)
    return bytes(out)


def write_pack(objects) -> bytes:
    """objects: iterable of (type_str, body). Full objects only, no deltas.
    Deterministic order: (type rank, sha)."""
    items = []
    for obj_type, body in objects:
        sha = util.git_object_id(obj_type, body)
        items.append((TYPE_IDS[obj_type], sha, obj_type, body))
    items.sort(key=lambda t: (t[0], t[1]))
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(items))
    for type_id, _sha, obj_type, body in items:
        out += _pack_entry_header(type_id, len(body))
        out += zlib.compress(body, 6)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# ---- reachability ------------------------------------------------------------
def _parse_tag_target(body: bytes):
    """The object a tag object points at (first line: 'object <sha>')."""
    for line in body.split(b"\n"):
        if line.startswith(b"object "):
            return line[7:].decode("ascii").strip()
        if line == b"":
            break
    return None


def closure(repo, commits, depth=None):
    """All objects reachable from `commits`.
    depth None: full closure. depth N: commits within N-1 edges of a want
    (depth 1 = the wants themselves), plus their trees/blobs.
    Tag objects are included and followed to their target commit at the
    same distance.
    Returns (objects: dict sha -> (type, body), boundary: set of commit shas at
    the depth limit)."""
    objects = {}
    boundary = set()
    seen_commits = set()
    queue = [(c, 0) for c in commits]
    while queue:
        sha, dist = queue.pop(0)
        if sha in seen_commits:
            continue
        seen_commits.add(sha)
        obj = repo.get_object(sha)
        if obj is None:
            continue
        if depth is not None and dist >= depth:
            continue
        objects[sha] = obj
        if obj[0] == "tag":
            target = _parse_tag_target(obj[1])
            if target:
                queue.append((target, dist))
            continue
        if obj[0] != "commit":
            continue
        tree_sha, parents, _ = _parse_commit(obj[1])
        _collect_tree(repo, tree_sha, objects)
        if depth is not None and dist == depth - 1:
            boundary.add(sha)
            continue
        for p in parents:
            queue.append((p, dist + 1))
    return objects, boundary


def _parse_commit(body: bytes):
    from .store import parse_commit
    return parse_commit(body)


def _collect_tree(repo, tree_sha, objects):
    stack = [tree_sha]
    while stack:
        sha = stack.pop()
        if sha in objects:
            continue
        obj = repo.get_object(sha)
        if obj is None:
            continue
        objects[sha] = obj
        if obj[0] != "tree":
            continue
        body = obj[1]
        i = 0
        n = len(body)
        while i < n:
            sp = body.index(b" ", i)
            mode = body[i:sp]
            nul = body.index(b"\x00", sp)
            child = body[nul + 1:nul + 21].hex()
            if mode == b"40000":
                stack.append(child)
            else:
                child_obj = repo.get_object(child)
                if child_obj is not None:
                    objects[child] = child_obj
            i = nul + 21


def tag_target(repo, tag_sha: str):
    """Peel a tag object -> target sha (follows tag chains)."""
    seen = set()
    cur = tag_sha
    while cur not in seen:
        seen.add(cur)
        obj = repo.get_object(cur)
        if obj is None or obj[0] != "tag":
            return cur
        for line in obj[1].decode("utf-8", "surrogateescape").split("\n"):
            if line.startswith("object "):
                cur = line[7:].strip()
                break
        else:
            return cur
    return cur


# ---- the git surface -----------------------------------------------------------
class Git:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ctx.store

    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        from urllib.parse import unquote, urlsplit
        parts = urlsplit(raw_path)
        path = unquote(parts.path)
        query = parts.query
        if not path.endswith(".git") and ".git/" not in path:
            return 404, [("content-type", "application/json")], b'{"message":"Not Found"}'
        # /{owner}/{repo}.git[/...]
        seg = path.lstrip("/").split("/")
        if len(seg) < 2 or not seg[1].endswith(".git"):
            return 404, [("content-type", "application/json")], b'{"message":"Not Found"}'
        owner, repo_name = seg[0], seg[1][:-4]
        rest = "/" + "/".join(seg[2:])
        if not self.ctx.authed(headers):
            return 401, [("content-type", "application/json")], b'{"message":"Requires authentication"}'
        repo = self.store.get_repo(owner, repo_name)
        if owner != util.VIEWER_LOGIN or repo is None:
            return 404, [("content-type", "application/json")], b'{"message":"Not Found"}'
        if method == "GET" and rest == "/info/refs":
            service = None
            for kv in query.split("&"):
                if kv.startswith("service="):
                    service = kv[8:]
            if service == "git-upload-pack":
                return self._info_refs_upload(repo, headers)
            if service == "git-receive-pack":
                return self._info_refs_receive(repo)
            return 400, [("content-type", "application/json")], b'{"message":"unknown service"}'
        if method == "POST" and rest == "/git-upload-pack":
            return self._upload_pack(repo, headers, body)
        if method == "POST" and rest == "/git-receive-pack":
            return self._receive_pack(repo, body)
        return 404, [("content-type", "application/json")], b'{"message":"Not Found"}'

    # ---- advertisements -------------------------------------------------------
    def _ref_lines_v0(self, repo, capabilities: str):
        """v0 ref lines; capabilities go after NUL on the first line."""
        lines = []
        refs = sorted(repo.refs.items())
        head_target = repo.default_branch
        head_sha = repo.refs.get("refs/heads/" + head_target)
        if head_sha:
            lines.append("%s HEAD\x00%s\n" % (head_sha, capabilities))
        elif refs:
            lines.append("%s %s\x00%s\n" % (refs[0][1], refs[0][0], capabilities))
        else:
            lines.append("%s capabilities^{}\x00%s\n" % (ZERO_OID, capabilities))
            return lines
        for name, sha in refs:
            if head_sha and name == "refs/heads/" + head_target and lines and lines[0].startswith(head_sha + " HEAD"):
                lines.append("%s %s\n" % (sha, name))
            else:
                lines.append("%s %s\n" % (sha, name))
        return lines

    def _info_refs_upload(self, repo, headers):
        if headers.get("git-protocol") == "version=2":
            body = b"".join([
                pkt_line(b"# service=git-upload-pack\n"),
                pkt_flush(),
                pkt_line(b"version 2\n"),
                pkt_line(b"ls-refs=unborn\n"),
                pkt_line(b"fetch=shallow\n"),
                pkt_line(b"object-format=sha1\n"),
                pkt_line(b"agent=github-forge/1.0\n"),
                pkt_flush(),
            ])
            return 200, [("content-type", "application/x-git-upload-pack-advertisement")], body
        caps = ("symref=HEAD:refs/heads/%s multi_ack side-band-64k ofs-delta shallow "
                "object-format=sha1 agent=github-forge/1.0" % repo.default_branch)
        out = bytearray()
        out += pkt_line(b"# service=git-upload-pack\n")
        out += pkt_flush()
        for line in self._ref_lines_v0(repo, caps):
            out += pkt_line(line.encode("utf-8"))
        out += pkt_flush()
        return 200, [("content-type", "application/x-git-upload-pack-advertisement")], bytes(out)

    def _info_refs_receive(self, repo):
        caps = "report-status side-band-64k delete-refs ofs-delta agent=github-forge/1.0"
        out = bytearray()
        out += pkt_line(b"# service=git-receive-pack\n")
        out += pkt_flush()
        for line in self._ref_lines_v0(repo, caps):
            out += pkt_line(line.encode("utf-8"))
        out += pkt_flush()
        return 200, [("content-type", "application/x-git-receive-pack-advertisement")], bytes(out)

    # ---- upload-pack v2 ---------------------------------------------------------
    def _upload_pack(self, repo, headers, body: bytes):
        try:
            packets, _ = parse_pkt_lines(body)
        except ValueError:
            return 400, [("content-type", "application/json")], b'{"message":"malformed pkt-line"}'
        # v2 requests carry their arguments across a delim (0001) section;
        # flatten every data packet, in order.
        lines = [p for kind, p in packets if kind == "data"]
        text = [ln.decode("utf-8", "surrogateescape") for ln in lines]
        if not text:
            return 400, [("content-type", "application/json")], b'{"message":"empty request"}'
        command = text[0].strip()
        args = text[1:]
        if command.startswith("command=ls-refs"):
            return self._ls_refs(repo, args)
        if command.startswith("command=fetch"):
            return self._fetch(repo, args)
        return 400, [("content-type", "application/json")], b'{"message":"unknown command"}'

    def _ls_refs(self, repo, args):
        peel = False
        prefixes = []
        for arg in args:
            a = arg.strip()
            if a == "peel":
                peel = True
            elif a.startswith("ref-prefix "):
                prefixes.append(a[len("ref-prefix "):])
        out = bytearray()
        head_sha = repo.refs.get("refs/heads/" + repo.default_branch)
        refs = sorted(repo.refs.items())

        def allowed(name: str) -> bool:
            return not prefixes or any(name.startswith(p) for p in prefixes)

        # The HEAD line is a ref named "HEAD": emit it exactly when the
        # prefix filter allows it (no prefixes, or a prefix of "HEAD"
        # such as the "ref-prefix HEAD" real git clients send on clone).
        if head_sha and allowed("HEAD"):
            out += pkt_line(("%s HEAD symref-target:refs/heads/%s\n" % (head_sha, repo.default_branch)).encode("utf-8"))
        for name, sha in refs:
            if not allowed(name):
                continue
            line = "%s %s" % (sha, name)
            if peel:
                obj = repo.get_object(sha)
                if obj is not None and obj[0] == "tag":
                    line += " peeled:" + tag_target(repo, sha)
            out += pkt_line((line + "\n").encode("utf-8"))
        out += pkt_flush()
        return 200, [("content-type", "application/x-git-upload-pack-result")], bytes(out)

    def _fetch(self, repo, args):
        wants = []
        haves = []
        shallow_in = []
        depth = None
        for arg in args:
            a = arg.strip()
            if a.startswith("want "):
                wants.append(a[5:].strip())
            elif a.startswith("have "):
                haves.append(a[5:].strip())
            elif a.startswith("shallow "):
                shallow_in.append(a[8:].strip())
            elif a.startswith("deepen "):
                try:
                    depth = int(a[7:].strip())
                except ValueError:
                    depth = None
        # objects to send: closure(wants, depth) minus closure(haves).
        # A shallow-listed have is held WITHOUT its parents (the client has
        # the shallow commit object itself, not its history), so subtract
        # only the commit + its tree for those.
        send = {}
        boundary = set()
        if wants:
            send, boundary = closure(repo, wants, depth)
            if haves:
                held = {}
                full_haves = [h for h in haves if h not in shallow_in]
                if full_haves:
                    held, _ = closure(repo, full_haves, None)
                for s in shallow_in:
                    if s in haves:
                        sh, _ = closure(repo, [s], 1)
                        held.update(sh)
                for sha in held:
                    send.pop(sha, None)
        cache_key = None
        if depth is None and not haves and len(wants) == 1:
            cache_key = (repo.name, wants[0], None)
        pack = None
        if wants:
            if cache_key is not None:
                pack = repo._pack_cache.get(cache_key)
            if pack is None:
                pack = write_pack(list(send.values()))
                if cache_key is not None:
                    repo._pack_cache[cache_key] = pack
        out = bytearray()
        if haves:
            out += pkt_line(b"acknowledgments\n")
            acked = False
            for h in haves:
                if any(repo.is_ancestor(h, w) for w in wants):
                    out += pkt_line(("ACK %s\n" % h).encode("ascii"))
                    acked = True
            if not acked:
                out += pkt_line(b"NAK\n")
            # Single-round negotiation: the pack is computed from this one
            # request, so the server is ready; without this line real git
            # refuses any section that follows ("no 'ready'").
            out += pkt_line(b"ready\n")
            out += pkt_delim()
        if depth is not None:
            out += pkt_line(b"shallow-info\n")
            for s in sorted(boundary):
                out += pkt_line(("shallow %s\n" % s).encode("ascii"))
            for s in shallow_in:
                # previously shallow, now at least one parent is included
                parents = repo.commit_parents(s)
                if parents and any(p in send for p in parents):
                    out += pkt_line(("unshallow %s\n" % s).encode("ascii"))
            out += pkt_delim()
        if pack is not None:
            out += pkt_line(b"packfile\n")
            for i in range(0, len(pack), MAX_PKT_PAYLOAD - 1):
                chunk = pack[i:i + MAX_PKT_PAYLOAD - 1]
                out += pkt_line(b"\x01" + chunk)
            out += pkt_flush()
        else:
            # No wants (empty repo): no packfile section at all; client takes pack=null.
            out += pkt_flush()
        return 200, [("content-type", "application/x-git-upload-pack-result")], bytes(out)

    # ---- receive-pack v0 ---------------------------------------------------------
    def _receive_pack(self, repo, body: bytes):
        try:
            lines, consumed = parse_pkt_stream(body)
        except ValueError:
            return 400, [("content-type", "application/json")], b'{"message":"malformed pkt-line"}'
        commands = []
        for raw in lines:
            text = raw.decode("utf-8", "surrogateescape")
            if "\x00" in text:
                text = text.split("\x00", 1)[0]
            text = text.strip()
            if not text:
                continue
            parts = text.split(" ")
            if len(parts) != 3:
                return 400, [("content-type", "application/json")], b'{"message":"bad command line"}'
            commands.append((parts[0], parts[1], parts[2]))
        pack_bytes = body[consumed:]
        unpack_ok = True
        if pack_bytes:
            try:
                objects = read_pack(pack_bytes, lambda sha: repo.get_object(sha))
                for sha, (obj_type, obj_body) in objects.items():
                    repo.put_object(obj_type, obj_body)
            except ValueError:
                unpack_ok = False
        report = bytearray()
        if unpack_ok:
            report += pkt_line(b"unpack ok\n")
        else:
            report += pkt_line(b"unpack error\n")
        for old, new, ref in commands:
            ok, reason = self._apply_command(repo, old, new, ref, unpack_ok)
            if ok:
                report += pkt_line(("ok %s\n" % ref).encode("utf-8"))
            else:
                report += pkt_line(("ng %s %s\n" % (ref, reason)).encode("utf-8"))
        report += pkt_flush()
        out = bytearray()
        # sideband band 1 carries the whole report stream; split into <=64k frames
        report_bytes = bytes(report)
        for i in range(0, len(report_bytes), MAX_PKT_PAYLOAD - 1):
            chunk = report_bytes[i:i + MAX_PKT_PAYLOAD - 1]
            out += pkt_line(b"\x01" + chunk)
        out += pkt_flush()
        repo.invalidate_caches()
        return 200, [("content-type", "application/x-git-receive-pack-result")], bytes(out)

    def _apply_command(self, repo, old: str, new: str, ref: str, unpack_ok: bool):
        if not unpack_ok:
            return False, "unpack failed"
        current = repo.refs.get(ref)
        if new == ZERO_OID:  # delete
            if current is None:
                return False, "remote ref does not exist"
            if old != ZERO_OID and old != current:
                return False, "old ref value does not match"
            del repo.refs[ref]
            return True, ""
        if not repo.has_object(new):
            return False, "object not found"
        if old == ZERO_OID:  # create
            if current is not None:
                return False, "already exists"
            repo.refs[ref] = new
            return True, ""
        if current is None:
            return False, "remote ref does not exist"
        if old != current:
            return False, "old ref value does not match"
        if new == old:
            return True, ""
        if ref.startswith("refs/tags/"):
            return False, "non-fast-forward"
        obj = repo.get_object(new)
        if obj is None or obj[0] != "commit":
            return False, "non-fast-forward"
        if repo.is_ancestor(old, new):
            repo.refs[ref] = new
            return True, ""
        return False, "non-fast-forward"
