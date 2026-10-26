"""Pure helpers for browsing a container's filesystem through the archive API.

Used by the container file browser (``kowan/api/containerfiles.py``) and by the
``LocalClient`` archive ops. Nothing here talks to Docker: it normalises paths,
decodes the ``X-Docker-Container-Path-Stat`` header, parses tar streams
incrementally (async, so a multi-GB ``GET /containers/{id}/archive`` never has to
be buffered) and builds the small tar payloads ``PUT /containers/{id}/archive``
expects.

Why the archive API and not ``exec``: it works on stopped containers and on
images without a shell or coreutils (distroless, scratch). ``exec`` is only an
optional accelerator for listing a big directory of a running container and the
only way to delete/rename (the Engine has no archive "remove" verb).
"""

from __future__ import annotations

import base64
import json
import posixpath
import tarfile
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..errors import Validation

# Longest path we accept (Linux PATH_MAX) and longest single name (NAME_MAX).
MAX_PATH_LEN = 4096
MAX_NAME_LEN = 255

# Go os.FileMode type bits as reported by the path-stat header.
_GO_MODE_DIR = 1 << 31
_GO_MODE_SYMLINK = 1 << 27
_GO_MODE_DEVICE = 1 << 26
_GO_MODE_PIPE = 1 << 25
_GO_MODE_SOCKET = 1 << 24
_GO_MODE_SETUID = 1 << 23
_GO_MODE_SETGID = 1 << 22
_GO_MODE_CHARDEV = 1 << 21
_GO_MODE_STICKY = 1 << 20

