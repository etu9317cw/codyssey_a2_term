"""analyze: 항목 선택, 조건별 기사 선택, 응답 검사(없는 근거 ID 제거), 저장·조회, 실패 기록 (메모리 DB, 가짜 AI)."""
import json
import unittest

from news_cli import db
from news_cli.ai import analyzer
from news_cli.ai.client import AIClient
from tests.support import AI_VALUES, FakeAIResponse, FakeAISession, FakeClock, add_clean_news, ai_reply, config

RESULT = {
    "trends": [{"title": "송전망 확충", "description": "HVDC 사업이 이어진다.", "article_ids": [1, 99, "3"]}],
    "keywords": [{"keyword": "HVDC", "reason": "여러 기사에 나옴"}, "SMP"],
    "common_diff": {"common": ["전력망 투자"], "differences": ["태양광은 지역 주민 참여"]},
    "implications": ["계통 투자 속도가 관건이다."],
}


class ParseItemsTest(unittest.TestCase):
    def test_two_or_more(self):
        self.assertEqual(analyzer.parse_items("trends, keywords,trends"), ["trends", "keywords"])

    def test_one_item_is_error(self):
        with self.assertRaises(ValueError):
            analyzer.parse_items("trends")

    def test_unknown_item_is_error(self):
        with self.assertRaises(ValueError):
            analyzer.parse_items("trends,sentiment")


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()
        add_clean_news(self.conn, "1", "HVDC 케이블 입찰", category="전력", published_at="2026-10-08 09:00:00")
        add_clean_news(self.conn, "2", "수상태양광 추진", category="태양광", published_at="2026-10-01 09:00:00")
        add_clean_news(self.conn, "3", "SMP 하락", category="전력", published_at="2026-10-09 09:00:00",
                       status="summarized")
        add_clean_news(self.conn, "4", "본문 없는 기사", body="", category="전력", status="no_body")
        for summary in ("옛 요약", "SMP가 56원 떨어졌다."):
            self.conn.execute("INSERT INTO summaries (news_id, summary, keywords, model, input_chars, output_chars, "
                              "created_at) VALUES (3, ?, '[]', 'm', 100, 10, '2026-10-10 10:00:00')", (summary,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def client(self, replies):
        self.session = FakeAISession(replies)
        return AIClient(self.config["ai"], AI_VALUES, session=self.session, sleep=FakeClock().sleep)

    def test_select_by_date_and_category(self):
        articles, total = analyzer.select_articles(self.conn)
        self.assertEqual(([a["id"] for a in articles], total), ([3, 1, 2], 3))  # 최신순, no_body 제외
        articles, _ = analyzer.select_articles(self.conn, date_from="2026-10-02", date_to="2026-10-08")
        self.assertEqual([a["id"] for a in articles], [1])
        articles, _ = analyzer.select_articles(self.conn, categories=["태양광"])
        self.assertEqual([a["id"] for a in articles], [2])

    def test_select_uses_latest_summary_and_max_articles(self):
        articles, total = analyzer.select_articles(self.conn, max_articles=1)
        self.assertEqual((len(articles), total), (1, 3))
        self.assertEqual(articles[0]["summary"], "SMP가 56원 떨어졌다.")

    def test_analysis_saved_and_fake_ids_removed(self):
        analysis = analyzer.run_analyze(self.conn, self.config, self.client([ai_reply(json.dumps(RESULT))]))
        self.assertEqual(analysis["result"]["trends"][0]["article_ids"], [1, 3])
        self.assertEqual(analysis["result"]["keywords"][1], {"keyword": "SMP", "reason": ""})
        self.assertEqual((analysis["article_count"], analysis["article_ids"]), (3, [1, 2, 3]))
        self.assertEqual(analysis["items"], list(analyzer.ITEM_NAMES))
        self.assertEqual((analysis["category"], analysis["date_from"]), (None, None))
        prompt = self.session.user_prompt(0)
        self.assertIn("[3] 2026-10-09 | 전력 | SMP 하락\n    SMP가 56원 떨어졌다.", prompt)
        self.assertNotIn("본문 없는 기사", prompt)

    def test_only_requested_items_are_asked_and_kept(self):
        analysis = analyzer.run_analyze(self.conn, self.config, self.client([ai_reply(json.dumps(RESULT))]),
                                        categories=["전력"], items=["keywords", "implications"])
        self.assertEqual(set(analysis["result"]), {"keywords", "implications"})
        self.assertEqual((analysis["category"], analysis["article_ids"]), ("전력", [1, 3]))
        prompt = self.session.user_prompt(0)
        self.assertIn('"implications"', prompt)
        self.assertNotIn('"trends"', prompt)

    def test_missing_item_asks_again(self):
        partial = {key: value for key, value in RESULT.items() if key != "implications"}
        replies = [ai_reply(json.dumps(partial)), ai_reply(json.dumps(RESULT))]
        analysis = analyzer.run_analyze(self.conn, self.config, self.client(replies))
        self.assertIsNotNone(analysis)
        self.assertEqual(len(self.session.calls), 2)

    def test_failure_recorded_in_ai_errors(self):
        result = analyzer.run_analyze(self.conn, self.config, self.client([FakeAIResponse(503)] * 3),
                                      date_from="2026-10-01", categories=["전력", "태양광"])
        self.assertIsNone(result)
        error = self.conn.execute("SELECT * FROM ai_errors").fetchone()
        self.assertEqual((error["task"], error["attempts"]), ("analyze", 3))
        self.assertEqual(error["target"], "기간 2026-10-01 ~ 최근 / 카테고리 전력, 태양광")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 0)

    def test_no_articles_skips_ai_call(self):
        client = self.client([])
        self.assertIsNone(analyzer.run_analyze(self.conn, self.config, client, categories=["수소"]))
        self.assertEqual(client.calls, 0)

    def test_history_and_show(self):
        analyzer.run_analyze(self.conn, self.config, self.client([ai_reply(json.dumps(RESULT))]))
        history = analyzer.format_history(analyzer.list_analyses(self.conn))
        self.assertEqual(len(history), 2)
        self.assertTrue(history[1].startswith("1 | "))
        text = "\n".join(analyzer.format_analysis(analyzer.get_analysis(self.conn, 1)))
        for label in ("[주요 트렌드]", "[핵심 키워드]", "[공통점·차이점]", "[시사점]", "근거 기사 ID: 1, 3"):
            self.assertIn(label, text)
        self.assertIsNone(analyzer.get_analysis(self.conn, 99))


if __name__ == "__main__":
    unittest.main()
