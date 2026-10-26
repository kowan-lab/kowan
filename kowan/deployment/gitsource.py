"""Git as a source for compose deployments.

A compose deployment may carry a :class:`kowan.models.GitSource` instead of (or in
addition to) inline YAML. Deploying it shallow-clones the repo into the data dir,
reads the compose file (plus a ``.env`` next to it, if any) and hands the result
to the normal compose path.

This module has two halves:

  * **Pure helpers** (no I/O) used by the push webhook and unit-tested directly:
    HMAC / token verification, ref matching, changed-path filtering, ``.env``
    parsing and ``${VAR}`` interpolation, and :func:`evaluate_push`, which turns a
    webhook request into a decision ("deploy" / "ignore" / "pong" / "unauthorized").
  * **git CLI wrappers** (``asyncio.create_subprocess_exec``): :func:`ls_remote`
    and :func:`clone`. We shell out to ``git`` (installed in the server image)
    instead of re-implementing the pack protocol. Credentials are never put in the
    URL or argv: an HTTP ``Authorization`` header is injected through the
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` environment
    (git >= 2.31), so it shows up neither in ``ps`` nor in the clone's
    ``.git/config``.

Why interpolate ``.env`` server-side? The compose YAML travels to the target host
as text (``Ops.compose_up(project, yaml)`` — possibly over the agent tunnel), so a
``.env`` file sitting in the server's checkout would not be next to it there.
Substituting the variables before sending keeps the behaviour of
``docker compose`` reading ``.env`` from the project dir.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

DEFAULT_REF = "main"
DEFAULT_PATH = "docker-compose.yml"

# Protocols git may use for a deployment repo. file:// and local paths are not
# allowed: a deployment must not be able to read arbitrary server directories.
ALLOWED_PROTOCOLS = "http:https:ssh:git"

_GIT_TIMEOUT = float(os.environ.get("KOWAN_GIT_TIMEOUT", "120"))
_MAX_COMPOSE_BYTES = 1 << 20  # 1 MiB is plenty for a compose file


class GitError(Exception):
    """A git operation (ls-remote / clone / read) failed."""


# ---------------------------------------------------------------------------
# webhook verification
# ---------------------------------------------------------------------------


def sign_body(secret: str, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value GitHub/Gitea send for ``body``."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return "sha256=" + mac


def verify_hub_signature(secret: str, body: bytes, header: str) -> bool:
    """Verify a GitHub/Gitea ``X-Hub-Signature-256`` header (``sha256=<hex>``).

    Gitea also sends a bare-hex ``X-Gitea-Signature``; both forms are accepted.
    Constant-time comparison; an empty secret or header never verifies.
    """
    if not secret or not header:
        return False
    got = header.strip()
    if got.lower().startswith("sha256="):
        got = got[7:]
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, got.lower())


def verify_gitlab_token(secret: str, header: str) -> bool:
    """Verify GitLab's ``X-Gitlab-Token`` (a shared secret echoed verbatim)."""
    if not secret or not header:
        return False
    return hmac.compare_digest(secret.encode(), header.strip().encode())


# ---------------------------------------------------------------------------
# ref + path matching
# ---------------------------------------------------------------------------


def candidate_refs(ref: str) -> List[str]:
    """Fully-qualified refs a configured ``ref`` may denote.

    ``main`` -> ``refs/heads/main`` or ``refs/tags/main``; an already-qualified
    ``refs/...`` is returned as-is; empty means the default branch (``main``).
    """
    ref = (ref or "").strip() or DEFAULT_REF
    if ref.startswith("refs/"):
        return [ref]
    return ["refs/heads/" + ref, "refs/tags/" + ref]


def ref_matches(configured: str, pushed: str) -> bool:
    """Whether a pushed ref (``refs/heads/main``) is the configured one (``main``)."""
    pushed = (pushed or "").strip()
    if not pushed:
        return False
    if not pushed.startswith("refs/"):
        # Some senders give a short name; treat it like a configured value.
        return bool(set(candidate_refs(configured)) & set(candidate_refs(pushed)))
    return pushed in candidate_refs(configured)


