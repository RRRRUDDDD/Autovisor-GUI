import unittest

from modules.ai_analysis_api import ANALYSIS_RUN_URL, USER_INFO_URL, AiAnalysisApi


class FakeResponse:
    def __init__(self, *, lines=None, payload=None, error=None):
        self.lines = lines or []
        self.payload = payload
        self.error = error
        self.closed = False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=False):
        return iter(self.lines)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, get_response=None, post_response=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.post_response


QUESTION = {
    "content": "1+1=?",
    "type": 1,
    "options": [
        {"id": "1", "content": "1"},
        {"id": "2", "content": "2"},
    ],
}


class UserInfoTests(unittest.TestCase):
    def test_get_user_id(self):
        session = FakeSession(get_response=FakeResponse(payload={"data": {"userId": 42}}))
        api = AiAnalysisApi(session=session)
        self.assertEqual(api.get_user_id("C1", "R1"), "42")
        call = session.get_calls[0]
        self.assertEqual(call["url"], USER_INFO_URL)
        self.assertEqual(call["params"]["userId"], 0)
        self.assertEqual(call["params"]["courseId"], "C1")
        self.assertEqual(call["params"]["recruitId"], "R1")

    def test_get_user_id_failures_return_empty(self):
        session = FakeSession(get_response=FakeResponse(error=RuntimeError("boom")))
        self.assertEqual(AiAnalysisApi(session=session).get_user_id(), "")

        session = FakeSession(get_response=FakeResponse(payload={"data": None}))
        self.assertEqual(AiAnalysisApi(session=session).get_user_id(), "")

        session = FakeSession(get_response=FakeResponse(payload=["bad"]))
        self.assertEqual(AiAnalysisApi(session=session).get_user_id(), "")


class AnalyzeStreamTests(unittest.TestCase):
    def test_analyze_collects_sse_and_stops(self):
        response = FakeResponse(
            lines=[
                "",
                "event: message",
                'data: {"choices":[{"message":{"content":"第一段"}}]}',
                'data: {"choices":[{"delta":{"content":"第二段"}}]}',
                'data: {"stop":true}',
                'data: {"choices":[{"message":{"content":"不应读取"}}]}',
            ]
        )
        session = FakeSession(post_response=response)
        api = AiAnalysisApi(session=session)
        text = api.analyze(QUESTION, user_id="42", course_id="C1", recruit_id="R1")
        self.assertEqual(text, "第一段第二段")
        self.assertTrue(response.closed)

        call = session.post_calls[0]
        self.assertEqual(call["url"], ANALYSIS_RUN_URL)
        self.assertTrue(call["stream"])
        self.assertEqual(call["headers"]["Accept"], "text/event-stream")
        self.assertEqual(call["json"]["userId"], 42)
        self.assertEqual(call["json"]["question"], "1+1=?")
        self.assertEqual(call["json"]["questionType"], 1)
        self.assertEqual(call["json"]["options"], QUESTION["options"])
        self.assertEqual(call["json"]["answer"], [])

    def test_analyze_stops_on_done_token(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"message":{"content":"答案"}}]}',
                "data: [DONE]",
                'data: {"choices":[{"message":{"content":"LATE"}}]}',
            ]
        )
        api = AiAnalysisApi(session=FakeSession(post_response=response))
        self.assertEqual(api.analyze(QUESTION), "答案")
        self.assertTrue(response.closed)

    def test_analyze_handles_bad_json_and_request_error(self):
        response = FakeResponse(
            lines=[
                "data: not-json",
                'data: {"choices":[]}',
                'data: {"choices":[{"message":{"content":"ok"}}]}',
            ]
        )
        api = AiAnalysisApi(session=FakeSession(post_response=response))
        self.assertEqual(api.analyze(QUESTION), "ok")

        failed = FakeResponse(error=RuntimeError("timeout"))
        api = AiAnalysisApi(session=FakeSession(post_response=failed))
        self.assertEqual(api.analyze(QUESTION), "")
        self.assertFalse(failed.closed)

    def test_non_numeric_user_id_falls_back_to_zero(self):
        response = FakeResponse(
            lines=['data: {"choices":[{"message":{"content":"x"}}]}']
        )
        session = FakeSession(post_response=response)
        AiAnalysisApi(session=session).analyze(QUESTION, user_id="abc")
        self.assertEqual(session.post_calls[0]["json"]["userId"], 0)


if __name__ == "__main__":
    unittest.main()