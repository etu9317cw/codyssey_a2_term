"""파싱: 저장해 둔 RSS·목록·기사 샘플에서 필드를 꺼내는지 (네트워크 없음)."""
import unittest

from news_cli.collect import crawler, rss
from tests.support import fixture_bytes, fixture_text


class RssParseTest(unittest.TestCase):
    def test_items_and_fields(self):
        items = rss.parse_rss(fixture_bytes("rss_sample.xml"))
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first["title"], "테스트 원전 인력 부족 기사")
        self.assertEqual(first["link"], "https://www.electimes.com/news/articleView.html?idxno=900001")
        self.assertEqual(first["author"], "홍길동 기자")
        self.assertEqual(first["pubDate"], "2026-10-10 07:00:00")
        # CDATA 안의 엔티티는 받은 그대로 둔다(정제 단계에서 복원)
        self.assertIn("&apos;대책&apos;", first["description"])

    def test_dc_creator_used_when_author_missing(self):
        items = rss.parse_rss(fixture_bytes("rss_sample.xml"))
        self.assertEqual(items[1]["author"], "김철수 기자")

    def test_broken_xml_raises(self):
        with self.assertRaises(rss.RssParseError):
            rss.parse_rss(b"<rss><channel><item><title>x</item></rss>")

    def test_missing_channel_raises(self):
        with self.assertRaises(rss.RssParseError):
            rss.parse_rss(b"<?xml version='1.0'?><feed></feed>")


class ListParseTest(unittest.TestCase):
    def test_entries(self):
        entries = crawler.parse_list(fixture_text("list_sample.html"))
        self.assertEqual([e["article_id"] for e in entries], ["900010", "900011"])  # 링크 없는 항목은 뺀다
        self.assertEqual(entries[0]["title"], "테스트 전력시장 가격 기사")
        self.assertEqual(entries[0]["date"], "2026.10.08 15:23")
        self.assertEqual(entries[0]["lead"], "전력시장에 테스트 현상이 나타났다.")

    def test_empty_page(self):
        self.assertEqual(crawler.parse_list("<html><body>점검 중</body></html>"), [])


class ArticleParseTest(unittest.TestCase):
    def setUp(self):
        self.article = crawler.parse_article(fixture_text("article_sample.html"))

    def test_meta(self):
        self.assertEqual(self.article["title"], "테스트 전력시장 가격 기사")
        self.assertEqual(self.article["section"], "에너지")
        self.assertEqual(self.article["section1"], "전력")
        self.assertEqual(self.article["published_time"], "2026-10-08T15:23:59+09:00")
        self.assertEqual(self.article["author"], "홍길동 기자")

    def test_body_keeps_only_article_paragraphs(self):
        body = self.article["body_html"]
        self.assertEqual(body.count("<p>"), 3)  # 빈 문단은 뺀다
        for not_body in ("ban_1", "사진 설명", "관련 기사 제목", "기자 소개", "소제목"):
            self.assertNotIn(not_body, body)
        self.assertIn("<strong>27.2기</strong>", body)  # 태그 제거는 정제 단계의 일

    def test_missing_body(self):
        article = crawler.parse_article("<html><head></head><body><div>구조가 바뀐 페이지</div></body></html>")
        self.assertEqual(article["body_html"], "")
        self.assertEqual(article["title"], "")

    def test_article_id_from_url(self):
        self.assertEqual(crawler.article_id_from_url("/news/articleView.html?idxno=373223"), "373223")
        self.assertEqual(crawler.article_id_from_url("https://x/a.html?page=2&idxno=5&b=1"), "5")
        self.assertIsNone(crawler.article_id_from_url("https://x/a.html?id=5"))


if __name__ == "__main__":
    unittest.main()