def compose_dir(path: str) -> str:
    """Directory (repo-relative, no leading/trailing slash) of the compose path."""
    p = posixpath.normpath("/" + (path or DEFAULT_PATH).strip()).lstrip("/")
    d = posixpath.dirname(p)
    return "" if d in ("", ".") else d


def paths_touch_dir(paths: List[str], compose_path: str) -> bool:
    """Whether any changed path lies under the compose file's directory.

    A compose file at the repo root makes every change relevant.
    """
    d = compose_dir(compose_path)
    if d == "":
        return bool(paths)
    prefix = d + "/"
    for p in paths:
        p = posixpath.normpath("/" + p).lstrip("/")
        if p == d or p.startswith(prefix):
            return True
    return False


def changed_paths(payload: dict) -> Optional[List[str]]:
    """Files touched by a push, from ``commits[].added/modified/removed``.

    GitHub, Gitea and GitLab all use this shape. Returns ``None`` when the payload
    does not list files (unknown, so the caller should not filter), else the list.
    """
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    seen = False
    out: List[str] = []
    for c in commits:
        if not isinstance(c, dict):
            continue
        for k in ("added", "modified", "removed"):
            v = c.get(k)
            if isinstance(v, list):
                seen = True
                out.extend(str(x) for x in v)
    return out if seen else None


# ---------------------------------------------------------------------------
# push evaluation (pure)
# ---------------------------------------------------------------------------


@dataclass
class PushDecision:
    action: str  # "deploy" | "ignore" | "pong" | "unauthorized" | "token"
    reason: str = ""
    provider: str = ""
    ref: str = ""
    commit: str = ""


