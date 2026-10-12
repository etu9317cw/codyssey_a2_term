"""[공통] SQLite 연결과 스키마.

역할 사이의 약속은 이 테이블들이다(설계서 3절).
- 역할 1: raw_news, fetch_log, clean_news를 채운다.
- 역할 2: clean_news를 읽어 summaries, analyses, ai_errors를 채운다.
- 역할 3: 읽기만 한다.
날짜는 모두 한국 시간 'YYYY-MM-DD HH:MM:SS' 문자열로 저장한다.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    method        TEXT NOT NULL CHECK (method IN ('rss', 'crawl')),
    channel       TEXT NOT NULL,
    url           TEXT NOT NULL,
    article_key   TEXT NOT NULL,
    payload       TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'cleaned', 'rejected', 'duplicate')),
    note          TEXT,
    UNIQUE (method, article_key, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_news (status);
CREATE INDEX IF NOT EXISTS idx_raw_key ON raw_news (article_key);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at     TEXT NOT NULL,
    method     TEXT NOT NULL,
    channel    TEXT NOT NULL,
    requested  INTEGER NOT NULL,
    succeeded  INTEGER NOT NULL,
    failed     INTEGER NOT NULL,
    saved      INTEGER NOT NULL,
    message    TEXT
);

CREATE TABLE IF NOT EXISTS clean_news (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    article_key         TEXT NOT NULL UNIQUE,
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    body_source         TEXT NOT NULL CHECK (body_source IN ('crawl', 'excerpt', 'none')),
    section             TEXT,
    category            TEXT NOT NULL,
    category_source     TEXT NOT NULL CHECK (category_source IN ('site', 'keyword', 'default')),
    author              TEXT NOT NULL,
    published_at        TEXT NOT NULL,
    published_date      TEXT NOT NULL,
    first_collected_at  TEXT NOT NULL,
    last_collected_at   TEXT NOT NULL,
    methods             TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('cleaned', 'summarized', 'summary_failed', 'no_body')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clean_date ON clean_news (published_date);
CREATE INDEX IF NOT EXISTS idx_clean_category ON clean_news (category);
CREATE INDEX IF NOT EXISTS idx_clean_status ON clean_news (status);

CREATE TABLE IF NOT EXISTS summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id       INTEGER NOT NULL REFERENCES clean_news (id),
    summary       TEXT NOT NULL,
    keywords      TEXT NOT NULL DEFAULT '[]',
    model         TEXT NOT NULL,
    input_chars   INTEGER NOT NULL,
    output_chars  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_news ON summaries (news_id);

CREATE TABLE IF NOT EXISTS ai_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL CHECK (task IN ('summarize', 'analyze')),
    target      TEXT NOT NULL,
    error       TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date_from      TEXT,
    date_to        TEXT,
    category       TEXT,
    article_count  INTEGER NOT NULL,
    article_ids    TEXT NOT NULL,
    items          TEXT NOT NULL,
    result         TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


def now_str():
    """현재 한국 시간 문자열."""
    return datetime.now(KST).strftime(DATETIME_FORMAT)


def connect(db_path):
    """DB 파일을 열고(없으면 만들고) 스키마를 준비한다. 실패하면 sqlite3.Error."""
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
