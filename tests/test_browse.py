"""조회(보너스): list 필터·쪽 계산, show 상세, 한글 폭 맞춤 (메모리 DB)."""
import unittest

from news_cli import db
from news_cli.output import browse
from news_cli.output.common import Filters, display_width, shorten, text_table
from tests.support import add_clean_news, add_summary


class ListTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        for n in range(1, 13):  # 1~12번, 번호가 클수록 최근 기사
            add_clean_news(self.conn, str(n), f"기사 {n}", category="전력" if n % 2 else "태양광",
                           status="summarized" if n <= 4 else "cleaned", published_at=f"2026-10-{n:02d} 09:00:00")
        add_summary(self.conn, 3, "가동률 100% 달성 소식", ["원전"])
        self.conn.execute("UPDATE clean_news SET title = '이용률 100%' WHERE id = 5")
        self.conn.execute("UPDATE clean_news SET title = '이용률 100점' WHERE id = 6")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def ids(self, filters, page=1, size=20):
        return [a["id"] for a in browse.list_page(self.conn, filters, page, size)[0]]

    def test_pages(self):
        articles, total, pages = browse.list_page(self.conn, Filters(), page=1, size=5)
        self.assertEqual(([a["id"] for a in articles], total, pages), ([12, 11, 10, 9, 8], 12, 3))
        self.assertEqual(self.ids(Filters(), page=3, size=5), [2, 1])
        with self.assertRaises(browse.PageError):
            browse.list_page(self.conn, Filters(), page=4, size=5)

    def test_filters(self):
        self.assertEqual(self.ids(Filters(categories=["태양광"], date_to="2026-10-06")), [6, 4, 2])
        self.assertEqual(self.ids(Filters(status="summarized", date_from="2026-10-03")), [4, 3])
        self.assertEqual(self.ids(Filters(keyword="가동률")), [3])  # 요약에서 찾음
        self.assertEqual(self.ids(Filters(keyword="100%")), [5, 3])  # %는 와일드카드가 아님

    def test_empty_result_is_not_error(self):
        articles, total, pages = browse.list_page(self.conn, Filters(categories=["수소"]), page=3)
        self.assertEqual((articles, total, pages), ([], 0, 1))
        self.assertIn("조건에 맞는 기사가 없습니다", browse.format_list([], 0, 1, 3, 10, Filters(categories=["수소"]))[0])

    def test_format_list_hints(self):
        articles, total, pages = browse.list_page(self.conn, Filters(), 1, 5)
        lines = browse.format_list(articles, total, pages, 1, 5, Filters())
        self.assertIn("12건 중 1–5 (1/3쪽)", lines[0])
        self.assertIn("다음 쪽: --page 2", lines[-1])
        articles, total, pages = browse.list_page(self.conn, Filters(), 3, 5)
        lines = browse.format_list(articles, total, pages, 3, 5, Filters())
        self.assertIn("12건 중 11–12 (3/3쪽)", lines[0])
        self.assertNotIn("다음 쪽", lines[-1])


class ShowTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.id = add_clean_news(self.conn, "77", "SMP 하락", body="가" * 800, status="summarized")
        add_summary(self.conn, self.id, "옛 요약")
        add_summary(self.conn, self.id, "SMP가 56원 떨어졌다.", ["SMP"])
        self.conn.execute("INSERT INTO ai_errors (task, target, error, attempts, created_at) "
                          "VALUES ('summarize', ?, 'HTTP 503', 3, '2026-10-10 09:00:00')", (f"news:{self.id}",))
        for method in ("rss", "crawl"):
            self.conn.execute("INSERT INTO raw_news (source, method, channel, url, article_key, payload, payload_hash, "
                              "fetched_at, status) VALUES ('electimes', ?, '전력', 'u', '77', '{}', ?, "
                              "'2026-10-10 08:00:00', 'cleaned')", (method, method))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_detail(self):
        article = browse.get_article(self.conn, self.id)
        self.assertEqual((article["summary"], article["summary_count"]), ("SMP가 56원 떨어졌다.", 2))
        self.assertEqual([r["method"] for r in article["raws"]], ["rss", "crawl"])
        text = "\n".join(browse.format_article(article, body_chars=500))
        for part in ("== 기사 #1 ==", "요약 2회 중 최신", "키워드: SMP", "[요약 실패 기록] 1건",
                     "500자 / 전체 800자 (전체는 --full)", "[수집 이력] raw 2건"):
            self.assertIn(part, text)
        self.assertNotIn("가" * 501, text)
        self.assertIn("가" * 800, "\n".join(browse.format_article(article, body_chars=500, full=True)))

    def test_missing(self):
        self.assertIsNone(browse.get_article(self.conn, 99))


class TextWidthTest(unittest.TestCase):
    def test_korean_counts_two(self):
        self.assertEqual(display_width("전력 AB"), 7)
        self.assertEqual(shorten("가나다라마", 6), "가나…")
        self.assertEqual(shorten("가나", 6), "가나")

    def test_table_aligns_columns(self):
        lines = text_table(["ID", "제목"], [(1, "전력"), (10, "a")], ["r", "l"])
        self.assertEqual(lines, ["ID  제목", "--  ----", " 1  전력", "10  a"])


if __name__ == "__main__":
    unittest.main()
