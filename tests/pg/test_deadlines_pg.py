"""Deadlines of contest problems and extensions (D-023) on a real PostgreSQL: contest.yaml fields and the warnings of
apply, extensions (refusals, replacing, audit), who an extension applies to, submission_decision, `late` fixed at
receipt, judgectl extension and dev_seed."""
import contextlib
import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 10:00 in Novosibirsk
DUE = datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)  # 12:00 in Novosibirsk
US = timedelta(microseconds=1)

if URL and HAVE_SA:
    from sqlalchemy import select

    from app import db, dev_seed
    from app import groups as g
    from app.auth import audit
    from app.contests import access, deadlines, service
    from app.contests.service import ContestError
    from app.contests.spec import from_data, parse
    from app.jsonable import plain
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import AfterDueSubmit, AuditLog, ContestState, Role, Submission, User
    from app.settings import Settings
    from app.timeutil import zone

NSK = "Asia/Novosibirsk"


def contest_yaml(problem="{label: A, problem: sum, due_at: '2026-11-10 12:00'}", slug="lesson", mode="open",
                 starts="2026-11-10 09:00", ends="2026-11-10 18:00"):
    return (f"format_version: 1\nslug: {slug}\ntitle: Урок\nstarts_at: {starts}\nends_at: {ends}\n"
            f"access: {{mode: {mode}}}\nproblems:\n  - {problem}\n")


