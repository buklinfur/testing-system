"""Pure-logic tests: no sandbox needed. Run: python -m unittest discover -s tests"""
import io
import sys
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner.archive import SubmissionError, extract_submission  # noqa: E402
from runner.checkers import check_exact, check_float, check_tokens  # noqa: E402
from runner.common import Limits, Verdict  # noqa: E402
from runner.config import RunnerConfig  # noqa: E402
from runner.explain import explain_runtime_error, sanitize_compile_log  # noqa: E402
from runner.languages import get_profile  # noqa: E402
from runner.sandbox.isolate import classify_isolate, parse_meta  # noqa: E402
from runner.scoring import first_fail_verdict, group_score, submission_verdict  # noqa: E402

V = Verdict
D = Decimal


class ScoringTests(unittest.TestCase):
    def test_sum_gives_a_share_per_passed_test(self):
        self.assertEqual(group_score("sum", D(40), [V.OK, V.OK, V.WA, V.OK]), D("30.00"))

    def test_sum_rounds_to_cents(self):
        self.assertEqual(group_score("sum", D(100), [V.OK, V.WA, V.WA]), D("33.33"))

    def test_all_or_nothing(self):
        self.assertEqual(group_score("all_or_nothing", D(60), [V.OK, V.OK]), D("60.00"))
        self.assertEqual(group_score("all_or_nothing", D(60), [V.OK, V.TL]), D(0))

    def test_skipped_is_not_passed(self):
        self.assertEqual(group_score("all_or_nothing", D(60), [V.OK, V.SKIPPED]), D(0))
        self.assertEqual(group_score("sum", D(10), [V.OK, V.SKIPPED]), D("5.00"))

    def test_min_behaves_as_all_or_nothing(self):
        self.assertEqual(group_score("min", D(10), [V.OK, V.WA]), D(0))

    def test_empty_group(self):
        self.assertEqual(group_score("sum", D(10), []), D(0))

    def test_submission_verdict(self):
        self.assertEqual(submission_verdict([V.OK, V.OK], D(100)), V.OK)
        self.assertEqual(submission_verdict([V.OK, V.WA], D(40)), V.PARTIAL)
        self.assertEqual(submission_verdict([V.TL, V.WA], D(0)), V.TL)
        self.assertEqual(submission_verdict([V.OK, V.SKIPPED], D(0)), V.OK)

    def test_first_fail_ignores_skipped(self):
        self.assertEqual(first_fail_verdict([V.OK, V.SKIPPED, V.RE, V.WA]), V.RE)
        self.assertEqual(first_fail_verdict([V.OK]), V.OK)


class CheckerTests(unittest.TestCase):
    def test_exact_ignores_trailing_whitespace_and_blank_lines(self):
        self.assertEqual(check_exact(b"1 2  \n3\n\n\n", b"1 2\n3").verdict, V.OK)

    def test_exact_is_sensitive_to_inner_spacing(self):
        self.assertEqual(check_exact(b"1  2\n", b"1 2\n").verdict, V.WA)

    def test_exact_line_count(self):
        self.assertEqual(check_exact(b"1\n2\n", b"1\n").verdict, V.WA)

    def test_tokens_ignore_layout(self):
        self.assertEqual(check_tokens(b"1\n2   3\n", b"1 2 3").verdict, V.OK)
        self.assertEqual(check_tokens(b"1 2", b"1 2 3").verdict, V.WA)
        self.assertEqual(check_tokens(b"", b"5").verdict, V.WA)

    def test_float(self):
        self.assertEqual(check_float(b"3.1415926", b"3.14159265", 1e-6).verdict, V.OK)
        self.assertEqual(check_float(b"3.1", b"3.14159265", 1e-6).verdict, V.WA)
        self.assertEqual(check_float(b"1000000.5", b"1000000.0", 1e-6).verdict, V.OK)  # relative error 5e-7
        self.assertEqual(check_float(b"1000002.0", b"1000000.0", 1e-6).verdict, V.WA)  # relative error 2e-6
        self.assertEqual(check_float(b"nan", b"1", 1.0).verdict, V.WA)
        self.assertEqual(check_float(b"abc", b"abc", 1e-6).verdict, V.OK)

    def test_checker_messages_do_not_contain_expected_values(self):
        for result in (check_exact(b"41\n", b"42\n"), check_tokens(b"41", b"42"), check_float(b"41", b"42", 1e-9)):
            self.assertNotIn("42", result.message)


