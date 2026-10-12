"""[공통] 명령줄 진입점: argparse 서브커맨드 → 각 모듈 호출.

공통 옵션(--config, --db, -v)은 모든 서브커맨드 뒤에 붙일 수 있다.
종료 코드: 0 성공, 1 실행 오류(설정·DB·전체 실패), 2 잘못된 옵션, 130 사용자 중단
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from news_cli import db
from news_cli.ai import analyzer
from news_cli.ai.client import AIAuthError, AIClient
from news_cli.ai.summarizer import run_summarize
from news_cli.collect.cleaner import run_clean
from news_cli.collect.fetcher import Fetcher
from news_cli.collect.http_client import HttpClient
from news_cli.logger import setup_logging
from news_cli.output import browse, exporter, report
from news_cli.output.common import STATUSES, Filters
from news_cli.settings import ConfigError, ai_settings, load_config, resolve_path

log = logging.getLogger("news_cli.cli")


class UsageError(Exception):
    """옵션 값이 설정과 맞지 않음."""


def _positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {value}")
    if number < 1:
        raise argparse.ArgumentTypeError(f"1 이상이어야 합니다: {value}")
    return number


def _date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value}")


def _pick(value, available, label):
    """'all' 또는 쉼표로 나열한 이름 → 이름 목록. 설정에 없는 이름이면 UsageError."""
    if value == "all":
        return list(available)
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in available]
    if unknown or not names:
        raise UsageError(f"알 수 없는 {label}: {', '.join(unknown) or value} (사용 가능: all, {', '.join(available)})")
    return names


def _filters(args, config, status=None, keyword=None):
    """--date-from·--date-to·--category → Filters. 날짜가 거꾸로이거나 모르는 카테고리면 UsageError."""
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise UsageError(f"--date-from({args.date_from})이 --date-to({args.date_to})보다 늦습니다")
    available = list(config["source"]["categories"]) + [config["clean"]["default_category"]]
    categories = None if args.category == "all" else _pick(args.category, available, "카테고리")
    return Filters(args.date_from, args.date_to, categories, status, keyword)


# 명령 ------------------------------------------------------------------

def cmd_fetch(args, config, conn):
    source = config["source"]
    if args.source and args.source != source["name"]:
        raise UsageError(f"지원하는 소스는 {source['name']}({source['label']})뿐입니다")
    feeds = _pick(args.feed, source["rss_feeds"], "피드")
    categories = _pick(args.category, source["categories"], "카테고리")
    fetch_conf = config["fetch"]
    if args.since:  # 기간 기준: 상한을 넉넉히 (명시한 --limit·--pages가 우선)
        limit = args.limit or fetch_conf.get("since_limit", fetch_conf["default_limit"])
        pages = args.pages or fetch_conf.get("since_pages", fetch_conf["default_pages"])
    else:
        limit = args.limit or fetch_conf["default_limit"]
        pages = args.pages or fetch_conf["default_pages"]

    client = HttpClient(config["http"], source["base_url"].rstrip("/") + source["robots_path"])
    fetcher = Fetcher(conn, config, client, recrawl=args.recrawl)
    log.info("수집 시작: %s, 방법=%s, 피드=%s, 카테고리=%s, %s채널당 최대 %d건, 목록 최대 %d쪽, 요청 간격 %.1f초",
             source["label"], args.method,
             ",".join(feeds) if args.method != "crawl" else "-",
             ",".join(categories) if args.method != "rss" else "-",
             f"{args.since} 이후 기사, " if args.since else "", limit, pages, client.delay)
    started = time.monotonic()
    results = fetcher.run(args.method, feeds, categories, limit, pages, since=args.since)

    requested = sum(r.requested for r in results)
    succeeded = sum(r.succeeded for r in results)
    failed = sum(r.failed for r in results)
    saved = sum(r.saved for r in results)
    log.info("수집 완료: 신규 raw %d건, 요청 %d회(성공 %d, 실패 %d), %.0f초",
             saved, requested, succeeded, failed, time.monotonic() - started)
    if requested and not succeeded:
        log.error("모든 요청이 실패했습니다. 네트워크나 사이트 상태를 확인하세요.")
        return 1
    return 0


def cmd_clean(args, config, conn):
    stats = run_clean(conn, config, policy=args.policy, limit=args.limit, rebuild=args.rebuild)
    log.info("정제 완료: raw %d건 처리 → 신규 %d건, 갱신 %d건, 중복 건너뜀 %d건 / raw 제외 %d건",
             stats["raw_processed"], stats["inserted"], stats["updated"], stats["skipped"], stats["raw_rejected"])
    rows = conn.execute("SELECT status, COUNT(*) FROM clean_news GROUP BY status ORDER BY status").fetchall()
    total = sum(count for _, count in rows)
    log.info("clean_news 현재 %d건 (%s)", total, ", ".join(f"{status} {count}" for status, count in rows) or "없음")
    return 0


def _ai_client(config):
    """AI 클라이언트. 키·주소 환경변수가 비어 있으면 이름만 안내하고 None(키 값은 출력하지 않는다)."""
    values, missing = ai_settings(config)
    if missing:
        log.error("AI API 설정이 없습니다. .env 또는 환경변수에 %s 값을 넣으세요 (.env.example 참고)", ", ".join(missing))
        return None
    return AIClient(config["ai"], values)


def cmd_summarize(args, config, conn):
    client = _ai_client(config)
    if client is None:
        return 1
    mode = "ids" if args.id else ("all" if args.all else "unsummarized")
    stats = run_summarize(conn, config, client, mode=mode, ids=args.id, force=args.force, limit=args.limit)
    ratio = f", 평균 압축률 {stats['output_chars'] / stats['input_chars']:.1%}" if stats["input_chars"] else ""
    log.info("요약 완료: 성공 %d건, 실패 %d건 (AI 호출 %d회%s)", stats["summarized"], stats["failed"], client.calls, ratio)
    if stats["failed"]:
        log.info("실패한 기사는 ai_errors에 기록했습니다. 다음에 summarize(기본 --unsummarized)로 다시 시도합니다.")
    return 1 if stats["failed"] and not stats["summarized"] else 0


def cmd_analyze(args, config, conn):
    if args.history:
        print("\n".join(analyzer.format_history(analyzer.list_analyses(conn))))
        return 0
    if args.show_id:
        analysis = analyzer.get_analysis(conn, args.show_id)
        if analysis is None:
            raise UsageError(f"id={args.show_id} 분석이 없습니다 (목록: analyze --history)")
        print("\n".join(analyzer.format_analysis(analysis)))
        return 0

    try:
        items = analyzer.parse_items(args.items)
    except ValueError as e:
        raise UsageError(str(e))
    filters = _filters(args, config)

    client = _ai_client(config)
    if client is None:
        return 1
    analysis = analyzer.run_analyze(conn, config, client, filters.date_from, filters.date_to, filters.categories,
                                    items, args.max_articles)
    if analysis is None:
        return 1
    print("\n".join(analyzer.format_analysis(analysis)))
    return 0


def cmd_report(args, config, conn):
    filters = _filters(args, config)
    analysis = None
    if args.analysis_id:
        analysis = report.load_analysis(conn, args.analysis_id)
        if analysis is None:
            raise UsageError(f"id={args.analysis_id} 분석이 없습니다 (목록: analyze --history)")
    paths, output = config["paths"], config["output"]
    result = report.run_report(conn, config, filters, args.top or output["top_n"],
                               args.format or output["report_format"],
                               resolve_path(paths["reports_dir"]), resolve_path(paths["charts_dir"]),
                               analysis, Path(args.output) if args.output else None)
    if result is None:
        return 1
    print(result[0])
    return 0


def cmd_export(args, config, conn):
    try:
        formats = exporter.parse_formats(args.format or ",".join(config["output"]["export_formats"]))
    except ValueError as e:
        raise UsageError(str(e))
    filters = _filters(args, config, status=args.status)
    output_dir = Path(args.output_dir) if args.output_dir else resolve_path(config["paths"]["exports_dir"])
    written = exporter.run_export(conn, filters, formats, output_dir, args.include_body)
    return 0 if written else 1


def cmd_list(args, config, conn):
    filters = _filters(args, config, status=args.status, keyword=args.keyword)
    size = args.size or config["output"]["list_page_size"]
    try:
        articles, total, pages = browse.list_page(conn, filters, args.page, size)
    except browse.PageError as e:
        raise UsageError(str(e))
    print("\n".join(browse.format_list(articles, total, pages, args.page, size, filters)))
    return 0


def cmd_show(args, config, conn):
    article = browse.get_article(conn, args.id)
    if article is None:
        raise UsageError(f"id={args.id} 기사가 없습니다 (목록: python main.py list)")
    print("\n".join(browse.format_article(article, config["output"]["show_body_chars"], args.full)))
    return 0


# 파서 ------------------------------------------------------------------

def _add_filter_options(p):
    p.add_argument("--date-from", type=_date, metavar="YYYY-MM-DD", help="발행일 시작 (포함)")
    p.add_argument("--date-to", type=_date, metavar="YYYY-MM-DD", help="발행일 끝 (포함)")
    p.add_argument("--category", default="all", help="all 또는 전력,태양광 처럼 쉼표로 (기본 all)")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="설정 파일 경로 (기본: 프로젝트 폴더의 config.json)")
    common.add_argument("--db", help="SQLite 파일 경로 (기본: config의 paths.db)")
    common.add_argument("-v", "--verbose", action="store_true", help="DEBUG 로그도 콘솔에 출력")

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="에너지 뉴스(전기신문) 수집·정제·AI 요약·분석·리포트 CLI",
        epilog="예: python main.py fetch --since 2026-10-06  /  python main.py fetch --category 전력,태양광 --limit 5")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<명령>")

    p = sub.add_parser("fetch", parents=[common], help="RSS 수집 + 세부 카테고리 크롤링 → raw_news",
                       description="RSS(방법 1)와 세부 카테고리 목록·기사 크롤링(방법 2)으로 뉴스를 모아 raw_news에 저장한다.")
    p.add_argument("--source", help="뉴스 소스 (현재 electimes만 지원)")
    p.add_argument("--method", choices=["rss", "crawl", "all"], default="all",
                   help="rss=피드만, crawl=카테고리 크롤링만, all=둘 다 + RSS 기사 본문 크롤링 (기본 all)")
    p.add_argument("--feed", default="all", help="RSS 피드: all 또는 energy,carbon,economy 중 쉼표로 (기본 all)")
    p.add_argument("--category", default="all", help="크롤링할 세부 카테고리: all 또는 전력,태양광 처럼 쉼표로 (기본 all)")
    p.add_argument("--since", type=_date, metavar="YYYY-MM-DD",
                   help="이 날짜(발행일) 이후 기사만. 카테고리별 기사 수를 기간 기준으로 비교할 때 사용")
    p.add_argument("--limit", type=_positive_int,
                   help="피드·카테고리당 최대 기사 수 (기본: config fetch.default_limit, --since면 fetch.since_limit)")
    p.add_argument("--pages", type=_positive_int,
                   help="카테고리 목록을 볼 최대 쪽 수 (기본: config fetch.default_pages, --since면 fetch.since_pages)")
    p.add_argument("--recrawl", action="store_true", help="이미 본문을 받은 기사도 다시 요청")
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("clean", parents=[common], help="raw_news → 검증·정규화·중복 처리 → clean_news",
                       description="새로 수집한 raw를 기사 단위로 검증·정규화하고 중복 정책에 따라 clean_news에 반영한다.")
    p.add_argument("--policy", choices=["skip", "upsert"],
                   help="이미 있는 기사와 겹칠 때: skip=기존 유지, upsert=갱신 (기본: config clean.duplicate_policy)")
    p.add_argument("--limit", type=_positive_int, help="이번에 처리할 최대 기사 수")
    p.add_argument("--rebuild", action="store_true", help="raw 전체를 처음부터 다시 정제 (정제 규칙을 바꾼 뒤 사용, upsert)")
    p.set_defaults(handler=cmd_clean)

    p = sub.add_parser("summarize", parents=[common], help="clean_news 기사별 AI 요약 → summaries",
                       description="기관 AI API로 기사마다 요약과 키워드를 만든다. 이미 요약된 기사는 기본으로 건너뛴다.")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--unsummarized", action="store_true",
                        help="요약 전·요약 실패 기사만 (기본)")
    target.add_argument("--all", action="store_true", help="본문이 있는 기사 전체 (이미 요약된 기사는 --force일 때만 다시)")
    target.add_argument("--id", type=_positive_int, nargs="+", metavar="ID", help="지정한 기사 ID (예: --id 1 2 3)")
    p.add_argument("--limit", type=_positive_int, help="이번에 요약할 최대 기사 수")
    p.add_argument("--force", action="store_true", help="이미 요약된 기사도 다시 요약 (--all·--id와 함께)")
    p.set_defaults(handler=cmd_summarize)

    p = sub.add_parser("analyze", parents=[common], help="기간·카테고리 조건으로 AI 종합 분석 → analyses",
                       description="조건에 맞는 기사들의 요약을 모아 AI로 트렌드·키워드·공통점/차이점·시사점을 분석한다.")
    _add_filter_options(p)
    p.add_argument("--items", default=",".join(analyzer.ITEM_NAMES),
                   help=f"분석 항목 2개 이상, 쉼표로 (기본 {','.join(analyzer.ITEM_NAMES)})")
    p.add_argument("--max-articles", type=_positive_int, help="분석에 넣을 최대 기사 수 (기본: config ai.analyze_max_articles)")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--history", action="store_true", help="저장된 분석 목록 (AI 호출 없음)")
    view.add_argument("--show-id", type=_positive_int, metavar="ID", help="저장된 분석 상세 (AI 호출 없음)")
    p.set_defaults(handler=cmd_analyze)

    p = sub.add_parser("report", parents=[common], help="차트 PNG + 리포트(품질 지표·TOP N·AI 인사이트) → 콘솔·파일",
                       description="조건에 맞는 기사로 차트와 리포트를 만든다. AI를 부르지 않고 저장된 분석을 싣는다.")
    _add_filter_options(p)
    p.add_argument("--top", type=_positive_int, help="TOP N의 N (기본: config output.top_n)")
    p.add_argument("--format", choices=["md", "txt"], help="리포트 파일 형식 (기본: config output.report_format)")
    p.add_argument("--analysis-id", type=_positive_int, metavar="ID",
                   help="실을 분석 ID (기본: 조건이 같은 최신 분석, 목록: analyze --history)")
    p.add_argument("--output", help="리포트 파일 경로 (기본: output/reports/report_날짜_시각.형식)")
    p.set_defaults(handler=cmd_report)

    p = sub.add_parser("export", parents=[common], help="기사 + 최신 요약 → CSV·JSONL·XLSX",
                       description="clean_news와 기사별 최신 요약을 파일로 내보낸다. 본문은 --include-body일 때만 넣는다.")
    p.add_argument("--format",
                   help=f"쉼표로 여러 개 (사용 가능: {','.join(exporter.FORMATS)}, 기본: config output.export_formats)")
    p.add_argument("--status", choices=["all", *STATUSES], default="all", help="기사 상태 (기본 all)")
    _add_filter_options(p)
    p.add_argument("--include-body", action="store_true", help="기사 본문도 넣기 (저작권상 외부 공개 금지)")
    p.add_argument("--output-dir", help="저장 폴더 (기본: config paths.exports_dir)")
    p.set_defaults(handler=cmd_export)

    p = sub.add_parser("list", parents=[common], help="(보너스) 기사 목록: 필터 + 쪽 나누기",
                       description="clean_news를 최신 발행순으로 한 쪽씩 보여 준다.")
    _add_filter_options(p)
    p.add_argument("--keyword", help="제목·요약·본문에 들어 있는 낱말")
    p.add_argument("--status", choices=["all", *STATUSES], default="all", help="기사 상태 (기본 all)")
    p.add_argument("--page", type=_positive_int, default=1, help="쪽 번호 (기본 1)")
    p.add_argument("--size", type=_positive_int, help="한 쪽에 보일 기사 수 (기본: config output.list_page_size)")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("show", parents=[common], help="(보너스) 기사 상세: 정보·요약·본문 앞부분·수집 이력",
                       description="기사 1건의 정보, 최신 요약, 본문 앞부분, raw 수집 이력을 보여 준다.")
    p.add_argument("id", type=_positive_int, metavar="ID", help="기사 ID (list에서 확인)")
    p.add_argument("--full", action="store_true", help="본문 전체 보기")
    p.set_defaults(handler=cmd_show)
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    setup_logging(resolve_path(config["paths"]["log"]), args.verbose)
    log.debug("명령: %s", " ".join(sys.argv[1:] if argv is None else argv))

    db_path = args.db or resolve_path(config["paths"]["db"])  # 명령줄 경로는 현재 폴더 기준
    try:
        conn = db.connect(db_path)
    except sqlite3.Error as e:
        log.error("DB 파일을 열 수 없습니다: %s (%s)", db_path, e)
        return 1

    try:
        return args.handler(args, config, conn)
    except UsageError as e:
        log.error("%s", e)
        return 2
    except AIAuthError as e:
        log.error("AI 요청을 멈춥니다(남은 기사도 같은 이유로 실패하므로): %s", e)
        return 1
    except KeyboardInterrupt:
        conn.commit()
        log.warning("사용자가 중단했습니다. 그때까지 저장한 내용은 DB에 남아 있습니다.")
        return 130
    finally:
        conn.close()