@requires_pg
class DeadlineTests(PgCase):
    def setUp(self):
        super().setUp()
        self.actor = audit.Actor(cli="cli:judge")
        self.a, self.b = self.student(1), self.student(2)
        self.s.commit()

    def apply(self, text):
        result = service.apply(self.s, self.actor, parse(text, zone(NSK)), T0)
        self.s.commit()
        return result

    def published(self, text):
        c = self.apply(text).contest
        service.set_state(self.s, self.actor, c, ContestState.published, T0)
        self.s.commit()
        return c

    def refused(self, code, func, *args, **kw):
        with self.assertRaises(ContestError) as ctx:
            func(*args, **kw)
        self.s.rollback()
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def extend(self, c, due, **kw):
        row, old = deadlines.add_extension(self.s, self.actor, c, "A", due, T0, **kw)
        self.s.commit()
        return row, old

    # ---------------------------------------------------------------- contest.yaml
    def test_apply_sets_deadlines_and_gives_them_back(self):
        text = contest_yaml("{label: A, problem: sum, available_from: '2026-11-10 09:30', due_at: '2026-11-10 12:00', "
                            "after_due: {statement: hidden, submit: 'no'}, note_md: 'Сдать до обеда'}")
        result = self.apply(text)
        cp = result.contest.problems[0]
        self.assertEqual((cp.available_from, cp.due_at, cp.after_due_statement.value, cp.after_due_submit.value,
                          cp.note_md), (T0 - timedelta(minutes=30), DUE, "hidden", "no", "Сдать до обеда"))
        self.assertEqual(result.warnings, [])
        self.assertEqual(self.apply(text).changes, [])  # idempotent
        spec = service.contest_detail(self.s, result.contest)["spec"]
        self.assertEqual(spec["problems"][0]["after_due"], {"statement": "hidden", "submit": "no"})
        again = service.apply(self.s, self.actor, from_data(plain(spec), zone(NSK)), T0)  # what GET gives, PUT takes
        self.assertEqual(again.changes, [])
        self.s.rollback()
        plain_again = self.apply(contest_yaml("{label: A, problem: sum}"))  # everything back to the defaults
        self.assertEqual(sorted(c.split(": ", 1)[1].split(" ")[0] for c in plain_again.changes),
                         ["after_due_statement", "after_due_submit", "available_from", "due_at", "note"])
        self.assertNotIn("after_due", service.contest_detail(self.s, plain_again.contest)["spec"]["problems"][0])

    def test_apply_warns_about_minute_59_and_extensions_left_without_effect(self):
        result = self.apply(contest_yaml("{label: A, problem: sum, due_at: '2026-11-10 23:59'}"))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("HH:59:59", result.warnings[0])
        for fine in ("2026-11-10 23:59:59", "2026-11-10 23:58", "2026-11-10 12:00"):
            with self.subTest(fine):
                self.assertEqual(self.apply(contest_yaml(f"{{label: A, problem: sum, due_at: '{fine}'}}")).warnings, [])
        c = result.contest
        self.extend(c, DUE + timedelta(hours=1), user=self.a)  # 13:00
        later = self.apply(contest_yaml("{label: A, problem: sum, due_at: '2026-11-10 14:00'}"))
        self.assertEqual(len(later.warnings), 1)
        self.assertIn("not earlier than 1 extension(s)", later.warnings[0])
        self.assertEqual(self.apply(contest_yaml("{label: A, problem: sum, due_at: '2026-11-10 12:30'}")).warnings, [])
        removed = self.apply(contest_yaml("{label: A, problem: sum}"))
        self.assertIn("the deadline is removed, so its 1 extension(s) mean nothing now", removed.warnings[0])

    # ---------------------------------------------------------------- extensions
    def test_extension_rules_and_replacing(self):
        c = self.published(contest_yaml())
        self.refused("extension_not_later", deadlines.add_extension, self.s, self.actor, c, "A", DUE, T0, user=self.a)
        self.refused("extension_not_later", deadlines.add_extension, self.s, self.actor, c, "A", DUE - US, T0,
                     user=self.a)
        self.refused("contest_problem_not_found", deadlines.add_extension, self.s, self.actor, c, "Z", DUE, T0,
                     user=self.a)
        admin = self.s.execute(select(User).where(User.role == Role.admin)).scalars().first()
        self.refused("not_a_student", deadlines.add_extension, self.s, self.actor, c, "A", DUE + US, T0, user=admin)
        group = g.create(self.s, self.actor, "7Б", T0)
        g.archive(self.s, self.actor, group, T0)
        self.s.commit()
        self.refused("group_archived", deadlines.add_extension, self.s, self.actor, c, "A", DUE + US, T0, group=group)

        row, old = self.extend(c, DUE + timedelta(hours=1), user=self.a, reason="болел")
        self.assertIsNone(old)
        again, old = self.extend(c, DUE + timedelta(hours=2), user=self.a)  # В7: replaces, the old one is in the audit
        self.assertEqual((again.id, old), (row.id, DUE + timedelta(hours=1)))
        entries = self.s.execute(select(AuditLog).where(AuditLog.action == "contest.extension_add")
                                 .order_by(AuditLog.id)).scalars().all()
        self.assertEqual([(e.details["user"], e.details["replaced"]) for e in entries],
                         [("student1", None), ("student1", "2026-11-10T06:00:00Z")])
        listed = deadlines.list_extensions(self.s, c)
        self.assertEqual([(r["user"], r["due_at"]) for r in listed], [("student1", DUE + timedelta(hours=2))])
        deadlines.remove_extension(self.s, self.actor, row.id, T0)
        self.s.commit()
        self.assertEqual(deadlines.list_extensions(self.s, c, "A"), [])
        self.refused("extension_not_found", deadlines.remove_extension, self.s, self.actor, row.id, T0)

        no_due = self.published(contest_yaml("{label: A, problem: sum}", slug="practice"))
        self.refused("no_deadline", deadlines.add_extension, self.s, self.actor, no_due, "A", DUE, T0, user=self.a)

    def test_an_extension_is_for_its_student_and_for_current_members_of_its_group(self):
        c = self.published(contest_yaml("{label: A, problem: sum, due_at: '2026-11-10 12:00', after_due: {submit: 'no'}}"))
        cp = c.problems[0]
        for user in (self.a, self.b):
            service.enter(self.s, user, c, T0)
        self.s.commit()
        late = DUE + timedelta(minutes=30)
        self.extend(c, DUE + timedelta(hours=1), user=self.a)
        decide = lambda user: service.submission_decision(self.s, user, c, cp, late)  # noqa: E731
        self.assertTrue(decide(self.a))
        self.assertEqual(decide(self.b).reason, "deadline_passed")  # only for that student

        group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, group, [self.b], T0)
        self.extend(c, DUE + timedelta(hours=2), group=group)
        self.assertEqual(service.extensions_for(self.s, cp, self.b), [DUE + timedelta(hours=2)])
        self.assertTrue(decide(self.b))
        self.assertEqual(sorted(service.extensions_for(self.s, cp, self.a)), [DUE + timedelta(hours=1)])
        g.remove_members(self.s, self.actor, group, [self.b], T0)  # removed: the group's extension is gone for them
        self.s.commit()
        self.assertEqual(decide(self.b).reason, "deadline_passed")
        g.add_members(self.s, self.actor, group, [self.b], T0)
        g.archive(self.s, self.actor, group, T0)  # an archived group extends nothing
        self.s.commit()
        self.assertEqual(service.extensions_for(self.s, cp, self.b), [])

    def test_late_is_fixed_at_receipt(self):
        c = self.published(contest_yaml())  # after_due submit: late (the default)
        cp = c.problems[0]
        service.enter(self.s, self.a, c, T0)
        self.s.commit()
        for now, late in ((DUE, False), (DUE + US, True)):  # due_at itself is still on time
            with self.subTest(now=now):
                d = service.submission_decision(self.s, self.a, c, cp, now)
                self.assertEqual((d.allowed, d.counted, d.late), (True, True, late))
        received = DUE + timedelta(minutes=5)
        d = service.submission_decision(self.s, self.a, c, cp, received)
        sub = Submission(user_id=self.a.id, contest_problem_id=cp.id, language_key="python", source_sha256="c" * 64,
                         created_at=received, late=d.late)
        self.s.add(sub)
        self.s.commit()
        self.extend(c, DUE + timedelta(hours=1), user=self.a)  # an extension given afterwards
        self.s.refresh(sub)
        self.assertTrue(sub.late)  # stays late
        later = service.submission_decision(self.s, self.a, c, cp, received + timedelta(minutes=1))
        self.assertEqual((later.allowed, later.late), (True, False))  # new ones are on time
        cp.after_due_submit = AfterDueSubmit.yes
        self.s.commit()
        self.assertFalse(service.submission_decision(self.s, self.b, c, cp, received).late)  # yes: never marked

    def test_submission_decision_takes_the_student_not_ids(self):
        import inspect

        params = inspect.signature(service.submission_decision).parameters
        self.assertIn("user", params)
        self.assertFalse({"user_id", "group_id", "participation_id"} & set(params))
        c = self.published(contest_yaml(mode="code"))
        self.assertEqual(service.submission_decision(self.s, self.a, c, c.problems[0], T0).reason, "not_participant")

    def test_two_submissions_at_once_cannot_both_pass_the_cooldown(self):
        """Two connections submit at the same moment (cooldown 15 s): the participation row is locked until the
        first one commits, so the second sees it and is refused."""
        import threading
        import time

        from app.models import Contest, ContestProblem

        c = self.published(contest_yaml("{label: A, problem: sum}"))  # submit_cooldown_sec: 15 by default
        service.enter(self.s, self.a, c, T0)
        self.s.commit()
        ids = (c.id, c.problems[0].id, self.a.id)
        barrier, results, errors = threading.Barrier(2), [], []

        def submit():
            session = self.Session()
            try:
                contest, cp, user = (session.get(Contest, ids[0]), session.get(ContestProblem, ids[1]),
                                     session.get(User, ids[2]))
                barrier.wait()
                decision = service.submission_decision(session, user, contest, cp, T0)
                if decision:
                    session.add(Submission(user_id=user.id, contest_problem_id=cp.id, language_key="python",
                                           source_sha256="c" * 64, created_at=T0, late=decision.late))
                    session.flush()
                    time.sleep(0.3)  # without the lock the other request would decide in this gap
                session.commit()
                results.append(decision.reason)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["cooldown", "ok"])
        self.s.expire_all()
        self.assertEqual(len(self.s.execute(select(Submission).where(Submission.user_id == self.a.id)).all()), 1)

    # ---------------------------------------------------------------- judgectl extension
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(session_factory=self.Session, settings=Settings(timezone=NSK), actor=self.actor, out=out,
                      err=err, interactive=lambda: False, clock=lambda: T0)
        code = main(list(argv), ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out

    def test_judgectl_extension(self):
        self.published(contest_yaml())
        g.create(self.s, self.actor, "7Б", T0)
        self.s.commit()
        out = self.ok("extension", "add", "lesson", "A", "--user", "student1", "--due", "2026-11-10 13:00",
                      "--reason", "болел")
        self.assertIn("lesson A: student student1 until 2026-11-10 13:00", out)
        out = self.ok("extension", "add", "lesson", "A", "--user", "student1", "--due", "2026-11-10 14:00")
        self.assertIn("was 2026-11-10 13:00", out)
        self.ok("extension", "add", "lesson", "A", "--group", "7Б", "--due", "2026-11-10T08:00:00Z")
        rows = json.loads(self.ok("extension", "list", "lesson", "--json"))
        self.assertEqual([(r["user"], r["group"], r["due_at"]) for r in rows],
                         [("student1", None, "2026-11-10T07:00:00Z"), (None, "7Б", "2026-11-10T08:00:00Z")])
        self.assertIn("BASE DUE", self.ok("extension", "list", "lesson", "A"))
        code, _, err = self.run_cli("extension", "add", "lesson", "A", "--user", "student1", "--due", "2026-11-10 11:00")
        self.assertEqual((code, "must be later than due_at" in err), (1, True))
        code, _, err = self.run_cli("extension", "add", "lesson", "A", "--user", "student1", "--due", "tomorrow")
        self.assertEqual((code, "not a time like" in err), (1, True))
        with self.assertRaises(SystemExit):  # --user or --group is required
            self.run_cli("extension", "add", "lesson", "A", "--due", "2026-11-10 13:00")
        self.assertIn("extension of group 7Б removed", self.ok("extension", "remove", str(rows[1]["id"])))
        actions = [a for a in self.s.execute(select(AuditLog.action)).scalars() if "extension" in a]
        self.assertEqual(actions, ["contest.extension_add"] * 3 + ["contest.extension_remove"])

    # ---------------------------------------------------------------- dev_seed: late and deadline_passed for real
    def test_dev_seed_writes_late_and_is_refused_after_the_deadline(self):
        blobs = tempfile.TemporaryDirectory()
        self.addCleanup(blobs.cleanup)

        def seed(*argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": blobs.name}), \
                    mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dev_seed.main(list(argv))
            return code, out.getvalue(), err.getvalue()

        window = {"starts": "2026-01-01 00:00", "ends": "2099-01-01 00:00"}  # dev_seed runs at the real now
        self.published(contest_yaml("{label: A, problem: sum, due_at: '2026-01-02 00:00'}", **window))
        code, out, err = seed("--contest", "lesson", "--label", "A")
        self.assertEqual(code, 0, err)
        made = json.loads(out)[0]
        self.assertEqual((made["counted"], made["late"]), (True, True))
        self.assertTrue(self.s.get(Submission, made["submission_id"]).late)
        self.apply(contest_yaml("{label: A, problem: sum, due_at: '2026-01-02 00:00', after_due: {submit: 'no'}}",
                                **window))
        code, _, err = seed("--contest", "lesson", "--label", "A", "--user", "2")
        self.assertEqual(code, 1)
        self.assertIn("refused by the access rules: deadline_passed", err)
