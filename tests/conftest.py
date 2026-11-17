import pytest

from evaltrack import create_app
from evaltrack.datasets import import_dataset
from evaltrack.db import connect, init_db
from evaltrack.providers import RetryPolicy
from evaltrack.seed import create_prompt_version


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def no_sleep():
    return RetryPolicy(sleep=lambda s: None)


@pytest.fixture
def small_dataset(conn):
    cases = [
        {"key": "a", "input": "say hello", "expected": "", "tags": "greet",
         "checks": [{"type": "contains", "value": "hello"}]},
        {"key": "b", "input": "say bye", "expected": "", "tags": "",
         "checks": [{"type": "contains", "value": "bye"}]},
    ]
    return import_dataset(conn, "tiny", cases)


@pytest.fixture
def make_version(conn):
    def _make(system="", name="bot"):
        return create_prompt_version(conn, name, system, "{{input}}")
    return _make


class EchoProvider:
    """Returns the prompt minus the word 'say'. Deterministic and instant."""

    def __init__(self, broken=()):
        self.broken = set(broken)

    def complete(self, system, prompt, model, temperature=0.0, max_tokens=300):
        from evaltrack.providers import Completion
        text = prompt.replace("say ", "")
        if text in self.broken:
            text = "something else"
        return Completion(text, 5, 3, 100.0)


@pytest.fixture
def echo():
    return EchoProvider


@pytest.fixture
def app(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "web.db"), "TESTING": True})
    return app


@pytest.fixture
def seeded_client(app):
    from evaltrack import get_db
    from evaltrack.seed import seed_demo
    with app.app_context():
        seed_demo(get_db())
    return app.test_client()
