"""[역할 1] fetch 실행: RSS 수집 + 세부 카테고리 크롤링 → raw_news, fetch_log.

- raw_news에는 받은 값을 그대로(JSON) 수집 시각·소스·방법·채널과 함께 저장한다.
  UNIQUE(method, article_key, payload_hash)라 같은 내용은 다시 쌓이지 않는다.
- 본문을 이미 받은 기사(raw에 crawl이 있는 기사)는 다시 요청하지 않는다(--recrawl로 해제).
- 요청 하나가 실패해도 기록하고 다음으로 넘어간다. 채널(피드·카테고리)마다 fetch_log에 건수를 남긴다.
- since(YYYY-MM-DD)를 주면 그 날 이후 기사만 받는다. 목록은 최신순이라 더 오래된 기사가 보이면 다음 쪽을 보지 않는다.
  limit·pages 상한 때문에 기간 안 기사를 다 받지 못했을 수 있으면 WARNING을 남긴다.
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field

from news_cli.collect import crawler, rss
from news_cli.collect.cleaner import extract_article_key, normalize_datetime
from news_cli.db import now_str

log = logging.getLogger(__name__)


def on_or_after(value, since):
    """피드·목록의 날짜가 since(YYYY-MM-DD) 이후인지. since가 없거나 날짜를 읽을 수 없으면 True(정제 단계에서 다시 본다)."""
    if not since:
        return True
    normalized = normalize_datetime(value)
    return normalized is None or normalized[:10] >= since


@dataclass
class ChannelStats:
    method: str          # rss | crawl
    channel: str         # 피드 이름 또는 세부 카테고리
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    saved: int = 0       # 새로 저장한 raw 수
    found: int = 0       # 피드·목록에서 찾은 기사 수
    skipped: int = 0     # 이미 수집한 기사(RSS: 같은 내용이 raw에 있음, 크롤링: 본문을 이미 받음)
    errors: list = field(default_factory=list)

    def count(self, result):
        self.requested += 1
        if result.ok:
            self.succeeded += 1
        else:
            self.failed += 1
            self.errors.append(f"{result.url} ({result.error})")

    def mark_failed(self, url, error):
        """응답은 받았지만 내용을 쓸 수 없는 경우(예: XML 파싱 실패)."""
        self.succeeded -= 1
        self.failed += 1
        self.errors.append(f"{url} ({error})")


def save_raw(conn, source, method, channel, url, article_key, payload, fetched_at):
    """raw_news에 저장. 새로 저장했으면 True, 같은 내용이 이미 있으면 False."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_news (source, method, channel, url, article_key, payload, payload_hash, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, method, channel, url, article_key or "h" + digest[:16], text, digest, fetched_at))
    return cur.rowcount == 1


