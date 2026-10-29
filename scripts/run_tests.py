"""Run the backend tests in parallel: one test module per process, as many
processes as CPUs (or -j N), with no dependency beyond the standard library.

The modules are independent (each test uses its own temporary folders and
random ports), so they can run side by side. The slowest start first, so the
run ends evenly: each run saves how long every module took in
.test-timings.json (not in git), and modules never timed go by file size.
With --coverage each process records its own data file and they are combined
at the end. A failing module's output is printed in full.

    python scripts/run_tests.py                  # every tests/test_*.py
    python scripts/run_tests.py -j 4 --coverage
    python scripts/run_tests.py --timeout 60     # stop a hung module, print its stacks
    python scripts/run_tests.py tests.test_imports tests.test_features
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMINGS = ROOT / ".test-timings.json"
_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)


def _timings() -> dict[str, float]:
    try:
        return {str(k): float(v) for k, v in json.loads(TIMINGS.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def modules() -> list[str]:
    """Every test module, slowest first (by the last run, else by size)."""
    known = _timings()
    files = (ROOT / "tests").glob("test_*.py")
    return [f"tests.{p.stem}" for p in sorted(
        files, key=lambda p: (known.get(f"tests.{p.stem}", 0.0), p.stat().st_size), reverse=True)]


def run(module: str, coverage: bool, timeout: float) -> tuple[str, int, int, str, float]:
    """``(module, exit code, tests ran, output, seconds)``.

    A module still running after ``timeout`` seconds gets SIGABRT: with
    ``-X faulthandler`` it prints every thread's stack first, so the output
    says where it hung."""
    cmd = [sys.executable, "-X", "faulthandler", "-m"]
    cmd += ["coverage", "run", "--parallel-mode", "-m", "unittest", module] if coverage else ["unittest", module]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGABRT)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        out = f"{out}\nTIMED OUT after {timeout:.0f}s (stacks above)"
    m = _RAN.findall(out)
    return module, proc.returncode, int(m[-1]) if m else 0, out, time.monotonic() - started


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("modules", nargs="*", help="test modules (default: every tests/test_*.py)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2, help="parallel processes")
    ap.add_argument("--coverage", action="store_true", help="record coverage and combine it at the end")
    ap.add_argument("--timeout", type=float, default=600, help="seconds a module may run before it is stopped")
    args = ap.parse_args()

    todo = args.modules or modules()
    if args.coverage:
        # Old data files only: ".coverage*" would also match .coveragerc.
        for f in [ROOT / ".coverage", *ROOT.glob(".coverage.*")]:
            f.unlink(missing_ok=True)
    started = time.monotonic()
    failed: list[tuple[str, str]] = []
    total = 0
    took_by_module = _timings()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run, m, args.coverage, args.timeout) for m in todo]
        for fut in as_completed(futures):
            module, code, ran, out, secs = fut.result()
            total += ran
            took_by_module[module] = round(secs, 2)
            if code != 0:
                failed.append((module, out))
                print(f"FAIL  {module} ({ran} tests, {secs:.1f}s)", flush=True)

    try:
        TIMINGS.write_text(json.dumps(took_by_module, indent=1, sort_keys=True) + "\n")
    except OSError:
        pass  # a read-only checkout still runs
    if args.coverage:
        subprocess.run([sys.executable, "-m", "coverage", "combine", "-q"], cwd=ROOT, check=True)
    for module, out in failed:
        print(f"\n{'=' * 70}\n{module}\n{'=' * 70}\n{out}")
    took = time.monotonic() - started
    print(f"\nRan {total} tests in {len(todo)} modules in {took:.1f}s with {args.jobs} processes: "
          + (f"FAILED ({len(failed)} modules)" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
