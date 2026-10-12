"""집계: 품질 지표 7개, TOP N(최신 요약 키워드, 표기 차이 합치기, 미상 기자 제외), 차트용 건수 (메모리 DB)."""
import unittest

from news_cli import db
from news_cli.output import stats
from news_cli.output.common import Filters, select_news
from tests.support import add_clean_news, add_summary


def seed(conn):
    conn.execute("INSERT INTO fetch_log (run_at, method, channel, requested, succeeded, failed, saved) "
                 "VALUES ('2026-10-10 10:00:00', 'rss', 'energy', 10, 9, 1, 9)")
    for n, status in enumerate(["cleaned"] * 6 + ["duplicate", "rejected", "new"]):
        conn.execute("INSERT INTO raw_news (source, method, channel, url, article_key, payload, payload_hash, "
                     "fetched_at, status) VALUES ('electimes', 'rss', 'energy', 'u', ?, '{}', ?, "
                     "'2026-10-10 10:00:00', ?)", (str(n), str(n), status))
    a = add_clean_news(conn, "1", "HVDC 입찰", category="전력", status="summarized", author="김기자",
                       published_at="2026-10-08 09:00:00", collected_at="2026-10-09 08:00:00")
    b = add_clean_news(conn, "2", "한전 SMP", category="전력", status="summarized", author="이기자",
                       published_at="2026-10-10 09:00:00")
    add_clean_news(conn, "3", "수상태양광", category="태양광", body_source="excerpt", published_at="2026-10-08 10:00:00")
    add_clean_news(conn, "4", "본문 없음", body="", category="태양광", status="no_body", published_at="2026-10-07 09:00:00")
    c = add_clean_news(conn, "5", "수소 충전소", category="수소", status="summary_failed", author="김기자",
                       published_at="2026-10-09 09:00:00")
    add_summary(conn, a, "옛 요약", ["옛 키워드"], 1000, 100)
    add_summary(conn, a, "새 요약", ["HVDC", "한국 전력"], 2000, 300)
    add_summary(conn, b, "요약", ["hvdc", "한국전력"], 1000, 200)
    for news_id in (c, a):
        conn.execute("INSERT INTO ai_errors (task, target, error, attempts, created_at) "
                     "VALUES ('summarize', ?, '503', 3, '2026-10-10 10:00:00')", (f"news:{news_id}",))
    conn.commit()


class QualityMetricsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        seed(self.conn)

    def tearDown(self):
        self.conn.close()

    def metrics(self, filters=Filters()):
        return {m["key"]: m for m in stats.quality_metrics(self.conn, filters)}

    def test_all_metrics(self):
        m = self.metrics()
        self.assertEqual(len(m), 7)
        self.assertAlmostEqual(m["fetch_success"]["value"], 9 / 10)
        self.assertEqual((m["clean_pass"]["numerator"], m["clean_pass"]["denominator"]), (7, 8))  # new는 빼고
        self.assertEqual((m["duplicate"]["numerator"], m["duplicate"]["denominator"]), (1, 8))
        self.assertEqual((m["body"]["numerator"], m["body"]["denominator"]), (3, 5))  # excerpt·none 제외
        self.assertEqual((m["summary_done"]["numerator"], m["summary_done"]["denominator"]), (2, 4))  # no_body 제외
        self.assertEqual((m["ai_fail"]["numerator"], m["ai_fail"]["denominator"]), (2, 5))
        self.assertEqual((m["compression"]["numerator"], m["compression"]["denominator"]), (500, 3000))  # 최신 요약만

    def test_filter_applies_only_to_article_metrics(self):
        m = self.metrics(Filters(categories=["태양광"]))
        self.assertAlmostEqual(m["fetch_success"]["value"], 0.9)
        self.assertEqual(m["clean_pass"]["denominator"], 8)
        self.assertEqual((m["body"]["numerator"], m["body"]["denominator"]), (0, 2))
        self.assertIsNone(m["ai_fail"]["value"])
        self.assertEqual(stats.format_value(m["ai_fail"]), "-")
        self.assertEqual(stats.format_value(m["clean_pass"]), "87.5% (7/8)")

    def test_empty_db(self):
        conn = db.connect(":memory:")
        self.assertTrue(all(m["value"] is None for m in stats.quality_metrics(conn, Filters())))
        conn.close()


class TopAndCountsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        seed(self.conn)
        self.articles = select_news(self.conn, Filters())

    def tearDown(self):
        self.conn.close()

    def test_top_n(self):
        top = stats.top_n(self.articles, 2)
        self.assertEqual(top["categories"], [("전력", 2), ("태양광", 2)])  # 같으면 이름순
        self.assertEqual(top["authors"], [("김기자", 2), ("이기자", 1)])  # 미상 제외
        keywords = dict(top["keywords"])
        self.assertEqual(sorted(k.lower().replace(" ", "") for k in keywords), ["hvdc", "한국전력"])
        self.assertEqual(set(keywords.values()), {2})  # 표기만 다른 키워드는 하나로

    def test_old_summary_keywords_not_counted(self):
        self.assertNotIn("옛 키워드", stats.keyword_counts(self.articles))

    def test_keyword_counted_once_per_article(self):
        counts = stats.keyword_counts([{"keywords": ["ESS", "ess", "E S S"]}, {"keywords": ["ESS"]}])
        self.assertEqual(counts, {"ESS": 2})

    def test_daily_counts_fill_empty_days(self):
        daily = stats.daily_counts(self.articles)
        self.assertEqual([d for d, _, _ in daily], ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"])
        self.assertEqual(dict((d, p) for d, p, _ in daily)["2026-10-08"], 2)
        self.assertEqual(dict((d, c) for d, _, c in daily), {"2026-10-07": 0, "2026-10-08": 0,
                                                              "2026-10-09": 1, "2026-10-10": 4})
        self.assertEqual(stats.daily_counts([]), [])

    def test_status_counts_and_span(self):
        self.assertEqual(stats.status_counts(self.articles),
                         [("cleaned", 1), ("summarized", 2), ("summary_failed", 1), ("no_body", 1)])
        self.assertEqual(stats.date_span(self.articles), ("2026-10-07", "2026-10-10"))


if __name__ == "__main__":
    unittest.main()
