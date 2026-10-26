"""``preflight`` and ``healthcheck`` sub-commands (server and agent).

``python -m kowan.server preflight [--db PATH]`` / ``python -m kowan.agent preflight``
run ONCE inside a throwaway container of the NEW image before an update touches
anything. They import every module, parse the configuration from the
environment and, for the server, open a COPY of the database in a temp dir and
run the migrations against it. They also check the image's own self-update
helper works, because the NEXT update's helper will run from this image. Exit
0/1 with a JSON summary on the last stdout line. The live database is never
opened.

``healthcheck`` is the Docker ``HEALTHCHECK`` command:

* server: ``GET /health`` on the local listen port (``KOWAN_ADDR``, https when
  ``KOWAN_TLS_CERT`` is set, self-signed accepted);
* agent: the heartbeat file is fresh. The agent touches it every 10 s while an
  edge tunnel session is up, or while the direct-mode listener is serving, and
  removes it when the edge session drops.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

HEARTBEAT_ENV = "KOWAN_AGENT_HEARTBEAT_FILE"
HEARTBEAT_DEFAULT = "/tmp/kowan-agent.heartbeat"
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_MAX_AGE = 45.0


def _check(checks: List[Dict[str, Any]], name: str, fn: Callable[[], Any]) -> Any:
    try:
        detail = fn()
        checks.append({"name": name, "ok": True, "detail": str(detail or "")})
        return detail
    except BaseException as err:  # noqa: BLE001 - SystemExit from imports too
        checks.append({"name": name, "ok": False, "error": f"{type(err).__name__}: {err}",
                       "trace": traceback.format_exc(limit=3)[-800:]})
        return None


def _helper_selftest() -> str:
    from .helper import Swap  # noqa: F401 - import is the check
    from .plan import decode_plan, encode_plan

    p = {"id": "selftest", "component": "server", "mode": "update", "target": "x", "image": "y"}
    if decode_plan(encode_plan(p))["target"] != "x":
        raise RuntimeError("plan round-trip mismatch")
    return "self-update helper ok"


def _emit(component: str, checks: List[Dict[str, Any]]) -> int:
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "component": component, "checks": checks}, separators=(",", ":")),
          flush=True)
    return 0 if ok else 1


def server_preflight(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="kowan.server preflight")
    ap.add_argument("--db", default="", help="path of a database COPY to migrate (in a temp dir)")
    ns = ap.parse_args(argv)
    checks: List[Dict[str, Any]] = []

    def _imports() -> str:
        from .. import server

        mods = server._registrars()
        return f"{len(mods)} api modules"

    _check(checks, "imports", _imports)
    holder: Dict[str, Any] = {}

    def _config() -> str:
        from ..config import load_server_config

        holder["cfg"] = load_server_config()
        c = holder["cfg"]
        # Never echo the config object itself: it holds secrets.
        return f"addr {c.addr}, data dir {c.data_dir}, tls {'on' if c.tls_cert else 'off'}"

    _check(checks, "config", _config)
    cfg = holder.get("cfg")
    _check(checks, "helper", _helper_selftest)
    if ns.db:
        def _db() -> str:
            from ..store import Store

            tmp = tempfile.mkdtemp(prefix="kowan-preflight-")
            try:
                copy = os.path.join(tmp, "kowan.db")
                shutil.copyfile(ns.db, copy)
                st = Store(copy)
                try:
                    users = st.count_users()
                    detail = f"migrated a copy ({users} users)"
                    key_detail = _cipher_check(st, cfg)
                    if key_detail:
                        detail += f"; {key_detail}"
                finally:
                    st.close()
                return detail
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        _check(checks, "database", _db)
    return _emit("server", checks)


def _cipher_check(st: Any, cfg: Any) -> str:
    """Check the secret key loads and attaches to the store (read-only)."""
    if cfg is None:
        return ""
    from ..secrets import Cipher
    from ..secrets.cipher import KEY_FILE_NAME, load_or_create_key

    key_path = os.path.join(cfg.data_dir, KEY_FILE_NAME)
    if not cfg.secret_key and not os.path.isfile(key_path):
        return "no secret key to check"
    st.use_cipher(Cipher(load_or_create_key(cfg.data_dir, cfg.secret_key)))
    return "secret key ok"


def agent_preflight(argv: List[str]) -> int:
    checks: List[Dict[str, Any]] = []

    def _imports() -> str:
        from .. import agent, agent_ops_server  # noqa: F401
        from ..docker.local import LocalClient  # noqa: F401

        return "agent modules"

    def _config() -> str:
        from ..config import load_agent_config

        cfg = load_agent_config()
        if cfg.mode not in ("agent", "edge"):
            raise ValueError(f"KOWAN_AGENT_MODE must be agent or edge, not {cfg.mode!r}")
        if cfg.mode == "edge" and not cfg.server_url:
            raise ValueError("edge mode requires KOWAN_SERVER_URL")
        return f"mode {cfg.mode}"

    _check(checks, "imports", _imports)
    _check(checks, "config", _config)
    _check(checks, "helper", _helper_selftest)
    return _emit("agent", checks)


# --------------------------------------------------------------------------- #
# healthchecks
# --------------------------------------------------------------------------- #


def server_healthcheck(argv: Optional[List[str]] = None) -> int:
    import ssl
    import urllib.request

    addr = os.environ.get("KOWAN_ADDR") or ":5081"
    port = addr.rsplit(":", 1)[-1] or "5081"
    scheme = "https" if os.environ.get("KOWAN_TLS_CERT") else "http"
    ctx = ssl._create_unverified_context() if scheme == "https" else None  # noqa: S323
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/health", timeout=4, context=ctx) as r:
            ok = r.status == 200
    except Exception as err:  # noqa: BLE001
        print(f"unhealthy: {err}", flush=True)
        return 1
    print("ok" if ok else "unhealthy", flush=True)
    return 0 if ok else 1


def heartbeat_path() -> str:
    return os.environ.get(HEARTBEAT_ENV, "").strip() or HEARTBEAT_DEFAULT


def touch_heartbeat() -> None:
    p = heartbeat_path()
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(str(int(time.time())))
        os.replace(tmp, p)
    except OSError:
        pass


def clear_heartbeat() -> None:
    try:
        os.remove(heartbeat_path())
    except OSError:
        pass


async def heartbeat_loop(interval: float = HEARTBEAT_INTERVAL) -> None:
    """Touch the heartbeat file until cancelled (then remove it)."""
    try:
        while True:
            touch_heartbeat()
            await asyncio.sleep(interval)
    finally:
        clear_heartbeat()


def agent_healthcheck(argv: Optional[List[str]] = None, now: Optional[float] = None) -> int:
    p = heartbeat_path()
    try:
        age = (now if now is not None else time.time()) - os.stat(p).st_mtime
    except OSError:
        print("unhealthy: no heartbeat (tunnel not connected / listener not serving)", flush=True)
        return 1
    if age > HEARTBEAT_MAX_AGE:
        print(f"unhealthy: heartbeat is {age:.0f}s old", flush=True)
        return 1
    print(f"ok ({age:.0f}s)", flush=True)
    return 0


def dispatch(component: str, args: List[str]) -> Optional[int]:
    """Handle the shared sub-commands; returns an exit code or None if not ours."""
    if not args:
        return None
    cmd, rest = args[0], args[1:]
    if cmd == "self-update":
        from .helper import main as helper_main

        return helper_main(rest)
    if cmd == "preflight":
        return server_preflight(rest) if component == "server" else agent_preflight(rest)
    if cmd == "healthcheck":
        return server_healthcheck(rest) if component == "server" else agent_healthcheck(rest)
    return None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(dispatch(sys.argv[1], sys.argv[2:]) or 0)
