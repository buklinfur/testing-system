"""The schema of the library of materials (D-083, D-084, migration b8e0cbcc4cda) on a real PostgreSQL: every new CHECK
and partial unique index refuses a row by its name and accepts one; every new foreign key says its ON DELETE in the
catalog; the users who made materials keep traces; a group the library shows materials to is kept after an import's
rollback and named when archived. The migration itself (backfill, refused downgrade): tests/pg/test_migrations_pg.py.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
from datetime import datetime, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import select, text
    from sqlalchemy.exc import DataError, IntegrityError

    from app import groups as groups_service
    from app.auth import audit, users as accounts
    from app.models import AuditLog, Group, MATERIAL_BODY_MAX_CHARS, MATERIAL_TITLE_MAX_CHARS, User

SHA = "e" * 64
NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
ASSIGNMENT = dict(kind="assignment", state="published", title="Проект", body_md="", grading="none",
                  answer_kind="text", grades_release="manual", published_at="2026-11-12 10:00+00")


@requires_pg
class MaterialSchemaTests(PgCase):
    def setUp(self):
        super().setUp()
        self.s.execute(text("INSERT INTO blobs (sha256, size_bytes) VALUES (:s, 3)"), {"s": SHA})
        self.material = self.insert("materials", slug="cycles", title="Циклы")
        self.version = self.insert("material_versions", material_id=self.material, version=1, body_md="")
        self.item = self.insert("feed_items", **ASSIGNMENT)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def insert(self, table, returning="id", **values):
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        return self.s.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params}) RETURNING {returning}"),
                              values).scalar_one()

    def refused(self, constraint, fn, *args, error=None, **kw):
        """The statement fails on exactly this constraint, and the session goes on (a savepoint)."""
        with self.assertRaises(error or IntegrityError) as caught:
            with self.s.begin_nested():
                fn(*args, **kw)
        self.assertIn(constraint, str(caught.exception.orig))

    def accepted(self, fn, *args, **kw):
        with self.s.begin_nested():
            return fn(*args, **kw)

    def check(self, constraint, fn, bad: dict, good: dict):
        with self.subTest(constraint=constraint, bad=bad):
            self.refused(constraint, fn, **bad)
            self.accepted(fn, **good)

    def new_material(self, slug="io-fast", title="Быстрый ввод", **kw):
        return self.insert("materials", slug=slug, title=title, **kw)

    def new_version(self, material=None, version=2, body_md="текст", **kw):
        return self.insert("material_versions", material_id=material or self.material, version=version,
                           body_md=body_md, **kw)

    def attachment(self, item=None, version=None, **values):
        values = {"kind": "file", "position": 0, "name": "схема.pdf", "blob_sha256": SHA, "size": 3, **values}
        if values["kind"] == "link":
            values = {k: v for k, v in values.items() if k not in ("name", "blob_sha256", "size")}
            values.setdefault("url", "https://e.ru/")
        return self.insert("attachments", item_id=item, material_version_id=version, **values)

    def ref(self, slug="cycles", **source):
        return self.insert("material_refs", slug=slug, **source)

    def section(self, title="Python", parent=None, position=0):
        return self.insert("library_sections", title=title, parent_id=parent, position=position)

    # ---------------------------------------------------------------- materials and versions
    def test_material_checks(self):
        cases = [
            ("ck_materials_slug_format", dict(slug="Cycles"), dict(slug="cycles-2")),
            ("ck_materials_slug_format", dict(slug="a"), dict(slug="ab")),
            ("ck_materials_slug_format", dict(slug="-ab"), dict(slug="1ab")),
            ("ck_materials_slug_format", dict(slug="a_b"), dict(slug="a-b")),
            ("ck_materials_slug_format", dict(slug="циклы"), dict(slug="cikly")),
            ("ck_materials_title_not_blank", dict(slug="t1", title="  "), dict(slug="t1", title="Т")),
        ]
        for constraint, bad, good in cases:
            self.check(constraint, self.new_material, bad, good)
        self.refused("uq_materials_slug", self.new_material, slug="cycles")
        self.refused("material_library", self.new_material, slug="lib", library="everyone", error=DataError)
        long_title = "я" * MATERIAL_TITLE_MAX_CHARS
        self.accepted(self.new_material, slug="long-title", title=long_title)
        self.refused("value too long", self.new_material, slug="longer-title", title=long_title + "я",
                     error=DataError)
        self.assertEqual(self.s.execute(text("SELECT library::text FROM materials WHERE id = :m"),
                                        {"m": self.material}).scalar_one(), "none", "the default")

    def test_version_checks(self):
        self.check("ck_material_versions_version_positive", self.new_version, dict(version=0), dict(version=2))
        self.check("ck_material_versions_body_length", self.new_version,
                   dict(version=3, body_md="я" * (MATERIAL_BODY_MAX_CHARS + 1)),
                   dict(version=3, body_md="я" * MATERIAL_BODY_MAX_CHARS))
        self.refused("uq_material_versions_material_version", self.new_version, version=1)
        self.accepted(self.new_version, self.new_material(), version=1)  # numbers are per material

    # ---------------------------------------------------------------- feed items of kind material
    def test_a_material_item_has_its_material_and_only_it(self):
        material_item = dict(kind="material", state="published", title="Циклы", body_md="",
                             published_at="2026-11-12 10:00+00")
        names = self.s.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'feed_items'::regclass")
                               ).scalars().all()
        self.assertNotIn("ck_feed_items_material_after_4v4", names)
        self.refused("ck_feed_items_material_link", self.insert, "feed_items", **material_item)
        self.accepted(self.insert, "feed_items", **material_item, material_id=self.material)
        self.refused("ck_feed_items_material_link", self.insert, "feed_items", **ASSIGNMENT, material_id=self.material)
        # a material item has no deadline and no grading: the CHECKs of 4v.1 already say so
        self.refused("ck_feed_items_due_only_assignment", self.insert, "feed_items", **material_item,
                     material_id=self.material, due_at="2026-11-20 10:00+00")
        self.refused("ck_feed_items_assignment_settings", self.insert, "feed_items", **material_item,
                     material_id=self.material, grading="none")

    # ---------------------------------------------------------------- attachments with two owners
    def test_an_attachment_has_exactly_one_owner(self):
        self.refused("ck_attachments_one_owner", self.attachment)
        self.refused("ck_attachments_one_owner", self.attachment, self.item, self.version)
        self.accepted(self.attachment, self.item)
        self.accepted(self.attachment, None, self.version)
        self.accepted(self.attachment, None, self.version, kind="link")

    def test_names_are_unique_in_a_material_version_ignoring_case(self):
        self.accepted(self.attachment, None, self.version, name="Схема.PDF")
        self.refused("uq_attachments_material_version_name", self.attachment, None, self.version, name="схема.pdf")
        self.accepted(self.attachment, None, self.version, kind="link")  # links have no names
        self.accepted(self.attachment, None, self.new_version(), name="схема.pdf")  # the next version has its own
        self.accepted(self.attachment, self.item, name="схема.pdf")  # an item with the same name: another owner
        self.refused("uq_attachments_item_name", self.attachment, self.item, name="СХЕМА.pdf")  # as in 4v.3

    # ---------------------------------------------------------------- links
    def test_a_link_has_exactly_one_source(self):
        pv = self.problem_version()
        self.refused("ck_material_refs_one_source", self.ref)
        self.refused("ck_material_refs_one_source", self.ref, feed_item_id=self.item, material_version_id=self.version)
        self.refused("ck_material_refs_one_source", self.ref, problem_version_id=pv, feed_item_id=self.item)
        for source in (dict(problem_version_id=pv), dict(feed_item_id=self.item),
                       dict(material_version_id=self.version)):
            with self.subTest(source=source):
                self.accepted(self.ref, **source)
        self.check("ck_material_refs_slug_format", self.ref, dict(slug="Bad", feed_item_id=self.item),
                   dict(slug="good", feed_item_id=self.item))

    def test_one_link_per_source_and_slug(self):
        pv = self.problem_version()
        for index, source in (("uq_material_refs_problem_version", dict(problem_version_id=pv)),
                              ("uq_material_refs_feed_item", dict(feed_item_id=self.item)),
                              ("uq_material_refs_material_version", dict(material_version_id=self.version))):
            with self.subTest(index):
                self.accepted(self.ref, "unknown-yet", **source)
                self.refused(index, self.ref, "unknown-yet", **source)
                self.accepted(self.ref, "other", **source)
        self.accepted(self.ref, "unknown-yet", feed_item_id=self.insert("feed_items", **ASSIGNMENT))  # another item

    def problem_version(self):
        problem = self.insert("problems", slug="memo-links")
        return self.insert("problem_versions", problem_id=problem, version=1, status="ready", type="batch",
                           titles="{}", statements='{"ru": "[[памятка:cycles]]"}', config="{}")

    def test_links_go_with_their_source(self):
        draft = self.insert("feed_items", **dict(ASSIGNMENT, state="draft", published_at=None))
        self.ref(feed_item_id=draft)
        self.s.execute(text("DELETE FROM feed_items WHERE id = :i"), {"i": draft})
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM material_refs")).scalar_one(), 0)

    # ---------------------------------------------------------------- the library
    def test_sections(self):
        top = self.section()
        self.check("ck_library_sections_title_not_blank", self.section, dict(title=" "), dict(title="Ввод"))
        self.check("ck_library_sections_position_non_negative", self.section, dict(position=-1), dict(position=1))
        self.refused("ck_library_sections_not_own_parent", self.s.execute,
                     text("UPDATE library_sections SET parent_id = id WHERE id = :t"), {"t": top})
        sub = self.accepted(self.section, "Циклы", top)
        self.refused("fk_library_sections_parent_id_library_sections", self.s.execute,
                     text("DELETE FROM library_sections WHERE id = :t"), {"t": top})  # it has a subsection
        self.insert("material_sections", returning="section_id", section_id=sub, material_id=self.material,
                    position=0)
        self.refused("pk_material_sections", self.insert, "material_sections", returning="section_id",
                     section_id=sub, material_id=self.material, position=1)
        self.refused("ck_material_sections_position_non_negative", self.insert, "material_sections",
                     returning="section_id", section_id=top, material_id=self.material, position=-1)
        self.s.execute(text("DELETE FROM library_sections WHERE id = :s"), {"s": sub})  # its materials leave it
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM material_sections")).scalar_one(), 0)
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM materials")).scalar_one(), 1)

    def test_library_groups(self):
        group = self.insert("groups", name="7Б")
        self.insert("material_groups", returning="group_id", material_id=self.material, group_id=group)
        self.refused("pk_material_groups", self.insert, "material_groups", returning="group_id",
                     material_id=self.material, group_id=group)
        self.refused("fk_material_groups_group_id_groups", self.s.execute,
                     text("DELETE FROM groups WHERE id = :g"), {"g": group})

    # ---------------------------------------------------------------- foreign keys
    def test_the_new_foreign_keys_say_their_on_delete(self):
        """The list of the report of stage 2: c = CASCADE, r = RESTRICT (an explicit rule, unlike NO ACTION 'a')."""
        rows = self.s.execute(text("""
            SELECT c.conname, c.confdeltype
              FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
             WHERE n.nspname = current_schema() AND c.contype = 'f'
               AND (c.conrelid::regclass::text IN ('materials', 'material_versions', 'material_groups',
                                                   'library_sections', 'material_sections', 'material_refs')
                    OR c.conname IN ('fk_attachments_material_version_id_material_versions',
                                     'fk_feed_items_material_id_materials'))""")).all()
        self.assertEqual(dict(rows), {
            "fk_materials_created_by_users": "r",
            "fk_material_versions_material_id_materials": "r",       # versions are never deleted, nor materials
            "fk_material_versions_created_by_users": "r",
            "fk_material_groups_material_id_materials": "c",         # the library's groups go with the material
            "fk_material_groups_group_id_groups": "r",               # groups are archived, never deleted
            "fk_material_groups_added_by_users": "r",
            "fk_library_sections_parent_id_library_sections": "r",   # no section with subsections is deleted
            "fk_material_sections_section_id_library_sections": "c",  # a deleted section lets its materials go
            "fk_material_sections_material_id_materials": "r",
            "fk_material_refs_problem_version_id_problem_versions": "c",  # derived rows go with their source
            "fk_material_refs_feed_item_id_feed_items": "c",
            "fk_material_refs_material_version_id_material_versions": "c",
            "fk_attachments_material_version_id_material_versions": "r",
            "fk_feed_items_material_id_materials": "r",
        })
        item = self.s.execute(text("SELECT confdeltype FROM pg_constraint WHERE conname = "
                                   "'fk_attachments_item_id_feed_items' AND conrelid = 'attachments'::regclass")).scalar_one()
        self.assertEqual(item, "c", "unchanged since 4v.3")

    # ---------------------------------------------------------------- users and groups
    def test_user_traces_know_materials(self):
        teacher = User(login="teacher", password_hash="x", display_name="T", role="admin")
        self.s.add(teacher)
        self.s.flush()
        material = self.new_material(created_by=teacher.id)
        version = self.new_version(material, version=1, created_by=teacher.id)
        self.attachment(None, version, created_by=teacher.id)
        self.insert("material_groups", returning="group_id", material_id=material,
                    group_id=self.insert("groups", name="8А"), added_by=teacher.id)
        self.s.commit()
        self.assertEqual(accounts.traces(self.s, teacher), {"attachments added": 1, "materials created": 1,
                                                            "material versions written": 1,
                                                            "library groups added": 1})
        with self.assertRaises(accounts.UserError) as caught:
            accounts.delete_user(self.s, audit.Actor(cli="cli:test"), teacher, NOW)
        self.assertEqual(caught.exception.code, "has_traces")

    def test_a_group_of_the_library_stays_after_a_rollback_and_is_named_when_archived(self):
        group = Group(name="9В")
        self.s.add(group)
        self.s.flush()
        self.assertIsNone(groups_service.why_kept_after_rollback(self.s, group))
        self.insert("material_groups", returning="group_id", material_id=self.material, group_id=group.id)
        self.assertEqual(groups_service.why_kept_after_rollback(self.s, group), "the library shows materials to it")
        self.assertEqual([m.slug for m in groups_service.materials_of(self.s, group)], ["cycles"])
        groups_service.archive(self.s, audit.Actor(cli="cli:test"), group, NOW)
        details = self.s.execute(select(AuditLog.details).where(AuditLog.action == "group.archive")).scalar_one()
        self.assertEqual(details["materials"], ["cycles"])
        self.assertEqual(details["feed_items"], [])
