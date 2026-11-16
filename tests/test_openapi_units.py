"""docs/openapi-admin.json and docs/openapi-student.json are what the app describes now (tools/gen-openapi.py writes
them; the app does not serve them)."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tool():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "tools" / "gen-openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def paths(name: str) -> dict:
    return json.loads((ROOT / "docs" / name).read_text(encoding="utf-8"))["paths"]


class OpenApiFileTests(unittest.TestCase):
    def test_the_files_match_the_app(self):
        gen = tool()
        for target, (out, _title, _prefixes) in gen.TARGETS.items():
            with self.subTest(target=target):
                self.assertEqual(out.read_text(encoding="utf-8"), gen.render(gen.build(target)),
                                 f"{out.name} is out of date: run python tools/gen-openapi.py")
        self.assertEqual(gen.main(["--check"]), 0)

    def test_it_holds_the_admin_and_auth_api_only_and_no_operations_on_admins(self):
        found = paths("openapi-admin.json")
        self.assertTrue(all(p.startswith(("/api/admin/", "/api/auth/")) for p in found))
        self.assertIn("/api/admin/submissions/rejudge", found)
        self.assertIn("/api/auth/profile", found)
        forbidden = ("/admins", "2fa", "totp", "worker")  # R2: admins, 2FA resets and worker tokens are console-only
        self.assertEqual([p for p in found if p.startswith("/api/admin/") and any(w in p for w in forbidden)], [])

    def test_the_student_file_holds_the_student_and_auth_api_only(self):
        found = paths("openapi-student.json")
        self.assertTrue(all(p.startswith(("/api/contests", "/api/submissions", "/api/load", "/api/feed", "/api/works",
                                          "/api/auth/")) for p in found))
        for path in ("/api/contests", "/api/contests/{slug}/problems/{label}/submissions", "/api/submissions/{submission_id}",
                     "/api/load", "/api/auth/login", "/api/feed", "/api/feed/{item_id}/draft/files", "/api/works"):
            self.assertIn(path, found)
        draft = found["/api/feed/{item_id}/draft/files"]["post"]["requestBody"]["content"]
        self.assertEqual(sorted(draft), ["application/octet-stream"])
        upload = found["/api/contests/{slug}/problems/{label}/submissions"]["post"]["requestBody"]["content"]
        self.assertEqual(sorted(upload), ["application/json", "application/zip", "text/plain"])
        doc = json.loads((ROOT / "docs" / "openapi-student.json").read_text(encoding="utf-8"))
        self.assertNotIn("ItemFields", doc["components"]["schemas"])  # no admin body in the student file


if __name__ == "__main__":
    unittest.main()
