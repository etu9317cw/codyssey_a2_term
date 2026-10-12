"""테스트 공용: 설정, 샘플 파일 경로, 가짜 HTTP 세션."""
import copy
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

with (ROOT / "config.json").open(encoding="utf-8") as _f:
    _CONFIG = json.load(_f)


def config():
    """테스트마다 바꿔도 되도록 설정 사본을 돌려준다."""
    return copy.deepcopy(_CONFIG)


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code=200, content=b"", encoding="utf-8"):
        self.status_code = status_code
        self.content = content
        self.encoding = encoding
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class FakeSession:
    """주소별로 미리 정한 응답(또는 예외)을 차례로 돌려준다. 정하지 않은 주소는 404."""

    def __init__(self, routes):
        self.routes = {url: list(items) for url, items in routes.items()}
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        queue = self.routes.get(url)
        if not queue:
            return FakeResponse(404)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    """time.sleep·time.monotonic 대용. 실제로 기다리지 않고 잔 시간을 기록한다."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def monotonic(self):
        return self.now


TIMEOUT = requests.Timeout("timeout")


# AI API 가짜 응답 -------------------------------------------------------

AI_VALUES = {"api_key": "test-key", "base_url": "https://ai.example", "model": "test-model"}


class FakeAIResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("JSON 아님")
        return self._payload


def ai_reply(content):
    """OpenAI 호환 /chat/completions 정상 응답."""
    return FakeAIResponse(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


class FakeAISession:
    """post 호출마다 미리 정한 응답(또는 예외)을 차례로 돌려주고, 보낸 본문을 기록한다."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.headers = {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self.replies:
            raise AssertionError("준비한 응답보다 많이 호출함")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def user_prompt(self, index):
        return self.calls[index]["json"]["messages"][-1]["content"]


def add_clean_news(conn, key, title="제목", body="본문 문장입니다. " * 10, category="전력", status="cleaned",
                   published_at="2026-10-08 09:00:00", author="미상", body_source=None,
                   collected_at="2026-10-10 10:00:00"):
    """요약·분석·출력 테스트용 clean_news 1건을 바로 넣고 id를 돌려준다."""
    cur = conn.execute(
        "INSERT INTO clean_news (article_key, url, title, body, body_source, section, category, category_source, "
        "author, published_at, published_date, first_collected_at, last_collected_at, methods, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, '에너지', ?, 'site', ?, ?, ?, ?, ?, 'crawl', ?, ?, ?)",
        (key, f"https://www.electimes.com/news/articleView.html?idxno={key}", title, body,
         body_source or ("crawl" if body else "none"), category, author, published_at, published_at[:10],
         collected_at, collected_at, status, collected_at, collected_at))
    conn.commit()
    return cur.lastrowid


def add_summary(conn, news_id, summary="요약 문장.", keywords=(), input_chars=1000, output_chars=100):
    """출력 테스트용 summaries 1행. 같은 기사에 여러 번 넣으면 마지막 행이 최신 요약이다."""
    conn.execute("INSERT INTO summaries (news_id, summary, keywords, model, input_chars, output_chars, created_at) "
                 "VALUES (?, ?, ?, 'test-model', ?, ?, '2026-10-10 11:00:00')",
                 (news_id, summary, json.dumps(list(keywords), ensure_ascii=False), input_chars, output_chars))
    conn.commit()
