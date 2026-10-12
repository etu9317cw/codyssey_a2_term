"""summarize: 대상 선택(기본 스킵·--force·no_body 제외), 저장, 실패 기록 후 계속, 본문 길이 제한 (메모리 DB, 가짜 AI)."""
import json
import os
import unittest
from unittest import mock

from news_cli import cli, db
from news_cli.ai.client import AIAuthError, AIClient
from news_cli.ai.summarizer import run_summarize, select_targets, validate_summary
from tests.support import AI_VALUES, FakeAIResponse, FakeAISession, FakeClock, add_clean_news, ai_reply, config

SUMMARY = '{"summary": "한전이 송전망 투자를 늘린다.", "keywords": ["한전", "송전망"]}'


class ValidateSummaryTest(unittest.TestCase):
    def test_cleans_keywords(self):
        data = validate_summary({"summary": " 첫 문장.\n 둘째 문장. ",
                                 "keywords": ["#SMP", "LNG", "SMP", " 전력  시장 ", "", "a", "b", "c"]})
        self.assertEqual(data["summary"], "첫 문장. 둘째 문장.")
        self.assertEqual(data["keywords"], ["SMP", "LNG", "전력 시장", "a", "b"])

    def test_keywords_as_text(self):
        self.assertEqual(validate_summary({"summary": "요약", "keywords": "원전, SMR"})["keywords"], ["원전", "SMR"])

    def test_empty_summary_is_error(self):
        for data in ({"keywords": []}, {"summary": "  "}, {"summary": 3}):
            with self.assertRaises(ValueError):
                validate_summary(data)

    def test_keywords_wrong_type_is_error(self):
        with self.assertRaises(ValueError):
            validate_summary({"summary": "요약", "keywords": {"a": 1}})


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()
        self.ids = {
            "cleaned": add_clean_news(self.conn, "1", "요약 전 기사"),
            "failed": add_clean_news(self.conn, "2", "요약 실패했던 기사", status="summary_failed"),
            "done": add_clean_news(self.conn, "3", "요약된 기사", status="summarized"),
            "no_body": add_clean_news(self.conn, "4", "본문 없는 기사", body="", status="no_body"),
        }

    def tearDown(self):
        self.conn.close()

    def client(self, replies):
        self.session = FakeAISession(replies)
        self.clock = FakeClock()
        return AIClient(self.config["ai"], AI_VALUES, session=self.session, sleep=self.clock.sleep)

    def status(self, news_id):
        return self.conn.execute("SELECT status FROM clean_news WHERE id = ?", (news_id,)).fetchone()[0]

    def test_default_targets_are_unsummarized_and_failed(self):
        targets, skipped = select_targets(self.conn)
        self.assertEqual([r["id"] for r in targets], [self.ids["cleaned"], self.ids["failed"]])
        self.assertEqual(sum(skipped.values()), 0)

    def test_all_skips_already_summarized_unless_force(self):
        targets, skipped = select_targets(self.conn, "all")
        self.assertNotIn(self.ids["done"], [r["id"] for r in targets])
        self.assertEqual((skipped["already"], skipped["no_body"]), (1, 1))
        targets, _ = select_targets(self.conn, "all", force=True)
        self.assertEqual(len(targets), 3)

    def test_ids_mode_reports_missing_and_no_body(self):
        targets, skipped = select_targets(self.conn, "ids", ids=[self.ids["no_body"], 99, self.ids["done"],
                                                                 self.ids["cleaned"]])
        self.assertEqual([r["id"] for r in targets], [self.ids["cleaned"]])
        self.assertEqual((skipped["not_found"], skipped["no_body"], skipped["already"]), (1, 1, 1))

    def test_limit(self):
        targets, skipped = select_targets(self.conn, limit=1)
        self.assertEqual(len(targets), 1)
        self.assertEqual(skipped["over_limit"], 1)

    def test_summary_saved_and_status_changed(self):
        stats = run_summarize(self.conn, self.config, self.client([ai_reply(SUMMARY)]), "ids", [self.ids["cleaned"]])
        self.assertEqual((stats["summarized"], stats["failed"]), (1, 0))
        row = self.conn.execute("SELECT * FROM summaries").fetchone()
        self.assertEqual(row["news_id"], self.ids["cleaned"])
        self.assertEqual(json.loads(row["keywords"]), ["한전", "송전망"])
        self.assertEqual((row["model"], row["output_chars"]), ("test-model", len("한전이 송전망 투자를 늘린다.")))
        self.assertEqual(self.status(self.ids["cleaned"]), "summarized")
        self.assertIn("요약 전 기사", self.session.user_prompt(0))

    def test_failure_is_recorded_and_next_article_continues(self):
        replies = [FakeAIResponse(500)] * 3 + [ai_reply(SUMMARY)]
        stats = run_summarize(self.conn, self.config, self.client(replies))
        self.assertEqual((stats["targets"], stats["summarized"], stats["failed"]), (2, 1, 1))
        error = self.conn.execute("SELECT * FROM ai_errors").fetchone()
        self.assertEqual((error["task"], error["target"], error["attempts"]),
                         ("summarize", f"news:{self.ids['cleaned']}", 3))
        self.assertEqual(self.status(self.ids["cleaned"]), "summary_failed")
        self.assertEqual(self.status(self.ids["failed"]), "summarized")

    def test_failed_article_is_retried_next_time(self):
        run_summarize(self.conn, self.config, self.client([FakeAIResponse(500)] * 3 + [ai_reply(SUMMARY)]))
        stats = run_summarize(self.conn, self.config, self.client([ai_reply(SUMMARY)]))
        self.assertEqual((stats["targets"], stats["summarized"]), (1, 1))
        self.assertEqual(self.status(self.ids["cleaned"]), "summarized")

    def test_force_adds_new_summary_row(self):
        run_summarize(self.conn, self.config, self.client([ai_reply(SUMMARY)]), "ids", [self.ids["done"]])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0)
        newer = '{"summary": "새 요약", "keywords": []}'
        run_summarize(self.conn, self.config, self.client([ai_reply(newer)]), "ids", [self.ids["done"]], force=True)
        run_summarize(self.conn, self.config, self.client([ai_reply(SUMMARY)]), "ids", [self.ids["done"]], force=True)
        rows = self.conn.execute("SELECT summary FROM summaries WHERE news_id = ? ORDER BY id",
                                 (self.ids["done"],)).fetchall()
        self.assertEqual([r[0] for r in rows], ["새 요약", "한전이 송전망 투자를 늘린다."])

    def test_long_body_is_cut_and_shortened_after_400(self):
        news_id = add_clean_news(self.conn, "5", "긴 기사", body="가" * 5000)
        stats = run_summarize(self.conn, self.config, self.client([FakeAIResponse(400), ai_reply(SUMMARY)]),
                              "ids", [news_id])
        self.assertEqual(stats["summarized"], 1)
        self.assertIn("가" * 4000 + "\n", self.session.user_prompt(0))
        self.assertNotIn("가" * 4001, self.session.user_prompt(0))
        self.assertNotIn("가" * 2001, self.session.user_prompt(1))
        self.assertEqual(self.conn.execute("SELECT input_chars FROM summaries").fetchone()[0], 2000)

    def test_auth_error_stops_whole_run(self):
        with self.assertRaises(AIAuthError):
            run_summarize(self.conn, self.config, self.client([FakeAIResponse(401)]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ai_errors").fetchone()[0], 0)
        self.assertEqual(self.status(self.ids["cleaned"]), "cleaned")


class AiCommandTest(unittest.TestCase):
    """키가 없을 때 안내 후 종료, 분석 항목 수 검사 (AI 호출 전 단계)."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()
        self.parser = cli.build_parser()

    def tearDown(self):
        self.conn.close()

    def test_missing_key_exits_with_1(self):
        empty = {name: "" for name in self.config["ai"]["env"].values()}
        with mock.patch.dict(os.environ, empty), self.assertLogs("news_cli.cli", "ERROR") as logs:
            code = cli.cmd_summarize(self.parser.parse_args(["summarize"]), self.config, self.conn)
        self.assertEqual(code, 1)
        self.assertIn("CODYSSEY_API_KEY", logs.output[0])

    def test_analyze_needs_two_items(self):
        args = self.parser.parse_args(["analyze", "--items", "trends"])
        with self.assertRaises(cli.UsageError):
            cli.cmd_analyze(args, self.config, self.conn)

    def test_analyze_rejects_reversed_dates(self):
        args = self.parser.parse_args(["analyze", "--date-from", "2026-10-10", "--date-to", "2026-10-01"])
        with self.assertRaises(cli.UsageError):
            cli.cmd_analyze(args, self.config, self.conn)

    def test_bad_date_format_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx, mock.patch("sys.stderr"):
            self.parser.parse_args(["analyze", "--date-from", "2026/10/01"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
