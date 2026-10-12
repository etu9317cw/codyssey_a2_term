"""정제 규칙: 텍스트 정규화, 날짜 통일, 결측값, 카테고리, 기사 병합 (DB·네트워크 없음)."""
import unittest

from news_cli.collect import cleaner
from tests.support import config


def raw(raw_id, method, payload, fetched_at="2026-10-10 10:00:00", channel="energy", key="900001"):
    return {"id": raw_id, "method": method, "channel": channel, "url": payload.get("link") or payload.get("url", ""),
            "article_key": key, "payload": payload, "fetched_at": fetched_at}


RSS_PAYLOAD = {
    "title": "원전 인력 부족 &apos;심화&apos;",
    "link": "https://www.electimes.com/news/articleView.html?idxno=900001",
    "description": "원전 일감이 늘고 있다.그러나 인력은 부족하다.",
    "author": "홍길동 기자",
    "pubDate": "2026-10-10 07:00:00",
}
CRAWL_PAYLOAD = {
    "url": "https://www.electimes.com/news/articleView.html?idxno=900001",
    "article_id": "900001",
    "title": "원전 인력 부족 ‘심화’",
    "section": "에너지",
    "section1": "원자력",
    "published_time": "2026-10-10T07:00:00+09:00",
    "author": "홍길동 기자",
    "body_html": "<p>[전기신문 홍길동 기자] 국내 원전 건설이 재개되면서 전문인력 수요가 크게 늘고 있다.</p>\n"
                 "<p>업계는 인력 양성 대책을 요구하고 있다. 설비용량은 0.3GW 늘었다.</p>",
}


class NormalizeTextTest(unittest.TestCase):
    def test_tags_entities_and_spaces(self):
        text = cleaner.normalize_text("<p>전력&nbsp;&nbsp;시장에 &lsquo;기현상&rsquo;</p>\n\n<p>  두 번째   문단 </p>")
        self.assertEqual(text, "전력 시장에 ‘기현상’\n두 번째 문단")

    def test_double_encoded_entity(self):
        self.assertEqual(cleaner.normalize_text("업계 &amp;apos;대책&amp;apos;"), "업계 '대책'")

    def test_br_becomes_newline_and_single_line_mode(self):
        self.assertEqual(cleaner.normalize_text("가<br />나"), "가\n나")
        self.assertEqual(cleaner.normalize_text("가<br />나", multiline=False), "가 나")

    def test_byline_and_copyright_removed(self):
        text = cleaner.normalize_text("[전기신문 홍길동 기자] 본문이다. 홍길동 기자 hong@electimes.com "
                                      "저작권자 © 전기신문 무단전재 및 재배포 금지")
        self.assertEqual(text, "본문이다.")

    def test_glued_sentences_split_but_decimals_kept(self):
        text = cleaner.normalize_text("일감이 늘었다.그러나 부족하다.8일 발표에서 0.3GW와 160.3원이 나왔다.")
        self.assertEqual(text, "일감이 늘었다. 그러나 부족하다. 8일 발표에서 0.3GW와 160.3원이 나왔다.")

    def test_zero_width_removed(self):
        self.assertEqual(cleaner.normalize_text("전​력"), "전력")

    def test_empty(self):
        self.assertEqual(cleaner.normalize_text(None), "")
        self.assertEqual(cleaner.normalize_text("<p> </p>"), "")


class NormalizeDatetimeTest(unittest.TestCase):
    def test_three_site_formats(self):
        self.assertEqual(cleaner.normalize_datetime("2026-10-10 09:42:45"), "2026-10-10 09:42:45")  # RSS
        self.assertEqual(cleaner.normalize_datetime("2026-10-10T09:42:45+09:00"), "2026-10-10 09:42:45")  # 기사
        self.assertEqual(cleaner.normalize_datetime("2026.10.10 09:42"), "2026-10-10 09:42:00")  # 목록

    def test_other_timezone_converted_to_kst(self):
        self.assertEqual(cleaner.normalize_datetime("2026-10-10T00:42:45Z"), "2026-10-10 09:42:45")
        self.assertEqual(cleaner.normalize_datetime("Fri, 09 Oct 2026 15:00:00 GMT"), "2026-10-10 00:00:00")

    def test_invalid(self):
        for value in (None, "", "어제", "2026-13-40 99:00"):
            self.assertIsNone(cleaner.normalize_datetime(value), value)


