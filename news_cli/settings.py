"""[공통] 설정 읽기: config.json(동작 설정)과 .env(비밀값).

config.json에는 API 키 값이 아니라 키를 담은 환경변수 이름만 적는다.
키 값은 .env 또는 환경변수에만 두고 화면·로그에 출력하지 않는다.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.json"

REQUIRED_SECTIONS = ("source", "http", "fetch", "clean", "ai", "output", "paths")
DUPLICATE_POLICIES = ("skip", "upsert")


class ConfigError(Exception):
    """설정 파일이 없거나 형식이 잘못됨."""


def load_config(path=None):
    """config.json을 읽어 dict로 돌려주고, .env를 환경변수로 불러온다."""
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.is_file():
        raise ConfigError(f"설정 파일이 없습니다: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"설정 파일 JSON 형식 오류: {path} ({e.lineno}행 {e.colno}열)") from e

    missing = [name for name in REQUIRED_SECTIONS if name not in config]
    if missing:
        raise ConfigError(f"설정 파일에 없는 항목: {', '.join(missing)}")
    if config["clean"].get("duplicate_policy") not in DUPLICATE_POLICIES:
        raise ConfigError("clean.duplicate_policy는 skip 또는 upsert여야 합니다")

    load_dotenv(ROOT / ".env")
    return config


def resolve_path(value):
    """설정의 상대 경로를 프로젝트 폴더 기준 절대 경로로 바꾼다(실행 위치와 상관없이 같은 파일을 쓰게)."""
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def ai_settings(config):
    """AI 호출 값(api_key, base_url, model)과 비어 있는 환경변수 이름 목록을 돌려준다."""
    env_names = config["ai"]["env"]
    values = {key: os.getenv(name, "").strip() for key, name in env_names.items()}
    if not values.get("model"):
        values["model"] = config["ai"].get("default_model", "")
    missing = [env_names[key] for key, value in values.items() if not value]
    return values, missing
