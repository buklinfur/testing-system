"""The sweep of throw-away schemas left by interrupted runs (tests/pg/pgbase.py): only <t|m>_<unixtime>_<hex> older
than 6 hours goes; a fresh one, the older format without a time and every other schema stay.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import time
import unittest

from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import create_engine, text

    from tests.pg import pgbase


@requires_pg
class SweepTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(URL)
        self.addCleanup(self.engine.dispose)
        self.created = []

        def drop_left():
            with self.engine.begin() as conn:
                for name in self.created:
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        self.addCleanup(drop_left)

    def make(self, name):
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{name}"'))
            conn.execute(text(f'CREATE TABLE "{name}".x (id int)'))  # a schema with something in it
        self.created.append(name)
        return name

    def existing(self):
        with self.engine.connect() as conn:
            return set(conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars())

    def test_only_stale_schemas_of_the_format_go(self):
        now = time.time()
        stale_t = self.make(pgbase.schema_name("t", now - 7 * 3600))
        stale_m = self.make(pgbase.schema_name("m", now - 7 * 3600))
        fresh = self.make(pgbase.schema_name("t", now - 60))
        almost = self.make(pgbase.schema_name("t", now - 6 * 3600 + 60))  # just under 6 h: a long run, still going
        old_format = self.make("t_" + "a1b2c3d4e5")  # the format before the sweep: no time, never touched
        foreign = [self.make(f"x_{int(now - 7 * 3600)}_a1b2c3d4e5"), self.make(f"t_{int(now - 7 * 3600)}_notahexvalue")]
        with self.engine.begin() as conn:
            dropped = pgbase.sweep_stale_schemas(conn, now=now)
        self.assertIn(stale_t, dropped)
        self.assertIn(stale_m, dropped)
        left = self.existing()
        self.assertFalse({stale_t, stale_m} & left)
        self.assertLessEqual({fresh, almost, old_format, *foreign, "public"}, left)
        self.assertFalse(set(dropped) & {fresh, almost, old_format, *foreign})

    def test_names_carry_their_time(self):
        name = pgbase.schema_name("t", 1_700_000_000)
        self.assertRegex(name, pgbase.SCHEMA_NAME)
        self.assertTrue(name.startswith("t_1700000000_"))
        self.assertLessEqual(len(name), 63, "PostgreSQL identifiers are at most 63 bytes")


if __name__ == "__main__":
    unittest.main()
