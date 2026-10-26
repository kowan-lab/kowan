"""Fast JSON with a stdlib fallback.

`orjson <https://github.com/ijl/orjson>`_ (Rust) serializes/parses several times
faster than :mod:`json`, which matters on the hot paths: every REST response
(``kowan.web.httpx.json_response``), request bodies (``read_json``), Docker Engine
responses (``LocalClient._request_json``) and the agent RPC frames. It's an
*optional* dependency: when it isn't installed (or a platform has no wheel) we
transparently use the stdlib, and the output is the same JSON value either way.

Parity rules with the stdlib code this replaces:

* Compact separators, insertion key order (orjson preserves dict order).
* ``default=str`` semantics: datetimes, dataclasses, bytes, sets... are passed
  through to :func:`_default`, which stringifies them exactly like
  ``json.dumps(..., default=str)`` did; pydantic models dump ``by_alias=True``.
* Non-finite floats become ``null`` in *both* paths (stdlib would emit the
  invalid literal ``NaN``).
* Anything orjson refuses (ints beyond 64 bits, nesting deeper than 254) falls
  back to the stdlib encoder, so nothing that used to serialize starts failing.
* Parsing: orjson first; on any failure we re-parse with the stdlib, so inputs
  the stdlib accepted are still accepted and error messages stay the stdlib's.

One observable difference: orjson writes non-ASCII characters as UTF-8 instead
of ``\\uXXXX`` escapes. Both are the same JSON value (bodies are served as
``charset=utf-8``) and every client decodes them identically.
"""

from __future__ import annotations

import json
import math
from typing import Any, Union

try:  # optional fast path
    import orjson as _orjson
except Exception:  # pragma: no cover - exercised only without the wheel
    _orjson = None

HAVE_ORJSON = _orjson is not None

if _orjson is not None:
    _OPTS = (
        _orjson.OPT_NON_STR_KEYS
        | _orjson.OPT_PASSTHROUGH_DATETIME
        | _orjson.OPT_PASSTHROUGH_DATACLASS
    )
    _ORJSON_ERRORS: tuple = (_orjson.JSONEncodeError, TypeError, ValueError, OverflowError)
else:  # pragma: no cover
    _OPTS = 0
    _ORJSON_ERRORS = ()


def _default(o: Any) -> Any:
    """``default=`` hook: pydantic models by alias, everything else ``str(o)``."""
    dump = getattr(o, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True)
    return str(o)


def encode_models(data: Any) -> Any:
    """Recursively dump pydantic models (by alias) inside dicts/lists/tuples."""
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True)
    if isinstance(data, dict):
        return {k: encode_models(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [encode_models(v) for v in data]
    return data


def _finite(data: Any) -> Any:
    """Replace NaN/Infinity with None (what orjson does), recursively."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {k: _finite(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_finite(v) for v in data]
    return data


def _std_dumps(data: Any, default: Any) -> bytes:
    try:
        s = json.dumps(data, separators=(",", ":"), default=default, allow_nan=False)
    except ValueError:
        # Out-of-range float somewhere: null it out like orjson, then retry.
        s = json.dumps(
            _finite(encode_models(data) if default is not None else data),
            separators=(",", ":"),
            default=default,
        )
    return s.encode("utf-8")


def dumps_response(data: Any) -> bytes:
    """Serialize an HTTP response body (models by alias, ``default=str``)."""
    if _orjson is not None:
        try:
            return _orjson.dumps(data, default=_default, option=_OPTS)
        except _ORJSON_ERRORS:
            pass
    return _std_dumps(encode_models(data), str)


def dumps(data: Any) -> bytes:
    """Strict compact serialization (no ``default``): unknown types raise TypeError."""
    if _orjson is not None:
        try:
            return _orjson.dumps(data, option=_orjson.OPT_NON_STR_KEYS)
        except _ORJSON_ERRORS:
            pass
    return _std_dumps(data, None)


def loads(data: Union[bytes, bytearray, memoryview, str]) -> Any:
    """Parse JSON; raises ``ValueError`` (``json.JSONDecodeError``) / ``UnicodeDecodeError``."""
    if _orjson is not None:
        try:
            return _orjson.loads(data)
        except (ValueError, TypeError):
            pass  # re-parse with the stdlib: same acceptance, same messages
    return json.loads(data)
