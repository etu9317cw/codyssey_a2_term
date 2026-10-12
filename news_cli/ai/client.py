"""[역할 2] 기관 AI API 호출: OpenAI 호환 /v1/chat/completions를 requests로 직접 부른다.

기관 API 제약과 대응(설계서 5절)
- response_format(JSON 모드) 미지원 → 프롬프트로 "JSON 객체 하나만" 지시하고, 응답에서 JSON 객체를 꺼낸다.
- 사고 토큰이 max_tokens에 포함됨 → max_tokens를 보내지 않는다(길이는 프롬프트로 제한).
- 긴 입력에서 간헐적 400 → 호출한 쪽이 준 더 짧은 프롬프트로 바꿔 다시 보낸다.
- 타임아웃·연결 실패·429·5xx → retry_backoff_sec만큼 기다렸다가 다시 보낸다.
- JSON을 꺼내지 못하거나 형식 검사에 실패 → "JSON만 다시" 문구를 붙여 다시 보낸다.
- 위 재시도를 모두 합쳐 한 건당 최대 max_attempts(3)번만 호출한다.
- 401·403은 키 문제라 다른 건도 똑같이 실패하므로 AIAuthError로 명령 전체를 멈춘다.
API 키 값은 요청 헤더에만 넣고 로그·오류 메시지에 쓰지 않는다.
"""
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
JSON_RETRY_NOTE = "\n\n(앞선 응답이 올바른 JSON이 아니었습니다. 설명 없이 위 형식의 JSON 객체 하나만 다시 출력하세요.)"

_FENCE_RE = re.compile(r"```(?:json)?", re.I)


class AIError(Exception):
    """재시도한 뒤에도 실패. attempts는 실제 호출 횟수."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


class AIAuthError(AIError):
    """인증 실패(401·403). 명령 전체를 멈춘다."""


class _CallFailed(Exception):
    """호출 1회 실패. kind: retry(기다렸다가 다시), shrink(입력 줄여 다시), auth, stop(다시 해도 같음)"""

    def __init__(self, error, kind):
        super().__init__(error)
        self.error = error
        self.kind = kind


def extract_json(text):
    """응답 텍스트에서 첫 JSON 객체를 꺼낸다. 코드펜스와 앞뒤 설명은 무시한다. 없으면 ValueError."""
    cleaned = _FENCE_RE.sub("", text or "")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            data, _ = decoder.raw_decode(cleaned, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("응답에서 JSON 객체를 찾지 못함")


class AIClient:
    def __init__(self, ai_conf, values, session=None, sleep=time.sleep):
        """values: settings.ai_settings()가 돌려준 {api_key, base_url, model}"""
        base = values["base_url"].strip().rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        self.url = base + "/chat/completions"
        self.model = values["model"]
        self.timeout = ai_conf["timeout_sec"]
        self.max_attempts = max(1, int(ai_conf["max_attempts"]))
        self.backoff = list(ai_conf["retry_backoff_sec"]) or [0]
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {values['api_key']}"
        self._sleep = sleep
        self.calls = 0  # 실제로 보낸 요청 수

    def _post(self, system, user):
        """1회 호출 → 응답 텍스트. 실패하면 _CallFailed."""
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        self.calls += 1
        try:
            resp = self.session.post(self.url, json=body, timeout=self.timeout)
        except requests.Timeout:
            raise _CallFailed(f"타임아웃({self.timeout}초)", "retry")
        except requests.ConnectionError:
            raise _CallFailed("연결 실패", "retry")
        except requests.RequestException as e:
            raise _CallFailed(f"요청 오류({type(e).__name__})", "stop")

        status = resp.status_code
        if status in (401, 403):
            raise _CallFailed(f"HTTP {status} 인증 실패", "auth")
        if status == 400:
            raise _CallFailed("HTTP 400 " + " ".join(resp.text[:120].split()), "shrink")
        if status != 200:
            raise _CallFailed(f"HTTP {status}", "retry" if status in RETRY_STATUS else "stop")
        try:
            text = resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise _CallFailed("응답 구조를 해석할 수 없음", "retry")
        if not text.strip():
            raise _CallFailed("빈 응답", "retry")
        return text

    def ask_json(self, system, prompts, validate):
        """JSON 응답을 받아 검사한 결과를 돌려준다: (결과 dict, 호출 횟수, 쓴 프롬프트 번호).

        prompts: [기본 프롬프트, 400일 때 쓸 더 짧은 프롬프트, ...]
        validate: dict → 정리한 dict. 형식이 틀리면 ValueError (JSON 재요청 대상)
        끝내 실패하면 AIError, 인증 실패면 AIAuthError.
        """
        level, note, error = 0, "", ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                text = self._post(system, prompts[level] + note)
                return validate(extract_json(text)), attempt, level
            except ValueError as e:
                error, kind = f"응답 형식 오류: {e}", "json"
            except _CallFailed as e:
                error, kind = e.error, e.kind

            if kind == "auth":
                raise AIAuthError(f"{error}. API 키 환경변수 값을 확인하세요", attempt)
            if kind == "stop" or attempt == self.max_attempts:
                break
            if kind == "shrink" and level + 1 < len(prompts):
                level += 1
                log.warning("AI 요청 거부(%s), 입력을 줄여 다시 시도(%d/%d)", error, attempt, self.max_attempts - 1)
            elif kind == "json":
                note = JSON_RETRY_NOTE
                log.warning("AI %s, JSON만 다시 요청(%d/%d)", error, attempt, self.max_attempts - 1)
            else:  # retry, 또는 더 줄일 수 없는 400(간헐적이라 같은 입력으로 다시)
                wait = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                log.warning("AI 요청 실패(%s), %s초 뒤 다시 시도(%d/%d)", error, wait, attempt, self.max_attempts - 1)
                self._sleep(wait)
        raise AIError(error, attempt)
