"""Git branch HEAD polling trigger.

Reads the branch tip SHA over the
git smart-HTTP protocol (GET ``.../info/refs?service=git-upload-pack``) with no
git binary and no clone, firing whenever the tip moves.

The response is a pkt-line ref advertisement; we parse it into refname -> SHA
and pick the configured branch (falling back main -> master). HTTP goes
through :mod:`kowan.httpcap` with optional HTTP Basic auth for private repos.
"""

from __future__ import annotations

from typing import Dict, Tuple

from kowan import httpcap

from . import FieldSpec, KindPoll, Poller, TypeGitCommit, register

# Bounds the single smart-HTTP ref advertisement fetch. The poller already wraps
# evaluate in a 25s budget; this is a tighter per-call cap.
_GIT_COMMIT_TIMEOUT = 20.0

# Caps the ref advertisement we read. Repos with thousands of refs can advertise
# a lot; 4 MiB is generous while still bounding memory.
_GIT_COMMIT_MAX_BODY = 4 << 20


class _GitCommitPoller(Poller):
    """Parsed config for one git-commit trigger."""

    def __init__(self, repo: str, branch: str, username: str, token: str) -> None:
        self.repo = repo
        self.branch = branch
        self.username = username
        self.token = token

    async def evaluate(self, config: Dict[str, str], state: str) -> Tuple[bool, str]:
        """Fetch the ref advertisement and resolve the configured branch tip.

        Seeding (state == "") records the current SHA without firing; afterwards
        it fires only when the tip differs from the stored SHA.
        """
        refs = await self._fetch_refs()

        branch = self.branch or "main"
        sha = refs.get("refs/heads/" + branch)
        # When the caller did not pin a branch, fall back to master if main is
        # absent (older repos use master as the default branch name).
        if sha is None and self.branch == "":
            sha = refs.get("refs/heads/master")
            branch = "master"
        if sha is None:
            raise ValueError(f"git-commit: branch {branch!r} not found in {self.repo}")

        if state == "":
            # Seed: record the current tip without firing for pre-existing history.
            return False, sha
        return sha != state, sha

    async def _fetch_refs(self) -> Dict[str, str]:
        """Perform the smart-HTTP ref advertisement request and return a map of
        refname -> 40-hex SHA."""
        # Just append the service query to the given URL; most hosts accept the
        # URL with or without a trailing ".git".
        request_url = self.repo.rstrip("/") + "/info/refs?service=git-upload-pack"

        headers = {"Accept": "application/x-git-upload-pack-advertisement"}
        auth = None
        if self.username or self.token:
            auth = (self.username, self.token)

        try:
            resp = await httpcap.request("GET", request_url, limit=_GIT_COMMIT_MAX_BODY, timeout=_GIT_COMMIT_TIMEOUT,
                                         follow_redirects=True, headers=headers, auth=auth)
        except httpcap.HTTPError as err:
            raise ValueError(f"git-commit: fetch {request_url}: {err}") from err
        body = resp.content
        if resp.status_code != 200:
            snippet = body.decode("utf-8", "replace").strip()
            raise ValueError(
                f"git-commit: {request_url}: unexpected status {resp.status_code}: {snippet}"
            )

        try:
            return _parse_pkt_refs(body)
        except ValueError as err:
            raise ValueError(f"git-commit: parse {request_url}: {err}") from err


def _parse_pkt_refs(data: bytes) -> Dict[str, str]:
    """Read the pkt-line ref advertisement and return refname -> SHA.

    Wire format: each packet is a 4-hex-digit big-endian length prefix (the
    length includes the 4 prefix bytes) followed by length-4 payload bytes;
    "0000" is a flush packet with no payload. The advertisement opens with a
    "# service=..." comment line and a flush, then one packet per ref::

        <40-hex-sha> <refname>\\0<capabilities>\\n   (\\0caps only on the first ref)
        <40-hex-sha> <refname>\\n                    (subsequent refs)
    """
    refs: Dict[str, str] = {}
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        len_hdr = data[pos : pos + 4]
        pos += 4
        try:
            pkt_len = int(len_hdr.decode("ascii"), 16)
        except ValueError as err:
            raise ValueError(f"invalid pkt length {len_hdr!r}: {err}") from err
        if pkt_len == 0:
            continue  # flush packet, no payload
        if pkt_len < 4:
            raise ValueError(f"invalid pkt length {pkt_len}")
        payload = data[pos : pos + (pkt_len - 4)]
        if len(payload) < pkt_len - 4:
            # Truncated final packet: stop.
            break
        pos += pkt_len - 4

        line = payload.decode("utf-8", "replace").rstrip("\n")
        # Skip the service-announcement comment line ("# service=git-upload-pack").
        if line.startswith("#"):
            continue
        # Strip capabilities advertised after a NUL on the first ref line.
        nul = line.find("\x00")
        if nul >= 0:
            line = line[:nul]
        # A ref line is "<40-hex-sha> <refname>".
        sp = line.find(" ")
        if sp != 40:
            continue
        sha = line[:40]
        name = line[sp + 1 :]
        if not _is_hex40(sha) or name == "":
            continue
        refs[name] = sha
    return refs


def _is_hex40(s: str) -> bool:
    """Whether ``s`` is exactly 40 hex digits (a SHA-1)."""
    if len(s) != 40:
        return False
    for c in s:
        if not (c.isdigit() or ("a" <= c <= "f") or ("A" <= c <= "F")):
            return False
    return True


def _new_git_commit(config: Dict[str, str]) -> Poller:
    repo = (config.get("repo") or "").strip()
    if not repo:
        raise ValueError("repo is required")
    return _GitCommitPoller(
        repo=repo,
        branch=(config.get("branch") or "").strip(),
        username=config.get("username", ""),
        token=config.get("token", ""),
    )


register(
    TypeGitCommit,
    KindPoll,
    "Git commit",
    "Fires when a branch's HEAD commit changes, polled over the git smart-HTTP protocol (no clone).",
    _new_git_commit,
    [
        FieldSpec(key="repo", label="Repository URL", required=True, placeholder="https://github.com/owner/repo.git"),
        FieldSpec(key="branch", label="Branch", placeholder="main"),
        # For private repos, auth comes from a shared connection (gitFetch), not
        # inline username/token. The poller resolves the referenced connection into
        # username/token before building the poller, so _new_git_commit still reads
        # config["username"]/["token"].
        FieldSpec(
            key="connectionRef",
            label="Credential",
            placeholder="(for private repos)",
            connection={"capability": "gitFetch", "optional": True},
        ),
    ],
)