def _h(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header read that also works on a plain dict."""
    v = headers.get(name)
    if v is None:
        lname = name.lower()
        for k, val in headers.items():
            if k.lower() == lname:
                return val
        return ""
    return v


def detect_provider(headers: Mapping[str, str]) -> str:
    """``github`` / ``gitea`` / ``gitlab`` from the event headers, else ``""``."""
    if _h(headers, "X-Gitlab-Event") or _h(headers, "X-Gitlab-Token"):
        return "gitlab"
    if _h(headers, "X-Gitea-Event") or _h(headers, "X-Gogs-Event"):
        return "gitea"
    if _h(headers, "X-GitHub-Event") or _h(headers, "X-Hub-Signature-256"):
        return "github"
    return ""


def evaluate_push(
    git: Optional[object],
    headers: Mapping[str, str],
    body: bytes,
) -> PushDecision:
    """Decide what a request on ``/hooks/deploy/{token}`` should do.

    ``git`` is the deployment's GitSource (or None). Requests without a git
    provider header — or for a deployment without a git source — are plain token
    webhooks (``action="token"``: possession of the path token redeploys).

    For a push from GitHub/Gitea (``X-Hub-Signature-256`` HMAC) or GitLab
    (``X-Gitlab-Token``) the per-deployment ``webhookSecret`` must verify; then the
    event must be a push to the configured ref and, when the payload lists files,
    must touch the compose file's directory. ``autoDeploy`` off ignores pushes.
    """
    provider = detect_provider(headers)
    if git is None or provider == "":
        return PushDecision("token")

    secret = getattr(git, "webhook_secret", "") or ""
    if provider == "gitlab":
        ok = verify_gitlab_token(secret, _h(headers, "X-Gitlab-Token"))
    else:
        sig = _h(headers, "X-Hub-Signature-256") or _h(headers, "X-Gitea-Signature")
        ok = verify_hub_signature(secret, body, sig)
    if not ok:
        return PushDecision("unauthorized", "signature mismatch", provider)

    event = (
        _h(headers, "X-GitHub-Event")
        or _h(headers, "X-Gitea-Event")
        or _h(headers, "X-Gogs-Event")
        or _h(headers, "X-Gitlab-Event")
    ).strip().lower()
    if event == "ping":
        return PushDecision("pong", "ping", provider)
    if event not in ("", "push", "push hook", "tag push hook", "tag_push"):
        return PushDecision("ignore", f"event {event!r} is not a push", provider)

    try:
        payload = _decode_payload(body)
    except ValueError:
        return PushDecision("ignore", "payload is not JSON", provider)
    if not isinstance(payload, dict):
        return PushDecision("ignore", "payload is not an object", provider)

    pushed_ref = str(payload.get("ref") or "")
    commit = str(payload.get("after") or payload.get("checkout_sha") or "")
    d = PushDecision("ignore", "", provider, pushed_ref, commit)

    if not getattr(git, "auto_deploy", False):
        d.reason = "auto deploy is off"
        return d
    if not ref_matches(getattr(git, "ref", ""), pushed_ref):
        d.reason = f"ref {pushed_ref!r} does not match {getattr(git, 'ref', '') or DEFAULT_REF!r}"
        return d
    if commit and set(commit) == {"0"}:
        d.reason = "ref was deleted"
        return d
    paths = changed_paths(payload)
    if paths is not None and not paths_touch_dir(paths, getattr(git, "path", "")):
        d.reason = "no changes under the compose directory"
        return d
    d.action = "deploy"
    return d


def _decode_payload(body: bytes) -> object:
    """JSON body, or GitHub's form-encoded ``payload=<json>`` variant."""
    raw = body or b"{}"
    if raw.startswith(b"payload="):
        from urllib.parse import parse_qs

        vals = parse_qs(raw.decode("utf-8", "replace")).get("payload") or ["{}"]
        return json.loads(vals[0])
    return json.loads(raw)


# ---------------------------------------------------------------------------
# .env parsing + interpolation (pure)
# ---------------------------------------------------------------------------


def parse_env_file(text: str) -> Dict[str, str]:
    """Parse a compose-style ``.env``: ``KEY=VALUE`` lines, ``#`` comments,
    optional ``export`` prefix, single/double quoted values."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            q = val[0]
            val = val[1:-1]
            if q == '"':
                val = val.replace("\\n", "\n").replace('\\"', '"')
        else:
            # Inline comment on an unquoted value: "VALUE # note".
            m = re.search(r"\s+#", val)
            if m:
                val = val[: m.start()]
        out[key] = val
    return out


_VAR_RE = re.compile(
    r"\$(?:(?P<esc>\$)|\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?[-?+])(?P<arg>[^}]*))?\}"
    r"|(?P<named>[A-Za-z_][A-Za-z0-9_]*))"
)


def interpolate(text: str, env: Mapping[str, str]) -> str:
    """Substitute ``$VAR`` / ``${VAR}`` / ``${VAR:-def}`` / ``${VAR-def}`` with
    values from ``env``.

    Variables NOT in ``env`` are left untouched (the target host's compose still
    resolves them from its own environment), and ``$$`` is preserved, so this is
    safe to apply to any compose file.
    """
    if not env:
        return text

    def repl(m: "re.Match[str]") -> str:
        if m.group("esc"):
            return m.group(0)
        name = m.group("braced") or m.group("named")
        op = m.group("op")
        if name not in env:
            # Unknown here: leave it (defaults included) for compose on the host.
            return m.group(0)
        val = env[name]
        if op == ":-" and val == "":
            return m.group("arg") or ""
        if op == ":+":
            return (m.group("arg") or "") if val != "" else ""
        if op == "+":
            return m.group("arg") or ""
        return val

    return _VAR_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# git CLI
# ---------------------------------------------------------------------------


@dataclass
class GitAuth:
    username: str = ""
    token: str = ""

    def empty(self) -> bool:
        return not self.username and not self.token


def validate_repo_url(url: str) -> str:
    """Return a cleaned repo URL or raise ``ValueError``."""
    url = (url or "").strip()
    if not url:
        raise ValueError("git.repoUrl is required")
    if url.startswith("-"):
        raise ValueError("git.repoUrl is invalid")
    low = url.lower()
    if low.startswith(("https://", "http://", "ssh://", "git://")):
        return url
    # scp-like "git@host:owner/repo.git"
    if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^/]", url):
        return url
    raise ValueError("git.repoUrl must be an https://, ssh:// or git@host: URL")


def _git_env(auth: Optional[GitAuth]) -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = ALLOWED_PROTOCOLS
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    pairs: List[Tuple[str, str]] = []
    if auth is not None and not auth.empty():
        user = auth.username or ("oauth2" if auth.token else "")
        raw = f"{user}:{auth.token}".encode()
        pairs.append(("http.extraHeader", "Authorization: Basic " + base64.b64encode(raw).decode()))
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


async def _run_git(args: List[str], auth: Optional[GitAuth], cwd: Optional[str] = None) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            env=_git_env(auth),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as err:
        raise GitError("git is not installed on the server") from err
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except asyncio.TimeoutError as err:
        proc.kill()
        raise GitError(f"git {args[0]} timed out after {_GIT_TIMEOUT:.0f}s") from err
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise GitError(f"git {args[0]}: {text[-800:] or 'exit ' + str(proc.returncode)}")
    return text


async def ls_remote(url: str, ref: str, auth: Optional[GitAuth] = None) -> str:
    """Return the commit SHA the configured ref points at on the remote."""
    cands = candidate_refs(ref)
    out = await _run_git(["ls-remote", "--", url, *cands, *(c + "^{}" for c in cands)], auth)
    refs: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    for c in cands:
        # An annotated tag's peeled ("^{}") entry is the commit itself.
        sha = refs.get(c + "^{}") or refs.get(c)
        if sha:
            return sha
    raise GitError(f"ref {ref or DEFAULT_REF!r} not found in {url}")


async def clone(url: str, ref: str, dest: str, auth: Optional[GitAuth] = None) -> str:
    """Shallow-clone ``ref`` of ``url`` into ``dest`` (replacing it atomically).

    Returns the checked-out commit SHA.
    """
    ref = (ref or "").strip() or DEFAULT_REF
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    elif ref.startswith("refs/tags/"):
        ref = ref[len("refs/tags/"):]
    parent = Path(dest).parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = str(dest) + ".tmp"
    # rmtree of a previous checkout walks/unlinks every file: keep it off the
    # event loop (a repo with thousands of files took 100ms+ per sync).
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    try:
        await _run_git(
            ["clone", "--depth", "1", "--single-branch", "--branch", ref, "--", url, tmp],
            auth,
        )
        sha = (await _run_git(["rev-parse", "HEAD"], None, cwd=tmp)).strip()
    except Exception:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
        raise
    old = str(dest) + ".old"
    await asyncio.to_thread(shutil.rmtree, old, True)
    if os.path.exists(dest):
        os.replace(dest, old)
    os.replace(tmp, dest)
    await asyncio.to_thread(shutil.rmtree, old, True)
    return sha


def read_compose(checkout: str, path: str) -> Tuple[str, str]:
    """Read the compose file at ``path`` inside ``checkout`` and a ``.env`` next
    to it (``""`` when absent). Refuses paths that escape the checkout."""
    root = Path(checkout).resolve()
    rel = (path or DEFAULT_PATH).strip().lstrip("/")
    target = (root / rel).resolve()
    if root not in target.parents:
        raise GitError(f"compose path {path!r} escapes the repository")
    if not target.is_file():
        raise GitError(f"compose file {rel!r} not found in the repository")
    if target.stat().st_size > _MAX_COMPOSE_BYTES:
        raise GitError(f"compose file {rel!r} is too large")
    compose = target.read_text(encoding="utf-8")
    env_text = ""
    env_path = target.parent / ".env"
    if env_path.is_file() and root in env_path.resolve().parents:
        env_text = env_path.read_text(encoding="utf-8")
    return compose, env_text


def render_compose(compose: str, env_text: str) -> str:
    """Apply the ``.env`` (if any) to the compose text."""
    if not env_text.strip():
        return compose
    return interpolate(compose, parse_env_file(env_text))
