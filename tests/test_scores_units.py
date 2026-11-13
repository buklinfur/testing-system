"""app.scores: the one function that turns the score of a problem version into points of a contest (D-090)."""
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app import scores  # noqa: E402
from app.models import ProblemType  # noqa: E402


class ContestScoreTests(unittest.TestCase):
    def test_without_points_of_the_contest_the_score_is_kept(self):
        self.assertEqual(scores.contest_score(D("42.5"), D(60), None), D("42.50"))
        self.assertEqual(scores.contest_score(D(7), None, None), D("7.00"))
        self.assertEqual(scores.contest_max(D(60), None), D("60.00"))
        self.assertIsNone(scores.contest_max(None, None))

    def test_points_of_the_contest_scale_the_score(self):
        self.assertEqual(scores.contest_score(D(60), D(60), D(100)), D("100.00"))
        self.assertEqual(scores.contest_score(D(30), D(60), D(100)), D("50.00"))
        self.assertEqual(scores.contest_score(D(1), D(3), D(100)), D("33.33"))
        self.assertEqual(scores.contest_score(D(2), D(3), D(100)), D("66.67"))  # half up
        self.assertEqual(scores.contest_score(D(1), D(8), D("0.1")), D("0.01"))  # 0.0125
        self.assertEqual(scores.contest_score(D(0), D(60), D(100)), D("0.00"))
        self.assertEqual(scores.contest_max(D(60), D(100)), D("100.00"))
        self.assertEqual(scores.contest_max(None, D(100)), D("100.00"))

    def test_no_maximum_gives_nothing_and_never_divides_by_zero(self):
        for maximum in (None, D(0), D("0.00")):
            with self.subTest(maximum=maximum):
                self.assertEqual(scores.contest_score(D(5), maximum, D(100)), D("0.00"))
        self.assertEqual(scores.contest_score(None, None, D(100)), D("0.00"))

    def test_shown(self):
        self.assertEqual(scores.shown(D(30), D(60), D(100)),
                         {"max_score": D(60), "contest_score": D("50.00"), "contest_max": D("100.00")})

    def test_quiz_max_counts_like_the_runner(self):
        from runner import quiz

        class P:
            questions = [{"id": "a", "correct": ["x"], "points": 2.5}, {"id": "b", "correct": ["y"]}]
            answers = [{"id": "c", "accept": ["1"]}, {"id": "d", "accept": ["2"], "points": 3}]

        self.assertEqual(scores.quiz_max(ProblemType.choice, {"questions": P.questions}),
                         D(str(quiz.grade_choice(P, {})["max_score"])))
        self.assertEqual(scores.quiz_max(ProblemType.text_answer, {"answers": P.answers}),
                         D(str(quiz.grade_text(P, {})["max_score"])))
        self.assertEqual(scores.quiz_max(ProblemType.choice, {}), D("0.00"))


if __name__ == "__main__":
    unittest.main()