class IsolateMetaTests(unittest.TestCase):
    LIM = Limits(time_ms=1000, wall_ms=3000, memory_mb=256)

    def classify(self, text, overflow=False):
        return classify_isolate(parse_meta(text), self.LIM, overflow)

    def test_ok(self):
        r = self.classify("time:0.012\ntime-wall:0.020\ncg-mem:3200\nexitcode:0\n")
        self.assertEqual((r.verdict, r.time_ms, r.memory_kb), (V.OK, 12, 3200))

    def test_timeout(self):
        r = self.classify("status:TO\nmessage:Time limit exceeded\ntime:1.2\ntime-wall:1.3\n")
        self.assertEqual(r.verdict, V.TL)

    def test_runtime_error_exit_code(self):
        r = self.classify("status:RE\nexitcode:3\nmessage:Exited with error status 3\ntime:0.01\n")
        self.assertEqual((r.verdict, r.exit_code), (V.RE, 3))

    def test_oom_kill_is_memory_limit(self):
        r = self.classify("status:SG\nexitsig:9\ncg-oom-killed:1\ncg-mem:262144\n")
        self.assertEqual(r.verdict, V.ML)

    def test_segfault_is_runtime_error(self):
        r = self.classify("status:SG\nexitsig:11\ncg-mem:2048\n")
        self.assertEqual((r.verdict, r.signal), (V.RE, 11))

    def test_segfault_near_memory_limit_is_memory_limit(self):
        r = self.classify("status:SG\nexitsig:11\ncg-mem:260000\n")
        self.assertEqual(r.verdict, V.ML)

    def test_file_size_signal_is_output_limit(self):
        r = self.classify("status:SG\nexitsig:25\n")
        self.assertEqual(r.verdict, V.OL)

    def test_stdout_overflow_is_output_limit(self):
        self.assertEqual(self.classify("status:SG\nexitsig:13\n", overflow=True).verdict, V.OL)

    def test_internal_error(self):
        self.assertEqual(self.classify("status:XX\nmessage:boom\n").verdict, V.IE)

    def test_finished_over_the_limit_is_tl(self):
        self.assertEqual(self.classify("time:1.5\nexitcode:0\n").verdict, V.TL)


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            if isinstance(data, zipfile.ZipInfo):
                z.writestr(data, "target")
            else:
                z.writestr(name, data)
    return buf.getvalue()


class ArchiveTests(unittest.TestCase):
    cfg = RunnerConfig()
    cpp = get_profile("cpp")

    def test_single_file_gets_default_name(self):
        self.assertEqual(extract_submission(b"code", False, self.cpp, self.cfg), {"main.cpp": b"code"})

    def test_multi_file_archive(self):
        files = extract_submission(make_zip({"a.cpp": b"1", "util/b.h": b"2"}), True, self.cpp, self.cfg)
        self.assertEqual(files, {"a.cpp": b"1", "util/b.h": b"2"})

    def test_single_top_level_folder_is_stripped(self):
        files = extract_submission(make_zip({"proj/a.cpp": b"1", "proj/b.cpp": b"2"}), True, self.cpp, self.cfg)
        self.assertEqual(set(files), {"a.cpp", "b.cpp"})

    def test_zip_slip_rejected(self):
        for bad in ("../evil.cpp", "/etc/passwd", "a/../../x.cpp"):
            with self.assertRaises(SubmissionError):
                extract_submission(make_zip({bad: b"x"}), True, self.cpp, self.cfg)

    def test_symlink_rejected(self):
        info = zipfile.ZipInfo("link.cpp")
        info.external_attr = (0o120777) << 16
        with self.assertRaises(SubmissionError):
            extract_submission(make_zip({"link.cpp": info}), True, self.cpp, self.cfg)

    def test_zip_bomb_rejected(self):
        bomb = make_zip({"big.cpp": b"0" * (20 * 1024 * 1024)})
        self.assertLess(len(bomb), self.cfg.max_archive_bytes)
        with self.assertRaises(SubmissionError):
            extract_submission(bomb, True, self.cpp, self.cfg)

    def test_too_many_files(self):
        entries = {f"f{i}.cpp": b"x" for i in range(self.cfg.max_files + 1)}
        with self.assertRaises(SubmissionError):
            extract_submission(make_zip(entries), True, self.cpp, self.cfg)

    def test_not_a_zip(self):
        with self.assertRaises(SubmissionError):
            extract_submission(b"not a zip", True, self.cpp, self.cfg)


class ExplainTests(unittest.TestCase):
    def test_compile_log_hides_server_paths_and_is_truncated(self):
        log = "/box/main.cpp:3:5: error: expected ';'\n" + "x" * 10000
        cleaned = sanitize_compile_log(log)
        self.assertTrue(cleaned.startswith("main.cpp:3:5: error"))
        self.assertNotIn("/box/", cleaned)
        self.assertLess(len(cleaned), 4100)

    def test_python_traceback_is_summarised(self):
        stderr = 'Traceback (most recent call last):\n  File "main.py", line 7, in <module>\n    print(1/0)\nZeroDivisionError: division by zero\n'
        self.assertEqual(explain_runtime_error("python", stderr, 1, None), "строка 7: ZeroDivisionError: division by zero")

    def test_signal_explanations(self):
        self.assertIn("сегментации", explain_runtime_error("cpp", "", None, 11))
        self.assertIn("делени", explain_runtime_error("cpp", "", None, 8))

    def test_exit_code(self):
        self.assertIn("кодом 3", explain_runtime_error("cpp", "", 3, None))


if __name__ == "__main__":
    unittest.main()
