"""[역할 3] report: 차트 PNG + 리포트(MD/TXT). 콘솔에는 TXT 형식으로 출력하고 파일로도 저장한다.

리포트 구성(설계서 7.2): 1 조건과 생성 시각, 2 품질 지표, 3 TOP N, 4 AI 인사이트, 5 차트 파일
- AI 인사이트는 지정한 분석(--analysis-id), 없으면 조건(기간·카테고리)이 같은 최신 분석을 싣는다.
  같은 조건의 분석이 없으면 AI를 부르지 않고 analyze 실행 방법을 적는다(리포트는 API 없이 만든다).
- 기사 본문은 싣지 않는다(저작권). 제목·링크·AI 요약·분석만 싣는다.
"""
import json
import logging
import os
from datetime import datetime

from news_cli.db import KST, now_str
from news_cli.output import charts, stats
from news_cli.output.common import Filters, select_news, shorten, text_table

log = logging.getLogger(__name__)

ITEM_LABELS = {"trends": "주요 트렌드", "keywords": "핵심 키워드", "common_diff": "공통점·차이점", "implications": "시사점"}
TOP_LABELS = (("categories", "카테고리", "기사 수"), ("keywords", "키워드", "기사 수"), ("authors", "기자", "기사 수"))
SCOPE_NOTE = "수집 성공률·정제 통과율·중복 비율은 fetch·clean 전체 실행 기준, 나머지는 조건에 맞는 기사 기준"


# 분석 고르기 -----------------------------------------------------------

def _decode(row):
    analysis = dict(row)
    for field in ("article_ids", "items", "result"):
        analysis[field] = json.loads(analysis[field])
    return analysis


def load_analysis(conn, analysis_id):
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return _decode(row) if row else None


def _same_conditions(analysis, filters):
    categories = set(analysis["category"].split(",")) if analysis["category"] else set()
    return (analysis["date_from"] == filters.date_from and analysis["date_to"] == filters.date_to
            and categories == set(filters.categories or []))


def matching_analysis(conn, filters):
    """조건(기간·카테고리)이 같은 분석 중 가장 최근 것. 없으면 None."""
    for row in conn.execute("SELECT * FROM analyses ORDER BY id DESC"):
        analysis = _decode(row)
        if _same_conditions(analysis, filters):
            return analysis
    return None


def analysis_filters(analysis):
    categories = analysis["category"].split(",") if analysis["category"] else None
    return Filters(analysis["date_from"], analysis["date_to"], categories)


# 리포트 데이터 ---------------------------------------------------------

def build_report(conn, config, filters, top, charts_dir, stamp, analysis=None):
    """리포트에 들어갈 값을 모으고 차트 PNG를 만든다. 조건에 맞는 기사가 없으면 None."""
    articles = select_news(conn, Filters(filters.date_from, filters.date_to, filters.categories))
    if not articles:
        return None

    note = None
    if analysis is None:
        analysis = matching_analysis(conn, filters)
        if analysis is None:
            options = "".join(f" --{name} {value}" for name, value in (
                ("date-from", filters.date_from), ("date-to", filters.date_to),
                ("category", ",".join(filters.categories or []))) if value)
            # 명령 끝 글자에 따라 조사가 달라지므로 명령 바로 뒤에는 조사를 붙이지 않는다
            note = (f"이 조건의 분석이 없습니다. `python main.py analyze{options}` 실행 후 리포트를 다시 만들거나, "
                    "저장된 분석을 `--analysis-id`로 지정하세요 (목록: `python main.py analyze --history`).")
            log.warning("이 조건의 AI 분석이 없어 리포트에 분석을 싣지 못했습니다 (analyze 먼저 실행 또는 --analysis-id)")
    elif not _same_conditions(analysis, filters):
        note = f"지정한 분석의 조건({analysis_filters(analysis).describe()})이 리포트 조건과 다릅니다."
        log.warning("분석 #%d의 조건이 리포트 조건과 다릅니다", analysis["id"])

    evidence = {}
    if analysis and "trends" in analysis["result"]:
        ids = sorted({i for trend in analysis["result"]["trends"] for i in trend["article_ids"]})
        if ids:
            rows = conn.execute(f"SELECT id, title, url FROM clean_news WHERE id IN ({', '.join('?' * len(ids))})", ids)
            evidence = {row["id"]: dict(row) for row in rows}

    charts.setup_korean_font(config["output"]["chart_fonts"])
    subtitle = filters.describe() if (filters.date_from or filters.date_to or filters.categories) else ""
    chart_files = [
        ("카테고리별 뉴스 수", charts.category_chart(stats.category_counts(articles),
                                                 charts_dir / f"report_{stamp}_category.png", subtitle)),
        ("일자별 수집 추이", charts.daily_chart(stats.daily_counts(articles),
                                               charts_dir / f"report_{stamp}_daily.png", subtitle)),
        ("요약 상태", charts.status_chart(stats.status_counts(articles),
                                         charts_dir / f"report_{stamp}_status.png", subtitle)),
    ]
    return {
        "generated_at": now_str(),
        "source": config["source"],
        "filters": filters,
        "article_count": len(articles),
        "date_span": stats.date_span(articles),
        "metrics": stats.quality_metrics(conn, filters),
        "top_n": top,
        "top": stats.top_n(articles, top, config["clean"]["default_author"]),
        "analysis": analysis,
        "analysis_note": note,
        "evidence": evidence,
        "charts": chart_files,
    }


