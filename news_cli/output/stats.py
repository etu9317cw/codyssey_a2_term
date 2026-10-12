"""[역할 3] 집계: 품질 지표, TOP N, 차트용 건수.

- 수집 성공률·정제 통과율·중복 비율은 fetch·clean 실행 전체 기준이다(raw에는 아직 카테고리·발행일이 정해지지 않은 행이 있어서).
- 나머지 지표와 TOP N·차트는 리포트 조건(기간·카테고리)에 맞는 기사 기준이다.
- 비율의 분모가 0이면 값은 None(화면에는 '-')이다.
"""
from collections import Counter
from datetime import date, timedelta

from news_cli.output.common import LATEST_SUMMARY_JOIN, STATUSES, where_clause

SCOPE_ALL = "전체 실행"
SCOPE_FILTERED = "조건 기사"


def _metric(key, label, numerator, denominator, scope, formula):
    return {"key": key, "label": label, "value": numerator / denominator if denominator else None,
            "numerator": numerator, "denominator": denominator, "scope": scope, "formula": formula}


def quality_metrics(conn, filters):
    """품질 지표 7개(dict 목록)."""
    metrics = []
    ok, failed = conn.execute("SELECT IFNULL(SUM(succeeded), 0), IFNULL(SUM(failed), 0) FROM fetch_log").fetchone()
    metrics.append(_metric("fetch_success", "수집 성공률", ok, ok + failed, SCOPE_ALL,
                           "성공한 HTTP 요청 ÷ (성공 + 실패)"))
    raw = dict(conn.execute("SELECT status, COUNT(*) FROM raw_news GROUP BY status").fetchall())
    processed = sum(count for status, count in raw.items() if status != "new")
    metrics.append(_metric("clean_pass", "정제 통과율", processed - raw.get("rejected", 0), processed, SCOPE_ALL,
                           "검증을 통과한 raw ÷ 정제한 raw"))
    metrics.append(_metric("duplicate", "중복 비율", raw.get("duplicate", 0), processed, SCOPE_ALL,
                           "중복으로 건너뛴 raw ÷ 정제한 raw"))

    condition, params = where_clause(filters)
    base = f"FROM clean_news c {LATEST_SUMMARY_JOIN} WHERE {condition}"
    row = conn.execute(
        "SELECT COUNT(*), IFNULL(SUM(c.body_source = 'crawl'), 0), IFNULL(SUM(c.status != 'no_body'), 0), "
        "IFNULL(SUM(c.status = 'summarized'), 0), IFNULL(SUM(s.input_chars), 0), IFNULL(SUM(s.output_chars), 0) "
        + base, params).fetchone()
    total, crawled, targets, summarized, input_chars, output_chars = row
    saved, errors = conn.execute(
        f"SELECT (SELECT COUNT(*) FROM summaries WHERE news_id IN (SELECT c.id {base})), "
        f"(SELECT COUNT(*) FROM ai_errors WHERE task = 'summarize' AND target IN (SELECT 'news:' || c.id {base}))",
        params + params).fetchone()

    metrics.append(_metric("body", "본문 확보율", crawled, total, SCOPE_FILTERED, "크롤링 본문이 있는 기사 ÷ 전체 기사"))
    metrics.append(_metric("summary_done", "요약 완료율", summarized, targets, SCOPE_FILTERED,
                           "요약 완료 기사 ÷ 본문이 있는 기사"))
    metrics.append(_metric("ai_fail", "AI 요약 실패율", errors, saved + errors, SCOPE_FILTERED,
                           "요약 실패 기록 ÷ (요약 저장 + 요약 실패 기록)"))
    metrics.append(_metric("compression", "평균 압축률", output_chars, input_chars, SCOPE_FILTERED,
                           "요약 글자 수 ÷ 요약에 넣은 본문 글자 수 (기사별 최신 요약)"))
    return metrics


def format_value(metric):
    if metric["value"] is None:
        return "-"
    return f"{metric['value']:.1%} ({metric['numerator']:,}/{metric['denominator']:,})"


# TOP N -----------------------------------------------------------------

def _top(counter, n):
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def keyword_counts(articles):
    """요약 키워드를 기사당 한 번씩 센다. 띄어쓰기·대소문자만 다른 키워드는 하나로 보고 가장 많이 쓴 표기로 보여 준다."""
    counts, forms = Counter(), {}
    for article in articles:
        seen = set()
        for keyword in article["keywords"]:
            norm = "".join(keyword.split()).lower()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            counts[norm] += 1
            forms.setdefault(norm, Counter())[keyword] += 1
    return Counter({forms[norm].most_common(1)[0][0]: count for norm, count in counts.items()})


def top_n(articles, n, unknown_author="미상"):
    """카테고리·키워드·기자 TOP N. 기자 이름이 없는 기사(미상)는 기자 순위에서 뺀다."""
    return {
        "categories": _top(Counter(a["category"] for a in articles), n),
        "keywords": _top(keyword_counts(articles), n),
        "authors": _top(Counter(a["author"] for a in articles if a["author"] != unknown_author), n),
    }


# 차트용 건수 -----------------------------------------------------------

def category_counts(articles):
    return _top(Counter(a["category"] for a in articles), None)


def status_counts(articles):
    counts = Counter(a["status"] for a in articles)
    return [(status, counts[status]) for status in STATUSES]


def daily_counts(articles):
    """[(날짜, 발행 기사 수, 처음 수집한 기사 수)]. 첫날부터 마지막 날까지 빈 날은 0으로 채운다."""
    published = Counter(a["published_date"] for a in articles)
    collected = Counter(a["first_collected_at"][:10] for a in articles)
    days = set(published) | set(collected)
    if not days:
        return []
    first, last = date.fromisoformat(min(days)), date.fromisoformat(max(days))
    result = []
    for offset in range((last - first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        result.append((day, published[day], collected[day]))
    return result


def date_span(articles):
    """(가장 이른 발행일, 가장 늦은 발행일). 기사가 없으면 (None, None)."""
    dates = [a["published_date"] for a in articles]
    return (min(dates), max(dates)) if dates else (None, None)
