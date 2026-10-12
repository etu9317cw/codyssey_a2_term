"""[역할 1] HTTP 요청: 타임아웃, 요청 간 지연, 재시도, robots.txt 확인.

파일 이름을 http.py로 하지 않은 것은 표준 라이브러리 http 모듈과 이름이 겹치지 않게 하려는 것이다.

정책(설계서 9절)
- 모든 요청에 타임아웃을 건다.
- 요청 사이에 request_delay_sec 이상 쉰다. robots.txt의 Crawl-delay가 더 길면 그것을 따른다.
- 타임아웃·연결 실패·429·5xx는 max_retries번까지 다시 시도한다(retry_backoff_sec만큼 대기).
- 404·403 등 나머지 4xx는 다시 시도해도 결과가 같으므로 바로 실패로 기록한다.
- robots.txt가 금지한 주소는 요청하지 않는다. robots.txt를 읽지 못하면(5xx·연결 실패) 이번 실행에서는 아무 요청도 하지 않는다.
- 실패해도 예외를 던지지 않고 FetchResult(ok=False)를 돌려준다. 호출한 쪽은 기록하고 다음 기사로 넘어간다.
"""
import logging
import time
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    content: bytes = b""
    text: str = ""
    error: str = ""


class HttpClient:
    def __init__(self, http_conf, robots_url, session=None, sleep=time.sleep, clock=time.monotonic):
        self.timeout = http_conf["timeout_sec"]
        self.delay = float(http_conf["request_delay_sec"])
        self.max_retries = int(http_conf["max_retries"])
        self.backoff = list(http_conf["retry_backoff_sec"]) or [0]
        self.user_agent = http_conf["user_agent"]
        self.robots_url = robots_url
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self._robots = None
        self.request_count = 0

    # 요청 간 지연 -------------------------------------------------------
    def _wait_turn(self):
        if self._last_request is None:
            return
        remain = self.delay - (self._clock() - self._last_request)
        if remain > 0:
            self._sleep(remain)

    def _send(self, url):
        self._wait_turn()
        try:
            return self.session.get(url, timeout=self.timeout)
        finally:
            self._last_request = self._clock()
            self.request_count += 1

    # robots.txt --------------------------------------------------------
    def _robot_parser(self):
        if self._robots is not None:
            return self._robots
        parser = RobotFileParser(self.robots_url)
        try:
            resp = self._send(self.robots_url)
        except requests.RequestException as e:
            log.warning("robots.txt를 읽지 못해 이번 실행에서는 요청하지 않습니다: %s", type(e).__name__)
            parser.disallow_all = True
        else:
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                parser.disallow_all = True
                log.warning("robots.txt 접근이 막혀 있어(HTTP %s) 요청하지 않습니다", resp.status_code)
            elif 400 <= resp.status_code < 500:
                parser.allow_all = True
            else:
                parser.disallow_all = True
                log.warning("robots.txt 응답 오류(HTTP %s)라 이번 실행에서는 요청하지 않습니다", resp.status_code)
        crawl_delay = None if (parser.allow_all or parser.disallow_all) else parser.crawl_delay(self.user_agent)
        if crawl_delay and float(crawl_delay) > self.delay:
            log.info("robots.txt Crawl-delay %s초를 따릅니다", crawl_delay)
            self.delay = float(crawl_delay)
        self._robots = parser
        return parser

    def allowed(self, url):
        return self._robot_parser().can_fetch(self.user_agent, url)

    # 요청 --------------------------------------------------------------
    def get(self, url):
        if not self.allowed(url):
            log.warning("robots.txt가 허용하지 않는 주소라 요청하지 않습니다: %s", url)
            return FetchResult(url, False, error="robots.txt 금지")

        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            status = None
            try:
                resp = self._send(url)
            except requests.Timeout:
                error, retry = f"타임아웃({self.timeout}초)", True
            except requests.ConnectionError:
                error, retry = "연결 실패", True
            except requests.RequestException as e:
                error, retry = f"요청 오류({type(e).__name__})", False
            else:
                status = resp.status_code
                if status == 200:
                    return FetchResult(url, True, status, resp.content, _decode(resp))
                error, retry = f"HTTP {status}", status in RETRY_STATUS

            if not retry or attempt == attempts:
                log.warning("요청 실패: %s (%s, %d회 시도)", url, error, attempt)
                return FetchResult(url, False, status, error=error)
            wait = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
            log.warning("요청 실패, %s초 뒤 다시 시도(%d/%d): %s (%s)", wait, attempt, self.max_retries, url, error)
            self._sleep(wait)


def _decode(resp):
    """헤더에 문자셋이 없으면 내용으로 추정한다(한글 깨짐 방지)."""
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text
