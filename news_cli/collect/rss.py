"""[역할 1] RSS 수집(방법 1): 피드 XML에서 기사 항목을 꺼낸다.

값은 받은 그대로(앞뒤 공백만 제거) 돌려준다. 태그·엔티티 정리와 날짜 통일은 정제 단계에서 한다.
"""
import xml.etree.ElementTree as ET

DC_NS = "{http://purl.org/dc/elements/1.1/}"


class RssParseError(Exception):
    """RSS XML을 해석할 수 없음."""


def _text(item, tag):
    return (item.findtext(tag) or "").strip()


def parse_rss(content):
    """RSS 2.0 XML(bytes 또는 str) → [{title, link, description, author, pubDate}, ...]"""
    if isinstance(content, bytes):
        content = content.lstrip()
    else:
        content = content.strip()
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise RssParseError(f"XML 형식 오류: {e}") from e
    channel = root.find("channel")
    if channel is None:
        raise RssParseError("channel 요소가 없습니다")
    return [
        {
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "description": _text(item, "description"),
            "author": _text(item, "author") or _text(item, f"{DC_NS}creator"),
            "pubDate": _text(item, "pubDate"),
        }
        for item in channel.findall("item")
    ]
