"""[역할 1] 정제: raw_news(status=new) → 검증·정규화·카테고리·중복 처리 → clean_news.

규칙(설계서 4절)
- 필수 필드: article_key, url, title, published_at. 하나라도 없으면 raw를 rejected로 표시하고 이유를 note에 남긴다.
- 텍스트 정규화: 태그 제거, HTML 엔티티 복원, 바이라인·저작권 문구 제거, 붙은 문장 띄우기, 공백 정리
- 날짜 통일: 여러 형식 → 한국 시간 'YYYY-MM-DD HH:MM:SS'
- 결측값: 기자 없음 → 미상 / 본문 없음 → RSS·목록 요약문(excerpt) → 그것도 없으면 no_body
- 카테고리: site(사이트 분류) > keyword(제목 키워드) > default(기타)
- 중복: 같은 실행 안의 같은 기사는 먼저 병합(크롤링 값 우선), 이미 clean에 있으면 skip 또는 upsert
"""
import hashlib
import html
import json
import logging
import re
from collections import Counter
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from news_cli.collect.crawler import article_id_from_url
from news_cli.db import DATETIME_FORMAT, KST, now_str

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("article_key", "url", "title", "published_at")
BODY_RANK = {"crawl": 2, "excerpt": 1, "none": 0}
SUMMARY_STATUSES = ("summarized", "summary_failed")

