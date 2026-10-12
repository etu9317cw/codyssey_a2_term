"""[역할 2] analyze: 조건(기간·카테고리)에 맞는 기사들을 AI로 종합 분석 → analyses.

- 입력: 조건에 맞는 기사 중 최신 max_articles건의 ID·발행일·카테고리·제목·요약(요약이 없으면 본문 앞부분)
- 항목: trends(주요 트렌드), keywords(핵심 키워드), common_diff(공통점·차이점), implications(시사점) 중 2개 이상
- 응답의 근거 기사 ID 중 입력에 없는 번호는 버린다(모델이 만들어 낸 ID를 리포트에 싣지 않으려는 것).
- 결과는 analyses에 저장하고, 목록(list_analyses)과 상세(get_analysis)로 다시 본다.
- 끝내 실패하면 ERROR 로그와 ai_errors에 남긴다.
"""
import json
import logging

from news_cli.ai import prompts
from news_cli.ai.client import AIAuthError, AIError
from news_cli.db import now_str

log = logging.getLogger(__name__)

ITEM_NAMES = tuple(prompts.ANALYSIS_ITEMS)
MIN_ITEMS = 2


def parse_items(value):
    """'trends,keywords' → ['trends', 'keywords']. 모르는 항목이 있거나 2개 미만이면 ValueError."""
    items = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    unknown = [name for name in items if name not in prompts.ANALYSIS_ITEMS]
    if unknown:
        raise ValueError(f"알 수 없는 분석 항목: {', '.join(unknown)} (사용 가능: {', '.join(ITEM_NAMES)})")
    if len(items) < MIN_ITEMS:
        raise ValueError(f"분석 항목은 {MIN_ITEMS}개 이상 골라야 합니다 (사용 가능: {', '.join(ITEM_NAMES)})")
    return items


def describe_conditions(date_from, date_to, categories):
    """(기간 설명, 카테고리 설명)"""
    period = "전체" if not (date_from or date_to) else f"{date_from or '처음'} ~ {date_to or '최근'}"
    return period, ", ".join(categories) if categories else "전체"


# 대상 기사 -------------------------------------------------------------

