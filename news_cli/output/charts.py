"""[역할 3] matplotlib 차트 → PNG.

- 카테고리별 기사 수(가로 막대), 일자별 추이(발행일·수집일 기준 선 2개), 요약 상태(막대)
- 한글 폰트는 config output.chart_fonts 순서대로 설치된 것을 고른다. 없으면 WARNING을 남기고 기본 폰트로 그린다(글자가 깨질 수 있음).
- 화면 없이도 돌도록 Agg 백엔드를 쓴다.
"""
import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (백엔드를 먼저 정해야 함)
from matplotlib import font_manager  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from news_cli.output.common import STATUS_LABELS  # noqa: E402

log = logging.getLogger(__name__)

FIGSIZE = (8, 4.5)
DPI = 120
COLOR = "#2f6db5"
COLOR_2 = "#e07b39"


def setup_korean_font(candidates):
    """설치된 첫 번째 한글 폰트 이름을 matplotlib에 지정하고 돌려준다. 없으면 None."""
    installed = {font.name for font in font_manager.fontManager.ttflist}
    plt.rcParams["axes.unicode_minus"] = False  # 한글 폰트에서 마이너스 기호가 네모로 보이는 문제
    for name in candidates:
        if name in installed:
            plt.rcParams["font.family"] = name
            return name
    log.warning("한글 폰트(%s)를 찾지 못해 기본 폰트로 그립니다. 차트의 한글이 깨질 수 있습니다", ", ".join(candidates))
    return None


def _save(fig, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def category_chart(counts, path, subtitle=""):
    """counts: [(카테고리, 기사 수)] 많은 순. 가장 많은 카테고리가 위에 오게 그린다."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    names = [name for name, _ in reversed(counts)]
    values = [count for _, count in reversed(counts)]
    bars = ax.barh(names, values, color=COLOR)
    ax.bar_label(bars, padding=3)
    ax.set_title("카테고리별 뉴스 수" + (f" ({subtitle})" if subtitle else ""))
    ax.set_xlabel("기사 수")
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlim(0, max(values + [1]) * 1.15)
    return _save(fig, path)


def daily_chart(daily, path, subtitle=""):
    """daily: [(YYYY-MM-DD, 발행 기사 수, 처음 수집한 기사 수)] 날짜순."""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    labels = [day[5:] for day, _, _ in daily]  # MM-DD
    ax.plot(labels, [p for _, p, _ in daily], marker="o", color=COLOR, label="발행일 기준 기사 수")
    ax.plot(labels, [c for _, _, c in daily], marker="s", linestyle="--", color=COLOR_2, label="수집일 기준 신규 저장 수")
    ax.set_title("일자별 수집 추이" + (f" ({subtitle})" if subtitle else ""))
    ax.set_xlabel("날짜")
    ax.set_ylabel("기사 수")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    if len(labels) > 10:
        step = (len(labels) + 9) // 10
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step])
    ax.tick_params(axis="x", rotation=45)
    return _save(fig, path)


def status_chart(counts, path, subtitle=""):
    """counts: [(상태, 기사 수)]"""
    fig, ax = plt.subplots(figsize=FIGSIZE)
    bars = ax.bar([STATUS_LABELS[status] for status, _ in counts], [count for _, count in counts], color=COLOR)
    ax.bar_label(bars, padding=3)
    ax.set_title("요약 상태" + (f" ({subtitle})" if subtitle else ""))
    ax.set_ylabel("기사 수")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylim(0, max([count for _, count in counts] + [1]) * 1.15)
    return _save(fig, path)
