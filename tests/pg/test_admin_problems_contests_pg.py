"""/api/admin problems and contests on a real PostgreSQL: uploads, the catalog, files under the XSS rules, contests
through the JSON form of contest.yaml."""
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, ROOT, URL, requires_pg

EXAMPLES = ROOT / "problems-examples"

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app.contests.spec import _TextDates as TextDates
    from app.models import Blob, Submission
    from app.settings import AuthSettings, ImportSettings, Settings

LIMIT = 300_000  # the archive limit of this test class: small, to reach 413 cheaply


def zipped(root: Path, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                z.writestr(prefix + p.relative_to(root).as_posix(), p.read_bytes())
    return buf.getvalue()


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminProblemsContestsTests(ApiCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        super().setUp()
        _boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))

    def settings(self):
        return Settings(public_origin=ORIGIN, blob_dir=str(self.tmp / "blobs"), timezone="Asia/Novosibirsk",
                        auth=AuthSettings(secret_key=Fernet.generate_key().decode()),
                        imports=ImportSettings(max_archive_bytes=LIMIT))

    def call(self, method, path, **kw):
        headers = {**(self.h if method != "get" else {}), **kw.pop("headers", {})}
        return getattr(self.b, method)(f"/api/admin{path}", headers=headers, **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def upload(self, data, dry_run=False, kind="application/zip"):
        return self.call("post", "/problems" + ("?dry_run=true" if dry_run else ""), content=data,
                         headers={"Content-Type": kind})

    def copy(self, name="sum-of-two") -> Path:
        root = self.tmp / f"{name}-{len(list(self.tmp.glob(name + '*')))}"
        shutil.copytree(EXAMPLES / name, root)
        return root

    def orphan_files(self):
        """Blob files without a row: what a refused upload must never leave behind."""
        store = self.tmp / "blobs"
        files = {p.name for p in store.rglob("*") if p.is_file() and not p.name.startswith(".")} if store.exists() else set()
        self.s.expire_all()
        return files - set(self.s.execute(select(Blob.sha256)).scalars())

    # ---------------------------------------------------------------- uploads
    def test_upload_dry_run_created_exists_and_refusals(self):
        data = zipped(EXAMPLES / "sum-of-two", "sum-of-two/")
        dry = self.ok(self.upload(data, dry_run=True))
        self.assertEqual((dry["result"], dry["version"], dry["dry_run"]), ("new", 1, True))
        self.assertFalse((self.tmp / "blobs").exists() and any((self.tmp / "blobs").rglob("*")))  # nothing written
        created = self.ok(self.upload(data), 201)
        self.assertEqual((created["slug"], created["result"], created["version"]), ("sum-of-two", "created", 1))
        self.assertEqual(self.ok(self.upload(data))["result"], "exists")

        self.assertEqual(self.upload(data, kind="application/octet-stream").json()["detail"], "unsupported_media_type")

        def chunks():  # no Content-Length: the limit is reached by counting
            for _ in range(LIMIT // 10000 + 2):
                yield b"\0" * 10000

        big = self.call("post", "/problems", content=chunks(), headers={"Content-Type": "application/zip"})
        self.assertEqual((big.status_code, big.json()["detail"]), (413, "too_large"))

        broken = self.copy()
        (broken / "tests" / "small01.out").unlink()
        (broken / "problem.yaml").write_text((broken / "problem.yaml").read_text().replace("time_ms: 1000",
                                                                                           "time_ms: 1000\n  wall_ms: 10"))
        r = self.upload(zipped(broken))
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "package_invalid"))
        self.assertTrue(any("missing answer file" in p for p in r.json()["problems"]))
        self.assertTrue(any("wall_ms" in p for p in r.json()["problems"]))  # every reason at once
        self.assertEqual(self.upload(b"PK\x03\x04 not really").json()["detail"], "package_invalid")

        self.ok(self.call("post", "/problems/sum-of-two/archive"))
        changed = self.copy()
        (changed / "statement" / "ru.md").write_text("Новое условие.\n", encoding="utf-8")
        r = self.upload(zipped(changed))
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "problem_archived"))
        self.assertEqual(self.orphan_files(), set())  # refusals leave no blob without a row

    # ---------------------------------------------------------------- catalog
    def test_catalog(self):
        self.ok(self.upload(zipped(EXAMPLES / "sum-of-two")), 201)
        self.ok(self.upload(zipped(EXAMPLES / "pair-with-sum")), 201)
        self.ok(self.call("post", "/problems/pair-with-sum/archive"))
        slugs = lambda **p: [i["slug"] for i in self.ok(self.call("get", "/problems", params=p))["items"]]  # noqa: E731
        self.assertNotIn("pair-with-sum", slugs())
        self.assertEqual(slugs(archived="true"), ["pair-with-sum"])
        self.assertIn("pair-with-sum", slugs(archived="all"))
        self.assertEqual(slugs(q="sum-of"), ["sum-of-two"])
        problem = self.ok(self.call("get", "/problems/sum-of-two"))
        self.assertEqual([(v["version"], v["status"], v["check_report"]) for v in problem["versions"]], [(1, "ready", None)])
        self.assertTrue(problem["versions"][0]["imported_at"].endswith("Z"))
        version = self.ok(self.call("get", "/problems/sum-of-two/versions/1"))
        self.assertIn("ru", version["statements"])
        self.assertIn(("solution", "solutions/ok.py", "OK"), [(p["role"], p["name"], p["expect"]) for p in version["programs"]])
        hidden = [t for g in version["groups"] if g["visibility"] == "hidden" for t in g["tests"]]
        self.assertTrue(hidden)  # admins see hidden tests (their names here, their files below)
        self.assertEqual(self.call("get", "/problems/nope").json()["detail"], "problem_not_found")
        self.assertEqual(self.call("get", "/problems/sum-of-two/versions/9").json()["detail"], "version_not_found")

    # ---------------------------------------------------------------- files under the XSS rules
    def test_files_of_a_version_are_never_a_page(self):
        root = self.copy()
        (root / "evil.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')
        (root / "statement" / "evil.html").write_text("<script>alert(1)</script>")
        (root / "statement" / "pic.png").write_text("<html><script>alert(1)</script></html>")  # a lie inside
        self.ok(self.upload(zipped(root)), 201)
        base = "/problems/sum-of-two/versions/1/files/"

        def fetch(path):
            return self.call("get", base + path)

        for path in ("evil.svg", "statement/evil.html"):
            with self.subTest(path=path):
                r = fetch(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["content-type"], "application/octet-stream")
                self.assertTrue(r.headers["content-disposition"].startswith("attachment;"))
                self.assertEqual(r.headers["x-content-type-options"], "nosniff")
                self.assertEqual(r.headers["content-security-policy"], "sandbox; default-src 'none'")
        r = fetch("statement/pic.png")  # the type comes from the name, never from the content
        self.assertEqual((r.headers["content-type"], r.headers["x-content-type-options"]), ("image/png", "nosniff"))
        self.assertTrue(r.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(r.headers["content-security-policy"], "sandbox; default-src 'none'")
        for path in ("tests/small01.in", "problem.yaml", "solutions/ok.py"):
            with self.subTest(path=path):
                r = fetch(path)
                self.assertEqual(r.headers["content-type"], "text/plain; charset=utf-8")
                self.assertTrue(r.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(fetch("tests/small01.in").content, (root / "tests" / "small01.in").read_bytes())
        for path in ("../problem.yaml", "%2e%2e/problem.yaml", "%2e%2e%2fproblem.yaml", "/etc/passwd",
                     "%2Fetc%2Fpasswd", "statement/%2e%2e/problem.yaml", "statement%2f..%2fproblem.yaml",
                     "nope.txt", "tests", "Problem.yaml"):  # (plain "a/../b" is resolved by the client itself)
            with self.subTest(path=path):
                r = fetch(path)
                self.assertEqual(r.status_code, 404, r.text)
                self.assertNotIn(b"format_version", r.content)

    # ---------------------------------------------------------------- contests
    def contest(self, **changes):
        spec = {"format_version": 1, "slug": "olymp", "title": "Олимпиада", "starts_at": "2026-11-10 09:00",
                "ends_at": "2026-11-10 18:00", "access": {"mode": "code"},
                "problems": [{"label": "A", "problem": "sum-of-two"}, {"label": "B", "problem": "pair-with-sum"}]}
        spec.update(changes)
        return spec

    def put(self, spec, slug="olymp", dry_run=False):
        return self.call("put", f"/contests/{slug}" + ("?dry_run=true" if dry_run else ""), json=spec)

    def test_every_time_in_a_contest_answer_is_utc_with_z(self):
        import re

        self.ok(self.upload(zipped(EXAMPLES / "sum-of-two")), 201)
        spec = self.contest(problems=[{"label": "A", "problem": "sum-of-two", "available_from": "2026-11-10 09:30",
                                       "due_at": "2026-11-10 12:00:00"}], freeze_at="2026-11-10 17:00",
                            leaderboard="visible")
        self.ok(self.put(spec), 201)
        times = []

        def walk(value):
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
                times.append(value)

        detail = self.ok(self.call("get", "/contests/olymp"))
        walk(detail)
        walk(self.ok(self.call("get", "/contests"))["items"])
        self.assertGreaterEqual(len(times), 6)  # starts, ends, freeze, available_from, due_at, created_at, ...
        self.assertEqual([t for t in times if not re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z$", t)], [])
        self.assertEqual(detail["spec"]["problems"][0]["due_at"], "2026-11-10T05:00:00Z")  # 12:00 in JUDGE_TIMEZONE

    def test_contests_through_json(self):
        for name in ("sum-of-two", "pair-with-sum"):
            self.ok(self.upload(zipped(EXAMPLES / name)), 201)
        first = self.ok(self.put(self.contest()), 201)
        self.assertEqual(first["changes"][0], "contest created (draft)")
        self.assertEqual(self.ok(self.put(self.contest()))["changes"], [])  # idempotent
        detail = self.ok(self.call("get", "/contests/olymp"))
        self.assertEqual((detail["state"], len(detail["invite_code"]), detail["spec"]["starts_at"]),
                         ("draft", 6, "2026-11-10T02:00:00Z"))  # 09:00 in JUDGE_TIMEZONE, answered in UTC
        self.assertEqual(self.ok(self.put(detail["spec"]))["changes"], [])  # what GET gives, PUT takes unchanged
        yaml_spec = yaml.load((ROOT / "contests-examples" / "lesson-fixed.yaml").read_text(encoding="utf-8"),
                              Loader=TextDates)  # as judgectl reads it: dates stay text
        self.ok(self.put({**yaml_spec, "slug": "lesson"}, slug="lesson"), 201)  # a contest.yaml, as JSON

        dry = self.ok(self.put(self.contest(title="Другое"), dry_run=True))
        self.assertEqual((dry["changes"], dry["dry_run"]), (["title: Олимпиада -> Другое"], True))
        self.assertEqual(self.ok(self.call("get", "/contests/olymp"))["spec"]["title"], "Олимпиада")

        self.ok(self.put(self.contest(access={"mode": "open"})))
        back = self.ok(self.put(self.contest()))
        self.assertTrue(any("code regenerate" in w for w in back["warnings"]))

        r = self.put(self.contest(slug="other"))
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "slug_mismatch"))
        r = self.put(self.contest(ends_at="2026-11-10 08:00", leaderboard="nope"))
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "contest_invalid"))
        r = self.put(self.contest(problems=[{"label": "A", "problem": "nope"}]))
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "contest_conflict"))
        self.assertIn("A: no problem 'nope'", r.json()["problems"][0])

        root = self.copy()
        (root / "statement" / "ru.md").write_text("Версия 2.\n", encoding="utf-8")
        self.ok(self.upload(zipped(root)), 201)
        row = next(x for x in detail["rows"] if x["label"] == "A")
        self.s.add(Submission(user_id=self.student(1).id, contest_problem_id=row["id"], language_key="python",
                              source_sha256="c" * 64))
        self.s.commit()
        pinned = [{"label": "A", "problem": "sum-of-two", "version": 2}, {"label": "B", "problem": "pair-with-sum"}]
        r = self.put(self.contest(problems=pinned))
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "contest_conflict"))
        self.assertIn("judgectl contest pin olymp A 2", r.json()["problems"][0])
        r = self.call("post", "/contests/olymp/pin", json={"label": "A", "version": 2})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "invalid_request"))  # rejudge is required
        self.assertEqual(self.ok(self.call("post", "/contests/olymp/pin", json={"label": "A", "version": 2,
                                                                               "rejudge": False}))["to"], 2)
        r = self.call("post", "/contests/olymp/pin", json={"label": "Z", "version": 1, "rejudge": False})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "contest_problem_not_found"))

        self.assertEqual(self.ok(self.call("post", "/contests/olymp/publish"))["state"], "published")
        code = self.ok(self.call("post", "/contests/olymp/code"))["invite_code"]
        self.assertNotEqual(code, detail["invite_code"])
        self.ok(self.put({**self.contest(access={"mode": "open"}), "problems": pinned}))
        self.assertEqual(self.call("post", "/contests/olymp/code").json()["detail"], "contest_is_open")
        self.assertEqual(self.ok(self.call("get", "/contests/olymp/participants"))["items"], [])
        listed = self.ok(self.call("get", "/contests", params={"state": "published"}))["items"]
        self.assertEqual([c["slug"] for c in listed], ["olymp", "c"])  # newest first; "c" is the seed's
        self.assertEqual(self.call("get", "/contests/nope").json()["detail"], "contest_not_found")