def select_articles(conn, date_from=None, date_to=None, categories=None, max_articles=40):
    """(분석에 쓸 기사 목록, 조건에 맞는 전체 건수). 본문 없는 기사는 빼고 최신 발행순으로 고른다."""
    where, params = ["c.status != 'no_body'"], []
    if date_from:
        where.append("c.published_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("c.published_date <= ?")
        params.append(date_to)
    if categories:
        where.append(f"c.category IN ({', '.join('?' * len(categories))})")
        params.extend(categories)
    condition = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM clean_news c WHERE {condition}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT c.id, c.published_date, c.category, c.title, c.body,
                   (SELECT s.summary FROM summaries s WHERE s.news_id = c.id ORDER BY s.id DESC LIMIT 1) AS summary
            FROM clean_news c WHERE {condition}
            ORDER BY c.published_at DESC, c.id DESC LIMIT ?""", params + [max_articles]).fetchall()
    return [dict(row) for row in rows], total


# 응답 검사 -------------------------------------------------------------

def _one_line(value):
    return " ".join(str(value or "").split())


def _text_list(value, name):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{name}이(가) 목록이 아님")
    texts = [_one_line(v) for v in value if _one_line(v)]
    if not texts:
        raise ValueError(f"{name}이(가) 비어 있음")
    return texts


def _check_trends(value, valid_ids, dropped):
    if not isinstance(value, list) or not value:
        raise ValueError("trends가 비어 있음")
    trends = []
    for trend in value:
        if not isinstance(trend, dict) or not _one_line(trend.get("title")):
            raise ValueError("trends 항목에 title이 없음")
        ids = []
        raw_ids = trend.get("article_ids") or []
        for raw in raw_ids if isinstance(raw_ids, list) else [raw_ids]:
            try:
                news_id = int(str(raw).strip("[] "))
            except ValueError:
                news_id = None
            if news_id in valid_ids:
                if news_id not in ids:
                    ids.append(news_id)
            else:
                dropped.append(raw)
        trends.append({"title": _one_line(trend["title"]), "description": _one_line(trend.get("description")),
                       "article_ids": ids})
    return trends


def _check_keywords(value, valid_ids, dropped):
    if not isinstance(value, list):
        raise ValueError("keywords가 목록이 아님")
    keywords = []
    for entry in value:
        keyword, reason = (entry.get("keyword"), entry.get("reason")) if isinstance(entry, dict) else (entry, "")
        if _one_line(keyword):
            keywords.append({"keyword": _one_line(keyword), "reason": _one_line(reason)})
    if not keywords:
        raise ValueError("keywords가 비어 있음")
    return keywords


def _check_common_diff(value, valid_ids, dropped):
    if not isinstance(value, dict):
        raise ValueError("common_diff가 객체가 아님")
    return {"common": _text_list(value.get("common"), "common"),
            "differences": _text_list(value.get("differences"), "differences")}


def _check_implications(value, valid_ids, dropped):
    return _text_list(value, "implications")


_CHECKERS = {"trends": _check_trends, "keywords": _check_keywords,
             "common_diff": _check_common_diff, "implications": _check_implications}


def make_validator(items, valid_ids):
    """요청한 항목만 검사·정리하는 함수와, 버린 근거 ID를 모으는 목록을 돌려준다."""
    dropped = []

    def validate(data):
        dropped.clear()
        missing = [item for item in items if item not in data]
        if missing:
            raise ValueError(f"항목 없음: {', '.join(missing)}")
        return {item: _CHECKERS[item](data[item], valid_ids, dropped) for item in items}

    return validate, dropped


# 실행·조회 -------------------------------------------------------------

def run_analyze(conn, config, client, date_from=None, date_to=None, categories=None, items=ITEM_NAMES,
                max_articles=None):
    """분석해서 analyses에 저장하고 저장한 분석(dict)을 돌려준다. 대상이 없거나 AI가 실패하면 None."""
    ai_conf = config["ai"]
    max_articles = max_articles or ai_conf["analyze_max_articles"]
    period, category_label = describe_conditions(date_from, date_to, categories)
    articles, total = select_articles(conn, date_from, date_to, categories, max_articles)
    if not articles:
        log.warning("조건에 맞는 기사가 없습니다 (기간 %s, 카테고리 %s)", period, category_label)
        return None
    if total > len(articles):
        log.info("조건에 맞는 기사 %d건 중 최신 %d건을 분석합니다 (--max-articles)", total, len(articles))
    no_summary = sum(1 for article in articles if not article["summary"])
    if no_summary:
        log.warning("요약이 없는 기사 %d건은 본문 앞부분을 씁니다 (summarize를 먼저 실행하면 더 정확합니다)", no_summary)
    log.info("분석 시작: 기간 %s, 카테고리 %s, 기사 %d건, 항목 %s", period, category_label, len(articles),
             ", ".join(prompts.ANALYSIS_ITEMS[item][0] for item in items))

    excerpt = ai_conf["analyze_excerpt_chars"]
    user_prompts = [prompts.analyze_user(articles, items, period, category_label, chars)
                    for chars in (excerpt, excerpt // 2)]  # 400 응답이면 기사별 내용을 절반으로 줄여 다시
    valid_ids = {article["id"] for article in articles}
    validate, dropped = make_validator(items, valid_ids)
    try:
        result, attempts, _ = client.ask_json(prompts.ANALYZE_SYSTEM, user_prompts, validate)
    except AIAuthError:
        raise
    except AIError as e:
        log.error("분석 실패 (%d회 시도): %s", e.attempts, e)
        conn.execute("INSERT INTO ai_errors (task, target, error, attempts, created_at) VALUES ('analyze', ?, ?, ?, ?)",
                     (f"기간 {period} / 카테고리 {category_label}", str(e), e.attempts, now_str()))
        conn.commit()
        return None
    if dropped:
        log.warning("입력에 없는 근거 기사 ID %d개를 뺐습니다: %s", len(dropped), ", ".join(map(str, dropped)))

    cur = conn.execute(
        "INSERT INTO analyses (date_from, date_to, category, article_count, article_ids, items, result, model, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date_from, date_to, ",".join(categories) if categories else None, len(articles),
         json.dumps(sorted(valid_ids)), json.dumps(list(items)), json.dumps(result, ensure_ascii=False),
         client.model, now_str()))
    conn.commit()
    log.info("분석 저장: id=%d (%d회 호출)", cur.lastrowid, attempts)
    return get_analysis(conn, cur.lastrowid)


def _decode(row):
    analysis = dict(row)
    for field in ("article_ids", "items", "result"):
        analysis[field] = json.loads(analysis[field])
    return analysis


def get_analysis(conn, analysis_id):
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return _decode(row) if row else None


def list_analyses(conn, limit=20):
    return [_decode(row) for row in conn.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT ?", (limit,))]


def format_analysis(analysis):
    """분석 1건 → 콘솔 출력용 줄 목록."""
    categories = analysis["category"].split(",") if analysis["category"] else None
    period, category_label = describe_conditions(analysis["date_from"], analysis["date_to"], categories)
    lines = [f"== 분석 #{analysis['id']} ({analysis['created_at']}, {analysis['model']}) ==",
             f"조건: 기간 {period}, 카테고리 {category_label}, 기사 {analysis['article_count']}건"]
    result = analysis["result"]
    for item in analysis["items"]:
        lines.append("")
        lines.append(f"[{prompts.ANALYSIS_ITEMS[item][0]}]")
        value = result.get(item)
        if item == "trends":
            for n, trend in enumerate(value, 1):
                evidence = ", ".join(map(str, trend["article_ids"])) or "없음"
                lines.append(f" {n}. {trend['title']} — {trend['description']} (근거 기사 ID: {evidence})")
        elif item == "keywords":
            for n, entry in enumerate(value, 1):
                lines.append(f" {n}. {entry['keyword']}" + (f" — {entry['reason']}" if entry["reason"] else ""))
        elif item == "common_diff":
            lines.append(" 공통점")
            lines.extend(f"  - {text}" for text in value["common"])
            lines.append(" 차이점")
            lines.extend(f"  - {text}" for text in value["differences"])
        else:
            lines.extend(f" {n}. {text}" for n, text in enumerate(value, 1))
    return lines


def format_history(analyses):
    """분석 목록 → 콘솔 출력용 줄 목록."""
    if not analyses:
        return ["저장된 분석이 없습니다."]
    lines = ["ID | 생성 시각 | 기간 | 카테고리 | 기사 수 | 항목"]
    for analysis in analyses:
        categories = analysis["category"].split(",") if analysis["category"] else None
        period, category_label = describe_conditions(analysis["date_from"], analysis["date_to"], categories)
        lines.append(f"{analysis['id']} | {analysis['created_at']} | {period} | {category_label} | "
                     f"{analysis['article_count']} | {','.join(analysis['items'])}")
    return lines
