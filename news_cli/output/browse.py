"""[역할 3] 보너스: list(목록 + 필터 + 쪽 나누기), show(기사 상세).

- list: 카테고리·기간·키워드(제목·요약·본문)·상태로 거르고 최신 발행순으로 한 쪽씩 보여 준다.
- show: 기사 정보, 최신 요약·키워드, 본문 앞부분(--full이면 전체), 요약 실패 기록, raw 수집 이력.
"""
import math

from news_cli.output.common import STATUS_LABELS, count_news, get_news, select_news, shorten, text_table

TITLE_WIDTH = 56


class PageError(Exception):
    """없는 쪽 번호."""


def list_page(conn, filters, page=1, size=10):
    """(그 쪽의 기사 목록, 전체 건수, 전체 쪽 수). 기사가 있는데 쪽 번호가 범위를 넘으면 PageError."""
    total = count_news(conn, filters)
    pages = max(1, math.ceil(total / size))
    if total and page > pages:
        raise PageError(f"{page}쪽은 없습니다 (전체 {total}건, {pages}쪽)")
    return select_news(conn, filters, limit=size, offset=(page - 1) * size), total, pages


def format_list(articles, total, pages, page, size, filters):
    if not total:
        return [f"조건에 맞는 기사가 없습니다 ({filters.describe()})"]
    first = (page - 1) * size + 1
    lines = [f"기사 목록 ({filters.describe()}): {total}건 중 {first}–{first + len(articles) - 1} ({page}/{pages}쪽)", ""]
    lines += text_table(["ID", "발행일", "카테고리", "상태", "제목"],
                        [(a["id"], a["published_date"], a["category"], STATUS_LABELS[a["status"]],
                          shorten(a["title"], TITLE_WIDTH)) for a in articles],
                        ["r", "l", "l", "l", "l"])
    hints = [f"다음 쪽: --page {page + 1}"] if page < pages else []
    hints.append("상세: python main.py show ID")
    return lines + ["", " / ".join(hints)]


def get_article(conn, news_id):
    """기사 상세(dict) 또는 None. 요약 횟수, 요약 실패 기록, raw 수집 이력을 함께 담는다."""
    article = get_news(conn, news_id)
    if article is None:
        return None
    article["summary_count"] = conn.execute("SELECT COUNT(*) FROM summaries WHERE news_id = ?",
                                            (news_id,)).fetchone()[0]
    article["errors"] = [dict(row) for row in conn.execute(
        "SELECT created_at, attempts, error FROM ai_errors WHERE task = 'summarize' AND target = ? ORDER BY id",
        (f"news:{news_id}",))]
    article["raws"] = [dict(row) for row in conn.execute(
        "SELECT id, method, channel, fetched_at, status, note FROM raw_news WHERE article_key = ? ORDER BY id",
        (article["article_key"],))]
    return article


def format_article(article, body_chars=500, full=False):
    a = article
    lines = [f"== 기사 #{a['id']} ==",
             f"제목: {a['title']}",
             f"주소: {a['url']}",
             f"발행: {a['published_at']} | 섹션: {a['section'] or '-'} | 카테고리: {a['category']} (근거: {a['category_source']})",
             f"기자: {a['author']}",
             f"상태: {STATUS_LABELS[a['status']]}({a['status']}) | 본문 출처: {a['body_source']} ({len(a['body']):,}자)",
             f"수집: 처음 {a['first_collected_at']}, 마지막 {a['last_collected_at']}, 방법 {a['methods']}",
             ""]

    if a["summary"]:
        lines.append(f"[AI 요약] {a['summary_model']}, {a['summarized_at']}"
                     + (f" (요약 {a['summary_count']}회 중 최신)" if a["summary_count"] > 1 else ""))
        lines.append(a["summary"])
        lines.append("키워드: " + (", ".join(a["keywords"]) or "없음"))
    else:
        lines.append("[AI 요약] 없음 (python main.py summarize --id {})".format(a["id"]))

    if a["errors"]:
        lines += ["", f"[요약 실패 기록] {len(a['errors'])}건"]
        lines.extend(f"  {e['created_at']} ({e['attempts']}회 시도) {e['error']}" for e in a["errors"])

    body = a["body"]
    lines.append("")
    if not body:
        lines.append("[본문] 없음")
    elif full or len(body) <= body_chars:
        lines += [f"[본문] 전체 {len(body):,}자", body]
    else:
        lines += [f"[본문 앞부분] {body_chars}자 / 전체 {len(body):,}자 (전체는 --full)", body[:body_chars] + "…"]

    lines += ["", f"[수집 이력] raw {len(a['raws'])}건"]
    lines += ["  " + line for line in text_table(
        ["raw ID", "방법", "채널", "수집 시각", "상태", "메모"],
        [(r["id"], r["method"], r["channel"], r["fetched_at"], r["status"], r["note"] or "") for r in a["raws"]],
        ["r", "l", "l", "l", "l", "l"])]
    return lines
