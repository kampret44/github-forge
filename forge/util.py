"""Small shared helpers: git object hashing, deterministic ids, base64, request ids.

Everything here is stdlib-only and deterministic: no wall clock, no unseeded
randomness, so two runs of the same trial leave identical state.
"""
import base64
import hashlib
import itertools
import zlib

VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
STRANGER_LOGIN = "olgabauer"
DEFAULT_BRANCH = "main"
# Fixed identity/timestamp for every commit the server itself creates
# (contents PUT, squash merge). Deterministic: replay-safe.
GIT_AUTHOR = "Forge <forge@localhost>"
GIT_EPOCH = 1735689600
MERGED_AT = "2026-01-01T00:00:00Z"


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def git_object_id(obj_type: str, body: bytes) -> str:
    """The sha1 of a git object: sha1("<type> <len>\\0<body>")."""
    header = ("%s %d\x00" % (obj_type, len(body))).encode("ascii")
    return hashlib.sha1(header + body).hexdigest()


def git_object_bytes(obj_type: str, body: bytes) -> bytes:
    header = ("%s %d\x00" % (obj_type, len(body))).encode("ascii")
    return header + body


def compress(data: bytes) -> bytes:
    return zlib.compress(data, 6)


def decompress(data: bytes) -> bytes:
    return zlib.decompress(data)


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def repo_global_id(owner: str, name: str) -> str:
    """Opaque, stable repository id (never the plain database id)."""
    digest = hashlib.sha1(("repo:" + owner + "/" + name).encode("utf-8")).digest()
    return "R_" + _b32(digest[:12])


def issue_global_id(owner: str, name: str, number: int) -> str:
    digest = hashlib.sha1(("issue:%s/%s:%d" % (owner, name, number)).encode("utf-8")).digest()
    return "I_" + _b32(digest[:12])


def pr_global_id(owner: str, name: str, number: int) -> str:
    digest = hashlib.sha1(("pr:%s/%s:%d" % (owner, name, number)).encode("utf-8")).digest()
    return "P_" + _b32(digest[:12])


def comment_global_id(owner: str, name: str, number: int, cid: int) -> str:
    digest = hashlib.sha1(("comment:%s/%s:%d:%d" % (owner, name, number, cid)).encode("utf-8")).digest()
    return "C_" + _b32(digest[:12])


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    # The contents API tolerates whitespace/newlines in base64 bodies.
    cleaned = "".join(text.split())
    return base64.b64decode(cleaned)


class RequestIdGen:
    """Monotonic request ids: non-empty, unique per process, deterministic."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def next(self) -> str:
        return "req-%d" % next(self._counter)
