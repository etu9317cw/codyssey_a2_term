"""[역할 1] 크롤링(방법 2): 세부 카테고리 목록 페이지와 기사 페이지를 BeautifulSoup으로 읽는다.

전기신문 페이지 구조(2026-10-10 확인)
- 목록: #section-list li.item 안에 h4.titles a(제목·링크), em.replace-date(2026.10.08 15:56), p.lead(요약문)
- 기사: meta og:title, article:section(상위 섹션), article:section1(세부 카테고리),
        article:published_time(ISO 8601), og:article:author / 본문 #article-view-content-div 안의 <p>
본문은 광고·사진·관련기사를 뺀 <p> 조각을 HTML 그대로 모은다. 태그 제거는 정제 단계에서 한다.
"""
import re

from bs4 import BeautifulSoup

_IDXNO_RE = re.compile(r"[?&]idxno=(\d+)")
_NOT_BODY = "script, style, figure, .ad-template, article.relation, article.account"


def article_id_from_url(url):
    match = _IDXNO_RE.search(url or "")
    return match.group(1) if match else None


def _text_of(node):
    return node.get_text(" ", strip=True) if node else ""


def parse_list(html_text):
    """목록 페이지 → [{article_id, title, date, lead}, ...] (페이지에 나온 순서)"""
    soup = BeautifulSoup(html_text, "html.parser")
    entries, seen = [], set()
    for item in soup.select("#section-list li.item"):
        link = item.select_one("h4.titles a[href]")
        article_id = article_id_from_url(link["href"]) if link else None
        if not article_id or article_id in seen:
            continue
        seen.add(article_id)
        entries.append({
            "article_id": article_id,
            "title": _text_of(link),
            "date": _text_of(item.select_one(".replace-date")),
            "lead": _text_of(item.select_one(".lead")),
        })
    return entries


def parse_article(html_text):
    """기사 페이지 → {title, section, section1, published_time, author, body_html}. 없는 값은 빈 문자열."""
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop})
        return (tag.get("content") or "").strip() if tag else ""

    paragraphs = []
    body = soup.select_one("#article-view-content-div")
    if body:
        for node in body.select(_NOT_BODY):
            node.decompose()
        for p in body.find_all("p"):
            inner = p.decode_contents().strip()
            if inner:
                paragraphs.append(f"<p>{inner}</p>")

    return {
        "title": meta("og:title"),
        "section": meta("article:section"),
        "section1": meta("article:section1"),
        "published_time": meta("article:published_time"),
        "author": meta("og:article:author") or meta("dable:author"),
        "body_html": "\n".join(paragraphs),
    }
