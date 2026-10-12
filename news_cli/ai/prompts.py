"""[역할 2] 프롬프트: 기사 요약과 인사이트 분석.

기관 API가 JSON 모드를 지원하지 않으므로 출력 형식을 예시로 보여 주고 JSON 객체 하나만 쓰게 한다.
max_tokens를 보내지 않으므로 길이 제한도 프롬프트에 적는다.
환각을 줄이려고 "주어진 기사에 없는 사실은 쓰지 않는다"를 시스템·사용자 프롬프트 양쪽에 둔다.
"""

SUMMARY_SYSTEM = (
    "당신은 에너지 산업 뉴스를 정리하는 편집자입니다. 주어진 기사 내용만 근거로 요약하고 추측이나 평가는 덧붙이지 않습니다. "
    "반드시 JSON 객체 하나만 출력하고 설명·인사·코드펜스는 쓰지 않습니다."
)

_SUMMARY_USER = """다음 기사를 요약하세요.

제목: {title}
본문:
{body}

출력 형식:
{{"summary": "요약", "keywords": ["키워드1", "키워드2", "키워드3"]}}

규칙:
- summary: 한국어 3문장 이내, 300자 이내. 기사에 나온 수치·기관명·지명은 그대로 쓰고 기사에 없는 내용은 넣지 않습니다.
- keywords: 기사의 핵심 명사 3~5개. 고유명사·기술 용어를 우선하고 '에너지', '정부'처럼 너무 일반적인 말은 피합니다."""


def summary_user(title, body):
    return _SUMMARY_USER.format(title=title, body=body)


ANALYZE_SYSTEM = (
    "당신은 에너지 산업 동향 분석가입니다. 주어진 기사 목록만 근거로 분석하고 목록에 없는 사실·수치는 만들지 않습니다. "
    "반드시 JSON 객체 하나만 출력하고 설명·인사·코드펜스는 쓰지 않습니다."
)

# 분석 항목: (이름, 출력 형식 한 줄, 작성 규칙)
ANALYSIS_ITEMS = {
    "trends": (
        "주요 트렌드",
        '"trends": [{"title": "트렌드 이름", "description": "설명 1~2문장", "article_ids": [기사 ID 숫자]}]',
        "주요 트렌드 3~5개(기사가 적으면 그보다 적게). article_ids에는 근거가 된 기사의 ID(목록의 [ID] 숫자)를 1개 이상 넣습니다.",
    ),
    "keywords": (
        "핵심 키워드",
        '"keywords": [{"keyword": "키워드", "reason": "중요한 이유 한 문장"}]',
        "핵심 키워드 5~10개를 중요한 순서로. 여러 기사에 걸쳐 나오는 말을 우선합니다.",
    ),
    "common_diff": (
        "공통점·차이점",
        '"common_diff": {"common": ["공통점 1문장"], "differences": ["차이점 1문장"]}',
        "카테고리 사이의 공통점 2~3개와 차이점 2~3개. 카테고리가 하나뿐이면 기사들 사이를 비교합니다.",
    ),
    "implications": (
        "시사점",
        '"implications": ["시사점 1~2문장"]',
        "업계·정책 관점의 시사점 2~3개.",
    ),
}

_ANALYZE_USER = """아래 에너지 뉴스 {count}건을 종합 분석하세요.

분석 조건: 기간 {period}, 카테고리 {category}
기사 목록 (형식: [ID] 발행일 | 카테고리 | 제목 / 내용):
{articles}

출력 형식 (아래 키만 담은 JSON 객체 하나):
{{
{fields}
}}

규칙:
{rules}
- 모든 문장은 한국어로 쓰고, 기사 목록에 없는 사실·수치는 쓰지 않습니다."""


def format_article(article, excerpt_chars):
    """분석 입력 1건: [ID] 날짜 | 카테고리 | 제목 / 요약(없으면 본문 앞부분)"""
    content = article["summary"] or article["body"][:excerpt_chars]
    content = " ".join(content.split())[:excerpt_chars]
    return f"[{article['id']}] {article['published_date']} | {article['category']} | {article['title']}\n    {content}"


def analyze_user(articles, items, period, category, excerpt_chars):
    fields = ",\n".join("  " + ANALYSIS_ITEMS[item][1] for item in items)
    rules = "\n".join(f"- {item}: {ANALYSIS_ITEMS[item][2]}" for item in items)
    lines = "\n".join(format_article(article, excerpt_chars) for article in articles)
    return _ANALYZE_USER.format(count=len(articles), period=period, category=category,
                                articles=lines, fields=fields, rules=rules)
