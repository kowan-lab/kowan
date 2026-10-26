"""A fake ``docker`` CLI on PATH (not a test module).

Compose and swarm deployments shell out to ``docker compose`` / ``docker stack``.
In tests that must never reach the machine's Docker, so ``FakeDockerCLI`` puts an
executable named ``docker`` first on PATH. It records every invocation (argv and
the content of any ``-f`` compose file) to a JSON-lines log and exits with the
code a test asked for.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from typing import Any, Dict, List

_SCRIPT = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
files = {{}}
for i, a in enumerate(argv):
    if a in ("-f", "--file", "-c", "--compose-file") and i + 1 < len(argv):
        try:
            files[argv[i + 1]] = open(argv[i + 1]).read()
        except OSError:
            files[argv[i + 1]] = None
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": argv, "files": files}}) + "\n")
code, out = 0, ""
if os.path.exists({code!r}):
    spec = open({code!r}).read().split(" ", 1)
    only = spec[1] if len(spec) > 1 else ""
    if not only or only in argv:
        code = int(spec[0] or 0)
        out = open({out!r}).read() if os.path.exists({out!r}) else ""
sys.stdout.write(out)
sys.exit(code)
'''


class FakeDockerCLI:
    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="fakecli")
        d = self._dir.name
        self.log = os.path.join(d, "calls.jsonl")
        self._code = os.path.join(d, "code")
        self._out = os.path.join(d, "out")
        path = os.path.join(d, "docker")
        with open(path, "w") as f:
            f.write(_SCRIPT.format(python=sys.executable, log=self.log, code=self._code, out=self._out))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = d + os.pathsep + self._old_path

    def fail_with(self, code: int, output: str = "", only: str = "") -> None:
        """Exit with ``code`` (printing ``output``); with ``only``, just for
        invocations whose argv contains that word (e.g. "up")."""
        with open(self._code, "w") as f:
            f.write(f"{code} {only}" if only else str(code))
        with open(self._out, "w") as f:
            f.write(output)

    def succeed(self, output: str = "") -> None:
        self.fail_with(0, output)

    def calls(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def close(self) -> None:
        os.environ["PATH"] = self._old_path
        self._dir.cleanup()