_BLOCK = 512


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def normalize_path(raw: Any) -> str:
    """Return a clean absolute container path or raise :class:`Validation`.

    Strict on purpose: ``..`` segments, NUL and other control characters are
    rejected outright (not silently collapsed), so what the audit log records is
    exactly what was touched. Empty means ``/``; duplicate slashes, ``.``
    segments and a trailing slash are dropped.
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise Validation("invalid path")
    if len(raw) > MAX_PATH_LEN:
        raise Validation("path too long")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise Validation("invalid path: control characters are not allowed")
    segs: List[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise Validation("invalid path: '..' is not allowed")
        segs.append(seg)
    return "/" + "/".join(segs)


def validate_name(raw: Any) -> str:
    """Validate a single path component (a new file/folder name, a rename target)."""
    if not isinstance(raw, str):
        raise Validation("invalid name")
    name = raw.strip()
    if not name or name in (".", ".."):
        raise Validation("name is required")
    if "/" in name or "\x00" in name:
        raise Validation("invalid name: '/' is not allowed")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise Validation("invalid name: control characters are not allowed")
    if len(name.encode("utf-8")) > MAX_NAME_LEN:
        raise Validation("name too long")
    return name


def join(dir_path: str, name: str) -> str:
    """Join an already-normalised directory and a validated name."""
    return normalize_path(posixpath.join(dir_path, name))


def parent(path: str) -> str:
    p = normalize_path(path)
    return posixpath.dirname(p) or "/"


def resolve_link(path: str, target: str) -> str:
    """Resolve a symlink ``target`` read at ``path`` to an absolute, clean path.

    Relative targets are resolved against the link's directory; ``..`` is
    collapsed here (unlike :func:`normalize_path`) because it comes from the
    container's own filesystem, and is clamped at ``/``.
    """
    base = target if target.startswith("/") else posixpath.join(parent(path), target)
    out: List[str] = []
    for seg in base.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)


# ---------------------------------------------------------------------------
# Modes / entries
# ---------------------------------------------------------------------------


def _perm_string(perm: int, kind: str, setuid=False, setgid=False, sticky=False) -> str:
    chars = []
    for i, shift in enumerate((6, 3, 0)):
        triad = (perm >> shift) & 0o7
        r = "r" if triad & 4 else "-"
        w = "w" if triad & 2 else "-"
        x = "x" if triad & 1 else "-"
        if i == 0 and setuid:
            x = "s" if x == "x" else "S"
        if i == 1 and setgid:
            x = "s" if x == "x" else "S"
        if i == 2 and sticky:
            x = "t" if x == "x" else "T"
        chars.append(r + w + x)
    return kind + "".join(chars)


def go_mode_kind(mode: int) -> str:
    if mode & _GO_MODE_DIR:
        return "dir"
    if mode & _GO_MODE_SYMLINK:
        return "symlink"
    if mode & (_GO_MODE_DEVICE | _GO_MODE_CHARDEV):
        return "device"
    if mode & _GO_MODE_PIPE:
        return "pipe"
    if mode & _GO_MODE_SOCKET:
        return "socket"
    return "file"


_KIND_CHAR = {"dir": "d", "symlink": "l", "device": "c", "pipe": "p", "socket": "s", "file": "-"}


def go_mode_string(mode: int) -> str:
    """Render a Go ``os.FileMode`` (as sent by the Engine) like ``ls -l``."""
    kind = go_mode_kind(mode)
    return _perm_string(
        mode & 0o777,
        _KIND_CHAR[kind],
        bool(mode & _GO_MODE_SETUID),
        bool(mode & _GO_MODE_SETGID),
        bool(mode & _GO_MODE_STICKY),
    )


def _parse_mtime(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    if not isinstance(v, str) or not v:
        return 0
    s = v.strip()
    # The Engine sends RFC3339Nano: trim fractional seconds beyond microseconds, normalise Z.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, c in enumerate(rest):
            if c.isdigit():
                frac += c
            else:
                tz = rest[i:]
                break
        s = head + "." + (frac[:6] or "0") + tz
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return 0


def decode_path_stat(header_value: str) -> Dict[str, Any]:
    """Decode the base64 JSON ``X-Docker-Container-Path-Stat`` header."""
    if not header_value:
        raise Validation("missing path stat")
    try:
        raw = base64.b64decode(header_value + "=" * (-len(header_value) % 4))
        doc = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise Validation(f"bad path stat header: {exc}") from exc
    if not isinstance(doc, dict):
        raise Validation("bad path stat header")
    return doc


def entry_from_stat(path: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """Build the API entry for ``path`` from a decoded Engine path stat."""
    mode = int(st.get("mode") or 0)
    kind = go_mode_kind(mode)
    name = posixpath.basename(path) or "/"
    return {
        "name": name,
        "path": path,
        "size": int(st.get("size") or 0) if kind == "file" else 0,
        "mode": go_mode_string(mode),
        "kind": kind,
        "isDir": kind == "dir",
        "isLink": kind == "symlink",
        "linkTarget": str(st.get("linkTarget") or ""),
        "modTime": _parse_mtime(st.get("mtime")),
    }


class TarHeader:
    """The subset of a tar member header the browser needs."""

    __slots__ = ("name", "size", "mode", "mtime", "type", "linkname", "uid", "gid", "uname", "gname")

    def __init__(self, name: str, size: int, mode: int, mtime: int, type: bytes,
                 linkname: str = "", uid: int = 0, gid: int = 0, uname: str = "", gname: str = "") -> None:
        self.name = name
        self.size = size
        self.mode = mode
        self.mtime = mtime
        self.type = type
        self.linkname = linkname
        self.uid = uid
        self.gid = gid
        self.uname = uname
        self.gname = gname

    @property
    def kind(self) -> str:
        t = self.type
        if t == tarfile.DIRTYPE:
            return "dir"
        if t == tarfile.SYMTYPE:
            return "symlink"
        if t == tarfile.LNKTYPE:
            return "file"
        if t in (tarfile.CHRTYPE, tarfile.BLKTYPE):
            return "device"
        if t == tarfile.FIFOTYPE:
            return "pipe"
        return "file"

    @property
    def is_regular(self) -> bool:
        return self.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)


def entry_from_tar(dir_path: str, name: str, h: TarHeader) -> Dict[str, Any]:
    kind = h.kind
    return {
        "name": name,
        "path": join(dir_path, name),
        "size": h.size if kind == "file" else 0,
        "mode": _perm_string(h.mode & 0o777, _KIND_CHAR[kind], bool(h.mode & 0o4000),
                             bool(h.mode & 0o2000), bool(h.mode & 0o1000)),
        "kind": kind,
        "isDir": kind == "dir",
        "isLink": kind == "symlink",
        "linkTarget": h.linkname if kind == "symlink" else "",
        "modTime": int(h.mtime),
    }


def sort_entries(entries: List[Dict[str, Any]]) -> None:
    """Directories first, then case-insensitive by name (stable, deterministic)."""
    entries.sort(key=lambda e: (0 if e.get("isDir") else 1, e.get("name", "").lower(), e.get("name", "")))


# ---------------------------------------------------------------------------
# Async tar reader
# ---------------------------------------------------------------------------


class _Buffered:
    """Exact-size reads over an async byte stream (``read(n)`` or async-iterable)."""

    def __init__(self, src: Any) -> None:
        self._src = src
        self._buf = bytearray()
        self._eof = False
        self._iter = None
        self.consumed = 0

    async def _more(self) -> bool:
        if self._eof:
            return False
        read = getattr(self._src, "read", None)
        chunk = b""
        if callable(read):
            chunk = await read(64 * 1024)
        else:
            if self._iter is None:
                self._iter = self._src.__aiter__()
            try:
                chunk = await self._iter.__anext__()
            except StopAsyncIteration:
                chunk = b""
        if not chunk:
            self._eof = True
            return False
        self._buf.extend(chunk)
        return True

    async def read_exact(self, n: int) -> Optional[bytes]:
        while len(self._buf) < n:
            if not await self._more():
                return None
        out = bytes(self._buf[:n])
        del self._buf[:n]
        self.consumed += n
        return out

    async def read_upto(self, n: int) -> bytes:
        if not self._buf:
            await self._more()
        take = min(n, len(self._buf))
        out = bytes(self._buf[:take])
        del self._buf[:take]
        self.consumed += take
        return out

    async def skip(self, n: int) -> bool:
        while n > 0:
            if not self._buf and not await self._more():
                return False
            take = min(n, len(self._buf))
            del self._buf[:take]
            self.consumed += take
            n -= take
        return True


def _nts(b: bytes) -> str:
    i = b.find(b"\x00")
    if i != -1:
        b = b[:i]
    return b.decode("utf-8", "surrogateescape")


def _num(b: bytes) -> int:
    if b and b[0] & 0x80:  # GNU base-256
        n = b[0] & 0x7F
        for c in b[1:]:
            n = (n << 8) | c
        return n
    s = _nts(b).strip(" \x00")
    if not s:
        return 0
    try:
        return int(s, 8)
    except ValueError:
        raise Validation("corrupt tar header")


def _parse_pax(data: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    pos = 0
    while pos < len(data):
        sp = data.find(b" ", pos)
        if sp == -1:
            break
        try:
            length = int(data[pos:sp])
        except ValueError:
            break
        if length <= 0:
            break
        rec = data[sp + 1 : pos + length - 1]  # drop trailing \n
        pos += length
        if b"=" in rec:
            k, v = rec.split(b"=", 1)
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "surrogateescape")
    return out


class AsyncTarReader:
    """Incremental tar reader over an async byte stream.

    ``async for header in reader`` yields :class:`TarHeader` objects; between
    iterations the caller may call :meth:`read_data` to consume (part of) the
    member's content — anything left unread is skipped automatically. Supports
    ustar prefixes, GNU long names (``L``/``K``) and PAX extended headers, which
    covers what the Docker daemon emits.
    """

    def __init__(self, src: Any) -> None:
        self._r = _Buffered(src)
        self._remaining = 0  # unread bytes of the current member's data
        self._pad = 0
        self.done = False

    @property
    def consumed(self) -> int:
        return self._r.consumed

    async def _finish_member(self) -> None:
        if self._remaining or self._pad:
            await self._r.skip(self._remaining + self._pad)
            self._remaining = 0
            self._pad = 0

    async def next(self) -> Optional[TarHeader]:
        await self._finish_member()
        long_name: Optional[str] = None
        long_link: Optional[str] = None
        pax: Dict[str, str] = {}
        while True:
            block = await self._r.read_exact(_BLOCK)
            if block is None or block == b"\x00" * _BLOCK:
                self.done = True
                return None
            typ = block[156:157] or b"0"
            size = _num(block[124:136])
            padded = (size + _BLOCK - 1) // _BLOCK * _BLOCK
            if typ in (b"L", b"K", b"x", b"g"):
                if size > 1 << 20:
                    raise Validation("tar extended header too large")
                data = await self._r.read_exact(padded)
                if data is None:
                    self.done = True
                    return None
                data = data[:size]
                if typ == b"L":
                    long_name = _nts(data)
                elif typ == b"K":
                    long_link = _nts(data)
                elif typ == b"x":
                    pax.update(_parse_pax(data))
                continue
            name = _nts(block[0:100])
            if block[257:262] == b"ustar":
                prefix = _nts(block[345:500])
                if prefix:
                    name = prefix + "/" + name
            linkname = _nts(block[157:257])
            if long_name is not None:
                name = long_name
            if long_link is not None:
                linkname = long_link
            if "path" in pax:
                name = pax["path"]
            if "linkpath" in pax:
                linkname = pax["linkpath"]
            if "size" in pax:
                try:
                    size = int(pax["size"])
                    padded = (size + _BLOCK - 1) // _BLOCK * _BLOCK
                except ValueError:
                    pass
            mtime = _num(block[136:148])
            if "mtime" in pax:
                try:
                    mtime = int(float(pax["mtime"]))
                except ValueError:
                    pass
            h = TarHeader(
                name=name,
                size=size,
                mode=_num(block[100:108]),
                mtime=mtime,
                type=typ if typ != b"\x00" else tarfile.REGTYPE,
                linkname=linkname,
                uid=int(pax.get("uid", 0) or 0) if "uid" in pax else _num(block[108:116]),
                gid=int(pax.get("gid", 0) or 0) if "gid" in pax else _num(block[116:124]),
                uname=pax.get("uname", _nts(block[265:297])),
                gname=pax.get("gname", _nts(block[297:329])),
            )
            has_data = h.type not in (tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE,
                                      tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE)
            if has_data:
                self._remaining = size
                self._pad = padded - size
            else:
                self._remaining = 0
                self._pad = 0
            return h

    def __aiter__(self) -> "AsyncTarReader":
        return self

    async def __anext__(self) -> TarHeader:
        h = await self.next()
        if h is None:
            raise StopAsyncIteration
        return h

    async def read_data(self, n: int = 64 * 1024) -> bytes:
        """Read up to ``n`` bytes of the current member ("" at its end)."""
        if self._remaining <= 0:
            return b""
        chunk = await self._r.read_upto(min(n, self._remaining))
        if not chunk:
            self._remaining = 0
            return b""
        self._remaining -= len(chunk)
        return chunk

    async def read_all(self, limit: int) -> bytes:
        out = bytearray()
        while True:
            chunk = await self.read_data()
            if not chunk:
                return bytes(out)
            out.extend(chunk)
            if len(out) > limit:
                raise Validation("file too large")


def tar_member_rel(base: str, name: str) -> Optional[str]:
    """Map a member name of a ``GET archive`` tar to a path relative to the
    requested directory, or None for the directory itself / foreign entries.

    The daemon names members after the requested path's basename (``etc``,
    ``etc/hosts``); for ``/`` it uses ``.``-relative or bare names. ``base`` is
    that basename ("" for the root).
    """
    nm = name
    while nm.startswith("./"):
        nm = nm[2:]
    nm = nm.lstrip("/").rstrip("/")
    if base:
        if nm == base:
            return None
        if not nm.startswith(base + "/"):
            return None
        nm = nm[len(base) + 1 :]
    if nm in ("", "."):
        return None
    return nm


async def list_tar_children(
    src: Any, dir_path: str, *, limit: int, max_scan_bytes: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """Collect the immediate children of ``dir_path`` from a ``GET archive`` tar.

    Returns ``(entries, truncated)``; stops early (truncated) after ``limit``
    children or ``max_scan_bytes`` of tar read. Grandchildren are skipped without
    buffering their content. Directories appearing only implicitly (a nested
    member without its own directory entry) are synthesised.
    """
    base = posixpath.basename(dir_path.rstrip("/"))
    reader = AsyncTarReader(src)
    seen: Dict[str, Dict[str, Any]] = {}
    truncated = False
    async for h in reader:
        rel = tar_member_rel(base, h.name)
        if rel is None:
            pass
        elif "/" not in rel:
            if rel not in seen:
                if len(seen) >= limit:
                    truncated = True
                    break
                seen[rel] = entry_from_tar(dir_path, rel, h)
        else:
            top = rel.split("/", 1)[0]
            if top not in seen:
                if len(seen) >= limit:
                    truncated = True
                    break
                seen[top] = {
                    "name": top, "path": join(dir_path, top), "size": 0, "mode": "d?????????",
                    "kind": "dir", "isDir": True, "isLink": False, "linkTarget": "", "modTime": 0,
                }
        if reader.consumed > max_scan_bytes:
            truncated = True
            break
    entries = list(seen.values())
    sort_entries(entries)
    return entries, truncated


async def first_regular_file(src: Any, limit: int) -> Tuple[Optional[TarHeader], bytes]:
    """Return the header and content of the first regular file in a tar stream."""
    reader = AsyncTarReader(src)
    async for h in reader:
        if h.is_regular:
            return h, await reader.read_all(limit)
    return None, b""


async def first_header(src: Any) -> Optional[TarHeader]:
    """Return the first member header of a tar stream (no content read)."""
    return await AsyncTarReader(src).next()


async def iter_first_file(src: Any) -> AsyncIterator[bytes]:
    """Stream the content of the first regular file of a tar stream."""
    reader = AsyncTarReader(src)
    async for h in reader:
        if h.is_regular:
            while True:
                chunk = await reader.read_data()
                if not chunk:
                    return
                yield chunk
    return


# ---------------------------------------------------------------------------
# Tar builders (PUT archive payloads)
# ---------------------------------------------------------------------------


def _header(name: str, *, size: int, mode: int, kind: bytes, uid: int, gid: int,
            uname: str, gname: str, mtime: Optional[int] = None) -> bytes:
    ti = tarfile.TarInfo(name)
    ti.size = size if kind == tarfile.REGTYPE else 0
    ti.mode = mode & 0o7777
    ti.type = kind
    ti.uid = max(0, int(uid))
    ti.gid = max(0, int(gid))
    ti.uname = uname or ""
    ti.gname = gname or ""
    ti.mtime = int(time.time()) if mtime is None else mtime
    return ti.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape")


def tar_dir(name: str, *, mode: int = 0o755, uid: int = 0, gid: int = 0,
            uname: str = "", gname: str = "") -> bytes:
    """A complete tar holding one empty directory ``name``."""
    return _header(name, size=0, mode=mode, kind=tarfile.DIRTYPE, uid=uid, gid=gid,
                   uname=uname, gname=gname) + b"\x00" * (2 * _BLOCK)


def tar_file_bytes(name: str, data: bytes, *, mode: int = 0o644, uid: int = 0, gid: int = 0,
                   uname: str = "", gname: str = "") -> bytes:
    """A complete tar holding one regular file ``name`` with ``data``."""
    pad = (-len(data)) % _BLOCK
    return (_header(name, size=len(data), mode=mode, kind=tarfile.REGTYPE, uid=uid, gid=gid,
                    uname=uname, gname=gname)
            + data + b"\x00" * pad + b"\x00" * (2 * _BLOCK))


class TarFileStream:
    """An async byte stream producing a one-file tar around ``content``.

    ``content`` must yield exactly ``size`` bytes (``read(n)``); the tar header,
    padding and end-of-archive blocks are emitted around it so a 1 GiB upload is
    never held in memory.
    """

    def __init__(self, name: str, size: int, content: Any, *, mode: int = 0o644,
                 uid: int = 0, gid: int = 0, uname: str = "", gname: str = "") -> None:
        self._head = _header(name, size=size, mode=mode, kind=tarfile.REGTYPE, uid=uid,
                             gid=gid, uname=uname, gname=gname)
        self._size = size
        self._left = size
        self._content = content
        self._tail = b"\x00" * ((-size) % _BLOCK) + b"\x00" * (2 * _BLOCK)
        self._stage = 0

    async def read(self, n: int = 64 * 1024) -> bytes:
        if n is None or n <= 0:
            n = 64 * 1024
        if self._stage == 0:
            self._stage = 1
            return self._head
        if self._stage == 1:
            if self._left > 0:
                chunk = await self._content.read(min(n, self._left))
                if not chunk:
                    raise Validation("upload ended early")
                self._left -= len(chunk)
                return chunk
            self._stage = 2
        if self._stage == 2:
            self._stage = 3
            return self._tail
        return b""

    def __aiter__(self) -> "TarFileStream":
        return self

    async def __anext__(self) -> bytes:
        b = await self.read()
        if not b:
            raise StopAsyncIteration
        return b


# Upper bound of tar bytes scanned to list one directory through the archive
# API (the daemon tars the whole subtree; children past this are "truncated").
MAX_SCAN_BYTES = 1 << 30
