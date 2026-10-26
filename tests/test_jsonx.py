"""orjson/stdlib JSON parity (kowan/jsonx.py) and the optional uvloop selection.

The orjson fast path must produce the same JSON value, in the same key order,
as the stdlib code it replaced (``json.dumps(..., separators=(",", ":"),
default=str)`` over models dumped by alias), and both paths emit ``null`` for
NaN/Infinity. Tests run whether or not orjson is installed.
"""

import asyncio
import dataclasses
import datetime
import json
import math
import os
import unittest
import uuid
from unittest import mock

from pydantic import BaseModel, ConfigDict, Field

from kowan import eventloop, jsonx
from kowan.web import httpx


class _Inner(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    repo_tags: list = Field(default_factory=list, alias="repoTags")


class _Outer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    image_id: str = Field(default="", alias="imageId")
    inner: _Inner = Field(default_factory=_Inner)
    when: datetime.datetime = datetime.datetime(2026, 1, 2, 3, 4, 5)


@dataclasses.dataclass
class _DC:
    a: int = 1


def _stdlib_reference(data):
    """What json_response produced before orjson (models dumped by alias)."""
    return json.dumps(jsonx.encode_models(data), separators=(",", ":"), default=str)


SAMPLES = [
    {"z": 1, "a": 2, "m": [1, 2.5, None, True, "x"]},  # key order kept
    [_Outer(imageId="sha256:1", inner=_Inner(repoTags=["a:1"]))],
    {"host": _Outer(), "nested": {"list": [_Inner(repoTags=["b"])]}},
    {"dt": datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)},
    {"d": datetime.date(2026, 10, 25), "u": uuid.UUID(int=5), "dc": _DC(), "b": b"raw"},
    {1: "int key", "s": {3, 4} and "set-ish"},
    {"big": 2**70, "neg": -(2**65)},
    {"text": "caffè ✓   \"quoted\" \\ \n"},
    (1, 2, 3),
    [],
    {},
]


class TestDumpsParity(unittest.TestCase):
    def test_same_json_value_and_key_order_as_stdlib(self):
        for data in SAMPLES:
            with self.subTest(data=repr(data)[:60]):
                ref = _stdlib_reference(data)
                got = jsonx.dumps_response(data).decode("utf-8")
                self.assertEqual(json.loads(got), json.loads(ref))
                # key order: re-serializing both parsed values must match exactly
                self.assertEqual(
                    json.dumps(json.loads(got), ensure_ascii=False),
                    json.dumps(json.loads(ref), ensure_ascii=False),
                )

    def test_ascii_payloads_are_byte_identical(self):
        for data in SAMPLES:
            ref = _stdlib_reference(data)
            if not ref.isascii() or "\\u" in ref:
                continue
            with self.subTest(data=repr(data)[:60]):
                self.assertEqual(jsonx.dumps_response(data), ref.encode())

    def test_nan_and_inf_become_null(self):
        data = {"cpu": float("nan"), "l": [float("inf"), -float("inf"), 1.5]}
        fast = jsonx.dumps_response(data)
        with mock.patch.object(jsonx, "_orjson", None):
            slow = jsonx.dumps_response(data)
        for out in (fast, slow):
            self.assertNotIn(b"NaN", out)
            self.assertNotIn(b"Infinity", out)
            self.assertEqual(json.loads(out), {"cpu": None, "l": [None, None, 1.5]})

    def test_stdlib_fallback_matches_reference(self):
        with mock.patch.object(jsonx, "_orjson", None):
            for data in SAMPLES:
                with self.subTest(data=repr(data)[:60]):
                    self.assertEqual(
                        jsonx.dumps_response(data), _stdlib_reference(data).encode()
                    )

    def test_strict_dumps_still_rejects_unknown_types(self):
        with self.assertRaises(TypeError):
            jsonx.dumps({"x": object()})
        self.assertEqual(json.loads(jsonx.dumps({"ok": True, "meta": [1]})), {"ok": True, "meta": [1]})


class TestLoads(unittest.TestCase):
    def test_round_trip_and_types(self):
        raw = b'{"a":[1,2.5,"x",null,true],"b":{"c":18446744073709551616}}'
        self.assertEqual(jsonx.loads(raw), json.loads(raw))
        self.assertEqual(jsonx.loads(raw.decode()), json.loads(raw))

    def test_stdlib_only_inputs_still_accepted(self):
        # The stdlib accepts NaN literals; orjson doesn't -> fallback keeps them.
        self.assertTrue(math.isnan(jsonx.loads(b'{"x":NaN}')["x"]))

    def test_errors_keep_stdlib_messages(self):
        bad = b'{"a": "unterminated'
        with self.assertRaises(ValueError) as fast:
            jsonx.loads(bad)
        with self.assertRaises(ValueError) as ref:
            json.loads(bad)
        self.assertEqual(str(fast.exception), str(ref.exception))


class TestHttpHelpers(unittest.TestCase):
    def test_json_response_body(self):
        resp = httpx.json_response([_Outer(imageId="i")], status=201)
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.content_type, "application/json")
        self.assertEqual(
            json.loads(resp.body),
            [{"imageId": "i", "inner": {"repoTags": []}, "when": "2026-01-02 03:04:05"}],
        )

    def test_json_response_none_is_empty(self):
        self.assertIsNone(httpx.json_response(None, 204).body)


class TestEventLoop(unittest.TestCase):
    def test_opt_out_env(self):
        with mock.patch.dict(os.environ, {"KOWAN_UVLOOP": "0"}):
            self.assertIsNone(eventloop.loop_factory())

    def test_run_executes_coroutine_on_selected_loop(self):
        async def main():
            return type(asyncio.get_running_loop()).__module__

        mod = eventloop.run(main())
        if eventloop.loop_factory() is not None:
            self.assertTrue(mod.startswith("uvloop"))
        else:
            self.assertTrue(mod.startswith("asyncio"))
        loop = eventloop.new_event_loop()
        try:
            self.assertEqual(loop.run_until_complete(asyncio.sleep(0, "ok")), "ok")
        finally:
            loop.close()
        asyncio.set_event_loop_policy(None)


if __name__ == "__main__":
    unittest.main()
