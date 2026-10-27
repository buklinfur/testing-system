"""CSV parsing of `judgectl user import` (no database): encodings, delimiters, header, errors with line numbers."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.judgectl.csvimport import CsvImportError, Row, parse  # noqa: E402

CLASS = "Фамилия;Имя;Логин;Имя в рейтинге\r\nИванов;Пётр;;\r\nЩукина;Яна;yana.sh;Яна Щ. (7Б)\r\n\r\nПодъячев;Чеслав\r\n"


class CsvImportTests(unittest.TestCase):
    def test_encodings_give_the_same_rows(self):
        want = [Row(2, "Иванов", "Пётр"), Row(3, "Щукина", "Яна", "yana.sh", "Яна Щ. (7Б)"), Row(5, "Подъячев", "Чеслав")]
        for encoding, data in (("utf-8", CLASS.encode()), ("utf-8-sig", CLASS.encode("utf-8-sig")),
                               ("cp1251", CLASS.encode("cp1251"))):
            with self.subTest(encoding=encoding):
                parsed = parse(data)
                self.assertEqual((parsed.encoding, parsed.delimiter, parsed.rows), (encoding, ";", want))

    def test_comma_delimiter_quotes_and_no_header(self):
        data = 'Иванов,Пётр\n"Петров","Иван","Petrov.I","Иван П., 7Б"\nСидоров,Олег,,\n'.encode("cp1251")
        parsed = parse(data)
        self.assertEqual(parsed.delimiter, ",")
        self.assertEqual(parsed.rows, [Row(1, "Иванов", "Пётр"), Row(2, "Петров", "Иван", "petrov.i", "Иван П., 7Б"),
                                       Row(3, "Сидоров", "Олег")])

    def test_all_problems_are_reported_with_line_numbers(self):
        data = ("фамилия;имя\n"
                "Иванов\n"  # 2: one field
                "Петров;Иван;ab\n"  # 3: login too short
                "Сидоров;Олег;oleg.s\n"
                "Смирнов;Олег;OLEG.S\n"  # 5: the same login as line 4
                ";Анна\n"  # 6: no surname
                "Ъ;Анна\n"  # 7: no login can be made from the surname
                "a;b;c;d;e\n"  # 8: five fields
                "Кузнецова;Анна;;" + "Я" * 129 + "\n").encode()  # 9: display name too long
        with self.assertRaises(CsvImportError) as ctx:
            parse(data)
        lines = [p.split(":")[0] for p in ctx.exception.problems]
        self.assertEqual(lines, ["line 2", "line 3", "line 5", "line 6", "line 7", "line 8", "line 9"])
        self.assertIn("line 4", ctx.exception.problems[2])
        self.assertIn("nothing was imported", str(ctx.exception))

    def test_empty_and_undecodable_files(self):
        for data in (b"", b"\n\n", "фамилия;имя\n".encode()):
            with self.subTest(data=data), self.assertRaises(CsvImportError):
                parse(data)
        with self.assertRaises(CsvImportError) as ctx:
            parse(b"\x98\x98;\x98\n")  # 0x98 is undefined in cp1251 and invalid UTF-8
        self.assertIn("neither UTF-8 nor cp1251", str(ctx.exception))

    def test_the_class_file_of_the_manual_check(self):
        """deploy/dev/students-30-cp1251.csv: what the final check of session 4a.1 imports as `judge`."""
        data = (ROOT / "deploy" / "dev" / "students-30-cp1251.csv").read_bytes()
        with self.assertRaises(UnicodeDecodeError):
            data.decode("utf-8")  # really cp1251, as Excel on Windows saves it
        parsed = parse(data)
        self.assertEqual((len(parsed.rows), parsed.encoding, parsed.delimiter), (30, "cp1251", ";"))
        self.assertEqual(parsed.rows[3], Row(5, "Подъячев", "Чеслав"))


if __name__ == "__main__":
    unittest.main()
