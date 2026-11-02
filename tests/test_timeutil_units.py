"""app.timeutil: UTC with Z on the way out, JUDGE_TIMEZONE for people and for times without an offset."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.timeutil import TimezoneError, display, iso_utc, parse_local, zone  # noqa: E402

NSK = timezone(timedelta(hours=7))


class TimeutilTests(unittest.TestCase):
    def test_iso_utc(self):
        moment = datetime(2026, 11, 27, 1, 6, 45, 398292, tzinfo=NSK)
        self.assertEqual(iso_utc(moment), "2026-11-26T18:06:45.398292Z")
        self.assertEqual(iso_utc(datetime(2026, 1, 1, 0, 0)), "2026-01-01T00:00:00Z")  # naive is taken as UTC
        self.assertIsNone(iso_utc(None))

    def test_zone(self):
        self.assertIs(zone(None), timezone.utc)
        self.assertIs(zone("utc"), timezone.utc)
        self.assertEqual(datetime(2026, 7, 1, tzinfo=zone("Asia/Novosibirsk")).utcoffset(), timedelta(hours=7))
        for bad in ("Mars/Olympus", "../etc/passwd"):
            with self.subTest(bad=bad), self.assertRaises(TimezoneError):
                zone(bad)

    def test_display_and_parse_local(self):
        tz = zone("Asia/Novosibirsk")
        moment = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(display(moment, tz), "2026-11-10 09:00")
        self.assertEqual(display(None, tz), "-")
        self.assertEqual(parse_local("2026-11-10 09:00", tz), moment)
        self.assertEqual(parse_local("2026-11-10T09:00:00+07:00", timezone.utc), moment)
        self.assertEqual(parse_local("2026-11-10T02:00Z", tz), moment)


if __name__ == "__main__":
    unittest.main()


class StoredTimeTests(unittest.TestCase):
    def test_every_time_column_is_timestamptz(self):
        """A naive timestamp column would store local wall time and break the UTC rule (D-014)."""
        from sqlalchemy import DateTime

        from app.models import Base

        naive = [f"{table.name}.{column.name}" for table in Base.metadata.tables.values() for column in table.columns
                 if isinstance(column.type, DateTime) and not column.type.timezone]
        self.assertEqual(naive, [])
        self.assertIn("due_at", Base.metadata.tables["deadline_extensions"].columns)  # the new tables are covered
