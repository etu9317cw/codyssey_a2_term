"""정제 실행(run_clean): 같은 실행 안 병합, skip/upsert, rejected, rebuild (메모리 DB)."""
import json
import unittest

from news_cli import db
from news_cli.collect.cleaner import run_clean
from news_cli.collect.fetcher import save_raw
from tests.support import config
from tests.test_cleaner_rules import CRAWL_PAYLOAD, RSS_PAYLOAD


class RunCleanTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()

    def tearDown(self):
        self.conn.close()

    def add(self, method, payload, fetched_at="2026-10-10 10:00:00", key="900001", channel="energy"):
        url = payload.get("url") or payload.get("link") or ""
        return save_raw(self.conn, "electimes", method, channel, url, key, payload, fetched_at)

    def raw_statuses(self):
        return [tuple(r) for r in self.conn.execute("SELECT id, status FROM raw_news ORDER BY id")]

    def clean_rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM clean_news ORDER BY id")]

    def test_same_content_is_stored_once_in_raw(self):
        self.assertTrue(self.add("rss", RSS_PAYLOAD))
        self.assertFalse(self.add("rss", RSS_PAYLOAD, fetched_at="2026-10-10 11:00:00"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0], 1)

    def test_rss_and_crawl_merged_into_one_article(self):
        self.add("rss", RSS_PAYLOAD)
        self.add("crawl", CRAWL_PAYLOAD, fetched_at="2026-10-10 10:00:05")
        stats = run_clean(self.conn, self.config)
        self.assertEqual((stats["inserted"], stats["raw_cleaned"]), (1, 2))
        rows = self.clean_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["methods"], rows[0]["body_source"], rows[0]["category"]),
                         ("crawl,rss", "crawl", "원자력"))
        self.assertEqual(self.raw_statuses(), [(1, "cleaned"), (2, "cleaned")])

    def test_skip_keeps_existing_and_marks_duplicate(self):
        self.add("crawl", CRAWL_PAYLOAD)
        run_clean(self.conn, self.config, policy="skip")
        before = self.clean_rows()[0]
        self.add("crawl", dict(CRAWL_PAYLOAD, title="바뀐 제목"), fetched_at="2026-10-11 10:00:00")
        stats = run_clean(self.conn, self.config, policy="skip")
        self.assertEqual((stats["skipped"], stats["raw_duplicate"]), (1, 1))
        self.assertEqual(self.clean_rows()[0], before)
        status, note = self.conn.execute("SELECT status, note FROM raw_news WHERE id = 2").fetchone()
        self.assertEqual(status, "duplicate")
        self.assertIn("skip", note)

    def test_upsert_updates_but_keeps_better_body(self):
        self.add("crawl", CRAWL_PAYLOAD)
        run_clean(self.conn, self.config)
        news_id = self.clean_rows()[0]["id"]
        self.conn.execute("UPDATE clean_news SET status = 'summarized' WHERE id = ?", (news_id,))
        self.add("rss", dict(RSS_PAYLOAD, title="바뀐 제목"), fetched_at="2026-10-11 10:00:00")
        stats = run_clean(self.conn, self.config, policy="upsert")
        self.assertEqual(stats["updated"], 1)
        row = self.clean_rows()[0]
        self.assertEqual(row["id"], news_id)                 # 같은 행을 갱신
        self.assertEqual(row["title"], "바뀐 제목")
        self.assertEqual(row["body_source"], "crawl")        # 요약문으로 덮지 않음
        self.assertEqual(row["status"], "summarized")        # 요약 상태 유지
        self.assertEqual(row["methods"], "crawl,rss")

    def test_invalid_raw_rejected_with_reason(self):
        self.add("rss", dict(RSS_PAYLOAD, pubDate=""), key="900003")
        stats = run_clean(self.conn, self.config)
        self.assertEqual(stats["raw_rejected"], 1)
        self.assertEqual(self.clean_rows(), [])
        status, note = self.conn.execute("SELECT status, note FROM raw_news").fetchone()
        self.assertEqual(status, "rejected")
        self.assertIn("published_at", note)

    def test_broken_payload_rejected(self):
        self.conn.execute("INSERT INTO raw_news (source, method, channel, url, article_key, payload, payload_hash,"
                          " fetched_at) VALUES ('electimes', 'rss', 'energy', 'u', '1', '{깨짐', 'h', 'now')")
        stats = run_clean(self.conn, self.config)
        self.assertEqual((stats["raw_processed"], stats["raw_rejected"]), (1, 1))

    def test_limit_counts_articles(self):
        for key in ("1", "2", "3"):
            self.add("rss", dict(RSS_PAYLOAD, link=f"https://www.electimes.com/news/articleView.html?idxno={key}"),
                     key=key)
        stats = run_clean(self.conn, self.config, limit=2)
        self.assertEqual(stats["inserted"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_news WHERE status = 'new'").fetchone()[0], 1)

    def test_rebuild_reapplies_rules_with_upsert(self):
        self.add("rss", RSS_PAYLOAD)
        run_clean(self.conn, self.config)
        self.assertEqual(self.clean_rows()[0]["category"], "원자력")
        # 정제 규칙(키워드)을 바꾸고 raw에서 다시 만든다 — 사이트에 다시 요청하지 않는다
        self.config["source"]["categories"]["원자력"]["keywords"] = []
        self.config["source"]["categories"]["수소"]["keywords"].append("인력")
        stats = run_clean(self.conn, self.config, policy="skip", rebuild=True)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(self.clean_rows()[0]["category"], "수소")

    def test_payload_is_stored_as_received(self):
        self.add("rss", RSS_PAYLOAD)
        payload = json.loads(self.conn.execute("SELECT payload FROM raw_news").fetchone()[0])
        self.assertEqual(payload, RSS_PAYLOAD)


if __name__ == "__main__":
    unittest.main()
