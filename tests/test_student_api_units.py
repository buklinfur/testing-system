"""The student API without a database (docs/STUDENT_API.md §6): STUDENT_STATUS names every refusal code the student
services can produce and no other, the statuses follow the convention, the poll interval, and only the exact
upload paths (a submission, a draft file) are exempt from the JSON body limit. Every code reached by a request:
tests/pg/test_student_api_pg.py (CONTEST_STATUS), tests/pg/test_student_classroom_api_pg.py (CLASSROOM_STATUS).
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
# where the refusals of the student API come from: StudentError("<code>", ...) of the services, Decision(False,
# "<code>") of the access rules (can_enter, can_start, can_submit, standings_open_for) and the deadline rules, the
# rate (rates.window_ok) and the intake (intake.CODES)
STUDENT_SOURCES = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "backend" / "app" / "student").glob("*.py"))
RULE_SOURCES = ["backend/app/contests/access.py", "backend/app/deadline_rules.py", "backend/app/rates.py"]
# the class feed (4b.2, D-101): what the student routes call - FeedError("...", "<code>") of the student services and
# of the draft, Decision(False, "<code>") of classroom/access.py, Refusal / _no("<code>") of attachment_rules
CLASSROOM_SOURCES = ["backend/app/classroom/access.py", "backend/app/classroom/attachment_rules.py",
                     "backend/app/classroom/drafts.py"]
# codes of the shared services (works.py, attachments.py: their other codes are a teacher's) a student route reaches
SHARED_CODES = {"file_not_found": "works.work_file", "draft_file_not_found": "works.submit_work (_take_drafts)",
                "quota_exceeded": "works.submit_work", "attachment_not_found": "attachments.item_file"}
# codes of the scanned classroom sources no student route can produce, each with its reason
NOT_STUDENT = {"too_many_attachments": "attachment_rules: the limit of a teacher's attachments of an item",
               "not_published": "classroom/access.py: an unpublished item is item_not_found first (it stays in the "
                                "table for the contests)"}

try:
    from app.student_api.deps import CLASSROOM_STATUS, CONTEST_STATUS, STUDENT_STATUS, UNREACHABLE
    from app.student_api.submissions import poll_after_s
    from app.submissions.intake import CODES as INTAKE_CODES
    from app.models import JudgementStatus
    HAVE_APP = True
except ImportError:  # pragma: no cover - fastapi is not installed
    HAVE_APP = False


def _name(func) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def raised_codes() -> set:
    codes = set()
    for rel in STUDENT_SOURCES + RULE_SOURCES + CLASSROOM_SOURCES:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for func in ast.walk(tree):  # problems.statement_refusal returns its code: return "<code>" / a if c else b
            if isinstance(func, ast.FunctionDef) and "refusal" in func.name:
                for ret in ast.walk(func):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        value = ret.value
                        for branch in (value.body, value.orelse) if isinstance(value, ast.IfExp) else (value,):
                            if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                                codes.add(branch.value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            if name in ("StudentError", "IntakeError") and node.args and isinstance(node.args[0], ast.Constant):
                codes.add(node.args[0].value)
            elif name == "FeedError" and len(node.args) >= 2 and isinstance(node.args[-1], ast.Constant):
                codes.add(node.args[-1].value)  # FeedError(message, "<code>", **data)
            elif name in ("Refusal", "_no") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                codes.add(node.args[0].value)  # attachment_rules: Refusal("<code>"[, index]), _no("<code>")
            elif name == "Decision" and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value is False and isinstance(node.args[1], ast.Constant):
                codes.add(node.args[1].value)
    return codes


@unittest.skipUnless(HAVE_APP, "needs fastapi")
class StudentStatusTests(unittest.TestCase):
    def test_every_code_has_a_status_and_none_is_stale(self):
        codes = raised_codes() | set(INTAKE_CODES)
        for seen in ("contest_not_found", "submission_not_found", "wrong_code", "cooldown", "rate_limited",
                     "deadline_passed", "standings_closed", "press_start", "label_needs_contest", "student_required",
                     "already_started", "not_personal", "source_encoding", "invalid_request", "statement_hidden",
                     "not_started_yet", "item_not_found", "not_an_assignment", "already_graded", "duplicate_name",
                     "file_content_mismatch", "too_many_files"):
            self.assertIn(seen, codes)  # the scan really sees each source
        self.assertTrue(set(NOT_STUDENT) <= codes)  # each one is really there
        codes = (codes - {c for c in NOT_STUDENT if c not in CONTEST_STATUS}) | set(SHARED_CODES)
        self.assertEqual(sorted(codes - set(STUDENT_STATUS)), [], "codes without an HTTP status")
        self.assertEqual(sorted(set(STUDENT_STATUS) - codes), [], "STUDENT_STATUS names codes nobody raises")

    def test_the_two_parts_agree(self):
        self.assertEqual(STUDENT_STATUS, {**CONTEST_STATUS, **CLASSROOM_STATUS})
        for code in set(CONTEST_STATUS) & set(CLASSROOM_STATUS):
            self.assertEqual(CONTEST_STATUS[code], CLASSROOM_STATUS[code], code)  # one status for a shared code
        self.assertEqual(set(CLASSROOM_STATUS) & set(NOT_STUDENT), set())

    def test_statuses_follow_the_convention(self):
        for code, status in STUDENT_STATUS.items():
            with self.subTest(code=code):
                self.assertIn(status, (403, 404, 409, 413, 422, 429))
                if code.endswith("_not_found") and code != "entry_not_found":  # a file missing inside the archive
                    self.assertEqual(status, 404)
                if code.endswith("_invalid"):
                    self.assertEqual(status, 422)
        self.assertEqual({c for c, s in STUDENT_STATUS.items() if s == 429}, {"rate_limited", "cooldown"})
        self.assertEqual({c for c, s in STUDENT_STATUS.items() if s == 413}, {"source_too_large", "file_too_large"})
        self.assertEqual({c for c, s in STUDENT_STATUS.items() if s == 403},
                         {"no_access", "standings_closed", "wrong_code", "student_required"})
        for code, status in INTAKE_CODES.items():
            self.assertEqual(STUDENT_STATUS[code], status)  # the intake's own statuses
        self.assertLessEqual(set(UNREACHABLE), set(STUDENT_STATUS))
        self.assertTrue(all(UNREACHABLE.values()))  # each with its reason


@unittest.skipUnless(HAVE_APP, "needs fastapi")
class PollTests(unittest.TestCase):
    def test_the_interval_grows_with_the_place_in_the_queue(self):
        q = JudgementStatus.queued
        self.assertIsNone(poll_after_s(None, None, 5))  # nothing judged: no polling
        for status in (JudgementStatus.compiling, JudgementStatus.running):
            self.assertEqual(poll_after_s(status, None, 5), 2)
        self.assertEqual([poll_after_s(q, p, 5) for p in (1, 5, 6, 10, 11, 16, 21, 1000)], [2, 2, 4, 4, 6, 8, 10, 10])
        self.assertEqual([poll_after_s(q, p, 1) for p in (1, 2, 3, 5, 6)], [2, 4, 6, 10, 10])
        self.assertEqual(poll_after_s(q, None, 5), 2)  # just claimed between the two reads

    def test_no_worker_online_is_ten_seconds_at_once(self):
        for place in (None, 1, 50):
            self.assertEqual(poll_after_s(JudgementStatus.queued, place, 0), 10)


@unittest.skipUnless(HAVE_APP, "needs fastapi")
class SubmissionPathTests(unittest.TestCase):
    def test_only_the_submission_path_itself_is_exempt(self):
        from app.student_api import UPLOAD_PATTERNS
        layer_patterns = list(UPLOAD_PATTERNS)
        self.assertEqual([(m, p.pattern) for m, p in layer_patterns],
                         [("POST", "/api/contests/[a-z0-9][a-z0-9-]{1,62}/problems/[A-Z][A-Z0-9]{0,7}/submissions"),
                          ("POST", "/api/feed/[1-9][0-9]{0,9}/draft/files")])
        from app.http_body import BodyLimitMiddleware
        layer = BodyLimitMiddleware(None, exempt_patterns=layer_patterns)
        good = "/api/contests/olymp-7/problems/A1/submissions"
        self.assertTrue(layer.is_exempt("POST", good))
        self.assertTrue(layer.is_exempt("POST", "/api/contests/" + "a" * 63 + "/problems/ABCDEFGH/submissions"))
        for method, path in [
            ("POST", good + "/"), ("POST", "/" + good), ("POST", good.replace("contests/", "contests//")),
            ("POST", good.replace("/problems", "//problems")), ("POST", good.replace("A1/", "A1//")),
            ("POST", "/api/contests/Olymp-7/problems/A1/submissions"), ("POST", "/api/contests/-x/problems/A/submissions"),
            ("POST", "/api/contests/x/problems/A/submissions"),  # a slug of one character cannot exist
            ("POST", "/api/contests/" + "a" * 64 + "/problems/A/submissions"),
            ("POST", "/api/contests/c1/problems/a/submissions"), ("POST", "/api/contests/c1/problems/1A/submissions"),
            ("POST", "/api/contests/c1/problems/ABCDEFGHI/submissions"),
            ("POST", "/api/contests/c1/problems/A/submissionsx"), ("POST", "/api/contests/c1/problems/A/submissions/1"),
            ("POST", "/api/contests/c1/problems/A%2Fsubmissions"), ("POST", "/api/contests/c1/enter"),
            ("POST", "/api/contests/c1/problems/A"), ("POST", "/API/contests/c1/problems/A/submissions"),
            ("PUT", good), ("PATCH", good), ("DELETE", good), ("GET", good),
        ]:
            with self.subTest(method=method, path=path):
                self.assertFalse(layer.is_exempt(method, path))

    def test_only_the_draft_upload_path_itself_is_exempt(self):
        from app.http_body import BodyLimitMiddleware
        from app.student_api import UPLOAD_PATTERNS
        layer = BodyLimitMiddleware(None, exempt_patterns=UPLOAD_PATTERNS)
        for path in ("/api/feed/1/draft/files", "/api/feed/42/draft/files", "/api/feed/9999999999/draft/files"):
            self.assertTrue(layer.is_exempt("POST", path), path)
        good = "/api/feed/42/draft/files"
        for method, path in [
            ("POST", good + "/"), ("POST", "/" + good), ("POST", good.replace("feed/", "feed//")),
            ("POST", good.replace("/draft", "//draft")), ("POST", good.replace("/files", "//files")),
            ("POST", "/api/feed/042/draft/files"), ("POST", "/api/feed/0/draft/files"),
            ("POST", "/api/feed/-1/draft/files"), ("POST", "/api/feed/+1/draft/files"),
            ("POST", "/api/feed/12345678901/draft/files"), ("POST", "/api/feed/x/draft/files"),
            ("POST", "/api/feed/\u0661/draft/files"), ("POST", "/api/feed/42/draft/files/1"),
            ("POST", "/api/feed/42/draft/files/1/delete"), ("POST", "/api/feed/42/draft/from-last"),
            ("POST", "/api/feed/42/draft"), ("POST", "/api/feed/42/work"), ("POST", "/api/feed/42/draft/filesx"),
            ("POST", "/api/feed/42%2Fdraft/files"), ("POST", "/API/feed/42/draft/files"),
            ("POST", "/api/admin/items/42/attachments"), ("PUT", good), ("PATCH", good), ("GET", good),
        ]:
            with self.subTest(method=method, path=path):
                self.assertFalse(layer.is_exempt(method, path))

    def test_the_public_app_exempts_it(self):
        from cryptography.fernet import Fernet

        from app.http_body import BodyLimitMiddleware
        from app.public_main import create_public_app
        from app.settings import AuthSettings, Settings
        app = create_public_app(Settings(public_origin="https://judge.example.ru",
                                         auth=AuthSettings(secret_key=Fernet.generate_key().decode())),
                                session_factory=lambda: None)
        found = [m for m in app.user_middleware if m.cls is BodyLimitMiddleware]
        self.assertEqual(len(found), 1)
        patterns = [p.pattern for _m, p in found[0].kwargs["exempt_patterns"]]
        self.assertIn("/api/contests/[a-z0-9][a-z0-9-]{1,62}/problems/[A-Z][A-Z0-9]{0,7}/submissions", patterns)
        self.assertIn("/api/feed/[1-9][0-9]{0,9}/draft/files", patterns)


if __name__ == "__main__":
    unittest.main()
