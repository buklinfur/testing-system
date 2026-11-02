"""The contest service on a real PostgreSQL: apply (R35), pin (R22), states, invite codes, join, personal start."""
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

EXAMPLES = ROOT / "problems-examples"
T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 10:00 in Novosibirsk: inside the windows below

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app.auth import audit
    from app.blobstore import BlobStore
    from app.contests import service
    from app.contests.service import INVITE_ALPHABET, ContestError
    from app.contests.spec import parse
    from app.models import (AuditLog, ContestProblem, ContestState, Judgement, ProblemVersion, Submission,
                            VersionStatus)
    from app.problems.importer import import_package
    from app.settings import ImportSettings
    from app.timeutil import zone

HEAD = """format_version: 1
slug: olymp
title: "Олимпиада"
starts_at: 2026-11-10 09:00
ends_at: 2026-11-10 18:00
"""


def problems(*entries):
    return "problems:\n" + "".join(f"  - {{{e}}}\n" for e in entries) if entries else "problems: []\n"


@requires_pg
class ContestServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blobs = BlobStore(self.tmp / "blobs")
        self.actor = audit.Actor(cli="cli:judge")
        self.versions = {}
        for name in ("sum-of-two", "pair-with-sum"):
            self.versions[(name, 1)] = self.imported(EXAMPLES / name)
        root = self.tmp / "sum-v2"
        shutil.copytree(EXAMPLES / "sum-of-two", root)
        (root / "statement" / "ru.md").write_text("Версия 2.\n", encoding="utf-8")
        self.versions[("sum-of-two", 2)] = self.imported(root)
        self.pupil = self.student(9)
        self.s.commit()

    def imported(self, root):
        v = import_package(self.s, self.blobs, ImportSettings(), self.actor, T0, directory=root).version
        self.s.commit()
        return v

    def apply(self, text):
        try:
            result = service.apply(self.s, self.actor, parse(text, zone("Asia/Novosibirsk")), T0)
            self.s.commit()
            return result
        except Exception:
            self.s.rollback()
            raise

    def rows(self, contest):
        self.s.expire_all()
        return [(r.label, r.problem_version_id, r.hidden, r.position) for r in self.s.execute(
            select(ContestProblem).where(ContestProblem.contest_id == contest.id).order_by(ContestProblem.id)).scalars()]

    def submit(self, cp_id):
        user = self.pupil
        self.s.add(Submission(user_id=user.id, contest_problem_id=cp_id, language_key="python", source_sha256="c" * 64))
        self.s.commit()

    def row_id(self, contest, label):
        return service.visible_row(self.s, contest, label).id

    # ---------------------------------------------------------------- apply
    def test_apply_creates_a_draft_and_is_idempotent(self):
        text = (ROOT / "contests-examples" / "olymp-personal.yaml").read_text(encoding="utf-8")
        text = text.replace("  - {label: C, problem: guess-number, points: 150}\n", "")
        first = self.apply(text)
        c = first.contest
        self.assertTrue(first.created)
        self.assertEqual((c.state, c.timing_mode.value, c.personal_duration), (ContestState.draft, "personal",
                                                                               timedelta(minutes=90)))
        self.assertIsNone(c.created_by)  # from the console
        self.assertEqual(len(c.invite_code), 6)
        self.assertTrue(set(c.invite_code) <= set(INVITE_ALPHABET))
        self.assertEqual([(lbl, h) for lbl, _v, h, _p in self.rows(c)], [("A", False), ("B", False)])
        audits = self.s.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "contest.apply")).scalar()
        again = self.apply(text)
        self.assertEqual((again.created, again.changes), (False, []))
        self.assertEqual(self.s.execute(select(func.count()).select_from(AuditLog)
                                        .where(AuditLog.action == "contest.apply")).scalar(), audits)  # nothing to log
        changed = self.apply(text.replace("Осенняя олимпиада, 7Б", "Олимпиада 7Б"))
        self.assertEqual(changed.changes, ["title: Осенняя олимпиада, 7Б -> Олимпиада 7Б"])
        self.assertEqual(changed.contest.invite_code, c.invite_code)  # the code survives re-applies

    def test_going_back_from_open_to_code_warns_that_the_old_code_works_again(self):
        body = problems("label: A, problem: sum-of-two")
        first = self.apply(HEAD + "access: {mode: code}\n" + body)
        code = first.contest.invite_code
        self.assertEqual(first.warnings, [])
        opened = self.apply(HEAD + "access: {mode: open}\n" + body)
        self.assertEqual((opened.warnings, opened.contest.invite_code), ([], code))  # kept, but unused while open
        back = self.apply(HEAD + "access: {mode: code}\n" + body)
        self.assertEqual(back.contest.invite_code, code)
        self.assertEqual(len(back.warnings), 1)
        self.assertIn("judgectl contest code regenerate olymp", back.warnings[0])
        self.assertNotIn(code, back.warnings[0])  # the code itself is not repeated in logs
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "contest.apply").order_by(AuditLog.id.desc())
                               .limit(1)).scalar_one()
        self.assertEqual(entry.details["warnings"], back.warnings)

    def test_a_removed_problem_is_hidden_with_submissions_and_deleted_without(self):
        c = self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 1", "label: B, problem: pair-with-sum")).contest
        a = self.row_id(c, "A")
        self.submit(a)
        result = self.apply(HEAD + problems())
        self.assertEqual(result.changes, ["A (sum-of-two): hidden (it has submissions, they stay)",
                                          "B (pair-with-sum): removed"])
        self.assertEqual(self.rows(c), [("A", self.versions[("sum-of-two", 1)].id, True, 0)])
        self.assertEqual(self.s.execute(select(Submission.contest_problem_id)).scalar_one(), a)

    def test_a_returning_problem_shows_its_newest_hidden_row_or_gets_a_new_one(self):
        """MODULE_4A 5.6: the newest hidden row comes back when its version is the one in the file (or the file
        names none); otherwise a new row is made and the hidden ones stay hidden."""
        v1, v2 = self.versions[("sum-of-two", 1)].id, self.versions[("sum-of-two", 2)].id
        c = self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 1")).contest
        row1 = self.row_id(c, "A")
        self.submit(row1)
        self.apply(HEAD + problems())  # row1 (v1) hidden
        r = self.apply(HEAD + problems("label: A, problem: sum-of-two"))  # no version: the newest hidden row
        self.assertEqual(self.row_id(c, "A"), row1)
        self.assertIn("A (sum-of-two): shown again with its submissions (v1)", r.changes)
        self.apply(HEAD + problems())  # hidden again
        r = self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 2"))  # v2: not row1's version
        row2 = self.row_id(c, "A")
        self.assertNotEqual(row2, row1)
        self.assertIn("A (sum-of-two): added, v2", r.changes)
        self.submit(row2)
        self.apply(HEAD + problems())  # two hidden rows now: row1 (v1), row2 (v2)
        self.apply(HEAD + problems("label: A, problem: sum-of-two"))  # no version: the newest, row2
        self.assertEqual(self.row_id(c, "A"), row2)
        self.apply(HEAD + problems())
        self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 1"))  # newest hidden is v2 -> a new row
        row3 = self.row_id(c, "A")
        self.assertNotIn(row3, (row1, row2))
        self.assertEqual([(v, h) for _l, v, h, _p in self.rows(c)], [(v1, True), (v2, True), (v1, False)])

    def test_versions_change_freely_until_there_are_submissions_then_pin_decides(self):
        v1, v2 = self.versions[("sum-of-two", 1)], self.versions[("sum-of-two", 2)]
        c = self.apply(HEAD + problems("label: A, problem: sum-of-two")).contest
        self.assertEqual(self.rows(c)[0][1], v2.id)  # without a version: the latest ready one
        r = self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 1"))
        self.assertEqual(r.changes, ["A (sum-of-two): v2 -> v1"])
        a = self.row_id(c, "A")
        self.submit(a)
        with self.assertRaises(ContestError) as ctx:
            self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 2", "label: B, problem: pair-with-sum"))
        self.assertIn("judgectl contest pin olymp A 2 --rejudge", str(ctx.exception))
        self.assertEqual(len(self.rows(c)), 1)  # nothing of that apply stayed (B was not added either)
        self.s.execute(Judgement.__table__.delete())  # the submission's first judgement does not matter here
        self.s.commit()
        contest = service.get_contest(self.s, "olymp")
        result = service.pin(self.s, self.actor, contest, "A", 2, rejudge=True, now=T0)
        self.s.commit()
        self.assertEqual((result["from"], result["to"], result["rejudge"]["created"]), (1, 2, 1))
        self.assertEqual(self.rows(c)[0][1], v2.id)
        jv = self.s.execute(select(Judgement.problem_version_id)).scalar_one()
        self.assertEqual(jv, v2.id)  # the rejudge runs on the new version
        self.assertEqual(self.apply(HEAD + problems("label: A, problem: sum-of-two, version: 2")).changes, [])
        v1.status = VersionStatus.failed
        self.s.commit()
        with self.assertRaises(ContestError) as ctx:
            service.pin(self.s, self.actor, contest, "A", 1, rejudge=False, now=T0)
        self.assertIn("only ready versions", str(ctx.exception))

    def test_labels_can_be_swapped_and_points_and_order_change(self):
        c = self.apply(HEAD + problems("label: A, problem: sum-of-two", "label: B, problem: pair-with-sum")).contest
        a, b = self.row_id(c, "A"), self.row_id(c, "B")
        r = self.apply(HEAD + problems("label: A, problem: pair-with-sum", "label: B, problem: sum-of-two, points: 50"))
        self.assertEqual((self.row_id(c, "A"), self.row_id(c, "B")), (b, a))  # the same rows, labels swapped
        self.assertIn("sum-of-two: label A -> B", r.changes)
        self.assertIn("B (sum-of-two): points - -> 50", r.changes)

    def test_every_problem_of_a_file_is_reported(self):
        self.s.execute(ProblemVersion.__table__.update().where(ProblemVersion.id == self.versions[("pair-with-sum", 1)].id)
                       .values(status=VersionStatus.pending_check))
        sum_problem = self.versions[("sum-of-two", 1)].problem
        sum_problem.archived = True
        self.s.commit()
        with self.assertRaises(ContestError) as ctx:
            self.apply(HEAD + "languages: [python, java]\n" + problems(
                "label: A, problem: sum-of-two", "label: B, problem: pair-with-sum", "label: C, problem: nope",
                "label: D, problem: pair-with-sum-x, version: 3"))
        message = str(ctx.exception)
        for fragment in ("languages not enabled in the system: java", "A: problem sum-of-two is archived",
                         "B: pair-with-sum has no ready version", "C: no problem 'nope'", "D: no problem 'pair-with-sum-x'"):
            self.assertIn(fragment, message)

    # ---------------------------------------------------------------- states, codes, join, start
    def test_publish_join_and_personal_start(self):
        text = HEAD + "timing_mode: personal\npersonal_duration: 90m\naccess: {mode: code}\n" + problems(
            "label: A, problem: sum-of-two")
        c = self.apply(text).contest
        student = self.student(1)
        self.s.commit()
        with self.assertRaises(ContestError) as ctx:
            service.enter(self.s, student, c, T0, c.invite_code)
        self.assertEqual(ctx.exception.problems, ["not_published"])  # a draft cannot be joined
        self.assertTrue(service.set_state(self.s, self.actor, c, ContestState.published, T0))
        self.assertFalse(service.set_state(self.s, self.actor, c, ContestState.published, T0))
        self.s.commit()
        with self.assertRaises(ContestError):
            service.enter(self.s, student, c, T0, "WRONG1")
        part = service.enter(self.s, student, c, T0, c.invite_code.lower())
        self.s.commit()
        self.assertEqual((part.joined_at, part.started_at), (T0, None))
        with self.assertRaises(ContestError) as ctx:
            service.enter(self.s, student, c, T0, c.invite_code)
        self.assertEqual(ctx.exception.problems, ["already_joined"])
        self.s.rollback()
        with self.assertRaises(ContestError) as ctx:
            service.start(self.s, student, c, T0 - timedelta(hours=5))  # 05:00, before 09:00
        self.assertEqual(ctx.exception.problems, ["not_started_yet"])
        part = service.start(self.s, student, c, T0)
        self.s.commit()
        self.assertEqual(part.started_at, T0)
        with self.assertRaises(ContestError) as ctx:
            service.start(self.s, student, c, T0 + timedelta(minutes=1))
        self.assertEqual(ctx.exception.problems, ["already_started"])
        self.s.rollback()
        old = c.invite_code
        new = service.regenerate_code(self.s, self.actor, c, T0)
        self.s.commit()
        self.assertNotEqual(new, old)
        self.assertEqual([u.login for _p, u in service.participants(self.s, c)], ["student1"])
        actions = [a for a in self.s.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars()
                   if a.startswith("contest.")]
        self.assertEqual(actions, ["contest.apply", "contest.publish", "contest.code_regenerate"])
