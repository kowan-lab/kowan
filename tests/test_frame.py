import unittest

from kowan.mux import frame


class TestFrame(unittest.TestCase):
    def test_data_roundtrip(self):
        raw = frame.encode(frame.TYPE_DATA, 0, 7, payload=b"hello")
        typ, flags, sid, length, payload = frame.decode(raw)
        self.assertEqual(typ, frame.TYPE_DATA)
        self.assertEqual(flags, 0)
        self.assertEqual(sid, 7)
        self.assertEqual(length, 5)
        self.assertEqual(payload, b"hello")

    def test_window_update_carries_delta_in_length(self):
        raw = frame.encode(frame.TYPE_WINDOW_UPDATE, 0, 3, length=4096)
        typ, flags, sid, length, payload = frame.decode(raw)
        self.assertEqual(typ, frame.TYPE_WINDOW_UPDATE)
        self.assertEqual(sid, 3)
        self.assertEqual(length, 4096)
        self.assertEqual(payload, b"")

    def test_flags_bitmask(self):
        raw = frame.encode(frame.TYPE_DATA, frame.FLAG_SYN | frame.FLAG_FIN, 9)
        typ, flags, sid, length, payload = frame.decode(raw)
        self.assertTrue(flags & frame.FLAG_SYN)
        self.assertTrue(flags & frame.FLAG_FIN)
        self.assertFalse(flags & frame.FLAG_RST)

    def test_short_frame_raises(self):
        with self.assertRaises(ValueError):
            frame.decode(b"\x00\x01")


if __name__ == "__main__":
    unittest.main()
