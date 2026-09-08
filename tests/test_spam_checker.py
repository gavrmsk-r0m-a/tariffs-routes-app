import unittest
from app.spam_checker import parse_response, score_change

class SpamParserTest(unittest.TestCase):
    def test_full_response_with_spam_and_clear_sections_is_valid(self):
        parsed = parse_response("spam\n1111111111 - hiya\n\nclear\n2222222222", ["1111111111", "2222222222"])
        self.assertEqual([], parsed["issues"])

    def test_missing_sections_are_not_inferred(self):
        parsed = parse_response("1111111111 - hiya\n2222222222", ["1111111111", "2222222222"])
        self.assertIn("Не удалось определить блоки spam / clear", parsed["issues"][0])
        self.assertEqual(2, parsed["summary"]["missing"])
        self.assertEqual(0, parsed["summary"]["clear"])

    def test_real_telegram_and_service_prefix(self):
        parsed=parse_response('[15.06.2026 11:11] DG_spam_bot: Ваш запрос обрабатывается...\n[15.06.2026 11:13] DG_spam_bot: spam\n390250030709 - hiya\n390250030707 - callfilter\n\nclear\n390250020820', ['390250030709','390250030707','390250020820'])
        self.assertEqual(parsed['summary']['spam'],2); self.assertEqual(parsed['summary']['clear'],1)
    def test_missing_extra_duplicate_and_blank_lines(self):
        p=parse_response('spam\n1111111 - HIYA\n1111111 - HIYA\n9999999 - abc\n\nclear\n2222222',['1111111','3333333'])
        self.assertTrue(p['rows'][0].duplicate); self.assertEqual(p['summary']['missing'],1); self.assertEqual(p['summary']['extra'],2)
    def test_conflict(self):
        p=parse_response('spam\n1111111 - hiya\nclear\n1111111',['1111111'])
        self.assertEqual(p['rows'][0].status,'error')
    def test_multiple_sources_use_strongest(self):
        p=parse_response('spam\n1111111 - hiya\n1111111 - truecaller',['1111111'])
        row=p['rows'][0]; self.assertEqual(row.source,'truecaller'); self.assertEqual(score_change(1,row.verdict,row.sources),(2,3,'strong'))

class SpamScoreTest(unittest.TestCase):
    def test_score_boundaries(self):
        cases=[(0,'spam',['hiya'],1),(4,'spam',['hiya'],5),(5,'spam',['hiya'],5),(0,'clear',[],0),(4,'clear',[],3),(3,'spam',['callfilter'],5),(4,'spam',['new'],5)]
        for before,v,sources,after in cases: self.assertEqual(score_change(before,v,sources)[1],after)
if __name__=='__main__': unittest.main()
