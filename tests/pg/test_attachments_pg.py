"""Services of files and links (classroom/attachments.py, works.submit_work / work_file; D-078..D-080,
docs/CLASSROOM.md §10) on a real PostgreSQL: a teacher's attachments under the lock of the item (two additions at
once: the limit and the duplicate name hold), the exact order, the archive only read, the audit without content;
who sees them (the statement hidden after the due moment hides them, a contest item as listed); hand-ins with files
and links, the quota by distinct blobs under the student's advisory lock (two hand-ins at once stay within it, the
same hand-in ten times costs it once), uploads that are refused leave nothing in the store; work_file only by the
pair of the path (another student's or item's file is not found).

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
AFTER = DUE + timedelta(hours=1)
KIB = 1024

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"%" * 64
ZIP = b"PK\x03\x04" + b"\x00" * 64
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
LINK = "https://scratch.mit.edu/projects/1"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import blobstore, groups as g
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.classroom import attachment_rules as ar
    from app.classroom import attachments, items, works
    from app.classroom.attachments import LinkIn
    from app.classroom.items import FeedError
    from app.models import (AssignmentSubmissionFile, AssignmentSubmissionLink, Attachment, AuditLog, Blob, Contest)
    from app.settings import FileSettings


def blob_files(store) -> list:
    return sorted(p.name for p in store.root.glob("*/*")
                  if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))


@requires_pg
class AttachmentServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blobs = BlobStore(Path(tmp.name) / "blobs")
        self.admin = self.seed["admin"]
        self.actor = audit.Actor(user_id=self.admin.id)
        self.a, self.b, self.outsider = self.student(1), self.student(2), self.student(3)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], T0)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def item(self, publish=True, answer_kind="text_or_files", **fields):
        base = dict(grading="pass_fail", due_at=DUE, answer_kind=answer_kind)
        base.update(fields)
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Проект", body_md="Сделай", **base)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def contest_item(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        item = items.create_item(self.s, self.actor, T0, kind="contest", title="Контест", contest=contest)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def upload(self, name, data):
        """A finished upload; the caller closes it (as the API does in a with block), addCleanup only in case."""
        up = self.blobs.incoming(name)
        self.addCleanup(up.close)
        up.write(data)
        return up.finish()

    def add_file(self, item, name, data, db=None, **kw):
        """attachments.add_file with the upload closed right after, accepted or refused (as by the API)."""
        with self.upload(name, data) as up:
            return attachments.add_file(db or self.s, self.actor, item, up, T0, **kw)

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(FeedError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.s.rollback()
        return caught.exception

    def attach(self, item, name="схема.pdf", data=PDF, **kw):
        row = self.add_file(item, name, data, **kw)
        self.s.commit()
        return row

    def link(self, item, url=LINK, title=None, **kw):
        row = attachments.add_link(self.s, self.actor, item, url, T0, title=title, **kw)
        self.s.commit()
        return row

    def hand_in(self, user, item, files=(), links=(), text=None, now=T0, session=None, **kw):
        db = session or self.s
        ups = [self.upload(name, data) for name, data in files]
        try:
            sub, _ = works.submit_work(db, user, item, now, blobs=self.blobs, text=text, files=ups, links=links, **kw)
        finally:
            for up in ups:
                up.close()
        if session is None:
            self.s.commit()
        return sub

    def audits(self, action):
        return [r.details for r in self.s.execute(select(AuditLog).where(AuditLog.action == action)
                                                  .order_by(AuditLog.id)).scalars()]

    def assertNothingStored(self, before):
        self.assertEqual(blob_files(self.blobs), before, "no new blob")
        self.assertEqual(list(self.blobs.staging().iterdir()), [], "no temporary file")

    def run_parallel(self, *jobs, hold=0.3):
        """Each job(session) in its own connection, released at once by a barrier; a job that succeeds keeps its
        transaction open `hold` seconds before committing. The results or the FeedError codes, in job order."""
        barrier, results, errors = threading.Barrier(len(jobs)), [None] * len(jobs), []

        def run(i, job):
            session = self.Session()
            try:
                barrier.wait()
                try:
                    results[i] = job(session)
                    session.flush()
                    time.sleep(hold)
                    session.commit()
                except FeedError as exc:
                    session.rollback()
                    results[i] = exc.code
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                session.close()

        workers = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(30)
        self.assertEqual(errors, [])
        self.s.expire_all()
        return results

    # ---------------------------------------------------------------- a teacher's attachments
    def test_files_and_links_in_their_order_with_the_audit_without_content(self):
        item = self.item(publish=False)  # a draft takes attachments too
        pdf = self.attach(item)
        link = self.link(item, f"  {LINK}  ", title="  Пример  ")
        pic = self.attach(item, "C:\\Users\\Петя\\рисунок.PNG", PNG)  # the base name of a path from a browser
        self.assertEqual([r.id for r in attachments.list_attachments(self.s, item)], [pdf.id, link.id, pic.id])
        self.assertEqual([r.position for r in attachments.list_attachments(self.s, item)], [0, 1, 2])
        self.assertEqual((pic.name, link.url, link.title), ("рисунок.PNG", LINK, "Пример"))
        self.assertEqual(self.blobs.get(pdf.blob_sha256), PDF)
        self.assertEqual(self.s.get(Blob, pdf.blob_sha256).size_bytes, len(PDF))
        added = self.audits("item.attachment_add")
        self.assertEqual(added[0], {"attachment": pdf.id, "kind": "file", "name": "схема.pdf",
                                    "sha256": pdf.blob_sha256, "size": len(PDF)})
        self.assertEqual(added[1], {"attachment": link.id, "kind": "link", "url": LINK, "title": "Пример"})
        self.assertNotIn(PDF.decode("latin-1"), str(added))
        self.assertEqual(list(self.blobs.staging().iterdir()), [])
        # a contest item takes them as well
        self.attach(self.contest_item(), "правила.pdf")

    def test_refusals_store_nothing(self):
        item = self.item()
        self.attach(item, "Схема.pdf")
        before = blob_files(self.blobs)
        cases = [("file_content_mismatch", "a.png", SVG), ("file_type_not_allowed", "a.svg", SVG),
                 ("file_type_not_allowed", "run.py.exe", b"MZ"), ("file_empty", "empty.txt", b""),
                 ("duplicate_name", "СХЕМА.PDF", PDF + b"other"), ("file_content_mismatch", "report.docx", PDF)]
        for code, name, data in cases:
            with self.subTest(code=code, name=name):
                self.refused(code, self.add_file, item, name, data)
        self.refused("file_too_large", self.add_file, item, "b.pdf", PDF, limits=ar.Limits(20, len(PDF) - 1, None))
        self.refused("link_invalid", attachments.add_link, self.s, self.actor, item, "javascript:alert(1)", T0)
        self.refused("link_title_invalid", attachments.add_link, self.s, self.actor, item, LINK, T0, title="я" * 201)
        self.s.expire_all()
        self.assertEqual(attachments.count(self.s, item), 1)
        self.assertNothingStored(before)

    def test_limit_of_attachments_and_teacher_limits_from_settings(self):
        item = self.item()
        limits = attachments.teacher_limits(FileSettings(item_attachments_max=3))
        self.link(item, limits=limits)
        self.attach(item, limits=limits)
        self.link(item, limits=limits)
        self.refused("too_many_attachments", attachments.add_link, self.s, self.actor, item, LINK, T0, limits=limits)

    def test_two_additions_at_once_one_takes_the_last_place(self):
        """19 attachments, the limit 20: two additions in two connections, released together; the second waits for
        the lock of the item and counts the first one."""
        item = self.item()
        for n in range(19):
            self.link(item, f"{LINK}{n}")
        results = self.run_parallel(
            lambda db: attachments.add_link(db, self.actor, items.get_item(db, item.id), LINK + "/a", T0).id,
            lambda db: attachments.add_link(db, self.actor, items.get_item(db, item.id), LINK + "/b", T0).id)
        self.assertEqual(sorted(map(type, results), key=str), [int, str], results)
        self.assertIn("too_many_attachments", results)
        self.assertEqual(attachments.count(self.s, item), 20)

    def test_two_files_of_one_name_at_once_one_is_refused(self):
        item = self.item()
        results = self.run_parallel(
            lambda db: self.add_file(items.get_item(db, item.id), "Отчёт.pdf", PDF, db).id,
            lambda db: self.add_file(items.get_item(db, item.id), "отчёт.PDF", PDF + b"2", db).id)
        self.assertIn("duplicate_name", results)
        self.assertEqual(attachments.count(self.s, item), 1)
        self.assertEqual(list(self.blobs.staging().iterdir()), [])

    def test_reorder_needs_exactly_the_current_set(self):
        item = self.item()
        x, y, z = self.link(item), self.attach(item), self.link(item, LINK + "/z")
        cases = {"missing": [x.id, y.id], "extra": [x.id, y.id, z.id, 999999], "repeated": [x.id, y.id, z.id, x.id],
                 "repeated instead of one": [x.id, x.id, z.id], "empty": []}
        for label, ids in cases.items():
            with self.subTest(label):
                err = self.refused("attachment_order_invalid", attachments.reorder, self.s, self.actor, item, ids, T0)
                self.assertTrue(err.data["missing"] or err.data["extra"] or err.data["repeated"])
        attachments.reorder(self.s, self.actor, item, [z.id, x.id, y.id], T0)
        self.s.commit()
        self.assertEqual([r.id for r in attachments.list_attachments(self.s, item)], [z.id, x.id, y.id])
        self.assertEqual(self.audits("item.attachment_order"), [{"before": [x.id, y.id, z.id],
                                                                 "after": [z.id, x.id, y.id]}])
        attachments.reorder(self.s, self.actor, item, [z.id, x.id, y.id], T0)  # the same order: no audit row
        self.s.commit()
        self.assertEqual(len(self.audits("item.attachment_order")), 1)

    def test_remove_keeps_the_blob_and_only_this_items_attachment(self):
        item, other = self.item(), self.item()
        pdf, foreign = self.attach(item), self.attach(other, "чужое.pdf")
        self.refused("attachment_not_found", attachments.remove, self.s, self.actor, item, foreign.id, T0)
        details = attachments.remove(self.s, self.actor, item, pdf.id, T0)
        self.s.commit()
        self.assertEqual(details, {"attachment": pdf.id, "kind": "file", "position": 0, "created_by": self.admin.id,
                                   "name": "схема.pdf", "size": len(PDF), "sha256": pdf.blob_sha256})
        self.assertEqual(self.audits("item.attachment_remove"), [details])
        self.assertEqual(self.blobs.get(pdf.blob_sha256), PDF, "the blob stays")
        self.assertEqual(attachments.count(self.s, other), 1)

    def test_archived_item_only_reads(self):
        item = self.item()
        pdf, link = self.attach(item), self.link(item)
        items.archive(self.s, self.actor, item, T0)
        self.s.commit()
        self.refused("item_archived", attachments.add_link, self.s, self.actor, item, LINK, T0)
        self.refused("item_archived", self.add_file, item, "b.pdf", PDF)
        self.refused("item_archived", attachments.remove, self.s, self.actor, item, pdf.id, T0)
        self.refused("item_archived", attachments.reorder, self.s, self.actor, item, [link.id, pdf.id], T0)
        self.assertEqual(len(attachments.list_attachments(self.s, item)), 2)
        self.assertEqual(attachments.item_file(self.s, self.admin, item, pdf.id, T0).id, pdf.id)

    def test_a_draft_takes_its_attachments_with_it(self):
        item = self.item(publish=False)
        pdf = self.attach(item)
        items.delete_item(self.s, self.actor, item, T0)
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(Attachment)).scalar_one(), 0)
        self.assertEqual(self.blobs.get(pdf.blob_sha256), PDF)

    # ---------------------------------------------------------------- who sees them
    def test_students_see_them_with_the_statement(self):
        hidden = self.item(after_due_statement="hidden")
        pdf = self.attach(hidden)
        self.assertEqual([r.id for r in attachments.visible_attachments(self.s, self.a, hidden, T0)], [pdf.id])
        self.assertEqual(attachments.item_file(self.s, self.a, hidden, pdf.id, T0).id, pdf.id)
        # after the due moment the statement is hidden, and its attachments with it - not for the teacher
        self.assertIsNone(attachments.visible_attachments(self.s, self.a, hidden, AFTER))
        self.refused("attachment_not_found", attachments.item_file, self.s, self.a, hidden, pdf.id, AFTER)
        self.assertEqual(attachments.item_file(self.s, self.admin, hidden, pdf.id, AFTER).id, pdf.id)
        # an extension gives the statement back
        items.add_extension(self.s, self.actor, hidden, AFTER + timedelta(days=1), T0, user=self.a)
        self.s.commit()
        self.assertEqual(attachments.item_file(self.s, self.a, hidden, pdf.id, AFTER).id, pdf.id)
        self.refused("attachment_not_found", attachments.item_file, self.s, self.b, hidden, pdf.id, AFTER)
        # outside the audience, a draft: nothing
        self.refused("attachment_not_found", attachments.item_file, self.s, self.outsider, hidden, pdf.id, T0)
        draft = self.item(publish=False)
        draft_pdf = self.attach(draft)
        self.refused("attachment_not_found", attachments.item_file, self.s, self.a, draft, draft_pdf.id, T0)
        self.assertIsNone(attachments.visible_attachments(self.s, self.a, draft, T0))

    def test_another_items_attachment_or_a_link_is_not_found(self):
        mine, other = self.item(), self.item()
        foreign, link = self.attach(other, "чужое.pdf"), self.link(mine)
        for viewer in (self.admin, self.a):
            with self.subTest(viewer=viewer.login):
                self.refused("attachment_not_found", attachments.item_file, self.s, viewer, mine, foreign.id, T0)
                self.refused("attachment_not_found", attachments.item_file, self.s, viewer, mine, link.id, T0)

    def test_contest_item_as_listed(self):
        item = self.contest_item()
        pdf = self.attach(item, "правила.pdf")
        self.assertEqual(attachments.item_file(self.s, self.a, item, pdf.id, T0).id, pdf.id)
        self.refused("attachment_not_found", attachments.item_file, self.s, self.outsider, item, pdf.id, T0)
        items.archive(self.s, self.actor, item, T0)
        self.s.commit()
        self.refused("attachment_not_found", attachments.item_file, self.s, self.a, item, pdf.id, T0)

    # ---------------------------------------------------------------- hand-ins
    def test_hand_in_with_files_and_links(self):
        item = self.item()
        sub = self.hand_in(self.a, item, files=[("main.py", b"print(1)\n"), ("answer.md", b"# my file\n")],
                           links=[LinkIn(f" {LINK} ", " Мой проект "), LinkIn(LINK + "/2")], text="Готово")
        rows = self.s.execute(select(AssignmentSubmissionFile).where(AssignmentSubmissionFile.submission_id == sub.id)
                              .order_by(AssignmentSubmissionFile.position)).scalars().all()
        self.assertEqual([(r.kind.value, r.path, r.position) for r in rows],
                         [("text", "answer.md", 0), ("file", "main.py", 1), ("file", "answer.md", 2)])
        links = self.s.execute(select(AssignmentSubmissionLink).where(AssignmentSubmissionLink.submission_id == sub.id)
                               .order_by(AssignmentSubmissionLink.position)).scalars().all()
        self.assertEqual([(r.url, r.title) for r in links], [(LINK, "Мой проект"), (LINK + "/2", None)])
        self.assertEqual(works.answer_text(self.s, self.blobs, sub), "Готово", "the text, not the file answer.md")
        card = works.work_detail(self.s, self.blobs, item, self.a, T0)
        event = card["events"][0]
        self.assertEqual([(f["kind"], f["name"]) for f in event["files"]],
                         [("text", "answer.md"), ("file", "main.py"), ("file", "answer.md")])
        self.assertEqual([lk["url"] for lk in event["links"]], [LINK, LINK + "/2"])
        self.assertNotIn("print(1)", str(card), "the card lists files, never their content")
        self.assertEqual(list(self.blobs.staging().iterdir()), [])

    def test_hand_in_rules_through_the_service(self):
        files_only, text_only = self.item(answer_kind="files"), self.item(answer_kind="text")
        before = blob_files(self.blobs)
        self.refused("files_required", self.hand_in, self.a, files_only)
        self.refused("text_not_expected", self.hand_in, self.a, files_only, files=[("a.py", b"1")], text="и текст")
        self.refused("files_not_expected", self.hand_in, self.a, text_only, files=[("a.py", b"1")], text="текст")
        self.refused("file_empty", self.hand_in, self.a, files_only, files=[("empty.txt", b"")])
        err = self.refused("file_content_mismatch", self.hand_in, self.a, files_only,
                           files=[("a.py", b"1"), ("pic.png", SVG)])
        self.assertEqual(err.data["index"], 1)
        self.refused("duplicate_name", self.hand_in, self.a, files_only, files=[("A.py", b"1"), ("a.PY", b"2")])
        self.refused("link_invalid", self.hand_in, self.a, files_only, links=[LinkIn("file:///etc/passwd")])
        self.refused("too_many_files", self.hand_in, self.a, files_only, links=[LinkIn(LINK)] * 3,
                     limits=attachments.hand_in_limits(FileSettings(submission_files_max=2)))
        self.refused("submission_too_large", self.hand_in, self.a, files_only,
                     files=[("a.zip", ZIP), ("b.zip", ZIP + b"b")],
                     limits=ar.Limits(10, 1000, 2 * len(ZIP)))
        self.assertNothingStored(before)
        self.hand_in(self.a, files_only, links=[LinkIn(LINK)])  # a link alone is a hand-in of files

    def test_the_same_hand_in_ten_times_costs_the_quota_once(self):
        item = self.item()
        data = b"x" * (40 * KIB)
        for _ in range(10):
            self.hand_in(self.a, item, files=[("main.py", data)], text="Готово", quota=50 * KIB)
        used, _owned = works.quota_used(self.s, self.a)
        self.assertEqual(used, len(data) + len("Готово".encode()))
        # a new file over the quota is refused, and nothing of it is stored
        before = blob_files(self.blobs)
        err = self.refused("quota_exceeded", self.hand_in, self.a, item, files=[("other.py", b"y" * (20 * KIB))],
                           quota=50 * KIB)
        self.assertIn("ask the teacher", str(err))
        self.assertNothingStored(before)
        # a known file always passes, also when the quota was lowered below what is used
        self.hand_in(self.a, item, files=[("again.py", data)], quota=KIB)
        # another student has their own quota; a file they share costs each of them
        self.hand_in(self.b, item, files=[("main.py", data)], quota=50 * KIB)

    def test_two_hand_ins_at_once_stay_within_the_quota(self):
        """Two hand-ins of one student on two items (no common work lock), each with a new file of 30 KB, quota 50
        KB: the advisory lock of the student makes the second count the first one."""
        one, two = self.item(), self.item()
        first, second = self.upload("a.py", b"a" * (30 * KIB)), self.upload("b.py", b"b" * (30 * KIB))

        def job(item, up):
            def run(db):
                with up:
                    return works.submit_work(db, self.a, items.get_item(db, item.id), T0, blobs=self.blobs,
                                             files=[up], quota=50 * KIB)[0].id
            return run
        results = self.run_parallel(job(one, first), job(two, second))
        self.assertEqual(list(self.blobs.staging().iterdir()), [], "the refused one closed, nothing left")
        self.assertIn("quota_exceeded", results)
        self.assertEqual(sorted(map(type, results), key=str), [int, str], results)
        self.assertEqual(works.quota_used(self.s, self.a)[0], 30 * KIB)

    # ---------------------------------------------------------------- files of a work: only by the pair of the path
    def test_work_file_only_by_the_pair(self):
        item, other_item = self.item(), self.item()
        mine = self.hand_in(self.a, item, files=[("main.py", b"print(1)\n")], text="Готово")
        theirs = self.hand_in(self.b, item, files=[("main.py", b"print(2)\n")])
        elsewhere = self.hand_in(self.a, other_item, files=[("main.py", b"print(3)\n")])
        file_id = lambda sub, kind="file": self.s.execute(  # noqa: E731
            select(AssignmentSubmissionFile.id).where(AssignmentSubmissionFile.submission_id == sub.id,
                                                      AssignmentSubmissionFile.kind == kind)).scalar_one()
        f_mine, f_text, f_theirs, f_elsewhere = file_id(mine), file_id(mine, "text"), file_id(theirs), \
            file_id(elsewhere)
        for viewer in (self.admin, self.a):
            got = works.work_file(self.s, viewer, item, self.a, f_mine)
            self.assertEqual(self.blobs.get(got.blob_sha256), b"print(1)\n")
            self.assertEqual(works.work_file(self.s, viewer, item, self.a, f_text).path, "answer.md")
        cases = [  # (viewer, item, student of the path, file id)
            (self.admin, item, self.a, f_theirs),      # another student's file under A's path
            (self.admin, item, self.a, f_elsewhere),   # A's file of another item
            (self.a, item, self.a, f_theirs),
            (self.a, item, self.b, f_theirs),          # A asks for B's pair
            (self.a, other_item, self.a, f_mine),
            (self.outsider, item, self.outsider, f_mine),
            (self.admin, item, self.a, 999999),
        ]
        for viewer, it, student, fid in cases:
            with self.subTest(viewer=viewer.login, student=student.login, fid=fid):
                self.refused("file_not_found", works.work_file, self.s, viewer, it, student, fid)
