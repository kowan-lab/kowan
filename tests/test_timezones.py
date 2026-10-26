"""Schedules accept IANA time zones even where the system has no zoneinfo
database (the images start FROM scratch): the tzdata package supplies it."""

import unittest
from unittest import mock


class TestZones(unittest.TestCase):
    def test_zone_resolves_without_system_database(self):
        import zoneinfo

        with mock.patch.object(zoneinfo, "TZPATH", ()):
            zoneinfo.reset_tzpath(to=[])
            try:
                zoneinfo.ZoneInfo.clear_cache()
                self.assertEqual(str(zoneinfo.ZoneInfo("Europe/Rome")), "Europe/Rome")
            finally:
                zoneinfo.reset_tzpath()


if __name__ == "__main__":
    unittest.main()
