import unittest

from app.spam_checker import PARSER_VERSION, parse_response, score_change


class SpamParserTest(unittest.TestCase):
    def assert_summary(self, parsed, *, spam=0, clear=0, missing=0, extra=0, errors=0):
        self.assertEqual(
            {"spam": spam, "clear": clear, "missing": missing, "extra": extra, "errors": errors},
            {key: parsed["summary"][key] for key in ("spam", "clear", "missing", "extra", "errors")},
        )

    def test_parser_version_is_two(self):
        self.assertEqual("2", PARSER_VERSION)

    def test_headers_are_optional_and_do_not_control_results(self):
        expected = ["3939393930", "3939393938"]
        with_headers = parse_response("spam\n3939393930 - hiya\nclear\n3939393938", expected)
        without_headers = parse_response("3939393930 - hiya\n3939393938", expected)
        for parsed in (with_headers, without_headers):
            self.assertEqual([], parsed["issues"])
            self.assert_summary(parsed, spam=1, clear=1)
            self.assertEqual(
                [(row.number, row.verdict, row.sources) for row in parsed["rows"]],
                [("3939393930", "spam", ["hiya"]), ("3939393938", "clear", [])],
            )

        header_does_not_apply = parse_response("spam\n3939393930", ["3939393930"])
        self.assertEqual("clear", header_does_not_apply["rows"][0].verdict)

    def test_hiya_is_case_insensitive_standalone_word_without_required_dash(self):
        for comment in ("hiya", "Hiya", "HIYA", "source: hIyA result"):
            with self.subTest(comment=comment):
                parsed = parse_response(f"3939393930 {comment}", ["3939393930"])
                row = parsed["rows"][0]
                self.assertEqual(("spam", ["hiya"]), (row.verdict, row.sources))
                self.assertEqual((1, 1, "soft"), score_change(0, row.verdict, row.sources))

        parsed = parse_response("3939393930 nothiyaexample", ["3939393930"])
        row = parsed["rows"][0]
        self.assertEqual(["nothiyaexample"], row.sources)
        self.assertEqual((2, 2, "strong"), score_change(0, row.verdict, row.sources))

    def test_whitespace_and_separator_only_are_clear(self):
        for raw in ("   3939393938       ", "3939393938 -", "3939393938 :", "3939393938 —", "3939393938 ;   "):
            with self.subTest(raw=raw):
                parsed = parse_response(raw, ["3939393938"])
                self.assert_summary(parsed, clear=1)
                self.assertEqual("clear", parsed["rows"][0].verdict)

    def test_all_supported_separators_and_whitespace_produce_same_spam_result(self):
        for suffix in (" - callfilter", " callfilter", "     callfilter", ": callfilter", " — callfilter", "; callfilter", "| callfilter"):
            with self.subTest(suffix=suffix):
                parsed = parse_response("3939393930" + suffix, ["3939393930"])
                self.assertEqual(("spam", ["callfilter"]), (parsed["rows"][0].verdict, parsed["rows"][0].sources))

    def test_unknown_source_is_a_strong_spam_result(self):
        parsed = parse_response("3939393930 brand-new-source", ["3939393930"])
        row = parsed["rows"][0]
        self.assertEqual(["brand-new-source"], row.sources)
        self.assertEqual((2, 2, "strong"), score_change(0, row.verdict, row.sources))

    def test_telegram_prefix_and_non_phone_noise_are_ignored(self):
        raw = """[15.06.2026 11:11] DG_spam_bot: Проверка началась
[15.06.2026 11:13] DG_spam_bot: spam
[15.06.2026 11:13] DG_spam_bot: 3939393930 - hiya
какой-то служебный текст
clear
3939393938
Проверка завершена"""
        parsed = parse_response(raw, ["3939393930", "3939393938"])
        self.assertEqual([], parsed["issues"])
        self.assert_summary(parsed, spam=1, clear=1)

    def test_missing_and_extra_are_reconciled_against_expected_numbers(self):
        missing = parse_response("1111111111 - hiya", ["1111111111", "2222222222"])
        self.assert_summary(missing, spam=1, missing=1)
        self.assertEqual("missing", missing["rows"][1].status)

        extra = parse_response("1111111111\n2222222222 - hiya", ["1111111111"])
        self.assert_summary(extra, clear=1, extra=1)
        self.assertEqual(("2222222222", "extra"), (extra["rows"][1].number, extra["rows"][1].status))

    def test_duplicates_do_not_duplicate_sources_or_score_changes(self):
        clear = parse_response("1111111111\n1111111111", ["1111111111"])["rows"][0]
        self.assertTrue(clear.duplicate)
        self.assertEqual(("clear", []), (clear.verdict, clear.sources))

        spam = parse_response("1111111111 - hiya\n1111111111 - hiya", ["1111111111"])["rows"][0]
        self.assertTrue(spam.duplicate)
        self.assertEqual(["hiya"], spam.sources)
        self.assertEqual((1, 1, "soft"), score_change(0, spam.verdict, spam.sources))

    def test_clear_and_spam_conflict_blocks_result(self):
        parsed = parse_response("1111111111\n1111111111 - hiya", ["1111111111"])
        self.assert_summary(parsed, errors=1)
        self.assertEqual(("conflict", "error"), (parsed["rows"][0].verdict, parsed["rows"][0].status))

    def test_multiple_spam_sources_use_strongest_single_change(self):
        parsed = parse_response("1111111111 - hiya\n1111111111 - callfilter", ["1111111111"])
        row = parsed["rows"][0]
        self.assertEqual(["hiya", "callfilter"], row.sources)
        self.assertEqual("callfilter", row.source)
        self.assertEqual((2, 3, "strong"), score_change(1, row.verdict, row.sources))


class SpamScoreTest(unittest.TestCase):
    def test_score_boundaries(self):
        cases = [
            (0, "spam", ["hiya"], 1), (4, "spam", ["hiya"], 5), (5, "spam", ["hiya"], 5),
            (0, "clear", [], 0), (4, "clear", [], 3), (3, "spam", ["callfilter"], 5),
            (4, "spam", ["new"], 5),
        ]
        for before, verdict, sources, after in cases:
            self.assertEqual(score_change(before, verdict, sources)[1], after)


if __name__ == "__main__":
    unittest.main()
