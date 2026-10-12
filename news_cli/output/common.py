"""[역할 3] 공통: 조건(기간·카테고리·상태·키워드) → clean_news + 기사별 최신 요약, 콘솔 표 출력.

역할 3은 clean_news·summaries·analyses 등을 읽기만 한다.
요약은 기사마다 여러 번 쌓일 수 있어서(summarize --force) 항상 가장 최근 행(id가 가장 큰 것)을 붙인다.
"""
import json
import unicodedata
from dataclasses import dataclass

STATUSES = ("cleaned", "summarized", "summary_failed", "no_body")
STATUS_LABELS = {"cleaned": "요약 전", "summarized": "요약 완료", "summary_failed": "요약 실패", "no_body": "본문 없음"}

LATEST_SUMMARY_JOIN = "LEFT JOIN summaries s ON s.id = (SELECT MAX(id) FROM summaries WHERE news_id = c.id)"
NEWS_COLUMNS = ("c.*, s.summary, s.keywords AS summary_keywords, s.model AS summary_model, "
                "s.created_at AS summarized_at")


@dataclass
class Filters:
    date_from: str = None    # 발행일 시작 (포함, YYYY-MM-DD)
    date_to: str = None      # 발행일 끝 (포함)
    categories: list = None  # None이면 전체
    status: str = None       # None 또는 'all'이면 전체
    keyword: str = None      # 제목·요약·본문에서 찾는다

    def period(self):
        return "전체" if not (self.date_from or self.date_to) else f"{self.date_from or '처음'} ~ {self.date_to or '최근'}"

    def category_label(self):
        return ", ".join(self.categories) if self.categories else "전체"

    def describe(self):
        parts = [f"기간 {self.period()}", f"카테고리 {self.category_label()}"]
        if self.status and self.status != "all":
            parts.append(f"상태 {self.status}")
        if self.keyword:
            parts.append(f"키워드 '{self.keyword}'")
        return ", ".join(parts)


def _like(text):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def where_clause(filters):
    """Filters → (SQL 조건, 값 목록). 별칭 c = clean_news, s = 최신 요약."""
    where, params = [], []
    if filters.date_from:
        where.append("c.published_date >= ?")
        params.append(filters.date_from)
    if filters.date_to:
        where.append("c.published_date <= ?")
        params.append(filters.date_to)
    if filters.categories:
        where.append(f"c.category IN ({', '.join('?' * len(filters.categories))})")
        params.extend(filters.categories)
    if filters.status and filters.status != "all":
        where.append("c.status = ?")
        params.append(filters.status)
    if filters.keyword:
        where.append("(c.title LIKE ? ESCAPE '\\' OR c.body LIKE ? ESCAPE '\\' "
                     "OR IFNULL(s.summary, '') LIKE ? ESCAPE '\\')")
        params.extend([_like(filters.keyword)] * 3)
    return " AND ".join(where) or "1 = 1", params


def count_news(conn, filters):
    condition, params = where_clause(filters)
    return conn.execute(f"SELECT COUNT(*) FROM clean_news c {LATEST_SUMMARY_JOIN} WHERE {condition}",
                        params).fetchone()[0]


def select_news(conn, filters, limit=None, offset=0):
    """조건에 맞는 기사(dict) 목록, 최신 발행순. keywords는 목록으로 풀어 둔다(요약 없으면 빈 목록)."""
    condition, params = where_clause(filters)
    sql = (f"SELECT {NEWS_COLUMNS} FROM clean_news c {LATEST_SUMMARY_JOIN} WHERE {condition} "
           "ORDER BY c.published_at DESC, c.id DESC")
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
    return [_decode(row) for row in conn.execute(sql, params)]


def get_news(conn, news_id):
    """기사 1건(dict, 최신 요약 포함) 또는 None."""
    row = conn.execute(f"SELECT {NEWS_COLUMNS} FROM clean_news c {LATEST_SUMMARY_JOIN} WHERE c.id = ?",
                       (news_id,)).fetchone()
    return _decode(row) if row else None


def _decode(row):
    item = dict(row)
    item["keywords"] = json.loads(item.pop("summary_keywords") or "[]")
    return item


# 콘솔 표 ---------------------------------------------------------------

def display_width(text):
    """한글처럼 넓은 글자는 2칸으로 센다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def shorten(text, width):
    """화면 폭 width를 넘으면 잘라서 '…'을 붙인다."""
    text = " ".join(str(text).split())
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = display_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def text_table(headers, rows, aligns=None):
    """헤더와 행 목록 → 칸을 맞춘 줄 목록. aligns는 칸마다 'l' 또는 'r'."""
    table = [[str(cell) for cell in headers]] + [[str(cell) for cell in row] for row in rows]
    widths = [max(display_width(row[i]) for row in table) for i in range(len(headers))]
    aligns = aligns or ["l"] * len(headers)

    def fmt(row):
        cells = []
        for cell, width, align in zip(row, widths, aligns):
            pad = " " * (width - display_width(cell))
            cells.append(pad + cell if align == "r" else cell + pad)
        return "  ".join(cells).rstrip()

    lines = [fmt(table[0]), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in table[1:])
    return lines