class AuthorAndKeyTest(unittest.TestCase):
    def test_author(self):
        self.assertEqual(cleaner.normalize_author("홍길동 기자(hong@electimes.com)", "미상"), "홍길동 기자")
        self.assertEqual(cleaner.normalize_author("  ", "미상"), "미상")
        self.assertEqual(cleaner.normalize_author(None, "미상"), "미상")

    def test_article_key(self):
        self.assertEqual(cleaner.extract_article_key(
            "https://www.electimes.com/news/articleView.html?idxno=373223"), "373223")
        a = cleaner.extract_article_key("https://Example.com/a?b=2&a=1&utm_source=x#top")
        b = cleaner.extract_article_key("https://example.com/a?a=1&b=2")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("u"))
        self.assertIsNone(cleaner.extract_article_key(""))


class CategoryTest(unittest.TestCase):
    def setUp(self):
        self.categories = config()["source"]["categories"]

    def test_site_first(self):
        self.assertEqual(cleaner.decide_category("원자력", "태양광 기사", self.categories, "기타"), ("원자력", "site"))

    def test_keyword_most_hits(self):
        # '수소' 1개 vs '태양광'·'태양전지' 2개 → 태양광
        self.assertEqual(cleaner.decide_category("", "태양광·태양전지와 수소", self.categories, "기타"),
                         ("태양광", "keyword"))

    def test_default(self):
        self.assertEqual(cleaner.decide_category("", "인사 발령", self.categories, "기타"), ("기타", "default"))

    def test_site_outside_config_falls_back_to_keyword(self):
        # 설정 밖 사이트 분류(산업정책·칼럼 이름)는 쓰지 않고 키워드 → 기본값 순서로 정한다
        self.assertEqual(cleaner.decide_category("산업정책", "해상풍력 입찰 공고", self.categories, "기타"),
                         ("풍력", "keyword"))
        self.assertEqual(cleaner.decide_category("웅 씨의 코너링", "인사 발령", self.categories, "기타"),
                         ("기타", "default"))


