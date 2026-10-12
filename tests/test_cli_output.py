"""명령줄: report·export·list·show 종료 코드와 옵션 검사 (임시 설정·DB·폴더, 네트워크·AI 없음)."""
import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from news_cli import db
from news_cli.cli import main
from tests.support import add_clean_news, add_summary, config


class CliOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        cfg = config()
        cfg["paths"] = {name: str(self.base / sub) for name, sub in (
            ("db", "news.db"), ("log", "app.log"), ("charts_dir", "charts"), ("reports_dir", "reports"),
            ("exports_dir", "exports"))}
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        conn = db.connect(self.base / "news.db")
        news_id = add_clean_news(conn, "1", "HVDC 입찰", status="summarized")
        add_summary(conn, news_id, "입찰방안 공개", ["HVDC"])
        add_clean_news(conn, "2", "수상태양광", category="태양광")
        conn.close()

    def tearDown(self):
        logger = logging.getLogger("news_cli")
        for handler in list(logger.handlers):  # 로그 파일을 닫아야 임시 폴더를 지울 수 있다
            handler.close()
            logger.removeHandler(handler)
        logger.addHandler(logging.NullHandler())  # 패키지 기본 상태로 되돌려 다음 테스트의 경고가 콘솔에 새지 않게
        logger.propagate = True
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([*args, "--config", str(self.config_path)])
        return code, out.getvalue()

    def test_export(self):
        code, _ = self.run_cli("export", "--status", "summarized")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.suffix for p in (self.base / "exports").iterdir()), [".csv", ".jsonl", ".xlsx"])
        self.assertEqual(self.run_cli("export", "--format", "csv,pdf")[0], 2)
        self.assertEqual(self.run_cli("export", "--category", "수소")[0], 1)

    def test_report(self):
        code, out = self.run_cli("report", "--format", "txt", "--top", "3")
        self.assertEqual(code, 0)
        self.assertIn("[3] TOP 3", out)
        self.assertEqual(len(list((self.base / "reports").glob("*.txt"))), 1)
        self.assertEqual(self.run_cli("report", "--analysis-id", "5")[0], 2)
        self.assertEqual(self.run_cli("report", "--category", "수소")[0], 1)

    def test_list_and_show(self):
        code, out = self.run_cli("list", "--keyword", "HVDC")
        self.assertEqual(code, 0)
        self.assertIn("1건 중 1–1", out)
        self.assertEqual(self.run_cli("list", "--date-from", "2026-10-09", "--date-to", "2026-10-01")[0], 2)
        self.assertEqual(self.run_cli("list", "--category", "없는분류")[0], 2)
        self.assertEqual(self.run_cli("list", "--page", "5")[0], 2)
        code, out = self.run_cli("show", "1")
        self.assertEqual(code, 0)
        self.assertIn("== 기사 #1 ==", out)
        self.assertEqual(self.run_cli("show", "99")[0], 2)


if __name__ == "__main__":
    unittest.main()
