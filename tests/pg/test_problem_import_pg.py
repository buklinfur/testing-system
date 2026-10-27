"""The problem importer on a real PostgreSQL: versions, identity by content (R29), refusals that write nothing."""
import io
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

EXAMPLES = ROOT / "problems-examples"
T0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app.auth import audit
    from app.blobstore import BlobStore
    from app.models import AuditLog, Problem, ProblemVersion, Program, VersionStatus
    from app.problems.importer import import_package
    from app.problems.unpack import ArchiveError
    from app.settings import ImportSettings
    from protocol.problem_package import PackageError, content_digest


def zipped(root: Path, prefix: str = "", extra=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                z.writestr(prefix + p.relative_to(root).as_posix(), p.read_bytes())
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


@requires_pg
class ImportTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blobs = BlobStore(self.tmp / "blobs")
        self.actor = audit.Actor(cli="cli:judge", sudo_user="teacher")

    def run_import(self, **source):
        result = import_package(self.s, self.blobs, ImportSettings(), self.actor, T0, **source)
        self.s.commit()
        return result

    def copy(self, name) -> Path:
        root = self.tmp / name
        shutil.copytree(EXAMPLES / name, root)
        return root

    def blob_files(self):
        return sorted(p.name for p in (self.tmp / "blobs").rglob("*") if p.is_file())

    def count(self, model):
        return self.s.execute(select(func.count()).select_from(model)).scalar_one()

    def test_every_example_imports_and_a_second_import_is_a_no_op(self):
        for root in sorted(EXAMPLES.iterdir()):
            with self.subTest(example=root.name):
                r = self.run_import(directory=root)
                v = r.version
                self.assertEqual((r.kind, v.version, v.status), ("created", 1, VersionStatus.ready))
                self.assertEqual(r.problem.slug, root.name)
                self.assertIsNone(r.problem.owner_id)  # imported from the console
                self.assertIsNone(v.imported_by)
                self.assertEqual(len(v.manifest_sha256), 64)
                self.assertEqual(v.content_sha256, content_digest(v.files))
                for sha in v.files.values():
                    self.assertTrue(self.blobs.exists(sha))
                self.assertIn("ru", v.statements)
                self.assertNotIn("test_groups", v.config)
        blobs, audits = self.blob_files(), self.count(AuditLog)
        for root in sorted(EXAMPLES.iterdir()):
            with self.subTest(again=root.name):
                r = self.run_import(directory=root)
                self.assertEqual((r.kind, r.version.version), ("exists", 1))
                self.assertIn("already exists as v1", r.describe())
        self.assertEqual(self.count(ProblemVersion), 5 + 1)  # and the seed's version
        self.assertEqual((self.blob_files(), self.count(AuditLog)), (blobs, audits))  # nothing written at all

    def test_jury_programs_and_reference_solutions_are_stored(self):
        v = self.run_import(directory=EXAMPLES / "pair-with-sum").version
        programs = {(p.role.value, p.name, p.language_key, p.expected_verdict and p.expected_verdict.value)
                    for p in self.s.execute(select(Program).where(Program.version_id == v.id)).scalars()}
        self.assertIn(("checker", "check.cpp", "cpp", None), programs)
        self.assertIn(("solution", "solutions/ok.py", "python", "OK"), programs)
        self.assertIn(("solution", "solutions/wa.py", "python", "WA"), programs)
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "problem.import")).scalar_one()
        self.assertEqual((entry.actor_cli, entry.details["slug"], entry.details["version"], entry.details["sudo_user"]),
                         ("cli:judge", "pair-with-sum", 1, "teacher"))

    def test_zip_and_folder_are_the_same_content_and_junk_does_not_matter(self):
        first = self.run_import(directory=EXAMPLES / "sum-of-two").version
        data = zipped(EXAMPLES / "sum-of-two", "sum-of-two/",
                      extra=[("sum-of-two/.DS_Store", b"x"), ("__MACOSX/sum-of-two/._problem.yaml", b"x"),
                             ("sum-of-two/problem.yaml:Zone.Identifier", b"[ZoneTransfer]")])
        r = self.run_import(archive=data, source_name="sum.zip")
        self.assertEqual((r.kind, r.version.id), ("exists", first.id))
        root = self.copy("sum-of-two")
        (root / "Thumbs.db").write_bytes(b"x")
        self.assertEqual(self.run_import(directory=root).kind, "exists")

    def test_changes_make_new_versions_the_old_ones_stay(self):
        root = self.copy("sum-of-two")
        v1 = self.run_import(directory=root).version
        (root / "statement" / "ru.md").write_text("Другое условие.\n", encoding="utf-8")
        v2 = self.run_import(directory=root).version  # only the statement changed: still a new version (R29)
        (root / "statement" / "images").mkdir()
        (root / "statement" / "images" / "pic.png").write_bytes(b"\x89PNG fake")
        r3 = self.run_import(archive=zipped(root))
        v3 = r3.version
        self.assertEqual([v.version for v in (v1, v2, v3)], [1, 2, 3])
        self.assertIn("statement/images/pic.png", v3.files)  # pictures of the statement are kept (for 4b)
        self.assertTrue(self.blobs.exists(v3.files["statement/images/pic.png"]))
        self.assertIsNotNone(v3.package_sha256)  # the uploaded zip itself
        self.assertTrue(self.blobs.exists(v3.package_sha256))
        self.s.refresh(v1)
        self.assertEqual(v1.statements["ru"], (EXAMPLES / "sum-of-two" / "statement" / "ru.md").read_text(encoding="utf-8"))

    def test_a_refused_package_writes_nothing(self):
        root = self.copy("sum-of-two")
        (root / "tests" / "small01.out").unlink()
        with self.assertRaises(PackageError) as ctx:
            self.run_import(directory=root)
        self.assertIn("missing answer file", str(ctx.exception))
        bad_zip = zipped(EXAMPLES / "sum-of-two", extra=[("../../evil.in", b"x")])
        with self.assertRaises(ArchiveError):
            self.run_import(archive=bad_zip)
        self.s.rollback()
        self.assertEqual((self.count(Problem), self.count(ProblemVersion), self.count(AuditLog)), (1, 1, 0))  # the seed
        self.assertEqual(self.blob_files(), [])

    def test_an_archived_problem_refuses_imports(self):
        r = self.run_import(directory=EXAMPLES / "sum-of-two")
        r.problem.archived = True
        self.s.commit()
        root = self.copy("sum-of-two")
        (root / "statement" / "ru.md").write_text("new\n", encoding="utf-8")
        with self.assertRaises(PackageError) as ctx:
            self.run_import(directory=root)
        self.assertIn("judgectl problem unarchive sum-of-two", str(ctx.exception))
        self.s.rollback()
        self.assertEqual(self.count(ProblemVersion), 2)  # the seed's and v1