def _header_lines(data):
    first, last = data["date_span"]
    source = data["source"]
    return [f"생성 시각: {data['generated_at']} (한국 시간)",
            f"조건: {data['filters'].describe()}",
            f"대상 기사: {data['article_count']}건 (발행일 {first} ~ {last})",
            f"출처: {source['label']} ({source['base_url']}) — 기사 본문은 싣지 않고 제목·링크·AI 요약·분석만 싣는다"]


def _analysis_title(analysis):
    return (f"분석 #{analysis['id']} ({analysis['created_at']}, {analysis['model']}, "
            f"기사 {analysis['article_count']}건, 조건: {analysis_filters(analysis).describe()})")


# MD --------------------------------------------------------------------

def _md_table(headers, rows):
    def cell(value):
        return str(value).replace("|", "\\|")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return lines


def _relative(path, base_dir):
    try:
        return os.path.relpath(path, base_dir).replace("\\", "/")
    except ValueError:  # Windows에서 드라이브가 다른 경우
        return path.as_posix()


def render_md(data, report_path):
    lines = ["# 에너지 뉴스 트렌드 리포트", "", "## 1. 조건과 생성 시각", ""]
    lines.extend(f"- {line}" for line in _header_lines(data))

    lines += ["", "## 2. 품질 지표", ""]
    lines += _md_table(["지표", "값", "계산", "기준"],
                       [(m["label"], stats.format_value(m), m["formula"], m["scope"]) for m in data["metrics"]])
    lines += ["", f"- {SCOPE_NOTE}"]

    lines += ["", f"## 3. TOP {data['top_n']}"]
    for key, label, unit in TOP_LABELS:
        title = f"{label} TOP {data['top_n']}" + (" (기사별 최신 요약의 키워드, 기사당 1회)" if key == "keywords" else "")
        lines += ["", f"### {title}", ""]
        rows = data["top"][key]
        lines += _md_table(["순위", label, unit], [(n, name, count) for n, (name, count) in enumerate(rows, 1)]) \
            if rows else ["(자료 없음)"]

    lines += ["", "## 4. AI 인사이트", ""]
    analysis = data["analysis"]
    if data["analysis_note"]:
        lines += [f"> {data['analysis_note']}", ""]
    if analysis:
        lines += [f"- {_analysis_title(analysis)}"]
        result = analysis["result"]
        for item in analysis["items"]:
            lines += ["", f"### {ITEM_LABELS[item]}", ""]
            value = result[item]
            if item == "trends":
                for n, trend in enumerate(value, 1):
                    lines.append(f"{n}. **{trend['title']}** — {trend['description']}")
                    for news_id in trend["article_ids"]:
                        article = data["evidence"].get(news_id)
                        lines.append(f"   - 근거 [{news_id}] " + (f"[{article['title']}]({article['url']})"
                                                                 if article else "(삭제된 기사)"))
            elif item == "keywords":
                lines.extend(f"{n}. **{e['keyword']}**" + (f" — {e['reason']}" if e["reason"] else "")
                             for n, e in enumerate(value, 1))
            elif item == "common_diff":
                lines.append("**공통점**")
                lines.extend(f"- {text}" for text in value["common"])
                lines += ["", "**차이점**"]
                lines.extend(f"- {text}" for text in value["differences"])
            else:
                lines.extend(f"{n}. {text}" for n, text in enumerate(value, 1))

    lines += ["", "## 5. 차트", ""]
    for label, path in data["charts"]:
        lines += [f"### {label}", "", f"![{label}]({_relative(path, report_path.parent)})", ""]
    return "\n".join(lines).rstrip() + "\n"


