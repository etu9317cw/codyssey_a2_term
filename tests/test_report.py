"""리포트: 분석 고르기(같은 조건 최신·지정·없음), MD·TXT 구성, 차트 PNG 생성 (메모리 DB, 임시 폴더, AI 호출 없음)."""
import json
import tempfile
import unittest
from pathlib import Path

from news_cli import db
from news_cli.output import charts, report
from news_cli.output.common import Filters
from tests.support import add_clean_news, add_summary, config

RESULT = {
    "trends": [{"title": "송전망 확충", "description": "HVDC 사업이 이어진다.", "article_ids": [1]}],
    "keywords": [{"keyword": "HVDC", "reason": "여러 기사"}],
    "implications": ["계통 투자 속도가 관건이다."],
}


def add_analysis(conn, category=None, date_from=None, items=("trends", "keywords", "implications")):
    cur = conn.execute(
        "INSERT INTO analyses (date_from, date_to, category, article_count, article_ids, items, result, model, "
        "created_at) VALUES (?, NULL, ?, 2, '[1, 2]', ?, ?, 'test-model', '2026-10-10 12:00:00')",
        (date_from, category, json.dumps(list(items)),
         json.dumps({item: RESULT[item] for item in items}, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


class ChooseAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.old_all = add_analysis(self.conn)
        self.power = add_analysis(self.conn, category="전력,태양광")
        self.new_all = add_analysis(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_latest_with_same_conditions(self):
        self.assertEqual(report.matching_analysis(self.conn, Filters())["id"], self.new_all)
        self.assertEqual(report.matching_analysis(self.conn, Filters(categories=["태양광", "전력"]))["id"], self.power)
        self.assertIsNone(report.matching_analysis(self.conn, Filters(categories=["전력"])))
        self.assertIsNone(report.matching_analysis(self.conn, Filters(date_from="2026-10-01")))

    def test_load_by_id(self):
        self.assertEqual(report.load_analysis(self.conn, self.power)["result"]["implications"], RESULT["implications"])
        self.assertIsNone(report.load_analysis(self.conn, 99))


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.config = config()
        a = add_clean_news(self.conn, "1", "HVDC 케이블 입찰", category="전력", status="summarized", author="김기자")
        add_summary(self.conn, a, "한전이 입찰방안을 공개했다.", ["HVDC"])
        add_clean_news(self.conn, "2", "수상태양광 추진", category="태양광", published_at="2026-10-06 09:00:00")
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.reports, self.charts = base / "reports", base / "charts"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_report(self, filters=Filters(), fmt="md", analysis=None):
        return report.run_report(self.conn, self.config, filters, 5, fmt, self.reports, self.charts, analysis)

    def test_md_report_sections_and_charts(self):
        add_analysis(self.conn)
        console, path = self.run_report()
        text = path.read_text(encoding="utf-8")
        for part in ("## 1. 조건과 생성 시각", "대상 기사: 2건 (발행일 2026-10-06 ~ 2026-10-08)",
                     "## 2. 품질 지표", "| 요약 완료율 | 50.0% (1/2) |", "## 3. TOP 5", "### 키워드 TOP 5",
                     "| 1 | HVDC | 1 |", "| 1 | 김기자 | 1 |", "## 4. AI 인사이트", "### 주요 트렌드",
                     "근거 [1] [HVDC 케이블 입찰](https://www.electimes.com/news/articleView.html?idxno=1)",
                     "### 시사점", "## 5. 차트", "](../charts/report_"):
            self.assertIn(part, text)
        self.assertNotIn("본문 문장입니다", text)  # 기사 본문은 싣지 않는다
        pngs = sorted(self.charts.glob("*.png"))
        self.assertEqual(len(pngs), 3)
        self.assertTrue(all(p.read_bytes()[:4] == b"\x89PNG" for p in pngs))
        self.assertIn("[4] AI 인사이트", console)  # 콘솔은 TXT 형식
        self.assertIn(str(path), console)

    def test_txt_report(self):
        add_analysis(self.conn)
        _, path = self.run_report(fmt="txt")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(path.suffix, ".txt")
        for part in ("[2] 품질 지표", "평균 압축률", "[3] TOP 5", "- 카테고리: 전력(1), 태양광(1)",
                     "<주요 트렌드>", "근거 [1] HVDC 케이블 입찰", "[5] 차트"):
            self.assertIn(part, text)
        self.assertNotIn("|---|", text)

    def test_no_matching_analysis_gives_hint(self):
        add_analysis(self.conn, category="전력")
        with self.assertLogs("news_cli", "WARNING"):
            _, path = self.run_report()
        text = path.read_text(encoding="utf-8")
        self.assertIn("이 조건의 분석이 없습니다. `python main.py analyze`", text)
        self.assertNotIn("### 주요 트렌드", text)

    def test_hint_keeps_conditions(self):
        with self.assertLogs("news_cli", "WARNING"):
            _, path = self.run_report(Filters(date_from="2026-10-01", categories=["전력"]))
        text = path.read_text(encoding="utf-8")
        self.assertIn("`python main.py analyze --date-from 2026-10-01 --category 전력` 실행 후", text)
        self.assertNotIn("`를", text)  # 명령 바로 뒤에 조사를 붙이지 않는다

    def test_given_analysis_with_other_conditions_is_noted(self):
        analysis = report.load_analysis(self.conn, add_analysis(self.conn, category="전력", items=("keywords", "implications")))
        with self.assertLogs("news_cli", "WARNING"):
            _, path = self.run_report(analysis=analysis)
        text = path.read_text(encoding="utf-8")
        self.assertIn("지정한 분석의 조건(기간 전체, 카테고리 전력)이 리포트 조건과 다릅니다", text)
        self.assertIn("### 핵심 키워드", text)
        self.assertNotIn("### 주요 트렌드", text)  # 그 분석에 있는 항목만

    def test_no_articles_makes_nothing(self):
        with self.assertLogs("news_cli", "WARNING"):
            self.assertIsNone(self.run_report(Filters(categories=["수소"])))
        self.assertFalse(self.reports.exists())
        self.assertFalse(self.charts.exists())


class FontTest(unittest.TestCase):
    def test_missing_font_warns(self):
        with self.assertLogs("news_cli", "WARNING"):
            self.assertIsNone(charts.setup_korean_font(["없는 폰트 이름"]))


if __name__ == "__main__":
    unittest.main()
