"""HTTP 정책(재시도·지연·robots.txt)과 fetch 흐름 — 가짜 세션을 써서 네트워크 없이 확인한다."""
import unittest

from news_cli import db
from news_cli.collect.fetcher import Fetcher
from news_cli.collect.http_client import HttpClient
from tests.support import TIMEOUT, FakeClock, FakeResponse, FakeSession, config, fixture_bytes

BASE = "https://www.electimes.com"
ROBOTS = BASE + "/robots.txt"
ROBOTS_OK = FakeResponse(200, b"User-agent: *\nDisallow: /admin/\n")
LIST_PAGE_2 = BASE + "/news/articleList.html?sc_sub_section_code=S2N98&view_type=sm&page=2"


def make_client(routes, http_conf=None):
    clock = FakeClock()
    session = FakeSession({ROBOTS: [ROBOTS_OK], **routes})
    client = HttpClient(http_conf or config()["http"], ROBOTS, session=session, sleep=clock.sleep,
                        clock=clock.monotonic)
    return client, session, clock


class HttpClientTest(unittest.TestCase):
    def test_success(self):
        client, _, _ = make_client({BASE + "/a": [FakeResponse(200, "본문".encode())]})
        result = client.get(BASE + "/a")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "본문")

    def test_404_is_not_retried(self):
        client, session, _ = make_client({})
        result = client.get(BASE + "/none")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "HTTP 404")
        self.assertEqual(session.calls.count(BASE + "/none"), 1)

    def test_5xx_retried_with_backoff_then_success(self):
        client, session, clock = make_client({BASE + "/a": [FakeResponse(503), FakeResponse(503), FakeResponse(200)]})
        result = client.get(BASE + "/a")
        self.assertTrue(result.ok)
        self.assertEqual(session.calls.count(BASE + "/a"), 3)
        self.assertIn(2, clock.sleeps)
        self.assertIn(4, clock.sleeps)

    def test_timeout_gives_up_after_retries(self):
        client, session, _ = make_client({BASE + "/slow": [TIMEOUT]})
        result = client.get(BASE + "/slow")
        self.assertFalse(result.ok)
        self.assertIn("타임아웃", result.error)
        self.assertEqual(session.calls.count(BASE + "/slow"), 3)  # 처음 1회 + 재시도 2회

    def test_delay_between_requests(self):
        client, _, clock = make_client({BASE + "/a": [FakeResponse(200)], BASE + "/b": [FakeResponse(200)]})
        client.get(BASE + "/a")
        client.get(BASE + "/b")
        # robots → /a, /a → /b 사이에 각각 2초
        self.assertEqual(clock.sleeps, [2.0, 2.0])

    def test_robots_disallow_blocks_request(self):
        client, session, _ = make_client({})
        result = client.get(BASE + "/admin/page")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "robots.txt 금지")
        self.assertNotIn(BASE + "/admin/page", session.calls)

    def test_robots_unreachable_blocks_everything(self):
        clock = FakeClock()
        session = FakeSession({ROBOTS: [FakeResponse(503)], BASE + "/a": [FakeResponse(200)]})
        client = HttpClient(config()["http"], ROBOTS, session=session, sleep=clock.sleep, clock=clock.monotonic)
        self.assertFalse(client.get(BASE + "/a").ok)
        self.assertNotIn(BASE + "/a", session.calls)

    def test_robots_crawl_delay_respected(self):
        clock = FakeClock()
        robots = FakeResponse(200, b"User-agent: *\nCrawl-delay: 5\nDisallow: /admin/\n")
        session = FakeSession({ROBOTS: [robots]})
        client = HttpClient(config()["http"], ROBOTS, session=session, sleep=clock.sleep, clock=clock.monotonic)
        client.get(BASE + "/x")
        self.assertEqual(client.delay, 5.0)


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()
        article = fixture_bytes("article_sample.html")
        self.routes = {
            BASE + "/rss/S1N41.xml": [FakeResponse(200, fixture_bytes("rss_sample.xml"))],
            BASE + "/news/articleList.html?sc_sub_section_code=S2N98&view_type=sm&page=1":
                [FakeResponse(200, fixture_bytes("list_sample.html"))],
            BASE + "/news/articleView.html?idxno=900001": [FakeResponse(200, article)],
            BASE + "/news/articleView.html?idxno=900002": [FakeResponse(200, article)],
            BASE + "/news/articleView.html?idxno=900010": [FakeResponse(200, article)],
            # 900011은 등록하지 않음 → 404
        }

    def tearDown(self):
        self.conn.close()

    def run_fetch(self, method="all", limit=2, recrawl=False, since=None, pages=1):
        client, session, _ = make_client(self.routes)
        fetcher = Fetcher(self.conn, self.config, client, recrawl=recrawl)
        return fetcher.run(method, ["energy"], ["전력"], limit, pages, since=since), session

    def test_all_method_saves_rss_and_crawl_raws(self):
        results, _ = self.run_fetch()
        by_channel = {(r.method, r.channel): r for r in results}
        self.assertEqual(by_channel[("rss", "energy")].saved, 2)
        self.assertEqual(by_channel[("crawl", "energy")].saved, 2)       # RSS 기사 본문
        crawl = by_channel[("crawl", "전력")]
        self.assertEqual((crawl.found, crawl.saved, crawl.failed), (2, 1, 1))  # 900011은 404
        methods = dict(self.conn.execute("SELECT method, COUNT(*) FROM raw_news GROUP BY method").fetchall())
        self.assertEqual(methods, {"rss": 2, "crawl": 3})
        logs = self.conn.execute("SELECT method, channel, requested, succeeded, failed, saved FROM fetch_log").fetchall()
        self.assertEqual(len(logs), 3)

    def test_second_run_does_not_request_crawled_articles_again(self):
        self.run_fetch()
        results, session = self.run_fetch()
        self.assertNotIn(BASE + "/news/articleView.html?idxno=900001", session.calls)
        self.assertEqual(sum(r.saved for r in results), 0)
        skipped = {(r.method, r.channel): r.skipped for r in results}
        self.assertEqual(skipped, {("rss", "energy"): 2, ("crawl", "energy"): 2, ("crawl", "전력"): 1})

    def test_recrawl_requests_again(self):
        self.run_fetch()
        _, session = self.run_fetch(recrawl=True)
        self.assertIn(BASE + "/news/articleView.html?idxno=900001", session.calls)

    def test_rss_method_only(self):
        results, session = self.run_fetch(method="rss", limit=3)
        self.assertEqual([(r.method, r.saved) for r in results], [("rss", 3)])
        self.assertFalse(any("articleView" in url for url in session.calls))

    def test_broken_feed_logged_and_skipped(self):
        self.routes[BASE + "/rss/S1N41.xml"] = [FakeResponse(200, b"<html>not rss")]
        results, _ = self.run_fetch(method="rss")
        self.assertEqual((results[0].succeeded, results[0].failed, results[0].saved), (0, 1, 0))

    def test_since_keeps_newer_list_entries_and_stops_paging(self):
        # 목록 900010(10-08)·900011(10-07) → 10-08 이후는 900010만. 오래된 기사를 봤으니 2쪽은 요청하지 않는다
        with self.assertNoLogs("news_cli.collect.fetcher", level="WARNING"):
            results, session = self.run_fetch(method="crawl", limit=10, since="2026-10-08", pages=3)
        self.assertEqual((results[0].found, results[0].saved), (1, 1))
        self.assertNotIn(LIST_PAGE_2, session.calls)
        self.assertNotIn(BASE + "/news/articleView.html?idxno=900011", session.calls)

    def test_since_warns_when_pages_run_out_inside_period(self):
        # 1쪽 기사가 모두 기간 안인데 --pages 1에서 멈춤 → 더 있을 수 있다고 WARNING
        with self.assertLogs("news_cli.collect.fetcher", level="WARNING") as logs:
            results, _ = self.run_fetch(method="crawl", limit=10, since="2026-10-01", pages=1)
        self.assertEqual(results[0].found, 2)
        self.assertIn("끝까지 받지 못했을 수", logs.output[0])

    def test_since_filters_rss_items(self):
        # 피드 10-10·10-09·날짜 없음 → 10-10 이후 + 날짜 없는 항목(정제에서 발행일을 다시 확인)
        results, _ = self.run_fetch(method="rss", limit=10, since="2026-10-10")
        self.assertEqual((results[0].found, results[0].saved), (2, 2))

    def test_since_rss_limit_warning(self):
        with self.assertLogs("news_cli.collect.fetcher", level="WARNING") as logs:
            results, _ = self.run_fetch(method="rss", limit=1, since="2026-10-01")
        self.assertEqual(results[0].found, 1)
        self.assertIn("--limit 1건만", logs.output[0])


if __name__ == "__main__":
    unittest.main()
