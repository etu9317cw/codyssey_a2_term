"""[공통] 로깅 설정.

- 콘솔: INFO 이상(-v를 주면 DEBUG도), `[INFO] 메시지` 형식
- 파일(logs/app.log): DEBUG 이상, 시각·모듈 이름 포함

INFO는 단계 시작·완료와 건수, WARNING은 건너뛴 기사와 재시도, ERROR는 최종 실패에 쓴다.
"""
import logging
import sys
from pathlib import Path

LOGGER_NAME = "news_cli"


def setup_logging(log_path, verbose=False):
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in logger.handlers:  # 다시 설정할 때 이전 로그 파일을 닫는다
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console)

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    return logger
