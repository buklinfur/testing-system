"""blobstore.Incoming (4v.3, D-080): a file being received is written into <blob root>/tmp - the same file system as
the blobs - with sha256, size and the first bytes counted on the way, and taken into the store by one os.replace
only by adopt(); a broken connection, a body over the limit and a refusal leave neither a blob nor a temporary
file. Also: the six limits of files in settings, their defaults equal to the rules'.

The refusal of the checks through the services (a type, a quota) is tested on PostgreSQL:
tests/pg/test_attachments_pg.py.
"""
import asyncio
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app import blobstore  # noqa: E402
from app.blobstore import BlobStore, TooLarge  # noqa: E402
from app.classroom import attachment_rules as ar  # noqa: E402
from app.settings import FileSettings, Settings  # noqa: E402

MIB = 1024 * 1024


def blob_files(store: BlobStore) -> list:
    """Every blob in the store: <root>/<2 hex>/<64 hex> (nothing else under the root is one)."""
    return sorted(p.relative_to(store.root).as_posix() for p in store.root.glob("*/*")
                  if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))


def tmp_files(store: BlobStore) -> list:
    return sorted(p.name for p in store.staging().iterdir())


class FakeRequest:
    """What http_body.save_body reads of a starlette Request: the headers and the body stream."""

    def __init__(self, chunks, length=None, fail_after=None):
        self.headers = {} if length is None else {"content-length": str(length)}
        self.chunks, self.fail_after = chunks, fail_after

    async def stream(self):
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                from starlette.requests import ClientDisconnect
                raise ClientDisconnect()
            yield chunk


class IncomingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = BlobStore(Path(self.dir.name) / "blobs")
        self.before = blob_files(self.store)

    def assertNothingLeft(self):
        self.assertEqual(blob_files(self.store), self.before, "the store is as before")
        self.assertEqual(tmp_files(self.store), [], "the temporary directory is empty")

    def test_counts_on_the_way_and_adopts_with_one_rename(self):
        data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 100
        with self.store.incoming("схема.png") as up:
            for i in range(0, len(data), 1000):
                up.write(data[i:i + 1000])
            self.assertEqual(len(tmp_files(self.store)), 1)
            self.assertEqual(blob_files(self.store), [])
            up.finish()
            self.assertEqual((up.sha256, up.size), (hashlib.sha256(data).hexdigest(), len(data)))
            self.assertEqual(up.head, data[:blobstore.HEAD_BYTES])
            self.assertEqual(up.name, "схема.png")
            temp = up.path
            sha = up.adopt()
        self.assertFalse(temp.exists(), "renamed, not copied")
        self.assertEqual(self.store.get(sha), data)
        self.assertEqual(tmp_files(self.store), [])

    def test_the_temporary_directory_is_on_the_file_system_of_the_blobs_and_never_a_blob(self):
        staging = self.store.staging()
        self.assertEqual(staging.parent, self.store.root)
        self.assertEqual(os.stat(staging).st_dev, os.stat(self.store.root).st_dev)
        self.assertFalse(blobstore.valid_sha256(staging.name))
        self.assertNotEqual(len(staging.name), 2, "a directory of blobs has a two-letter name")

    def test_a_blob_that_is_there_stays_and_the_copy_goes(self):
        sha = self.store.put(b"x = 1\n")
        with self.store.incoming("a.py") as up:
            up.write(b"x = 1\n")
            self.assertEqual(up.finish().adopt(), sha)
        self.assertEqual(blob_files(self.store), [f"{sha[:2]}/{sha}"])
        self.assertEqual(tmp_files(self.store), [])

    def test_copy_from_a_file(self):
        with tempfile.TemporaryFile() as src:
            src.write(b"%PDF-1.7\n" * 1000)
            src.seek(0)
            with self.store.incoming("a.pdf", limit=MIB) as up:
                up.copy_from(src)
                self.assertEqual(up.size, 9000)
                self.assertTrue(up.head.startswith(b"%PDF-"))
        self.assertNothingLeft()

    def test_adopt_needs_a_finished_file_and_write_a_running_one(self):
        with self.store.incoming() as up:
            up.write(b"a")
            with self.assertRaises(ValueError):
                up.adopt()
            up.finish()
            with self.assertRaises(ValueError):
                up.write(b"b")
        self.assertNothingLeft()

    # ---------------------------------------------------------------- the three ways an upload ends without a blob
    def test_cleanup_when_the_connection_breaks(self):
        """The body stream fails half way (the client went away): the exception goes up, the with block closes the
        Incoming."""
        from app.http_body import save_body
        request = FakeRequest([b"PK\x03\x04" + b"a" * 1000, b"b" * 1000, b"c" * 1000], fail_after=2)
        with self.assertRaises(Exception) as caught:
            with self.store.incoming("project.sb3", limit=MIB) as up:
                asyncio.run(save_body(request, up, MIB))
        self.assertEqual(type(caught.exception).__name__, "ClientDisconnect")
        self.assertEqual(up.size, 2004, "written until the break")
        self.assertNothingLeft()

    def test_cleanup_when_the_body_is_over_the_limit(self):
        from app.api_errors import ApiError
        from app.http_body import save_body
        for request in (FakeRequest([b"a" * 600, b"b" * 600]),                 # counted: refused while streaming
                        FakeRequest([b"a" * 600, b"b" * 600], length=1200)):  # declared: refused before reading
            with self.subTest(declared=bool(request.headers)):
                with self.assertRaises(ApiError) as caught:
                    with self.store.incoming("big.txt", limit=1000) as up:
                        asyncio.run(save_body(request, up, 1000))
                self.assertEqual((caught.exception.status, caught.exception.code), (413, "too_large"))
                self.assertNothingLeft()
        # the Incoming's own limit (judgectl and dev_seed copy without save_body)
        with self.assertRaises(TooLarge):
            with self.store.incoming("big.txt", limit=1000) as up:
                up.write(b"a" * 600)
                up.write(b"b" * 600)
        self.assertEqual(up.size, 600, "the chunk over the limit is not written")
        self.assertNothingLeft()

    def test_cleanup_when_the_checks_refuse(self):
        """A finished upload the service refuses (never adopted): closing removes it."""
        with self.store.incoming("pic.png") as up:
            up.write(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
            up.finish()
            refused = ar.check_file(up.name, up.size, up.head, MIB)
            self.assertEqual(refused.reason, "file_content_mismatch")
        self.assertFalse(up.adopted)
        self.assertNothingLeft()


class FileSettingsTests(unittest.TestCase):
    def test_defaults_are_the_rules(self):
        f = FileSettings()
        self.assertEqual((f.submission_files_max, f.submission_file_max_bytes, f.submission_total_max_bytes),
                         (ar.SUBMISSION_FILES_MAX, ar.SUBMISSION_FILE_MAX_BYTES, ar.SUBMISSION_TOTAL_MAX_BYTES))
        self.assertEqual(f.student_quota_bytes, ar.STUDENT_QUOTA_BYTES)
        self.assertEqual((f.item_attachments_max, f.attachment_max_bytes),
                         (ar.ITEM_ATTACHMENTS_MAX, ar.ATTACHMENT_MAX_BYTES))
        self.assertEqual(ar.TEACHER_LIMITS.files_max, f.item_attachments_max)
        self.assertEqual(ar.TEACHER_LIMITS.file_max_bytes, f.attachment_max_bytes)
        self.assertEqual(blobstore.HEAD_BYTES, ar.HEAD_BYTES)
        self.assertEqual(Settings.from_env({}).files, f)

    def test_from_env(self):
        f = Settings.from_env({"JUDGE_SUBMISSION_FILES_MAX": "3", "JUDGE_SUBMISSION_FILE_MAX_MB": "1",
                               "JUDGE_SUBMISSION_TOTAL_MAX_MB": "2", "JUDGE_STUDENT_QUOTA_MB": "0",
                               "JUDGE_ITEM_ATTACHMENTS_MAX": "5", "JUDGE_ATTACHMENT_MAX_MB": "7"}).files
        self.assertEqual(f, FileSettings(3, MIB, 2 * MIB, 0, 5, 7 * MIB))

    def test_bad_values_stop_the_start(self):
        for name, value in (("JUDGE_SUBMISSION_FILES_MAX", "0"), ("JUDGE_SUBMISSION_FILE_MAX_MB", "-1"),
                            ("JUDGE_STUDENT_QUOTA_MB", "-1"), ("JUDGE_ATTACHMENT_MAX_MB", "0"),
                            ("JUDGE_ITEM_ATTACHMENTS_MAX", "много")):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                Settings.from_env({name: value})

    def test_the_example_env_lists_all_six(self):
        example = (ROOT / "deploy/env/backend.env.example").read_text(encoding="utf-8")
        for name in ("JUDGE_SUBMISSION_FILES_MAX=10", "JUDGE_SUBMISSION_FILE_MAX_MB=20",
                     "JUDGE_SUBMISSION_TOTAL_MAX_MB=50", "JUDGE_STUDENT_QUOTA_MB=200", "JUDGE_ITEM_ATTACHMENTS_MAX=20",
                     "JUDGE_ATTACHMENT_MAX_MB=50"):
            self.assertIn(f"# {name}\n", example)


if __name__ == "__main__":
    unittest.main()
