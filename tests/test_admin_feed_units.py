"""STATUS of the class-feed admin API (app/admin_api/feed.py) is complete: every refusal code the services of the
feed and its comments can produce - FeedError and ExtensionError raised with a literal code, the refusals of the
rules (Decision with False, _no("code") of comment_rules, the REFUSALS of grading.next_status, the codes
release_target returns, Refusal("code") of attachment_rules) - has an HTTP status, and STATUS names no code nobody raises (docs/ADMIN_API.md §5.8, §5.9).
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
SOURCES = ["backend/app/classroom/access.py", "backend/app/classroom/grading.py", "backend/app/classroom/items.py",
           "backend/app/classroom/works.py", "backend/app/deadline_rules.py", "backend/app/extensions.py",
           "backend/app/classroom/comment_rules.py", "backend/app/classroom/comments.py",
           "backend/app/classroom/attachment_rules.py", "backend/app/classroom/attachments.py"]
# the refusals of comment_rules, written as _no("<code>"): the scan must see them (it once saw only
# Decision(False, ...))
COMMENT_RULE_CODES = {"comments_disabled", "comments_teacher_only", "not_your_comment", "rate_limited",
                      "comment_invalid", "already_deleted"}
# the refusals of attachment_rules, written as Refusal("<code>"[, index]): the scan must see them (before 4v.3
# stage 5 it saw only Decision(False, ...), _no("...") and FeedError, and missed these)
REFUSAL_CODES = {"too_many_files", "too_many_attachments", "duplicate_name", "submission_too_large"}
# codes the endpoints raise themselves (ApiError) rather than the services
API_ONLY = {"work_not_found", "confirmation_mismatch", "due_at_invalid", "available_from_invalid", "user_not_found",
            "group_not_found", "contest_not_found", "material_not_found"}

try:
    from app.admin_api.feed import COMMENT_STUDENT_ONLY, HAND_IN_ONLY, NO_ROUTE_YET, RESULT_ONLY, STATUS
    from app.classroom import grading
    HAVE_APP = True
except ImportError:  # pragma: no cover - fastapi is not installed
    HAVE_APP = False


def _name(func) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def service_codes() -> set:
    """Literal refusal codes in the sources of the feed's services and rules."""
    codes = set()
    for rel in SOURCES:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in ("FeedError", "ExtensionError") \
                    and len(node.args) >= 2:
                last = node.args[-1]
                for branch in (last.body, last.orelse) if isinstance(last, ast.IfExp) else (last,):
                    if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                        codes.add(branch.value)
            elif isinstance(node, ast.Call) and _name(node.func) == "append" and node.args \
                    and isinstance(node.args[0], ast.Tuple) and node.args[0].elts:
                first = node.args[0].elts[0]  # items._validate: problems.append(("<code>", "<message>"))
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    codes.add(first.value)
            elif isinstance(node, ast.Call) and _name(node.func) == "Decision" and len(node.args) >= 2:
                first, second = node.args[0], node.args[1]
                if isinstance(first, ast.Constant) and first.value is False and isinstance(second, ast.Constant):
                    codes.add(second.value)
            elif isinstance(node, ast.Call) and _name(node.func) == "Refusal" and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    codes.add(node.args[0].value)  # attachment_rules: Refusal("<code>"[, index])
            elif isinstance(node, ast.Call) and _name(node.func) == "_no" and len(node.args) == 1:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    codes.add(node.args[0].value)  # comment_rules: _no("<code>") is Decision(False, "<code>")
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
                first, second = node.value.elts
                if isinstance(first, ast.Constant) and first.value is None and isinstance(second, ast.Constant) \
                        and isinstance(second.value, str):
                    codes.add(second.value)  # release_target: (None, "<code>")
    return codes


@unittest.skipUnless(HAVE_APP, "needs fastapi")
class FeedStatusTests(unittest.TestCase):
    def test_every_code_has_a_status_and_none_is_stale(self):
        codes = service_codes() | set(grading.REFUSALS.values()) | API_ONLY
        self.assertGreater(len(codes), 40)  # the scan really sees the services
        for new in ("work_not_found", "missing_needs_item", "score_invalid", "thread_not_found", "reason_too_long",
                    "file_empty", "link_title_invalid", "attachment_order_invalid", "attachment_not_found",
                    "file_not_found"):
            self.assertIn(new, codes)
        self.assertLessEqual(COMMENT_RULE_CODES, service_codes())
        self.assertLessEqual(REFUSAL_CODES, service_codes())
        self.assertEqual(sorted(codes - set(STATUS)), [], "codes without an HTTP status")
        self.assertEqual(sorted(set(STATUS) - codes), [], "STATUS names codes nobody raises")

    def test_statuses_follow_the_convention(self):
        for code, status in STATUS.items():
            with self.subTest(code=code):
                self.assertIn(status, (403, 404, 409, 422, 429))
                if status == 429:
                    self.assertEqual(code, "rate_limited")
                if code.endswith("_not_found"):
                    self.assertEqual(status, 404)
                if code.endswith("_invalid"):
                    self.assertEqual(status, 422)
        self.assertLessEqual(HAND_IN_ONLY | RESULT_ONLY | COMMENT_STUDENT_ONLY | NO_ROUTE_YET, set(STATUS))
        self.assertEqual(HAND_IN_ONLY & RESULT_ONLY, set())
        self.assertEqual(COMMENT_STUDENT_ONLY & (HAND_IN_ONLY | RESULT_ONLY), set())
        self.assertEqual(NO_ROUTE_YET, set(), "every service code has its admin route since 4v.3 stage 5")


if __name__ == "__main__":
    unittest.main()
