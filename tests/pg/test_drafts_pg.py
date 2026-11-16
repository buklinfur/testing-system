"""Services of a student's draft (classroom/drafts.py, works.submit_work(drafts=...); D-097..D-099, docs/CLASSROOM.md
§10 «Черновик ученика») on a real PostgreSQL: uploads one at a time, the cheap refusals before the body and every
refusal again after it (nothing stored), the hand-in taking the named draft files, a deleted file freeing its name
but not the quota, a copy of the last hand-in after a return, the reaper's purge; under the student's lock, two
uploads at the limits, an upload against a hand-in, one draft file handed in twice; and an upload of bytes the store
already holds (a teacher's file of a hidden item) answered exactly like an upload of new bytes.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import hashlib
from datetime import timedelta

from tests.pg.pgbase import HAVE_SA, URL, requires_pg
from tests.pg.test_attachments_pg import DUE, KIB, PDF, PNG, SVG, T0, AttachmentServiceTests, blob_files

if URL and HAVE_SA:
    from sqlalchemy import select

    from app.classroom import attachment_rules as ar
    from app.classroom import drafts, items, works
    from app.classroom.attachments import LinkIn
    from app.models import AssignmentSubmission, AssignmentSubmissionFile, WorkDraftFile

HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@requires_pg
class DraftServiceTests(AttachmentServiceTests):
    """The helpers of the attachment tests (items, uploads, two connections); none of their tests run here."""

    def setUp(self):
        super().setUp()
        self.files = self.item()

    # ---------------------------------------------------------------- helpers
    def add(self, name, data, user=None, item=None, db=None, now=T0, **kw):
        """drafts.add_file with the upload closed right after, as the API does; committed when it is self.s."""
        with self.upload(name, data) as up:
            row = drafts.add_file(db or self.s, user or self.a, item or self.files, up, now, **kw)
        if db is None:
            self.s.commit()
        return row

    def precheck(self, name="photo.jpg", user=None, item=None, now=T0, **kw):
        try:
            return drafts.precheck(self.s, user or self.a, item or self.files, name, now, **kw)
        finally:
            self.s.rollback()

    def submit(self, ids, user=None, item=None, db=None, **kw):
        sub, _ = works.submit_work(db or self.s, user or self.a, item or self.files, T0, blobs=self.blobs,
                                   drafts=ids, **kw)
        if db is None:
            self.s.commit()
        return sub

    def live(self, user=None, item=None):
        return [r.name for r in drafts.list_draft(self.s, user or self.a, item or self.files, T0)]

    def rows(self, **where):
        query = select(WorkDraftFile)
        for key, value in where.items():
            query = query.where(getattr(WorkDraftFile, key) == value)
        return list(self.s.execute(query.order_by(WorkDraftFile.id)).scalars())

    def handed(self, sub):
        return [(f.path, f.size) for f in self.s.execute(
            select(AssignmentSubmissionFile).where(AssignmentSubmissionFile.submission_id == sub.id)
            .order_by(AssignmentSubmissionFile.position)).scalars()]

    # ---------------------------------------------------------------- the path of a draft
    def test_upload_one_at_a_time_then_hand_in_by_ids(self):
        first = self.add("стр1.jpg", JPEG)
        second = self.add("IMG_0002.HEIC", HEIC)
        third = self.add("notes.txt", b"x")
        self.assertEqual(self.live(), ["стр1.jpg", "IMG_0002.HEIC", "notes.txt"])
        self.assertEqual(drafts.describe(first)["name"], "стр1.jpg")
        self.assertNotIn("sha256", drafts.describe(first), "the student needs no hash")
        self.assertEqual(drafts.draft_file(self.s, self.a, self.files, second.id, T0).blob_sha256,
                         second.blob_sha256)
        sub = self.submit([second.id, first.id], links=[LinkIn("https://example.ru/x")])
        self.assertEqual(self.handed(sub), [("IMG_0002.HEIC", len(HEIC)), ("стр1.jpg", len(JPEG))],
                         "in the order of the hand-in")
        self.assertEqual(self.live(), ["notes.txt"], "a file not named stays in the draft")
        self.assertEqual(self.rows(id=first.id), [], "a handed-in draft row is gone")
        files = works.submission_files(self.s, sub)["files"]
        self.assertEqual(works.work_file(self.s, self.a, self.files, self.a, files[0]["id"]).path, "IMG_0002.HEIC")

    def test_names_are_cleaned(self):
        row = self.add("C:\\Users\\Петя\\Фото.JPG", JPEG)
        self.assertEqual(row.name, "Фото.JPG")
        self.assertEqual(self.precheck("D:\\a\\notes.txt"), "notes.txt")

    def test_a_hand_in_with_drafts_follows_the_rules_of_a_hand_in(self):
        a = self.add("a.py", b"a")
        self.add("b.py", b"b")
        text = self.item(answer_kind="text")
        mark = self.item(answer_kind="mark")
        self.refused("files_not_expected", self.submit, [a.id], item=text, text="ответ")
        self.refused("files_not_expected", self.submit, [a.id], item=mark)
        self.refused("duplicate_name", self.submit, [a.id, a.id])  # one file named twice
        self.refused("too_many_files", self.submit, [a.id], links=[LinkIn("https://e.ru/1")] * 2,
                     limits=ar.Limits(2, 30 * KIB, 100 * KIB))
        self.refused("submission_too_large", self.submit, [a.id], limits=ar.Limits(5, 30 * KIB, 0))
        self.assertEqual(self.live(), ["a.py", "b.py"], "refused hand-ins leave the draft as it was")

    # ---------------------------------------------------------------- the draft file of another
    def test_another_students_or_items_or_deleted_file_is_not_found(self):
        mine = self.add("a.py", b"a")
        theirs = self.add("b.py", b"b", user=self.b)
        other_item = self.item()
        elsewhere = self.add("c.py", b"c", item=other_item)
        gone = self.add("d.py", b"d")
        drafts.remove(self.s, self.a, self.files, gone.id, T0)
        self.s.commit()
        for row in (theirs, elsewhere, gone):
            with self.subTest(file=row.name):
                self.refused("draft_file_not_found", drafts.draft_file, self.s, self.a, self.files, row.id, T0)
                self.refused("draft_file_not_found", drafts.remove, self.s, self.a, self.files, row.id, T0)
                self.refused("draft_file_not_found", self.submit, [mine.id, row.id])
        self.refused("draft_file_not_found", drafts.draft_file, self.s, self.a, self.files, 2_147_483_647, T0)
        self.assertEqual(self.live(), ["a.py"])
        self.assertEqual(self.live(user=self.b), ["b.py"])

    def test_an_item_the_student_does_not_see_is_not_found(self):
        draft = self.item(publish=False)
        later = self.item(available_from=T0 + timedelta(days=1))
        for item, user in ((draft, self.a), (later, self.a), (self.files, self.outsider)):
            with self.subTest(item=item.id, user=user.login):
                self.refused("item_not_found", self.precheck, item=item, user=user)
                self.refused("item_not_found", self.add, "a.py", b"a", item=item, user=user)
                self.refused("item_not_found", drafts.list_draft, self.s, user, item, T0)
                self.refused("item_not_found", drafts.from_last, self.s, user, item, T0)
        self.refused("not_a_student", self.precheck, user=self.admin)

    # ---------------------------------------------------------------- refusals before and after the body
    def test_cheap_refusals_before_the_body(self):
        text = self.item(answer_kind="text")
        closed = self.item(due_at=T0 - timedelta(hours=1), after_due_submit="no")
        self.refused("files_not_expected", self.precheck, item=text)
        self.refused("deadline_passed", self.precheck, item=closed)
        self.refused("file_type_not_allowed", self.precheck, "pic.svg")
        self.refused("file_type_not_allowed", self.precheck, "README")
        self.add("Photo.jpg", JPEG)
        self.refused("duplicate_name", self.precheck, "photo.JPG")
        self.refused("too_many_files", self.precheck, limits=ar.Limits(1, KIB, KIB))
        self.refused("quota_exceeded", self.precheck, "big.jpg", declared=KIB, quota=KIB)  # len(JPEG) + 1 KB
        self.assertEqual(self.precheck("big.jpg", declared=KIB - len(JPEG), quota=KIB), "big.jpg")
        works.grade_work(self.s, self.actor, self.files, self.a, T0, passed=True, release=True)
        self.s.commit()
        self.refused("already_graded", self.precheck)

    def test_every_refusal_after_the_body_stores_nothing(self):
        self.add("taken.pdf", PDF)
        before = blob_files(self.blobs)
        cases = [
            ("file_content_mismatch", ("pic.png", SVG), {}),
            ("file_content_mismatch", ("photo.heic", JPEG), {}),
            ("file_empty", ("empty.txt", b""), {}),
            ("duplicate_name", ("TAKEN.pdf", PDF + b"!"), {}),
            ("file_too_large", ("a.py", b"a" * 101), {"limits": ar.Limits(5, 100, 1000)}),
            ("submission_too_large", ("a.py", b"a" * 100), {"limits": ar.Limits(5, 100, len(PDF) + 99)}),
            ("too_many_files", ("a.py", b"a"), {"limits": ar.Limits(1, 100, 1000)}),
            ("quota_exceeded", ("a.py", b"a" * 10), {"quota": len(PDF) + 9}),
        ]
        for code, (name, data), kw in cases:
            with self.subTest(code=code, name=name):
                self.refused(code, self.add, name, data, **kw)
                self.assertNothingStored(before)
        self.assertEqual(self.live(), ["taken.pdf"])

    def test_the_state_changes_while_the_body_arrives(self):
        """precheck passed, then the item is archived or the due moment passes: add_file refuses. An archived item
        stays visible only with the student's own work (without it: item_not_found)."""
        self.submit([self.add("first.py", b"f").id])
        self.assertEqual(self.precheck("a.py"), "a.py")
        items.archive(self.s, self.actor, self.files, T0)
        self.s.commit()
        self.refused("item_archived", self.add, "a.py", b"a")
        self.refused("item_not_found", self.add, "a.py", b"a", user=self.b)
        other = self.item(due_at=DUE, after_due_submit="no")
        self.refused("deadline_passed", self.add, "a.py", b"a", item=other, now=DUE + timedelta(seconds=1))
        self.assertEqual(list(self.blobs.staging().iterdir()), [])

    # ---------------------------------------------------------------- delete, quota, purge
    def test_a_deleted_file_frees_its_name_but_not_the_quota(self):
        row = self.add("photo.jpg", b"\xff\xd8\xff" + b"1" * 997)
        self.assertEqual(works.quota_used(self.s, self.a)[0], 1000)
        drafts.remove(self.s, self.a, self.files, row.id, T0 + timedelta(minutes=1))
        self.s.commit()
        self.assertEqual(self.live(), [])
        self.assertEqual(works.quota_used(self.s, self.a)[0], 1000, "the deleted file still counts")
        self.refused("quota_exceeded", self.add, "photo.jpg", b"\xff\xd8\xff" + b"2" * 997, quota=1500)
        self.add("photo.jpg", b"\xff\xd8\xff" + b"1" * 997, quota=1000)  # the same bytes again cost nothing
        self.assertEqual(self.live(), ["photo.jpg"])

    def test_the_quota_counts_hand_ins_and_drafts_once_per_blob(self):
        one = self.add("a.py", b"a" * 100)
        self.submit([one.id])
        self.add("a.py", b"a" * 100)  # the same bytes as the handed-in file
        self.add("b.py", b"b" * 50)
        self.assertEqual(works.quota_used(self.s, self.a)[0], 150)
        self.assertEqual(works.quota_used(self.s, self.b)[0], 0)

    # ---------------------------------------------------------------- how long a draft lives (D-100)
    def test_without_a_due_moment_the_draft_lives_from_its_last_change(self):
        free = self.item(due_at=None)
        old = self.add("old.py", b"o", item=free, now=T0)
        gone = self.add("gone.py", b"g", item=free, now=T0)
        drafts.remove(self.s, self.a, free, gone.id, T0)
        self.s.commit()
        self.assertEqual(drafts.purge(self.s, T0 + timedelta(days=14), 14), 0, "exactly 14 days: not yet")
        self.assertEqual(drafts.purge(self.s, T0 + timedelta(days=14, seconds=1), 14), 2, "deleted files go too")
        self.s.commit()
        self.assertEqual(self.rows(item_id=free.id), [])
        self.assertTrue(self.blobs.exists(old.blob_sha256), "the blob stays for the collector")
        self.assertEqual(works.quota_used(self.s, self.a)[0], 0, "purged files leave the quota")

    def test_any_change_of_the_draft_extends_it_as_a_whole(self):
        free = self.item(due_at=None)
        first = self.add("first.py", b"1", item=free, now=T0)
        self.add("second.py", b"2", item=free, now=T0 + timedelta(days=10))  # an upload
        self.assertEqual(drafts.purge(self.s, T0 + timedelta(days=20), 14), 0, "the first file lives on")
        self.assertEqual([f["expires_at"] for f in drafts.draft_view(self.s, self.a, free, T0, 14)],
                         [T0 + timedelta(days=24)] * 2, "one moment for the whole draft")
        drafts.remove(self.s, self.a, free, first.id, T0 + timedelta(days=20))  # a delete
        self.s.commit()
        self.assertEqual(drafts.purge(self.s, T0 + timedelta(days=34), 14), 0)
        self.assertEqual(drafts.purge(self.s, T0 + timedelta(days=34, seconds=1), 14), 2)
        self.s.commit()
        self.add("third.py", b"3", item=free, now=T0 + timedelta(days=40))
        self.assertEqual(drafts.from_last(self.s, self.a, free, T0 + timedelta(days=50)), [])
        self.assertEqual(drafts.expires_at(self.s, self.a, free, 14), T0 + timedelta(days=54),
                         "from_last is a change even when it copies nothing")

    def test_with_a_due_moment_a_month_away_a_file_of_twenty_days_lives(self):
        now = T0 + timedelta(days=20)
        later = self.item(due_at=now + timedelta(days=30))
        self.add("photo.jpg", JPEG, item=later, now=T0)
        self.assertEqual(drafts.purge(self.s, now, 14), 0)
        self.assertEqual(drafts.draft_view(self.s, self.a, later, now, 14)[0]["expires_at"],
                         now + timedelta(days=44))

    def test_an_extension_moves_the_draft_of_that_student_only(self):
        due = self.item(due_at=DUE, after_due_submit="yes")
        self.add("a.jpg", JPEG, item=due, now=T0)
        self.add("b.jpg", JPEG, item=due, user=self.b, now=T0)
        self.assertEqual(drafts.expires_at(self.s, self.a, due, 14), DUE + timedelta(days=14))
        items.add_extension(self.s, self.actor, due, DUE + timedelta(days=10), T0, user=self.a)
        self.s.commit()
        self.assertEqual(drafts.expires_at(self.s, self.a, due, 14), DUE + timedelta(days=24))
        self.assertEqual(drafts.purge(self.s, DUE + timedelta(days=14, seconds=1), 14), 1)
        self.s.commit()
        self.assertEqual([r.user_id for r in self.rows(item_id=due.id)], [self.a.id], "the other student's went")
        self.assertEqual(drafts.purge(self.s, DUE + timedelta(days=24, seconds=1), 14), 1)

    def test_a_file_uploaded_after_the_due_moment_does_not_expire_at_once(self):
        late = self.item(due_at=DUE, after_due_submit="late")
        when = DUE + timedelta(days=20)
        self.add("late.jpg", JPEG, item=late, now=when)
        self.assertEqual(drafts.expires_at(self.s, self.a, late, 14), when + timedelta(days=14))
        self.assertEqual(drafts.purge(self.s, when + timedelta(hours=1), 14), 0)

    # ---------------------------------------------------------------- after a return
    def test_from_last_copies_the_last_hand_in_after_a_return(self):
        self.assertEqual(drafts.from_last(self.s, self.a, self.files, T0), [], "nothing handed in yet")
        a, b = self.add("a.jpg", JPEG), self.add("b.heic", HEIC)
        self.submit([a.id, b.id], links=[LinkIn("https://e.ru/1")])
        works.return_work(self.s, self.actor, self.files, self.a, T0, "переделай фото b")
        self.s.commit()
        used = works.quota_used(self.s, self.a)[0]
        self.add("B.HEIC", HEIC + b"!")  # a new photo under the name of an old one
        copied = drafts.from_last(self.s, self.a, self.files, T0)
        self.s.commit()
        self.assertEqual([r.name for r in copied], ["a.jpg"], "the name the draft has is skipped")
        self.assertEqual(self.live(), ["B.HEIC", "a.jpg"])
        self.assertEqual(works.quota_used(self.s, self.a)[0], used + len(HEIC) + 1, "copies cost nothing")
        self.refused("too_many_files", drafts.from_last, self.s, self.a, self.files, T0, limits=ar.Limits(1, KIB, KIB))
        works.grade_work(self.s, self.actor, self.files, self.a, T0, passed=True, release=True)
        self.s.commit()
        self.refused("already_graded", drafts.from_last, self.s, self.a, self.files, T0)

    # ---------------------------------------------------------------- the store does not tell
    def test_an_upload_of_bytes_the_store_holds_is_answered_like_new_bytes(self):
        """A teacher's solution attached to a hidden item: a student uploading the very same bytes gets what a student
        uploading new bytes of the same size gets - the same answer, the same codes at the quota boundary, the full
        size in the quota - and the stored file is not touched."""
        secret = b"%PDF-1.7\n" + b"s" * 991  # 1000 bytes
        other = b"%PDF-1.7\n" + b"o" * 991
        hidden = self.item(publish=False)
        self.attach(hidden, "solution.pdf", secret)
        stored = self.blobs.path(hashlib.sha256(secret).hexdigest())
        mtime, content = stored.stat().st_mtime_ns, stored.read_bytes()

        def attempt(user, data, quota):
            try:
                row = self.add("работа.pdf", data, user=user, quota=quota)
                return ("ok", {k: v for k, v in drafts.describe(row).items() if k != "id"})
            except items.FeedError as exc:
                self.s.rollback()
                return (exc.code, exc.data)

        self.assertEqual(attempt(self.a, secret, 999), attempt(self.b, other, 999))  # quota_exceeded, same data
        self.assertEqual(attempt(self.a, secret, 1000), attempt(self.b, other, 1000))  # ok, same row
        self.assertEqual(works.quota_used(self.s, self.a), (1000, set()))
        self.assertEqual(works.quota_used(self.s, self.a)[0], works.quota_used(self.s, self.b)[0])
        self.assertEqual((stored.stat().st_mtime_ns, stored.read_bytes()), (mtime, content))
        self.assertEqual(list(self.blobs.staging().iterdir()), [])

    # ---------------------------------------------------------------- two connections
    def test_two_uploads_at_once_at_the_count_limit(self):
        self.add("a.py", b"a")
        ups = [self.upload("b.py", b"b"), self.upload("c.py", b"c")]

        def job(up):
            def run(db):
                with up:
                    return drafts.add_file(db, self.a, items.get_item(db, self.files.id), up, T0,
                                           limits=ar.Limits(2, KIB, 10 * KIB)).id
            return run
        results = self.run_parallel(job(ups[0]), job(ups[1]))
        self.assertEqual(sorted(map(type, results), key=str), [int, str], results)
        self.assertIn("too_many_files", results)
        self.assertEqual(len(self.live()), 2)

    def test_two_uploads_at_once_at_the_quota(self):
        other = self.item()
        ups = [self.upload("a.py", b"a" * (30 * KIB)), self.upload("b.py", b"b" * (30 * KIB))]

        def job(item, up):
            def run(db):
                with up:
                    return drafts.add_file(db, self.a, items.get_item(db, item.id), up, T0, quota=50 * KIB).id
            return run
        results = self.run_parallel(job(self.files, ups[0]), job(other, ups[1]))
        self.assertIn("quota_exceeded", results)
        self.assertEqual(works.quota_used(self.s, self.a)[0], 30 * KIB)

    def test_one_draft_file_handed_in_twice_at_once(self):
        row = self.add("a.py", b"a")

        def job(db):
            return works.submit_work(db, self.a, items.get_item(db, self.files.id), T0, blobs=self.blobs,
                                     drafts=[row.id])[0].id
        results = self.run_parallel(job, job)
        self.assertEqual(sorted(map(type, results), key=str), [int, str], results)
        self.assertIn("draft_file_not_found", results)
        self.assertEqual(self.rows(), [])

    def test_an_upload_against_a_hand_in(self):
        """The hand-in takes the files it names; an upload at the same moment lands in the draft, before or after
        the hand-in, never in it and never lost."""
        first = self.add("a.py", b"a")
        up = self.upload("b.py", b"b")

        def hand_in(db):
            return works.submit_work(db, self.a, items.get_item(db, self.files.id), T0, blobs=self.blobs,
                                     drafts=[first.id])[0].id

        def upload(db):
            with up:
                return drafts.add_file(db, self.a, items.get_item(db, self.files.id), up, T0).id
        sub_id, new_id = self.run_parallel(hand_in, upload)
        self.assertIsInstance(sub_id, int)
        self.assertIsInstance(new_id, int)
        self.assertEqual(self.live(), ["b.py"])
        self.assertEqual([f["name"] for f in works.submission_files(self.s, self.s.get(
            AssignmentSubmission, sub_id))["files"]], ["a.py"])


# The attachment tests run in their own module: here only their helpers are inherited (a None attribute is not
# collected), and the imported class is not left in this module for the loader to find.
for _name in dir(AttachmentServiceTests):
    if _name.startswith("test_") and _name not in vars(DraftServiceTests):
        setattr(DraftServiceTests, _name, None)
del AttachmentServiceTests, _name
