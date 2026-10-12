"""[역할 2] summarize: clean_news 기사별 AI 요약 → summaries.

대상 선택
- unsummarized(기본): 요약 전(cleaned)과 요약에 실패했던(summary_failed) 기사
- all: 본문이 있는 기사 전체. 이미 요약된 기사는 건너뛴다(force면 다시 요약)
- ids: 지정한 기사. 이미 요약된 기사는 건너뛴다(force면 다시 요약)
본문이 없는 기사(no_body)는 어떤 경우에도 요약하지 않는다.

다시 요약하면 summaries에 새 행을 쌓고, 읽는 쪽은 기사별 최신 행(가장 큰 id)을 쓴다.
실패하면 ERROR 로그와 ai_errors에 남기고 상태를 summary_failed로 바꾼 뒤 다음 기사로 넘어간다.
기사 1건마다 커밋하므로 중간에 멈춰도 그때까지 한 요약은 남는다.
"""
import json
import logging
from collections import Counter

from news_cli.ai import prompts
from news_cli.ai.client import AIAuthError, AIError
from news_cli.db import now_str

log = logging.getLogger(__name__)

MAX_KEYWORDS = 5


def validate_summary(data):
    """{"summary": str, "keywords": [str]} 형식 확인과 정리. 틀리면 ValueError."""
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary가 비어 있음")
    keywords = data.get("keywords", [])
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    if not isinstance(keywords, list):
        raise ValueError("keywords가 목록이 아님")
    cleaned = []
    for keyword in keywords:
        text = " ".join(str(keyword).split()).lstrip("#")
        if text and text not in cleaned:
            cleaned.append(text)
    return {"summary": " ".join(summary.split()), "keywords": cleaned[:MAX_KEYWORDS]}


def select_targets(conn, mode="unsummarized", ids=None, force=False, limit=None):
    """요약할 기사 행 목록과 건너뛴 건수(Counter)를 돌려준다."""
    skipped = Counter()
    if mode == "ids":
        wanted = list(dict.fromkeys(ids or []))
        found = {row["id"]: row for row in conn.execute(
            f"SELECT * FROM clean_news WHERE id IN ({', '.join('?' * len(wanted))})", wanted)} if wanted else {}
        rows = []
        for news_id in wanted:
            row = found.get(news_id)
            if row is None:
                log.warning("id=%d 기사가 없습니다", news_id)
                skipped["not_found"] += 1
            else:
                rows.append(row)
    elif mode == "all":
        rows = conn.execute("SELECT * FROM clean_news ORDER BY id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM clean_news WHERE status IN ('cleaned', 'summary_failed') "
                            "ORDER BY id").fetchall()

    targets = []
    for row in rows:
        if row["status"] == "no_body":
            skipped["no_body"] += 1
            if mode == "ids":
                log.warning("id=%d 본문이 없어 요약하지 않습니다", row["id"])
        elif row["status"] == "summarized" and not force:
            skipped["already"] += 1
            if mode == "ids":
                log.info("id=%d 이미 요약됨, 건너뜀 (다시 하려면 --force)", row["id"])
        else:
            targets.append(row)
    if limit and len(targets) > limit:
        skipped["over_limit"] = len(targets) - limit
        targets = targets[:limit]
    return targets, skipped


def _record_failure(conn, news_id, error, attempts):
    conn.execute("INSERT INTO ai_errors (task, target, error, attempts, created_at) VALUES ('summarize', ?, ?, ?, ?)",
                 (f"news:{news_id}", error, attempts, now_str()))
    conn.execute("UPDATE clean_news SET status = 'summary_failed' WHERE id = ?", (news_id,))
    conn.commit()


def summarize_one(conn, client, row, ai_conf):
    """기사 1건 요약 → summaries 저장. 성공하면 저장한 요약 dict, 실패하면 None. 인증 실패(AIAuthError)는 위로 보낸다."""
    body = row["body"]
    bodies = [body[:ai_conf["max_body_chars"]]]
    if len(bodies[0]) > ai_conf["retry_body_chars"]:
        bodies.append(body[:ai_conf["retry_body_chars"]])  # 400 응답이면 이 길이로 다시
    user_prompts = [prompts.summary_user(row["title"], text) for text in bodies]

    try:
        result, attempts, level = client.ask_json(prompts.SUMMARY_SYSTEM, user_prompts, validate_summary)
    except AIAuthError:
        raise
    except AIError as e:
        log.error("요약 실패 id=%d (%d회 시도): %s", row["id"], e.attempts, e)
        _record_failure(conn, row["id"], str(e), e.attempts)
        return None

    input_chars = len(bodies[level])
    conn.execute(
        "INSERT INTO summaries (news_id, summary, keywords, model, input_chars, output_chars, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row["id"], result["summary"], json.dumps(result["keywords"], ensure_ascii=False), client.model,
         input_chars, len(result["summary"]), now_str()))
    conn.execute("UPDATE clean_news SET status = 'summarized' WHERE id = ?", (row["id"],))
    conn.commit()
    result.update(input_chars=input_chars, attempts=attempts)
    return result


def run_summarize(conn, config, client, mode="unsummarized", ids=None, force=False, limit=None):
    """대상 기사를 차례로 요약하고 건수(Counter)를 돌려준다."""
    targets, skipped = select_targets(conn, mode, ids, force, limit)
    stats = Counter(targets=len(targets))
    for key, count in skipped.items():
        stats["skipped_" + key] = count
    notes = []
    if skipped["already"]:
        notes.append(f"이미 요약됨 {skipped['already']}건 건너뜀, 다시 하려면 --force")
    if skipped["no_body"]:
        notes.append(f"본문 없음 {skipped['no_body']}건 제외")
    if skipped["over_limit"]:
        notes.append(f"--limit로 {skipped['over_limit']}건 남김")
    log.info("요약 시작: 대상 %d건%s", len(targets), f" ({', '.join(notes)})" if notes else "")

    ai_conf = config["ai"]
    for index, row in enumerate(targets, 1):
        result = summarize_one(conn, client, row, ai_conf)
        if result is None:
            stats["failed"] += 1
            continue
        stats["summarized"] += 1
        stats["input_chars"] += result["input_chars"]
        stats["output_chars"] += len(result["summary"])
        log.info("[%d/%d] id=%d 요약 완료 (%d자 → %d자): %s",
                 index, len(targets), row["id"], result["input_chars"], len(result["summary"]), row["title"])
        log.debug("    요약: %s / 키워드: %s", result["summary"], ", ".join(result["keywords"]))
    return stats
