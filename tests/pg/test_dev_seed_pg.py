"""app.dev_seed end to end without isolate: package -> published version -> backend manifest -> the worker's
ProblemStore -> the runner (local backend) judges the package's own reference solutions with the expected
verdicts. Skipped unless JUDGE_TEST_DATABASE_URL is set."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

EXAMPLES = ROOT / "problems-examples"

if URL and HAVE_SA:
    import yaml
    from sqlalchemy import func, select

    from app import db, dev_seed
    from app.blobstore import BlobStore
    from app.manifest_db import build_manifest
    from app.models import ProblemVersion, Submission, User
    from runner.config import RunnerConfig
    from runner.judge import Judge
    from worker.cache import DiskCache
    from worker.materialize import ProblemStore


class _BlobClient:
    """What ProblemStore needs from the worker's API client, served straight from the database and blob store."""

    def __init__(self, session, blobs):
        self.session, self.blobs = session, blobs

    def manifest(self, version_id):
        data, digest = build_manifest(self.session, version_id)
        return {"manifest": data, "manifest_sha256": digest}

    def download_blob(self, sha256, write):
        write(self.blobs.get(sha256))


@requires_pg
class DevSeedTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blobs = BlobStore(self.tmp / "blobs")
        self.teacher = self.s.get(User, self.seed["teacher"].id)
        for key, name in dev_seed.LANGUAGES:
            dev_seed._get_or_create(self.s, dev_seed.Language, {"key": key}, name=name, config={})
        self.s.commit()  # main() works in its own session: uncommitted rows here would block it forever

    def version(self, root):
        v = dev_seed.ensure_version(self.s, self.blobs, dev_seed.load_package(root), self.teacher)
        self.s.commit()
        return v

    def test_same_content_reuses_the_version_and_changes_create_a_new_one(self):
        first = self.version(EXAMPLES / "sum-of-two")
        self.assertEqual(self.version(EXAMPLES / "sum-of-two").id, first.id)
        changed = self.tmp / "sum-of-two"
        shutil.copytree(EXAMPLES / "sum-of-two", changed)
        (changed / "tests" / "small01.in").write_text("40 2\n")
        second = self.version(changed)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.version, first.version + 1)
        count = self.s.execute(select(func.count()).select_from(ProblemVersion)
                               .where(ProblemVersion.problem_id == first.problem_id)).scalar_one()
        self.assertEqual(count, 2, "the first version is kept unchanged")

    @mock.patch.dict(os.environ, {"JUDGE_ALLOW_UNSAFE_LOCAL": "1"})
    def test_reference_solutions_get_their_expected_verdicts(self):
        store = ProblemStore(DiskCache(self.tmp / "cache"), _BlobClient(self.s, self.blobs))
        judge = Judge(RunnerConfig(backend="local"))
        for name in ("sum-of-two", "pair-with-sum", "guess-number"):
            v = self.version(EXAMPLES / name)
            _data, digest = build_manifest(self.s, v.id)
            loaded = store.get(v.id, digest)
            spec = yaml.safe_load((EXAMPLES / name / "problem.yaml").read_text(encoding="utf-8"))
            for sol in spec["solutions"]:
                path = EXAMPLES / name / sol["path"]
                lang = {".py": "python", ".cpp": "cpp", ".c": "c"}[path.suffix]
                report = judge.judge_submission(loaded.problem, lang, path.read_bytes())
                got = report.verdict if report.verdict.value in ("CE", "IE") else report.first_fail_verdict
                with self.subTest(problem=name, solution=sol["path"]):
                    self.assertEqual(got.value, sol["expect"], report.error)
                    self.assertEqual(len(loaded.test_ids), len(report.tests))

    def test_main_enqueues_archive_submissions_for_several_students(self):
        zip_path = self.tmp / "sol.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("main.py", "from helper import add\nprint(add(*map(int, input().split())))\n")
            z.writestr("helper.py", "def add(a, b):\n    return a + b\n")
        env = {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": str(self.tmp / "blobs")}
        # main() opens its own session through app.db: give it this test's schema
        with mock.patch.dict(os.environ, env), mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                contextlib.redirect_stdout(io.StringIO()):
            for user in (4, 5):
                self.assertEqual(dev_seed.main(["--package", str(EXAMPLES / "sum-of-two"), "--source", str(zip_path),
                                                "--archive", "--entry", "main.py", "--user", str(user), "--count", "2"]), 0)
        subs = self.s.execute(select(Submission, User.login).join(User, User.id == Submission.user_id)
                              .where(Submission.is_archive.is_(True))).all()
        self.assertEqual(sorted(login for _s, login in subs), ["dev-student-4"] * 2 + ["dev-student-5"] * 2)
        self.assertTrue(all(s.entry_file == "main.py" for s, _l in subs))

    def test_refuses_without_the_dev_flag(self):
        with mock.patch.dict(os.environ, {"JUDGE_DEV": ""}):
            self.assertEqual(dev_seed.main([]), 2)

if __name__ == "__main__":
    unittest.main()