class BuildRecordTest(unittest.TestCase):
    def setUp(self):
        self.config = config()

    def test_merge_rss_and_crawl_prefers_crawl(self):
        record, reason = cleaner.build_record([
            raw(1, "rss", RSS_PAYLOAD, "2026-10-10 10:00:00"),
            raw(2, "crawl", CRAWL_PAYLOAD, "2026-10-10 10:00:05"),
        ], self.config)
        self.assertIsNone(reason)
        self.assertEqual(record["title"], "원전 인력 부족 ‘심화’")
        self.assertEqual(record["body_source"], "crawl")
        self.assertNotIn("[전기신문", record["body"])
        self.assertEqual(record["category"], "원자력")
        self.assertEqual(record["category_source"], "site")
        self.assertEqual(record["section"], "에너지")
        self.assertEqual(record["methods"], "crawl,rss")
        self.assertEqual(record["published_at"], "2026-10-10 07:00:00")
        self.assertEqual(record["published_date"], "2026-10-10")
        self.assertEqual((record["first_collected_at"], record["last_collected_at"]),
                         ("2026-10-10 10:00:00", "2026-10-10 10:00:05"))
        self.assertEqual(record["status"], "cleaned")

    def test_rss_only_uses_excerpt_and_keyword_category(self):
        record, _ = cleaner.build_record([raw(1, "rss", RSS_PAYLOAD)], self.config)
        self.assertEqual(record["title"], "원전 인력 부족 '심화'")
        self.assertEqual(record["body_source"], "excerpt")
        self.assertEqual(record["body"], "원전 일감이 늘고 있다. 그러나 인력은 부족하다.")
        self.assertEqual((record["category"], record["category_source"]), ("원자력", "keyword"))
        self.assertEqual(record["section"], "에너지")  # 피드의 섹션

    def test_short_crawl_body_falls_back_to_excerpt(self):
        crawl = dict(CRAWL_PAYLOAD, body_html="<p>짧다</p>", lead="목록 요약문이다.")
        record, _ = cleaner.build_record([raw(2, "crawl", crawl)], self.config)
        self.assertEqual((record["body"], record["body_source"]), ("목록 요약문이다.", "excerpt"))

    def test_no_body(self):
        crawl = dict(CRAWL_PAYLOAD, body_html="")
        record, _ = cleaner.build_record([raw(2, "crawl", crawl)], self.config)
        self.assertEqual((record["body"], record["body_source"], record["status"]), ("", "none", "no_body"))

    def test_missing_author_becomes_default(self):
        record, _ = cleaner.build_record([raw(1, "rss", dict(RSS_PAYLOAD, author=""))], self.config)
        self.assertEqual(record["author"], "미상")

    def test_missing_required_fields_rejected(self):
        record, reason = cleaner.build_record([raw(1, "rss", dict(RSS_PAYLOAD, pubDate="어제"))], self.config)
        self.assertIsNone(record)
        self.assertIn("published_at", reason)
        record, reason = cleaner.build_record([raw(1, "rss", dict(RSS_PAYLOAD, title=" ", link=""))], self.config)
        self.assertIsNone(record)
        self.assertIn("url", reason)
        self.assertIn("title", reason)

    def test_latest_raw_of_same_method_wins(self):
        newer = dict(RSS_PAYLOAD, title="수정된 제목")
        record, _ = cleaner.build_record([
            raw(5, "rss", newer, "2026-10-10 12:00:00"),
            raw(1, "rss", RSS_PAYLOAD, "2026-10-10 10:00:00"),
        ], self.config)
        self.assertEqual(record["title"], "수정된 제목")


class MergeRecordsTest(unittest.TestCase):
    def setUp(self):
        cfg = config()
        self.crawled, _ = cleaner.build_record([raw(2, "crawl", CRAWL_PAYLOAD, "2026-10-10 10:00:00")], cfg)
        self.rss_only, _ = cleaner.build_record(
            [raw(3, "rss", dict(RSS_PAYLOAD, title="제목 수정", author=""), "2026-10-11 09:00:00")], cfg)

    def test_better_values_are_not_overwritten(self):
        merged = cleaner.merge_records(self.crawled, self.rss_only, "미상")
        self.assertEqual(merged["title"], "제목 수정")                 # 새 값으로 갱신
        self.assertEqual(merged["body_source"], "crawl")             # 본문은 요약문으로 덮지 않음
        self.assertEqual(merged["body"], self.crawled["body"])
        self.assertEqual(merged["category_source"], "site")          # site 카테고리 유지
        self.assertEqual(merged["author"], "홍길동 기자")              # 미상으로 덮지 않음
        self.assertEqual(merged["methods"], "crawl,rss")
        self.assertEqual(merged["last_collected_at"], "2026-10-11 09:00:00")

    def test_body_upgraded_from_excerpt_to_crawl(self):
        merged = cleaner.merge_records(self.rss_only, self.crawled, "미상")
        self.assertEqual(merged["body_source"], "crawl")
        self.assertEqual(merged["category_source"], "site")

    def test_summary_status_kept(self):
        existing = dict(self.crawled, status="summarized")
        merged = cleaner.merge_records(existing, self.rss_only, "미상")
        self.assertEqual(merged["status"], "summarized")

    def test_no_body_becomes_cleaned_when_body_arrives(self):
        existing = dict(self.rss_only, body="", body_source="none", status="no_body")
        merged = cleaner.merge_records(existing, self.crawled, "미상")
        self.assertEqual(merged["status"], "cleaned")


if __name__ == "__main__":
    unittest.main()
