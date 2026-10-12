"""AI 클라이언트: JSON 추출, 재시도 정책(5xx·타임아웃·400·401·형식 오류), 요청 본문 (가짜 세션, 네트워크 없음)."""
import unittest

from news_cli.ai.client import JSON_RETRY_NOTE, AIAuthError, AIClient, AIError, extract_json
from tests.support import AI_VALUES, TIMEOUT, FakeAIResponse, FakeAISession, FakeClock, ai_reply, config

GOOD = '{"answer": "ok"}'


def need_answer(data):
    if "answer" not in data:
        raise ValueError("answer 없음")
    return data


class ExtractJsonTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(extract_json('```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})

    def test_text_before_and_after(self):
        text = '요약 결과입니다.\n{"summary": "문장", "keywords": ["전력"]}\n이상입니다.'
        self.assertEqual(extract_json(text)["keywords"], ["전력"])

    def test_skips_brace_that_is_not_json(self):
        self.assertEqual(extract_json('형식 {키: 값} 에 맞춘 결과: {"a": 1}'), {"a": 1})

    def test_no_object_raises(self):
        for text in ("JSON이 없습니다", '["목록만"]', "", None):
            with self.assertRaises(ValueError):
                extract_json(text)


class AIClientTest(unittest.TestCase):
    def make(self, replies, values=None):
        self.session = FakeAISession(replies)
        self.clock = FakeClock()
        return AIClient(config()["ai"], values or AI_VALUES, session=self.session, sleep=self.clock.sleep)

    def test_request_has_no_max_tokens_or_response_format(self):
        client = self.make([ai_reply(GOOD)])
        result, attempts, level = client.ask_json("시스템", ["질문"], need_answer)
        self.assertEqual((result, attempts, level), ({"answer": "ok"}, 1, 0))
        call = self.session.calls[0]
        self.assertEqual(call["url"], "https://ai.example/v1/chat/completions")
        self.assertEqual(set(call["json"]), {"model", "messages"})
        self.assertEqual([m["role"] for m in call["json"]["messages"]], ["system", "user"])
        self.assertEqual(call["timeout"], 90)
        self.assertEqual(self.session.headers["Authorization"], "Bearer test-key")

    def test_base_url_with_v1_is_not_doubled(self):
        client = self.make([], dict(AI_VALUES, base_url="https://ai.example/v1/"))
        self.assertEqual(client.url, "https://ai.example/v1/chat/completions")

    def test_server_error_then_success(self):
        client = self.make([FakeAIResponse(503), ai_reply(GOOD)])
        _, attempts, _ = client.ask_json("s", ["u"], need_answer)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.clock.sleeps, [2])

    def test_gives_up_after_three_attempts(self):
        client = self.make([TIMEOUT, TIMEOUT, TIMEOUT])
        with self.assertRaises(AIError) as ctx:
            client.ask_json("s", ["u"], need_answer)
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertIn("타임아웃", str(ctx.exception))
        self.assertEqual(self.clock.sleeps, [2, 4])
        self.assertEqual(client.calls, 3)

    def test_bad_request_switches_to_shorter_prompt(self):
        client = self.make([FakeAIResponse(400, text="Invalid request"), ai_reply(GOOD)])
        _, attempts, level = client.ask_json("s", ["긴 입력", "짧은 입력"], need_answer)
        self.assertEqual((attempts, level), (2, 1))
        self.assertEqual(self.session.user_prompt(1), "짧은 입력")
        self.assertEqual(self.clock.sleeps, [])

    def test_bad_request_without_shorter_prompt_waits_and_retries(self):
        client = self.make([FakeAIResponse(400), ai_reply(GOOD)])
        _, attempts, level = client.ask_json("s", ["입력"], need_answer)
        self.assertEqual((attempts, level), (2, 0))
        self.assertEqual(self.clock.sleeps, [2])

    def test_auth_error_stops_without_retry(self):
        client = self.make([FakeAIResponse(401), ai_reply(GOOD)])
        with self.assertRaises(AIAuthError):
            client.ask_json("s", ["u"], need_answer)
        self.assertEqual(client.calls, 1)

    def test_not_found_is_not_retried(self):
        client = self.make([FakeAIResponse(404)])
        with self.assertRaises(AIError) as ctx:
            client.ask_json("s", ["u"], need_answer)
        self.assertEqual(ctx.exception.attempts, 1)

    def test_broken_json_asks_again_with_note(self):
        client = self.make([ai_reply("요약: 전력 수요 증가"), ai_reply(GOOD)])
        result, attempts, _ = client.ask_json("s", ["질문"], need_answer)
        self.assertEqual((result, attempts), ({"answer": "ok"}, 2))
        self.assertEqual(self.session.user_prompt(1), "질문" + JSON_RETRY_NOTE)
        self.assertEqual(self.clock.sleeps, [])

    def test_failed_validation_counts_as_format_error(self):
        client = self.make([ai_reply('{"other": 1}')] * 3)
        with self.assertRaises(AIError) as ctx:
            client.ask_json("s", ["u"], need_answer)
        self.assertIn("answer 없음", str(ctx.exception))
        self.assertEqual(ctx.exception.attempts, 3)

    def test_empty_or_strange_response_is_retried(self):
        client = self.make([ai_reply(""), FakeAIResponse(200, {"unexpected": True}), ai_reply(GOOD)])
        _, attempts, _ = client.ask_json("s", ["u"], need_answer)
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()