class Fetcher:
    def __init__(self, conn, config, client, recrawl=False):
        self.conn = conn
        self.client = client
        self.source = config["source"]
        self.base_url = self.source["base_url"].rstrip("/")
        self.crawled = set() if recrawl else {
            row[0] for row in conn.execute("SELECT DISTINCT article_key FROM raw_news WHERE method = 'crawl'")}
        self.run_at = now_str()

    def run(self, method, feeds, categories, limit, pages, since=None):
        """method: rss | crawl | all. since: 이 날짜(YYYY-MM-DD) 이후 기사만. 채널별 ChannelStats 목록을 돌려준다."""
        results = []
        if method in ("rss", "all"):
            for feed in feeds:
                results.extend(self._fetch_feed(feed, limit, since, with_body=(method == "all")))
        if method in ("crawl", "all"):
            for category in categories:
                results.append(self._crawl_category(category, limit, pages, since))
        return results

    # RSS -----------------------------------------------------------------
    def _fetch_feed(self, feed, limit, since, with_body):
        url = self.base_url + self.source["rss_feeds"][feed]["path"]
        stats = ChannelStats("rss", feed)
        result = self.client.get(url)
        stats.count(result)
        items = []
        if result.ok:
            try:
                items = [item for item in rss.parse_rss(result.content) if on_or_after(item["pubDate"], since)]
            except rss.RssParseError as e:
                log.error("RSS 피드 %s 해석 실패, 이 피드는 건너뜁니다: %s", feed, e)
                stats.mark_failed(url, str(e))
        if since and len(items) > limit:
            log.warning("[RSS] %s: %s 이후 기사 %d건 중 --limit %d건만 받습니다", feed, since, len(items), limit)
        items = items[:limit]
        stats.found = len(items)

        fetched_at = now_str()
        for item in items:
            key = extract_article_key(item["link"])
            if save_raw(self.conn, self.source["name"], "rss", feed, item["link"] or url, key, item, fetched_at):
                stats.saved += 1
        stats.skipped = stats.found - stats.saved  # 같은 내용이 이미 raw에 있음
        self._finish(stats)
        if not with_body:
            return [stats]

        body_stats = ChannelStats("crawl", feed)
        for item in items:
            key = extract_article_key(item["link"])
            if key and key.isdigit():
                body_stats.found += 1
                self._crawl_article(key, body_stats, feed, extra={})
        self._finish(body_stats)
        return [stats, body_stats]

    # 크롤링 ---------------------------------------------------------------
    def _crawl_category(self, name, limit, pages, since=None):
        code = self.source["categories"][name]["code"]
        stats = ChannelStats("crawl", name)
        entries = []
        reached_older = False  # since보다 오래된 기사를 봤으면 기간 안 기사는 다 본 것
        for page in range(1, pages + 1):
            url = self.base_url + self.source["list_path"].format(code=code, page=page)
            result = self.client.get(url)
            stats.count(result)
            if not result.ok:
                break
            page_entries = crawler.parse_list(result.text)
            if not page_entries:
                log.warning("목록에서 기사를 찾지 못했습니다(카테고리 코드나 페이지 구조 확인 필요): %s", url)
                break
            in_range = [e for e in page_entries if on_or_after(e["date"], since)]
            known = {e["article_id"] for e in entries}
            entries.extend(e for e in in_range if e["article_id"] not in known)
            if len(in_range) < len(page_entries):
                reached_older = True
                break
            if len(entries) >= limit:
                break
        if since and not (reached_older and len(entries) <= limit):
            log.warning("[크롤링] %s: %s 이후 기사를 끝까지 받지 못했을 수 있습니다(--limit %d, --pages %d 상한)",
                        name, since, limit, pages)
        entries = entries[:limit]
        stats.found = len(entries)

        for entry in entries:
            self._crawl_article(entry["article_id"], stats, name, extra={
                "list_category": name,
                "list_title": entry["title"],
                "list_date": entry["date"],
                "lead": entry["lead"],
            })
        self._finish(stats)
        return stats

    def _crawl_article(self, article_id, stats, channel, extra):
        if article_id in self.crawled:
            stats.skipped += 1
            log.debug("이미 본문을 받은 기사라 건너뜀: %s", article_id)
            return
        url = self.base_url + self.source["article_path"].format(id=article_id)
        result = self.client.get(url)
        stats.count(result)
        if not result.ok:
            return
        payload = crawler.parse_article(result.text)
        payload.update(extra, url=url, article_id=article_id)
        if not payload["body_html"]:
            log.warning("기사 본문을 찾지 못했습니다(페이지 구조 변경 가능): %s", url)
        if save_raw(self.conn, self.source["name"], "crawl", channel, url, article_id, payload, now_str()):
            stats.saved += 1
        self.crawled.add(article_id)

    # 기록 -----------------------------------------------------------------
    def _finish(self, stats):
        message = "; ".join(stats.errors[:3]) + (f" 외 {len(stats.errors) - 3}건" if len(stats.errors) > 3 else "")
        self.conn.execute(
            "INSERT INTO fetch_log (run_at, method, channel, requested, succeeded, failed, saved, message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_at, stats.method, stats.channel, stats.requested, stats.succeeded, stats.failed,
             stats.saved, message or None))
        self.conn.commit()
        label = "RSS" if stats.method == "rss" else "크롤링"
        log.info("[%s] %s: 기사 %d건, 요청 %d회(성공 %d, 실패 %d), 신규 저장 %d건, 이미 수집 %d건",
                 label, stats.channel, stats.found, stats.requested, stats.succeeded, stats.failed,
                 stats.saved, stats.skipped)
