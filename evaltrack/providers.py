import hashlib
import os
import random
import threading
import time
from dataclasses import dataclass

import requests


class ProviderError(Exception):
    """A call failed and retrying won't help (bad key, bad request, gave up)."""

    def __init__(self, message, attempts=1):
        super().__init__(message)
        self.attempts = attempts


class RetryableError(ProviderError):
    """Rate limits, 5xx, timeouts. Worth another try."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


def estimate_tokens(text):
    # Rough rule of thumb (~4 chars per token for English). Only used when the
    # provider doesn't report usage, e.g. the mock provider.
    return max(1, round(len(text or "") / 4))


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    sleep: callable = time.sleep

    def delay_for(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        backoff = self.base_delay * (2 ** (attempt - 1))
        # jitter so parallel workers that hit a 429 together don't retry together
        return min(self.max_delay, backoff + random.uniform(0, self.base_delay))


def call_with_retry(fn, policy=None):
    """Run fn() until it succeeds or we run out of attempts.

    Returns (result, attempts). Latency reported by the provider is for the
    successful attempt only; backoff time is not counted as model latency.
    """
    policy = policy or RetryPolicy()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn(), attempt
        except RetryableError as exc:
            if attempt == policy.max_attempts:
                raise ProviderError(
                    f"gave up after {attempt} attempts: {exc}", attempts=attempt
                ) from exc
            policy.sleep(policy.delay_for(attempt, exc.retry_after))
        except ProviderError as exc:
            exc.attempts = attempt
            raise


class OpenAICompatibleProvider:
    """Works with any /chat/completions endpoint: Groq, OpenAI, local servers."""

    def __init__(self, base_url, api_key, timeout=30, session=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def complete(self, system, prompt, model, temperature=0.0, max_tokens=300):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        start = time.perf_counter()
        try:
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RetryableError(f"network error ({type(exc).__name__})") from exc
        latency_ms = (time.perf_counter() - start) * 1000

        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(
                f"HTTP {resp.status_code}",
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status_code >= 400:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected response format from provider") from exc

        usage = data.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=usage.get("prompt_tokens") or estimate_tokens(system + prompt),
            completion_tokens=usage.get("completion_tokens") or estimate_tokens(text),
            latency_ms=latency_ms,
        )


def _parse_retry_after(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MockProvider:
    """Offline provider so the whole app works without an API key.

    `responder(system, prompt)` decides what the "model" says. Latency is
    simulated from output length so shorter prompts look faster, like a real
    model. `failure_rate` injects transient errors to exercise retries.
    """

    def __init__(self, responder=None, failure_rate=0.0, seed=7, real_sleep=False):
        if responder is None:
            from .demo import support_bot_responder
            responder = support_bot_responder
        self.responder = responder
        self.failure_rate = failure_rate
        self.real_sleep = real_sleep
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def complete(self, system, prompt, model, temperature=0.0, max_tokens=300):
        with self._lock:
            roll = self._rng.random()
        if roll < self.failure_rate:
            raise RetryableError("HTTP 503 (simulated)")

        text = self.responder(system or "", prompt)
        completion_tokens = min(estimate_tokens(text), max_tokens)
        jitter = int(hashlib.md5(prompt.encode()).hexdigest()[:4], 16) % 60
        latency_ms = 120 + completion_tokens * 9 + jitter
        if self.real_sleep:
            time.sleep(latency_ms / 1000)
        return Completion(
            text=text,
            prompt_tokens=estimate_tokens(system + prompt),
            completion_tokens=completion_tokens,
            latency_ms=float(latency_ms),
        )


PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


def get_provider(name, **kwargs):
    if name == "mock":
        return MockProvider(**kwargs)
    if name not in PROVIDERS:
        raise ProviderError(f"unknown provider '{name}'")
    base_url, env_var = PROVIDERS[name]
    api_key = os.environ.get(env_var)
    if not api_key:
        raise ProviderError(f"{env_var} is not set; export it or use the mock provider")
    return OpenAICompatibleProvider(base_url, api_key, **kwargs)