# TXT -------------------------------------------------------------------

def render_txt(data, report_path=None):
    rule = "=" * 60
    lines = [rule, " 에너지 뉴스 트렌드 리포트", rule, "[1] 조건과 생성 시각"]
    lines += ["  " + line for line in _header_lines(data)]

    lines += ["", "[2] 품질 지표"]
    lines += ["  " + line for line in text_table(
        ["지표", "값", "기준"], [(m["label"], stats.format_value(m), m["scope"]) for m in data["metrics"]],
        ["l", "r", "l"])]
    lines += ["  * " + SCOPE_NOTE]
    lines += [f"  * {m['label']} = {m['formula']}" for m in data["metrics"]]

    lines += ["", f"[3] TOP {data['top_n']}"]
    for key, label, unit in TOP_LABELS:
        rows = data["top"][key]
        lines.append(f"  - {label}: " + (", ".join(f"{name}({count})" for name, count in rows) if rows else "자료 없음"))

    lines += ["", "[4] AI 인사이트"]
    analysis = data["analysis"]
    if data["analysis_note"]:
        lines.append("  ! " + data["analysis_note"])
    if analysis:
        lines.append("  " + _analysis_title(analysis))
        result = analysis["result"]
        for item in analysis["items"]:
            lines.append(f"  <{ITEM_LABELS[item]}>")
            value = result[item]
            if item == "trends":
                for n, trend in enumerate(value, 1):
                    lines.append(f"   {n}. {trend['title']} — {trend['description']}")
                    for news_id in trend["article_ids"]:
                        article = data["evidence"].get(news_id)
                        lines.append(f"      근거 [{news_id}] " + (shorten(article["title"], 50) if article else "(삭제된 기사)"))
            elif item == "keywords":
                lines.extend(f"   {n}. {e['keyword']}" + (f" — {e['reason']}" if e["reason"] else "")
                             for n, e in enumerate(value, 1))
            elif item == "common_diff":
                lines.append("   공통점")
                lines.extend(f"    - {text}" for text in value["common"])
                lines.append("   차이점")
                lines.extend(f"    - {text}" for text in value["differences"])
            else:
                lines.extend(f"   {n}. {text}" for n, text in enumerate(value, 1))

    lines += ["", "[5] 차트"]
    lines.extend(f"  - {label}: {path}" for label, path in data["charts"])
    if report_path:
        lines += ["", f"리포트 파일: {report_path}"]
    return "\n".join(lines) + "\n"


# 실행 ------------------------------------------------------------------

def run_report(conn, config, filters, top, fmt, reports_dir, charts_dir, analysis=None, output=None):
    """리포트를 만들어 파일로 저장하고 (콘솔용 TXT, 파일 경로)를 돌려준다. 조건에 맞는 기사가 없으면 None."""
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    data = build_report(conn, config, filters, top, charts_dir, stamp, analysis)
    if data is None:
        log.warning("조건에 맞는 기사가 없습니다 (%s)", filters.describe())
        return None
    report_path = output or reports_dir / f"report_{stamp}.{fmt}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_md(data, report_path) if fmt == "md" else render_txt(data, report_path)
    report_path.write_text(text, encoding="utf-8")
    log.info("리포트 저장: %s (차트 %d개: %s)", report_path, len(data["charts"]), charts_dir)
    return render_txt(data, report_path), report_path
