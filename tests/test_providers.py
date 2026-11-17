import pytest
import requests

from evaltrack.providers import (MockProvider, OpenAICompatibleProvider, ProviderError,
                                 RetryableError, RetryPolicy, call_with_retry, get_provider)


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Plays back a scripted list of responses (or exceptions)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


OK_BODY = {
    "choices": [{"message": {"content": "hello"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


def provider(script):
    return OpenAICompatibleProvider("https://api.example.com/v1/", "sk-test", session=FakeSession(script))


def test_successful_call_builds_request_and_reads_usage():
    p = provider([FakeResponse(200, OK_BODY)])
    out = p.complete("be nice", "hi", "llama-3.1-8b-instant", 0.2, 50)
    call = p.session.calls[0]
    assert call["url"] == "https://api.example.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"]["messages"][0] == {"role": "system", "content": "be nice"}
    assert call["json"]["max_tokens"] == 50
    assert (out.text, out.prompt_tokens, out.completion_tokens) == ("hello", 12, 3)


def test_system_message_is_omitted_when_empty():
    p = provider([FakeResponse(200, OK_BODY)])
    p.complete("", "hi", "m")
    assert [m["role"] for m in p.session.calls[0]["json"]["messages"]] == ["user"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limits_and_server_errors_are_retryable(status):
    with pytest.raises(RetryableError):
        provider([FakeResponse(status, {})]).complete("", "hi", "m")


def test_retry_after_header_is_parsed():
    with pytest.raises(RetryableError) as info:
        provider([FakeResponse(429, {}, {"Retry-After": "2"})]).complete("", "hi", "m")
    assert info.value.retry_after == 2.0


def test_timeouts_are_retryable():
    with pytest.raises(RetryableError, match="Timeout"):
        provider([requests.Timeout()]).complete("", "hi", "m")


def test_client_errors_are_not_retried():
    with pytest.raises(ProviderError) as info:
        provider([FakeResponse(401, {"error": "bad key"})]).complete("", "hi", "m")
    assert not isinstance(info.value, RetryableError)


def test_malformed_body_raises_provider_error():
    with pytest.raises(ProviderError, match="unexpected response"):
        provider([FakeResponse(200, {"choices": []})]).complete("", "hi", "m")


def test_missing_usage_falls_back_to_estimate():
    body = {"choices": [{"message": {"content": "a" * 40}}]}
    out = provider([FakeResponse(200, body)]).complete("", "hi", "m")
    assert out.completion_tokens == 10


def test_call_with_retry_recovers_after_transient_failures():
    p = provider([FakeResponse(503, {}), requests.ConnectionError(), FakeResponse(200, OK_BODY)])
    sleeps = []
    result, attempts = call_with_retry(lambda: p.complete("", "hi", "m"),
                                       RetryPolicy(max_attempts=3, sleep=sleeps.append))
    assert result.text == "hello"
    assert attempts == 3
    assert len(sleeps) == 2


def test_call_with_retry_gives_up_and_reports_attempts():
    p = provider([FakeResponse(503, {})] * 3)
    with pytest.raises(ProviderError, match="gave up after 3 attempts") as info:
        call_with_retry(lambda: p.complete("", "hi", "m"), RetryPolicy(sleep=lambda s: None))
    assert info.value.attempts == 3


def test_call_with_retry_does_not_retry_permanent_errors():
    p = provider([FakeResponse(400, {}), FakeResponse(200, OK_BODY)])
    with pytest.raises(ProviderError):
        call_with_retry(lambda: p.complete("", "hi", "m"), RetryPolicy(sleep=lambda s: None))
    assert len(p.session.calls) == 1


def test_backoff_grows_and_respects_retry_after():
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0)
    assert 1.0 <= policy.delay_for(1) <= 2.0
    assert 4.0 <= policy.delay_for(3) <= 5.0
    assert policy.delay_for(10) == 8.0
    assert policy.delay_for(1, retry_after=3) == 3


def test_get_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GROQ_API_KEY"):
        get_provider("groq")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert isinstance(get_provider("groq"), OpenAICompatibleProvider)


def test_unknown_provider():
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("skynet")


def test_mock_provider_injects_failures_deterministically():
    p = MockProvider(failure_rate=1.0)
    with pytest.raises(RetryableError):
        p.complete("", "hi", "mock-small")
    out = MockProvider(failure_rate=0.0).complete("", "Do you deliver to Udupi?", "mock-small")
    assert "Udupi" in out.text
