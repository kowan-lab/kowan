"""Pure policy logic for auto-update: modes, labels, windows, durations.

Everything here is deterministic and I/O free (``tests/test_autoupdate.py``).

Label contract (documented in docs/labels.md)::

    kowan.update=off|notify|pull|apply
    kowan.update.window=<window>          see parse_window
    kowan.update.min-age=2d               see parse_duration
    kowan.scan.block=never|critical|high|worse
    kowan.notify=off                      no monitor / auto-update alerts
    kowan.hidden=true                     hidden from lists by default (UI)

Precedence: container label > stored policy (UI/API) > global default. The
resolved policy carries a ``sources`` map so the UI can show "from label" and
render that field read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from kowan.scan.gate import BLOCK_NEVER, parse_block_policy

LABEL_UPDATE = "kowan.update"
LABEL_WINDOW = "kowan.update.window"
LABEL_MIN_AGE = "kowan.update.min-age"
LABEL_SCAN_BLOCK = "kowan.scan.block"
LABEL_NOTIFY = "kowan.notify"
LABEL_HIDDEN = "kowan.hidden"

MODE_OFF = "off"
MODE_NOTIFY = "notify"
MODE_PULL = "pull"
MODE_APPLY = "apply"
MODES = (MODE_OFF, MODE_NOTIFY, MODE_PULL, MODE_APPLY)


_FALSY = {"off", "false", "0", "no", "none", "disabled"}
_TRUTHY = {"true", "1", "yes", "on"}


def parse_mode(value: Optional[str]) -> str:
    key = (value or "").strip().lower() or MODE_OFF
    if key not in MODES:
        raise ValueError(f"invalid update mode {value!r} (use off|notify|pull|apply)")
    return key


def notify_disabled(labels: Optional[Dict[str, str]]) -> bool:
    """``kowan.notify=off`` (or false/0/no): suppress alerts for this container."""
    return str((labels or {}).get(LABEL_NOTIFY, "")).strip().lower() in _FALSY


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(w|d|h|m|s)")
_UNIT = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(value: Optional[str]) -> Optional[timedelta]:
    """``2d``, ``36h``, ``1d12h``, ``90m``, ``1w``; a bare number means days.
    Empty -> ``None`` (no minimum). Raises ``ValueError`` when malformed."""
    s = (value or "").strip().lower().replace(" ", "")
    if not s or s in ("0", "off", "none"):
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return timedelta(days=float(s))
    pos = 0
    total = 0.0
    for m in _DUR_RE.finditer(s):
        if m.start() != pos:
            raise ValueError(f"invalid duration {value!r} (e.g. 2d, 12h, 1d12h)")
        total += float(m.group(1)) * _UNIT[m.group(2)]
        pos = m.end()
    if pos != len(s) or total <= 0:
        raise ValueError(f"invalid duration {value!r} (e.g. 2d, 12h, 1d12h)")
    return timedelta(seconds=total)


def format_duration(td: Optional[timedelta]) -> str:
    if td is None:
        return ""
    secs = int(td.total_seconds())
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            parts.append(f"{secs // size}{unit}")
            secs %= size
    return "".join(parts) or f"{secs}s"


# ---------------------------------------------------------------------------
# Maintenance windows
# ---------------------------------------------------------------------------

_DAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def _parse_days(tok: str) -> Set[int]:
    out: Set[int] = set()
    for part in tok.lower().split(","):
        part = part.strip()
        if not part:
            continue
        if part in ("daily", "everyday", "*"):
            return set(range(7))
        if part == "weekdays":
            out |= {0, 1, 2, 3, 4}
            continue
        if part == "weekends":
            out |= {5, 6}
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in _DAYS or b not in _DAYS:
                raise ValueError(f"invalid weekday range {part!r}")
            i, j = _DAYS[a], _DAYS[b]
            k = i
            while True:
                out.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
            continue
        if part not in _DAYS:
            raise ValueError(f"invalid weekday {part!r}")
        out.add(_DAYS[part])
    return out


@dataclass
class Window:
    """A maintenance window, evaluated in the control plane's local time.

    Either a daily time range (``start``/``end`` minutes since midnight,
    ``end <= start`` = overnight, the night belongs to its start day) limited
    to ``days`` (Mon=0), or a cron schedule that opens the window at each
    match for ``duration``.
    """

    spec: str = ""
    start: int = 0
    end: int = 0
    days: Set[int] = field(default_factory=lambda: set(range(7)))
    cron: Any = None
    duration: timedelta = timedelta(hours=1)

    def is_open(self, now: datetime) -> bool:
        if self.cron is not None:
            t = now.replace(second=0, microsecond=0)
            for m in range(int(self.duration.total_seconds() // 60) + 1):
                if self.cron.matches(t - timedelta(minutes=m)):
                    if m == 0 or (now - (t - timedelta(minutes=m))) < self.duration:
                        return True
            return False
        mins = now.hour * 60 + now.minute
        today = now.weekday()
        yesterday = (today - 1) % 7
        if self.start < self.end:
            return today in self.days and self.start <= mins < self.end
        # overnight (or 24h when start == end)
        if mins >= self.start and today in self.days:
            return True
        return mins < self.end and yesterday in self.days

    def next_open(self, now: datetime, horizon: timedelta = timedelta(days=8)) -> Optional[datetime]:
        """The next minute at which the window is open (``now`` when open)."""
        if self.is_open(now):
            return now
        t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = now + horizon
        while t <= end:
            if self.is_open(t):
                return t
            t += timedelta(minutes=1)
        return None

    def describe(self) -> str:
        if self.cron is not None:
            return f"cron {self.spec}"
        days = "daily" if len(self.days) == 7 else ",".join(_DAY_NAMES[d] for d in sorted(self.days))
        return f"{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d} {days}"


def parse_window(value: Optional[str]) -> Optional[Window]:
    """Parse a maintenance window; empty -> ``None`` (always open).

    Accepted forms::

        02:00-05:00                 every day
        02:00-05:00 sat,sun         weekend nights (tokens in any order)
        mon-fri 22:00-06:00         overnight, starting Mon..Fri
        cron 0 3 * * 6 2h           opens at each cron match for 2h (default 1h)
        0 3 * * 6                   cron without the keyword, 1h
        @daily 3h                   cron macros are fine too
    """
    s = (value or "").strip()
    if not s:
        return None
    toks = s.split()
    if toks[0].lower() == "cron":
        toks = toks[1:]
    is_cron = bool(toks) and (toks[0].startswith("@") or (len(toks) >= 5 and not _RANGE_RE.match(toks[0])
                                                          and toks[0].lower().rstrip(",") not in _DAYS
                                                          and not _RANGE_RE.match(toks[-1])))
    if is_cron:
        from kowan.workflow.scheduler import CronParseError, parse_cron

        n = 1 if toks[0].startswith("@") else 5
        expr = " ".join(toks[:n])
        rest = toks[n:]
        if len(rest) > 1:
            raise ValueError(f"invalid window {value!r}: expected '<cron> [duration]'")
        dur = parse_duration(rest[0]) if rest else timedelta(hours=1)
        if dur is None or dur > timedelta(days=1):
            raise ValueError("window duration must be between 1m and 24h")
        try:
            sched = parse_cron(expr)
        except CronParseError as err:
            raise ValueError(f"invalid cron window {expr!r}: {err}") from err
        return Window(spec=" ".join(toks), cron=sched, duration=dur)

    rng: Optional[Tuple[int, int]] = None
    days: Set[int] = set()
    for tok in toks:
        m = _RANGE_RE.match(tok)
        if m:
            if rng is not None:
                raise ValueError(f"invalid window {value!r}: more than one time range")
            h1, m1, h2, m2 = (int(x) for x in m.groups())
            if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
                raise ValueError(f"invalid time range {tok!r}")
            rng = ((h1 % 24) * 60 + m1, (h2 % 24) * 60 + m2 if not (h2 == 24 and m2 == 0) else 0)
            continue
        days |= _parse_days(tok)
    if rng is None:
        raise ValueError(f"invalid window {value!r} (e.g. '02:00-05:00 sat,sun' or 'cron 0 3 * * 6 2h')")
    return Window(spec=s, start=rng[0], end=rng[1], days=days or set(range(7)))


# ---------------------------------------------------------------------------
# Effective policy (labels > stored > defaults)
# ---------------------------------------------------------------------------

@dataclass
class EffectivePolicy:
    mode: str = MODE_OFF
    window: str = ""
    min_age: str = ""
    scan_block: str = BLOCK_NEVER
    sources: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    notify: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "window": self.window, "minAge": self.min_age, "scanBlock": self.scan_block,
            "sources": dict(self.sources), "errors": list(self.errors), "notify": self.notify,
        }

    @property
    def window_obj(self) -> Optional[Window]:
        try:
            return parse_window(self.window)
        except ValueError:
            return None

    @property
    def min_age_td(self) -> Optional[timedelta]:
        try:
            return parse_duration(self.min_age)
        except ValueError:
            return None


_FIELDS = (
    # (field, label, stored key, parser)
    ("mode", LABEL_UPDATE, "mode", parse_mode),
    ("window", LABEL_WINDOW, "window", lambda v: (parse_window(v), (v or "").strip())[1]),
    ("min_age", LABEL_MIN_AGE, "minAge", lambda v: (parse_duration(v), (v or "").strip())[1]),
    ("scan_block", LABEL_SCAN_BLOCK, "scanBlock", parse_block_policy),
)


def resolve_policy(
    labels: Optional[Dict[str, str]],
    stored: Optional[Dict[str, Any]] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> EffectivePolicy:
    """Merge label / stored / default values field by field.

    An invalid label value is reported in ``errors`` and ignored (the stored
    or default value applies); an invalid *mode* label falls back to ``off``
    rather than to a possibly more aggressive stored mode.
    """
    labels = labels or {}
    stored = stored or {}
    defaults = defaults or {}
    out = EffectivePolicy(notify=not notify_disabled(labels))
    for attr, label, key, parser in _FIELDS:
        value: Any = None
        source = "default"
        if label in labels:
            try:
                value = parser(labels[label])
                source = "label"
            except ValueError as err:
                out.errors.append(f"{label}: {err}")
                if attr == "mode":
                    value, source = MODE_OFF, "label"
        if value is None and stored.get(key) not in (None, ""):
            try:
                value = parser(stored[key])
                source = "policy"
            except ValueError as err:
                out.errors.append(f"stored {key}: {err}")
        if value is None:
            dv = defaults.get(key)
            try:
                value = parser(dv) if dv not in (None, "") else None
            except ValueError:
                value = None
            source = "default"
        if value is None:
            value = getattr(EffectivePolicy, attr) if attr != "scan_block" else BLOCK_NEVER
            if attr in ("window", "min_age"):
                value = ""
        setattr(out, attr, value)
        out.sources[_camel(attr)] = source
    return out


def _camel(attr: str) -> str:
    head, *rest = attr.split("_")
    return head + "".join(p.capitalize() for p in rest)


def validate_policy_body(body: Dict[str, Any]) -> Dict[str, str]:
    """Validate/normalise a stored-policy body from the API; raises ValueError."""
    out = {
        "mode": parse_mode(body.get("mode", MODE_OFF)),
        "window": str(body.get("window") or "").strip(),
        "minAge": str(body.get("minAge") or "").strip(),
        "scanBlock": str(body.get("scanBlock") or "").strip(),
    }
    parse_window(out["window"])
    parse_duration(out["minAge"])
    if out["scanBlock"]:
        out["scanBlock"] = parse_block_policy(out["scanBlock"])
    return out


def is_default_policy(p: Optional[Dict[str, Any]]) -> bool:
    """A stored policy that changes nothing: mode off (the default) and every
    other field empty (inherit). Saving one deletes the row instead, so turning
    a knob back to off leaves nothing behind."""
    if not p:
        return True
    return (parse_mode(p.get("mode") or MODE_OFF) == MODE_OFF
            and not any(str(p.get(k) or "").strip() for k in ("window", "minAge", "scanBlock")))


def age_ok(min_age: Optional[timedelta], first_seen: Optional[datetime], created: Optional[datetime],
           now: datetime) -> Tuple[bool, Optional[timedelta]]:
    """Minimum-age gate. The image age is the larger of "since Kowan first saw
    this digest" and "since the image was built" (when the new image is local
    and its Created time is plausible). Returns ``(ok, age)``."""
    if min_age is None:
        return True, None
    ages = []
    if first_seen is not None:
        ages.append(now - first_seen)
    if created is not None and created.year >= 2015 and created <= now:
        ages.append(now - created)
    if not ages:
        return False, None
    age = max(ages)
    return age >= min_age, age
