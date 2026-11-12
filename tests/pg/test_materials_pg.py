"""Services of the library of materials (materials/service.py, library.py, refs.py; D-083..D-085, docs/CLASSROOM.md §11)
on a real PostgreSQL: versions (two changes at once give two versions in a row; the same text gives none; attachments
copied with shared blobs), attachments of a version (the precheck before the body, the decision again under the lock
of the material, nothing left in the store after a refusal), the fixed slug, who opens a material on real rows by
each of the four bases and without any, "not found" for a student without a basis (the view, a file, the links -
compared whole), the search of the library, links written when texts are saved (import with its dry run and
"exists", feed items, versions) and a link to an unknown slug that starts working once the material exists, material
items of the feed, the audit without content, where a material is used, the sections.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(days=1)
PDF = b"%PDF-1.7\n" + b"%" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
LINK = "https://docs.python.org/3/tutorial/"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import blobstore, groups as g
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.classroom import attachment_rules as ar
    from app.classroom import items
    from app.classroom.items import FeedError
    from app.materials import library, service as ms
    from app.models import (AccessMode, AuditLog, Contest, ContestGroup, ContestProblem, FeedItem, Material,
                            MaterialRef, MaterialVersion, ProblemVersion)
    from app.problems.importer import import_package
    from app.settings import ImportSettings


def blob_files(store) -> list:
    return sorted(p.name for p in store.root.glob("*/*")
                  if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))


@requires_pg
class MaterialServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blobs = BlobStore(self.tmp / "blobs")
        self.admin = self.seed["admin"]
        self.actor = audit.Actor(user_id=self.admin.id)
        self.a, self.b = self.student(1), self.student(2)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a], T0)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def material(self, slug="cycles", title="Циклы", body="Цикл for", **kw):
        result = ms.create_material(self.s, self.actor, T0, slug=slug, title=title, body_md=body, **kw)
        self.s.commit()
        return result.version.material_id and self.s.get(Material, result.version.material_id)

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(FeedError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.s.rollback()
        return caught.exception

    def upload(self, name, data):
        up = self.blobs.incoming(name)
        self.addCleanup(up.close)
        up.write(data)
        return up.finish()

    def add_file(self, material, name="схема.pdf", data=PDF, db=None, **kw):
        with self.upload(name, data) as up:
            return ms.add_file(db or self.s, self.actor, material, up, T0, **kw)

    def assertNothingStored(self, before):
        self.assertEqual(blob_files(self.blobs), before, "no new blob")
        self.assertEqual(list(self.blobs.staging().iterdir()), [], "no temporary file")

    def numbers(self, material):
        return list(self.s.execute(select(MaterialVersion.version).where(MaterialVersion.material_id == material.id)
                                   .order_by(MaterialVersion.version)).scalars())

    def audits(self, action):
        return [d for d in self.s.execute(select(AuditLog.details).where(AuditLog.action == action)
                                          .order_by(AuditLog.id)).scalars()]

    def run_parallel(self, *jobs, hold=0.3):
        """Each job(session) in its own connection, released together; a success holds its transaction `hold`
        seconds before committing. The results or the FeedError codes, in job order."""
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

    def feed_item(self, kind="assignment", body="", publish=True, **kw):
        fields = dict(grading="none") if kind == "assignment" else {}
        fields.update(kw)
        item = items.create_item(self.s, self.actor, T0, kind=kind, title="З", body_md=body, **fields)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def material_item(self, material, publish=True, **kw):
        item = items.create_item(self.s, self.actor, T0, kind="material", title="", material=material, **kw)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    # ---------------------------------------------------------------- versions
    def test_create_gives_version_1_and_the_slug_is_taken_once(self):
        m = self.material(body="См. [[памятка:io-fast]]")
        self.assertEqual((m.slug, m.title, m.library.value, self.numbers(m)), ("cycles", "Циклы", "none", [1]))
        self.refused("slug_taken", ms.create_material, self.s, self.actor, T0, slug="cycles", title="Другое")
        for slug in ("Cycles", " cycles", "c", "циклы"):
            self.refused("slug_invalid", ms.create_material, self.s, self.actor, T0, slug=slug, title="Т")
        self.refused("title_invalid", ms.create_material, self.s, self.actor, T0, slug="t-1", title="   ")
        self.refused("library_invalid", ms.create_material, self.s, self.actor, T0, slug="t-2", title="Т",
                     library="everyone")
        self.refused("body_too_long", ms.create_material, self.s, self.actor, T0, slug="t-3", title="Т",
                     body_md="я" * 100_001)
        result = ms.create_material(self.s, self.actor, T0, slug="other", title="Другое", body_md="[[памятка:nope]]")
        self.assertIn("there is no material nope", result.warnings[0])
        self.s.rollback()

    def test_same_text_makes_no_version_and_a_new_text_keeps_the_attachments(self):
        m = self.material()
        v2 = self.add_file(m)
        self.s.commit()
        same = ms.new_version(self.s, self.actor, m, LATER, "Цикл for")
        self.assertEqual((same.created, same.version.version), (False, 2))
        new = ms.new_version(self.s, self.actor, m, LATER, "Цикл for и while")
        self.s.commit()
        self.assertEqual((new.created, new.version.version, self.numbers(m)), (True, 3, [1, 2, 3]))
        old_rows, new_rows = ms.version_attachments(self.s, v2), ms.version_attachments(self.s, new.version)
        self.assertEqual([(r.name, r.blob_sha256, r.size) for r in new_rows],
                         [(r.name, r.blob_sha256, r.size) for r in old_rows], "the same files, the same blobs")
        self.assertNotEqual([r.id for r in new_rows], [r.id for r in old_rows], "copied rows")
        self.assertEqual(ms.get_version(self.s, m, 1).body_md, "Цикл for", "an old version never changes")
        self.assertEqual(len(blob_files(self.blobs)), 1)

    def test_two_changes_at_once_give_two_versions_in_a_row(self):
        m = self.material()
        results = self.run_parallel(
            lambda db: ms.new_version(db, self.actor, db.get(Material, m.id), LATER, "текст A").version.version,
            lambda db: ms.new_version(db, self.actor, db.get(Material, m.id), LATER, "текст B").version.version)
        self.assertEqual(sorted(results), [2, 3], "the second waits for the lock and builds on the first")
        self.assertEqual(self.numbers(m), [1, 2, 3])
        results = self.run_parallel(
            lambda db: ms.add_link(db, self.actor, db.get(Material, m.id), LINK + "a", LATER).version,
            lambda db: ms.add_link(db, self.actor, db.get(Material, m.id), LINK + "b", LATER).version)
        self.assertEqual(sorted(results), [4, 5])
        latest = ms.latest_version(self.s, m)
        self.assertEqual(sorted(r.url for r in ms.version_attachments(self.s, latest)), [LINK + "a", LINK + "b"],
                         "each change keeps the other one")

    # ---------------------------------------------------------------- attachments of a version
    def test_attachments_make_versions_in_their_order_with_the_audit_without_content(self):
        m = self.material()
        v2 = self.add_file(m)
        v3 = ms.add_link(self.s, self.actor, m, f"  {LINK}  ", T0, title="  Учебник  ")
        rows = ms.version_attachments(self.s, v3)
        self.assertEqual([(r.kind.value, r.position) for r in rows], [("file", 0), ("link", 1)])
        v4 = ms.reorder(self.s, self.actor, m, [rows[1].id, rows[0].id], T0)
        self.assertTrue(v4.created)
        again = ms.reorder(self.s, self.actor, m, [r.id for r in ms.version_attachments(self.s, v4.version)], T0)
        self.assertFalse(again.created, "the same order: no version")
        self.s.commit()
        bad = self.refused("attachment_order_invalid", ms.reorder, self.s, self.actor, m, [rows[0].id], T0)
        self.assertTrue(bad.data["missing"])
        link_row = ms.version_attachments(self.s, ms.latest_version(self.s, m))[0]
        v5 = ms.remove(self.s, self.actor, m, link_row.id, T0)
        self.s.commit()
        self.assertEqual([r.kind.value for r in ms.version_attachments(self.s, v5)], ["file"])
        self.assertEqual(self.numbers(m), [1, 2, 3, 4, 5])
        self.assertEqual(len(ms.version_attachments(self.s, v2)), 1, "old versions keep theirs")
        self.refused("attachment_not_found", ms.remove, self.s, self.actor, m, link_row.id, T0)
        added = self.audits("material.attachment_add")
        self.assertEqual(added[0]["name"], "схема.pdf")
        self.assertEqual(set(added[0]), {"slug", "version", "attachment", "kind", "name", "sha256", "size"})
        self.assertEqual(added[1]["url"], LINK)
        self.assertNotIn(PDF.decode("latin-1"), str(self.audits("material.attachment_add")))

    def test_precheck_before_the_body_and_the_decision_again_under_the_lock(self):
        m = self.material()
        self.add_file(m, "Схема.pdf")
        self.s.commit()
        before = blob_files(self.blobs)
        # the cheap refusals before the body, nothing locked or written
        self.refused("duplicate_name", ms.precheck_file, self.s, m, "СХЕМА.PDF")
        self.refused("file_type_not_allowed", ms.precheck_file, self.s, m, "a.svg")
        self.refused("too_many_attachments", ms.precheck_file, self.s, m, "b.pdf", ar.Limits(1, 10 ** 6, None))
        self.assertEqual(ms.precheck_file(self.s, m, "C:\\Docs\\план.pdf"), "план.pdf")
        # the state changes while the body is received: add_file decides again, and nothing is stored
        changes = [
            ("duplicate_name", lambda: self.add_file(m, "ПЛАН.pdf", PDF + b"first"), "план.pdf"),
            ("material_archived", lambda: ms.archive(self.s, self.actor, m, T0), "другой.pdf"),
        ]
        for code, change, name in changes:
            with self.subTest(code):
                self.assertTrue(ms.precheck_file(self.s, m, name))
                change()
                self.s.commit()
                stored = blob_files(self.blobs)
                self.refused(code, self.add_file, m, name, PDF + b"second")
                self.assertNothingStored(stored)
        ms.unarchive(self.s, self.actor, m, T0)
        self.s.commit()
        limits = ar.Limits(ms.version_attachments(self.s, ms.latest_version(self.s, m)).__len__(), 10 ** 6, None)
        self.refused("too_many_attachments", self.add_file, m, "лишний.pdf", PDF + b"x", limits=limits)
        for code, name, data in (("file_content_mismatch", "a.png", SVG), ("file_empty", "e.txt", b""),
                                 ("file_content_mismatch", "a.pdf", PNG)):
            with self.subTest(code=code, name=name):
                self.refused(code, self.add_file, m, name, data)
        self.assertEqual(list(self.blobs.staging().iterdir()), [], "no temporary file after any refusal")
        self.assertEqual(len(blob_files(self.blobs)), len(before) + 1, "only the accepted ПЛАН.pdf")

    # ---------------------------------------------------------------- the fixed slug, archive
    def test_the_slug_never_changes(self):
        m = self.material()
        self.refused("slug_immutable", ms.update_material, self.s, self.actor, m, T0, slug="loops", title="Петли")
        self.s.expire_all()
        m = self.s.get(Material, m.id)
        self.assertEqual((m.slug, m.title), ("cycles", "Циклы"), "nothing changed")
        self.assertEqual(self.audits("material.update"), [])
        self.assertEqual(ms.update_material(self.s, self.actor, m, T0, slug="cycles", title="Петли"), ["title"])
        self.s.commit()
        self.assertEqual(self.audits("material.update")[0]["changes"], {"title": ["Циклы", "Петли"]})
        self.refused("material_invalid", ms.update_material, self.s, self.actor, m, T0, body_md="x")

    def test_an_archived_material_is_read_only(self):
        m = self.material()
        self.assertTrue(ms.archive(self.s, self.actor, m, T0))
        self.assertFalse(ms.archive(self.s, self.actor, m, T0))
        self.s.commit()
        for fn, args in ((ms.new_version, ("новый",)), (ms.add_link, (LINK,))):
            self.refused("material_archived", fn, self.s, self.actor, m, T0, *args)
        self.refused("material_archived", ms.update_material, self.s, self.actor, m, T0, title="Т")
        self.refused("material_archived", ms.set_library_groups, self.s, self.actor, m, T0, [self.group])

    # ---------------------------------------------------------------- who opens it, on real rows
    def opens(self, user, m, now=LATER):
        return ms.open_basis(self.s, self.s.get(type(user), user.id), self.s.get(Material, m.id), now)

    def test_each_basis_on_real_rows(self):
        m = self.material()
        self.assertIsNone(self.opens(self.a, m))
        self.assertEqual(self.opens(self.admin, m), "admin")
        # 1. the library: all students, then a group, then archived
        ms.update_material(self.s, self.actor, m, T0, library="all_students")
        self.s.commit()
        self.assertEqual((self.opens(self.a, m), self.opens(self.b, m)), ("library", "library"))
        ms.update_material(self.s, self.actor, m, T0, library="groups")
        ms.set_library_groups(self.s, self.actor, m, T0, [self.group])
        self.s.commit()
        self.assertEqual((self.opens(self.a, m), self.opens(self.b, m)), ("library", None))
        ms.archive(self.s, self.actor, m, T0)
        self.s.commit()
        self.assertIsNone(self.opens(self.a, m), "an archived material leaves the library")
        # 2. a material item of it in the feed: made while the material is not archived, then archived again - an
        # archived material still opens this way
        ms.unarchive(self.s, self.actor, m, T0)
        item = self.material_item(m, publish=False)
        items.publish(self.s, self.actor, item, T0)
        ms.archive(self.s, self.actor, m, T0)
        self.s.commit()
        self.assertEqual((self.opens(self.a, m), self.opens(self.b, m)), ("feed", None))
        items.archive(self.s, self.actor, item, T0)
        self.s.commit()
        self.assertIsNone(self.opens(self.a, m))
        # 3. a feed item linking it, after its deadline with the statement hidden
        link_item = self.feed_item(body="Повтори [[памятка:cycles|циклы]]", due_at=T0 + timedelta(hours=1),
                                   after_due_statement="hidden")
        self.assertEqual(self.opens(self.a, m, LATER), "item_link")
        items.archive(self.s, self.actor, link_item, T0)
        self.s.commit()
        self.assertIsNone(self.opens(self.a, m))
        # 4. a contest problem whose statement links it: a group contest, the student not joined yet
        cp = self.seed["cp"]
        self.s.add(MaterialRef(slug="cycles", problem_version_id=cp.problem_version_id))
        contest = self.s.get(Contest, cp.contest_id)
        contest.access_mode = AccessMode.groups
        self.s.add(ContestGroup(contest_id=contest.id, group_id=self.group.id))
        self.s.commit()
        self.assertEqual((self.opens(self.a, m), self.opens(self.b, m)), ("problem_link", None))
        self.s.get(ContestProblem, cp.id).hidden = True
        self.s.commit()
        self.assertIsNone(self.opens(self.a, m))

    def test_without_a_basis_everything_is_not_found(self):
        m = self.material(body="[[памятка:cycles]]")
        v = self.add_file(m)
        self.s.commit()
        file_id = ms.version_attachments(self.s, v)[0].id
        pupil = self.s.get(type(self.a), self.a.id)
        self.refused("material_not_found", ms.material_view, self.s, pupil, m, LATER)
        self.refused("material_not_found", ms.material_file, self.s, pupil, m, file_id, LATER)
        admin_view = ms.material_view(self.s, self.admin, m, LATER)
        self.assertEqual((admin_view["version"], admin_view["links"]),
                         (2, [{"slug": "cycles", "status": "ok", "id": m.id, "title": "Циклы"}]))
        # with a basis: the latest version and its file only; an older version is the admin's
        ms.update_material(self.s, self.actor, m, T0, library="all_students")
        self.s.commit()
        view = ms.material_view(self.s, pupil, m, LATER)
        self.assertEqual((view["version"], view["body_md"], [a["name"] for a in view["attachments"]]),
                         (2, "[[памятка:cycles]]", ["схема.pdf"]))
        self.assertNotIn("library", view)
        self.assertEqual(ms.material_file(self.s, pupil, m, file_id, LATER).id, file_id)
        v3 = ms.add_link(self.s, self.actor, m, LINK, T0)
        self.s.commit()
        self.refused("attachment_not_found", ms.material_file, self.s, pupil, m, file_id, LATER, version=2)
        self.assertEqual(ms.material_file(self.s, self.admin, m, file_id, LATER, version=2).id, file_id)
        link_id = ms.version_attachments(self.s, v3)[1].id
        self.refused("attachment_not_found", ms.material_file, self.s, pupil, m, link_id, LATER)

    def test_a_student_cannot_tell_missing_from_closed(self):
        self.material("open-one", "Открытая", library="all_students")
        self.material("closed", "Закрытая")
        archived = self.material("old", "Старая", library="all_students")
        ms.archive(self.s, self.actor, archived, T0)
        self.s.commit()
        pupil = self.s.get(type(self.a), self.a.id)
        text = "[[памятка:open-one]] [[памятка:closed]] [[памятка:old]] [[памятка:never]] [[памятка:closed|ещё]]"
        out = ms.resolve(self.s, pupil, [text], LATER)
        self.assertEqual(out[0], {"slug": "open-one", "available": True, "id": out[0]["id"], "title": "Открытая"})
        unavailable = out[1:]
        self.assertEqual(unavailable, [{"slug": s, "available": False} for s in ("closed", "old", "never")],
                         "the same answer for no access, archived without a basis and no such material")
        admin = ms.resolve(self.s, self.admin, [text], LATER)
        self.assertEqual([r["status"] for r in admin], ["ok", "ok", "archived", "unknown"])

    # ---------------------------------------------------------------- the library
    def test_search_ignores_case_in_cyrillic_and_takes_wildcards_literally(self):
        for slug, title in (("a-1", "Циклы"), ("a-2", "ЦИКЛЫ while"), ("a-3", "Вложенные циклы"),
                            ("a-4", "Условия"), ("a-5", "100% правильно"), ("a-6", "snake_case"),
                            ("a-7", "Путь C:\\temp"), ("a-8", "1000 правильно"), ("a-9", "snakeXcase")):
            self.material(slug, title, library="all_students")
        pupil = self.s.get(type(self.a), self.a.id)

        def found(q):
            return [r["title"] for r in library.search(self.s, pupil, q)]
        self.assertEqual(found("циклы"), ["Вложенные циклы", "Циклы", "ЦИКЛЫ while"])
        self.assertEqual(found("ЦИКЛЫ"), found("циклы"))
        self.assertEqual(found("%"), ["100% правильно"])
        self.assertEqual(found("_"), ["snake_case"])
        self.assertEqual(found("\\"), ["Путь C:\\temp"])
        self.assertEqual(found("0% п"), ["100% правильно"], "not 1000 правильно")
        self.assertEqual(found("e_c"), ["snake_case"], "not snakeXcase")
        self.assertEqual(found("  "), [])
        self.assertEqual(library.library_view(self.s, pupil, LATER, q="циклы")["materials"][0]["slug"], "a-3")

    def test_sections_two_levels_with_orders_and_what_a_student_sees(self):
        cli = self.actor
        python = library.create_section(self.s, cli, T0, "Python")
        loops = library.create_section(self.s, cli, T0, "Циклы", python)
        io = library.create_section(self.s, cli, T0, "Ввод-вывод")
        self.s.commit()
        self.refused("section_too_deep", library.create_section, self.s, cli, T0, "Глубже",
                     self.s.get(type(loops), loops.id))
        self.refused("section_too_deep", library.move_section, self.s, cli, self.s.get(type(python), python.id), T0,
                     self.s.get(type(io), io.id))
        self.refused("section_invalid", library.move_section, self.s, cli, self.s.get(type(io), io.id), T0,
                     self.s.get(type(io), io.id))
        a = self.material("a-1", "Цикл for", library="all_students")
        b = self.material("a-2", "Цикл while", library="all_students")
        hidden = self.material("a-3", "Только преподавателям")
        for m in (a, b, hidden):
            library.place(self.s, cli, m, self.s.get(type(loops), loops.id), T0)
        self.assertFalse(library.place(self.s, cli, a, self.s.get(type(loops), loops.id), T0))
        library.place(self.s, cli, a, self.s.get(type(io), io.id), T0)  # in two sections
        self.s.commit()
        loops = self.s.get(type(loops), loops.id)
        bad = self.refused("order_invalid", library.reorder_materials, self.s, cli, loops, T0, [a.id, a.id])
        self.assertEqual(bad.data["repeated"], [a.id])
        library.reorder_materials(self.s, cli, self.s.get(type(loops), loops.id), T0, [b.id, a.id, hidden.id])
        library.reorder_sections(self.s, cli, T0, [io.id, python.id])
        self.s.commit()
        pupil = self.s.get(type(self.a), self.a.id)
        view = library.library_view(self.s, pupil, LATER)
        self.assertEqual([s["title"] for s in view["sections"]], ["Ввод-вывод", "Python"])
        self.assertEqual([m["slug"] for m in view["sections"][1]["sections"][0]["materials"]], ["a-2", "a-1"])
        self.assertEqual(view["unsorted"], [])
        admin = library.library_view(self.s, self.admin, LATER)
        self.assertEqual([m["slug"] for m in admin["sections"][1]["sections"][0]["materials"]], ["a-2", "a-1", "a-3"])
        self.refused("section_not_empty", library.delete_section, self.s, cli, self.s.get(type(python), python.id),
                     T0)
        out = library.delete_section(self.s, cli, self.s.get(type(loops), loops.id), T0)
        self.s.commit()
        self.assertEqual(out["materials"], ["a-2", "a-1", "a-3"])
        self.assertEqual(self.s.execute(select(func.count()).select_from(Material)).scalar_one(), 3)
        self.assertEqual([m["slug"] for m in library.library_view(self.s, pupil, LATER)["unsorted"]], ["a-2"])

    # ---------------------------------------------------------------- links when texts are saved
    def package(self, statement):
        root = self.tmp / "pkg"
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(ROOT / "problems-examples" / "sum-of-two", root)
        (root / "statement" / "ru.md").write_text(statement, encoding="utf-8")
        return root

    def refs_of(self, **source):
        (column, value), = source.items()
        return sorted(self.s.execute(select(MaterialRef.slug).where(getattr(MaterialRef, column) == value))
                      .scalars())

    def test_import_warns_and_writes_links_only_for_a_created_version(self):
        self.material()
        old = self.material("old-io", "Старый ввод")
        ms.archive(self.s, self.actor, old, T0)
        self.s.commit()
        root = self.package("См. [[памятка:cycles]], [[Памятка:old-io]], [[памятка:later]] и [[памятка:Bad]]")

        def run(**kw):
            r = import_package(self.s, self.blobs, ImportSettings(), self.actor, T0, directory=root, **kw)
            self.s.commit()
            return r
        dry = run(dry_run=True)
        self.assertEqual(dry.kind, "new")
        self.assertEqual(self.s.execute(select(func.count()).select_from(MaterialRef)
                                        .where(MaterialRef.problem_version_id.is_not(None))).scalar_one(), 0)
        link_warnings = [w for w in dry.warnings if "памятка" in w]
        self.assertEqual(len(link_warnings), 3, dry.warnings)
        self.assertTrue(link_warnings[0].startswith("statement/ru.md: [[памятка:Bad]] is not a link"))
        created = run()
        self.assertEqual(created.kind, "created")
        self.assertEqual(self.refs_of(problem_version_id=created.version.id), ["cycles", "later", "old-io"])
        self.assertEqual([w for w in created.warnings if "памятка" in w], link_warnings)
        again = run()
        self.assertEqual((again.kind, [w for w in again.warnings if "памятка" in w]), ("exists", link_warnings))
        self.assertEqual(self.s.execute(select(func.count()).select_from(MaterialRef)).scalar_one(), 3,
                         "exists writes nothing")

    def test_feed_items_and_versions_write_their_links_again(self):
        item = self.feed_item(body="[[памятка:cycles]]")
        self.assertEqual(self.refs_of(feed_item_id=item.id), ["cycles"])
        self.assertIn("there is no material cycles", items.link_warnings(self.s, item)[0])
        items.update_item(self.s, self.actor, item, T0, body_md="[[памятка:loops]] [[памятка:io-fast]]")
        self.s.commit()
        self.assertEqual(self.refs_of(feed_item_id=item.id), ["io-fast", "loops"])
        items.update_item(self.s, self.actor, item, T0, title="Другое")  # no new body: the links stay
        self.s.commit()
        self.assertEqual(self.refs_of(feed_item_id=item.id), ["io-fast", "loops"])
        m = self.material("other", "Другое", body="[[памятка:loops]]")
        v1 = ms.latest_version(self.s, m)
        v2 = ms.new_version(self.s, self.actor, m, T0, "[[памятка:arrays]]").version
        self.s.commit()
        self.assertEqual((self.refs_of(material_version_id=v1.id), self.refs_of(material_version_id=v2.id)),
                         (["loops"], ["arrays"]))

    def test_a_link_to_an_unknown_slug_works_once_the_material_exists(self):
        self.feed_item(body="Повтори [[памятка:loops]]")
        pupil = self.s.get(type(self.a), self.a.id)
        self.assertEqual(ms.resolve(self.s, pupil, ["[[памятка:loops]]"], LATER),
                         [{"slug": "loops", "available": False}])
        m = self.material("loops", "Петли")
        self.assertEqual(ms.open_basis(self.s, pupil, m, LATER), "item_link")
        self.assertEqual(ms.resolve(self.s, pupil, ["[[памятка:loops]]"], LATER),
                         [{"slug": "loops", "available": True, "id": m.id, "title": "Петли"}])

    # ---------------------------------------------------------------- material items
    def test_material_items(self):
        m = self.material()
        item = items.create_item(self.s, self.actor, T0, kind="material", title="  ", material=m,
                                 comments_enabled=True, available_from=LATER)
        self.s.commit()
        self.assertEqual((item.title, item.material_id, item.comments_enabled), ("Циклы", m.id, True))
        detail = items.item_detail(self.s, item)
        self.assertEqual(detail["material"], {"id": m.id, "slug": "cycles", "title": "Циклы", "archived": False})
        self.assertEqual(detail["link_warnings"], [])
        self.refused("material_required", items.create_item, self.s, self.actor, T0, kind="material", title="М")
        self.refused("item_invalid", items.create_item, self.s, self.actor, T0, kind="assignment", title="З",
                     grading="none", material=m)
        self.refused("item_invalid", items.create_item, self.s, self.actor, T0, kind="material", title="М",
                     material=m, due_at=LATER)
        ms.archive(self.s, self.actor, m, T0)
        self.s.commit()
        self.refused("material_archived", items.create_item, self.s, self.actor, T0, kind="material", title="М",
                     material=self.s.get(Material, m.id))
        self.assertEqual(self.audits("item.create")[0]["material_id"], m.id)

    # ---------------------------------------------------------------- where it is used, the audit
    def test_where_used_and_the_audit_without_content(self):
        m = self.material(body="секретный текст памятки")
        self.material_item(m)
        self.feed_item(body="См. [[памятка:cycles]]")
        self.s.add(MaterialRef(slug="cycles", problem_version_id=self.seed["version"].id))
        other = self.material("other", "Другое", body="[[памятка:cycles]]")
        ms.new_version(self.s, self.actor, other, T0, "ссылки больше нет")
        self.material("third", "Третье", body="[[памятка:cycles]]")
        self.s.commit()
        used = ms.where_used(self.s, self.s.get(Material, m.id))
        self.assertEqual(len(used["material_items"]), 1)
        self.assertEqual(len(used["linking_items"]), 1)
        self.assertEqual(used["problems"], ["sum@1"])
        self.assertEqual(used["materials"], ["third"], "only the latest version of another material counts")
        ms.new_version(self.s, self.actor, m, T0, "новый секретный текст")
        self.s.commit()
        texts = str([d for d in self.s.execute(select(AuditLog.details).where(AuditLog.action.like("material.%")))
                     .scalars()])
        self.assertNotIn("секретный", texts)
        self.assertEqual(self.audits("material.version")[-1], {"slug": "cycles", "version": 2, "from": 1,
                                                               "body_chars": len("новый секретный текст")})
