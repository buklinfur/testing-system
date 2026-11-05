"""docs/openapi-admin.json is what the app describes now (tools/gen-openapi.py writes it; the app does not serve it)."""
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


class OpenApiFileTests(unittest.TestCase):
    def test_the_file_matches_the_app(self):
        gen = tool()
        current = (ROOT / "docs" / "openapi-admin.json").read_text(encoding="utf-8")
        self.assertEqual(current, gen.render(gen.build()), "docs/openapi-admin.json is out of date: run "
                                                            "python tools/gen-openapi.py")

    def test_it_holds_the_admin_and_auth_api_only_and_no_operations_on_admins(self):
        paths = json.loads((ROOT / "docs" / "openapi-admin.json").read_text(encoding="utf-8"))["paths"]
        self.assertTrue(all(p.startswith(("/api/admin/", "/api/auth/")) for p in paths))
        self.assertIn("/api/admin/submissions/rejudge", paths)
        self.assertIn("/api/auth/profile", paths)
        forbidden = ("/admins", "2fa", "totp", "worker")  # R2: admins, 2FA resets and worker tokens are console-only
        self.assertEqual([p for p in paths if p.startswith("/api/admin/") and any(w in p for w in forbidden)], [])


if __name__ == "__main__":
    unittest.main()