_BREAK_RE = re.compile(r"<\s*(?:br|/p|/div|/h\d|/li)\s*/?\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\f\v 　]+")
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
# 한글 문장 끝 마침표 뒤에 바로 다음 문장이 붙은 경우(문단 경계가 사라진 요약문). 소수점(0.3GW)은 건드리지 않는다.
_GLUED_SENTENCE_RE = re.compile(r"(?<=[가-힣])([.!?])(?=[가-힣0-9“‘])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BYLINE_PATTERNS = (
    re.compile(r"\[\s*전기신문[^\]]*\]"),                              # [전기신문 홍길동 기자]
    re.compile(r"[가-힣]{2,4}\s*기자\s*\(?\s*[\w.+-]+@[\w-]+(?:\.[\w-]+)+\s*\)?"),  # 홍길동 기자 hong@electimes.com
    re.compile(r"저작권자\s*[©ⓒ]\s*.*?금지"),
    re.compile(r"출처\s*:\s*전기신문\s*(?:\([^)]*\))?"),
)
_DATE_FORMATS = (
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


# 텍스트 ---------------------------------------------------------------

def _strip_markup(value):
    """태그를 지우고 엔티티를 복원한다. <br>·</p>는 줄바꿈으로 바꾼다."""
    text = _BREAK_RE.sub("\n", str(value))
    text = _TAG_RE.sub("", text)
    for _ in range(3):  # &amp;apos; 처럼 두 번 인코딩된 경우까지
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    return _ZERO_WIDTH_RE.sub("", text).replace("\r", "\n")


def _collapse_spaces(text, multiline):
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return ("\n" if multiline else " ").join(lines)


def normalize_text(value, multiline=True):
    """제목·본문 정규화. multiline=False면 한 줄로 만든다."""
    if not value:
        return ""
    text = _strip_markup(value)
    for pattern in _BYLINE_PATTERNS:
        text = pattern.sub("", text)
    text = _GLUED_SENTENCE_RE.sub(r"\1 ", text)
    return _collapse_spaces(text, multiline)


def normalize_author(value, default):
    """'홍길동 기자(hong@electimes.com)' → '홍길동 기자'. 비면 default."""
    if not value:
        return default
    text = _EMAIL_RE.sub("", _strip_markup(value))
    text = re.sub(r"[()\[\]<>]", " ", text)
    return _collapse_spaces(text, multiline=False) or default


# 날짜 -----------------------------------------------------------------

def normalize_datetime(value):
    """여러 날짜 형식 → 한국 시간 'YYYY-MM-DD HH:MM:SS'. 해석할 수 없으면 None.

    RSS '2026-10-10 09:42:45', 기사 '2026-10-10T09:42:45+09:00', 목록 '2026.10.10 09:42',
    표준 RSS 'Fri, 10 Oct 2026 09:42:45 +0900'. 시간대가 없으면 한국 시간으로 본다.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):  # Python 3.10의 fromisoformat은 Z(UTC) 표기를 읽지 못한다
        text = text[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(KST).replace(tzinfo=None)
    return dt.strftime(DATETIME_FORMAT)


# 주소 -----------------------------------------------------------------

def normalize_url(url):
    """비교용 주소: 소문자 호스트, 조각(#) 제거, utm_* 제거, 쿼리 정렬."""
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_"))
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def extract_article_key(url):
    """기사 ID(idxno). 주소에 ID가 없으면 정규화한 주소의 해시('u' + 16자리). 주소가 없으면 None."""
    if not url or not url.strip():
        return None
    article_id = article_id_from_url(url)
    if article_id:
        return article_id
    return "u" + hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


# 카테고리 -------------------------------------------------------------

def decide_category(site_category, title, categories, default):
    """(카테고리, 근거) — site > keyword(제목에 걸린 키워드가 가장 많은 것, 같으면 설정 순서) > default

    site는 설정에 있는 세부 카테고리일 때만 쓴다. 설정 밖의 사이트 분류(산업정책, 칼럼 이름 등)는
    키워드 규칙으로 넘긴다. 사이트가 준 원래 값은 raw_news payload에 그대로 남아 있다.
    """
    if site_category in categories:
        return site_category, "site"
    best, best_hits = None, 0
    for name, conf in categories.items():
        hits = sum(1 for keyword in conf.get("keywords", []) if keyword in title)
        if hits > best_hits:
            best, best_hits = name, hits
    if best:
        return best, "keyword"
    return default, "default"


# 기사 1건 만들기 ------------------------------------------------------

def _first(*values):
    for value in values:
        if value:
            return value
    return ""


def build_record(raws, config):
    """같은 article_key의 raw 여러 건 → (clean 레코드, None) 또는 (None, 거부 이유).

    raws: [{id, method, channel, url, article_key, payload(dict), fetched_at}, ...]
    방법(rss/crawl)마다 가장 늦게 받은 raw를 쓰고, 값이 겹치면 크롤링 값을 우선한다.
    """
    clean_conf = config["clean"]
    source_conf = config["source"]
    latest = {}
    for raw in sorted(raws, key=lambda r: (r["fetched_at"], r["id"])):
        latest[raw["method"]] = raw
    crawl = latest["crawl"]["payload"] if "crawl" in latest else {}
    rss = latest["rss"]["payload"] if "rss" in latest else {}
    rss_channel = latest["rss"]["channel"] if "rss" in latest else ""

    title = normalize_text(_first(crawl.get("title"), crawl.get("list_title"), rss.get("title")), multiline=False)

    crawl_body = normalize_text(crawl.get("body_html"))
    excerpt = _first(normalize_text(rss.get("description")), normalize_text(crawl.get("lead")))
    if len(crawl_body) >= clean_conf["min_body_chars"]:
        body, body_source = crawl_body, "crawl"
    elif excerpt:
        body, body_source = excerpt, "excerpt"
    else:
        body, body_source = "", "none"

    feed_section = source_conf["rss_feeds"].get(rss_channel, {}).get("section", "")
    site_category = normalize_text(_first(crawl.get("list_category"), crawl.get("section1")), multiline=False)
    category, category_source = decide_category(
        site_category, title, source_conf["categories"], clean_conf["default_category"])

    published_at = (normalize_datetime(crawl.get("published_time"))
                    or normalize_datetime(rss.get("pubDate"))
                    or normalize_datetime(crawl.get("list_date")))
    fetched = sorted(raw["fetched_at"] for raw in raws)

    record = {
        "article_key": raws[0]["article_key"],
        "url": _first(crawl.get("url"), rss.get("link")).strip(),
        "title": title,
        "body": body,
        "body_source": body_source,
        "section": normalize_text(_first(crawl.get("section"), feed_section), multiline=False),
        "category": category,
        "category_source": category_source,
        "author": normalize_author(_first(crawl.get("author"), rss.get("author")), clean_conf["default_author"]),
        "published_at": published_at,
        "published_date": published_at[:10] if published_at else "",
        "first_collected_at": fetched[0],
        "last_collected_at": fetched[-1],
        "methods": ",".join(sorted(latest)),
        "status": "cleaned" if body else "no_body",
    }
    missing = [field for field in REQUIRED_FIELDS if not record[field]]
    if missing:
        return None, f"필수 필드 누락: {', '.join(missing)}"
    return record, None


def merge_records(existing, new, default_author):
    """upsert 규칙: 새 값으로 갱신하되 비어 있지 않은 기존 값을 더 나쁜 값으로 덮지 않는다.

    - 본문: 새 본문의 출처 등급(crawl > excerpt > none)이 같거나 높을 때만 바꾼다.
    - 카테고리: 기존이 site이고 새 값이 site가 아니면 기존 값을 둔다.
    - 기자: 새 값이 기본값(미상)이면 기존 값을 둔다.
    - 상태: 요약된 기사(summarized·summary_failed)는 상태를 유지한다. 요약은 지우지 않는다.
    """
    merged = dict(existing)
    for field in ("url", "title", "section", "published_at", "published_date"):
        if new.get(field):
            merged[field] = new[field]
    if new["body"] and BODY_RANK[new["body_source"]] >= BODY_RANK[existing["body_source"]]:
        merged["body"], merged["body_source"] = new["body"], new["body_source"]
    if not (existing["category_source"] == "site" and new["category_source"] != "site"):
        merged["category"], merged["category_source"] = new["category"], new["category_source"]
    if new["author"] != default_author:
        merged["author"] = new["author"]
    merged["first_collected_at"] = min(existing["first_collected_at"], new["first_collected_at"])
    merged["last_collected_at"] = max(existing["last_collected_at"], new["last_collected_at"])
    merged["methods"] = ",".join(sorted(set(existing["methods"].split(",")) | set(new["methods"].split(","))))
    if existing["status"] not in SUMMARY_STATUSES:
        merged["status"] = "cleaned" if merged["body"] else "no_body"
    return merged


# DB 실행 --------------------------------------------------------------

_CLEAN_FIELDS = ("article_key", "url", "title", "body", "body_source", "section", "category", "category_source",
                 "author", "published_at", "published_date", "first_collected_at", "last_collected_at",
                 "methods", "status")


def _mark_raws(conn, ids, status, note):
    conn.executemany("UPDATE raw_news SET status = ?, note = ? WHERE id = ?", [(status, note, i) for i in ids])


def _insert_clean(conn, record, now):
    columns = _CLEAN_FIELDS + ("created_at", "updated_at")
    values = [record[f] for f in _CLEAN_FIELDS] + [now, now]
    cur = conn.execute(
        f"INSERT INTO clean_news ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", values)
    return cur.lastrowid


def _update_clean(conn, news_id, record, now):
    fields = _CLEAN_FIELDS[1:]  # article_key는 바꾸지 않는다
    assignments = ", ".join(f"{f} = ?" for f in fields)
    conn.execute(f"UPDATE clean_news SET {assignments}, updated_at = ? WHERE id = ?",
                 [record[f] for f in fields] + [now, news_id])


def _load_raws(conn, article_key):
    raws, broken = [], []
    for row in conn.execute("SELECT * FROM raw_news WHERE status = 'new' AND article_key = ? ORDER BY id",
                            (article_key,)):
        raw = dict(row)
        try:
            raw["payload"] = json.loads(raw["payload"])
        except json.JSONDecodeError:
            broken.append(raw["id"])
            continue
        raws.append(raw)
    return raws, broken


def run_clean(conn, config, policy=None, limit=None, rebuild=False):
    """raw_news(new)를 기사 단위로 정제해 clean_news에 반영하고 건수를 돌려준다."""
    policy = policy or config["clean"]["duplicate_policy"]
    if rebuild:
        conn.execute("UPDATE raw_news SET status = 'new', note = NULL")
        if policy != "upsert":
            log.info("--rebuild는 기존 기사를 새 규칙으로 갱신해야 하므로 upsert 정책으로 실행합니다")
        policy = "upsert"

    sql = "SELECT article_key FROM raw_news WHERE status = 'new' GROUP BY article_key ORDER BY MIN(id)"
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    keys = [row[0] for row in conn.execute(sql, params)]
    log.info("정제 시작: 대상 기사 %d건 (중복 정책 %s)", len(keys), policy)

    stats = Counter()
    default_author = config["clean"]["default_author"]
    for key in keys:
        raws, broken = _load_raws(conn, key)
        stats["raw_processed"] += len(raws) + len(broken)
        if broken:
            _mark_raws(conn, broken, "rejected", "payload JSON 해석 실패")
            stats["raw_rejected"] += len(broken)
            log.warning("raw payload를 읽을 수 없어 제외: id %s", broken)
        if not raws:
            continue
        ids = [raw["id"] for raw in raws]

        record, reason = build_record(raws, config)
        if record is None:
            _mark_raws(conn, ids, "rejected", reason)
            stats["raw_rejected"] += len(ids)
            log.warning("기사 제외(%s): key=%s", reason, key)
            continue
        if record["status"] == "no_body":
            log.warning("본문 없음(요약 대상에서 제외): %s", record["title"])

        now = now_str()
        merged_note = f"병합({record['methods']})" if len(ids) > 1 else ""
        existing = conn.execute("SELECT * FROM clean_news WHERE article_key = ?", (key,)).fetchone()
        if existing is None:
            news_id = _insert_clean(conn, record, now)
            _mark_raws(conn, ids, "cleaned", f"clean id={news_id} 신규 {merged_note}".strip())
            stats["inserted"] += 1
            stats["raw_cleaned"] += len(ids)
        elif policy == "skip":
            _mark_raws(conn, ids, "duplicate", f"이미 있음(clean id={existing['id']}), skip")
            stats["skipped"] += 1
            stats["raw_duplicate"] += len(ids)
            log.warning("중복이라 건너뜀(skip): %s", record["title"])
        else:
            merged = merge_records(dict(existing), record, default_author)
            _update_clean(conn, existing["id"], merged, now)
            _mark_raws(conn, ids, "cleaned", f"clean id={existing['id']} upsert 갱신 {merged_note}".strip())
            stats["updated"] += 1
            stats["raw_cleaned"] += len(ids)
    conn.commit()
    return stats
