"""에너지 뉴스 수집·정제·AI 분석 CLI (전기신문)."""
import logging

# 로깅 설정(logger.setup_logging) 전에는 아무것도 출력하지 않는다(테스트 등에서 모듈만 쓸 때).
logging.getLogger(__name__).addHandler(logging.NullHandler())
